"""`python -m cutover`: runbook steps 1 to 4 and the state rollback as subcommands.

Exit codes: 0 done, 1 a check failed or a call was refused, 2 a usage error.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from cutover.config import (
    ENVIRONMENTS,
    HCP_HOST,
    Environment,
    UsageError,
    check_hcp_workspace,
    check_issue_key,
    check_plane_workspace,
    state_key,
)
from cutover.engine import EngineError, EngineResolver, cloud_backend, engine_env, s3_backend
from cutover.hcp import HcpApi, HcpClient, HcpError, HcpStateVersion, HcpWorkspace, VariableShape, load_hcp_token
from cutover.output import Console, Redactor
from cutover.plane import HttpPlane, PlaneApi, PlaneError, default_token_provider
from cutover.s3state import S3StateStore, StateStore, StoreError
from cutover.statefile import PrivateDir, StateError, StateMeta, check_match, parse_meta, parse_state

LOCK_REASON = "{issue}: cutover to the owned control plane"
IGNORED_PREFIXES = ("TFC_",)
FAILURES = (HcpError, PlaneError, StoreError, EngineError, StateError)


class Refused(Exception):
    """A precondition the command will not proceed without."""


def _hcp_factory(token: str) -> HcpApi:
    """The real HCP client."""
    return HcpClient(token)


def _plane_factory(environment: Environment) -> PlaneApi:
    """The real plane client, reading an existing token only when a call is made."""
    host = environment.host
    return HttpPlane(environment.api_url, default_token_provider(host))


def _store_factory(environment: Environment) -> StateStore:
    """The real S3 store on the environment's profile."""
    return S3StateStore(environment)


@dataclass
class Deps:
    """Everything a command reaches outside the process, swappable for fakes in tests."""

    console: Console = field(default_factory=lambda: Console(Redactor()))
    token_loader: Callable[[], str] = load_hcp_token
    hcp_factory: Callable[[str], HcpApi] = _hcp_factory
    plane_factory: Callable[[Environment], PlaneApi] = _plane_factory
    store_factory: Callable[[Environment], StateStore] = _store_factory
    resolver: EngineResolver = field(default_factory=EngineResolver)
    private_parent: Path | None = None


class Session:
    """One command's view of its dependencies, with the HCP token registered for redaction."""

    def __init__(self, deps: Deps) -> None:
        """Hold the deps; the token is read on first use."""
        self.deps = deps
        self.console = deps.console
        self._token: str | None = None
        self._hcp: HcpApi | None = None

    @property
    def token(self) -> str:
        """The HCP token, registered with the redactor the moment it is read."""
        if self._token is None:
            self._token = self.deps.token_loader()
            self.console.redactor.add(self._token)
        return self._token

    @property
    def hcp(self) -> HcpApi:
        """The HCP client."""
        if self._hcp is None:
            self._hcp = self.deps.hcp_factory(self.token)
        return self._hcp

    def say(self, message: str) -> None:
        """Print a progress line."""
        self.console.info(message)


def _environment(name: str) -> Environment:
    """The named environment."""
    return ENVIRONMENTS[name]


def _require_our_lock(session: Session, workspace: HcpWorkspace) -> None:
    """Refuse unless the HCP workspace is locked by the token's own user."""
    if not workspace.locked:
        raise Refused(f"HCP workspace {workspace.name} is not locked; run lock first")
    if workspace.locked_by != session.hcp.account_id():
        raise Refused(f"HCP workspace {workspace.name} is locked by someone else ({workspace.locked_by})")


def _require_idle(session: Session, workspace: HcpWorkspace) -> None:
    """Refuse while HCP has a run in flight."""
    active = session.hcp.active_runs(workspace.id)
    if active:
        raise Refused(f"HCP workspace {workspace.name} has runs in flight: {', '.join(active)}")


def _require_state_version(session: Session, workspace: HcpWorkspace) -> HcpStateVersion:
    """HCP's current state version, refusing a workspace with none."""
    version = session.hcp.current_state_version(workspace.id)
    if version is None:
        raise Refused(f"HCP workspace {workspace.name} has no state")
    return version


def _plane_problems(plane: PlaneApi, plane_ws: str, engine_version: str) -> list[str]:
    """Every reason the plane workspace is not ready to take this state."""
    found = plane.workspace(plane_ws)
    if found is None:
        return [f"plane workspace {plane_ws} does not exist"]
    problems: list[str] = []
    if found.engine != "terraform":
        problems.append(f"plane workspace engine is {found.engine}, expected terraform")
    if found.engine_version != engine_version:
        problems.append(
            f"plane engine_version {found.engine_version} does not equal the HCP state's terraform-version "
            f"{engine_version}; set it exactly before moving state"
        )
    if found.vcs_repo:
        problems.append(f"plane workspace already has vcs_repo {found.vcs_repo}; connect VCS only after the move")
    active = plane.active_runs(plane_ws)
    if active:
        problems.append(f"plane workspace has runs in flight: {', '.join(active)}")
    return problems


def _download_meta(session: Session, version: HcpStateVersion) -> tuple[bytes, StateMeta]:
    """The HCP state body and its metadata, registered for redaction and checked against the version."""
    body = session.hcp.download_state(version)
    session.console.redactor.add_state(parse_state(body))
    meta = parse_meta(body)
    if meta.serial != version.serial:
        raise StateError(f"downloaded serial {meta.serial} does not match the state version's {version.serial}")
    return body, meta


def cmd_preflight(session: Session, args: argparse.Namespace) -> int:
    """Read only: HCP idle with state, plane workspace present, exact engine match and no VCS yet."""
    hcp_ws = check_hcp_workspace(args.hcp_workspace)
    plane_ws = check_plane_workspace(args.plane_workspace)
    workspace = session.hcp.workspace(hcp_ws)
    problems: list[str] = []
    session.say(f"HCP {workspace.organization}/{workspace.name}: locked={workspace.locked}")
    active = session.hcp.active_runs(hcp_ws)
    if active:
        problems.append(f"HCP has runs in flight: {', '.join(active)}")
    version = session.hcp.current_state_version(hcp_ws)
    if version is None:
        problems.append("HCP workspace has no state")
    else:
        session.say(f"HCP state: serial={version.serial} terraform_version={version.terraform_version}")
    if args.no_plane:
        session.say("plane checks skipped (--no-plane)")
    elif version is not None:
        problems.extend(
            _plane_problems(session.deps.plane_factory(_environment(args.env)), plane_ws, version.terraform_version)
        )
    for problem in problems:
        session.console.error(f"FAIL {problem}")
    if problems:
        return 1
    session.say("preflight ok")
    return 0


def cmd_lock(session: Session, args: argparse.Namespace) -> int:
    """Lock the HCP workspace with a reason naming the issue; locking twice is a no-op."""
    hcp_ws = check_hcp_workspace(args.hcp_workspace)
    reason = LOCK_REASON.format(issue=check_issue_key(args.issue))
    workspace = session.hcp.workspace(hcp_ws)
    if workspace.locked:
        if workspace.locked_by == session.hcp.account_id():
            session.say(f"HCP workspace {workspace.name} is already locked by you")
            return 0
        raise Refused(f"HCP workspace {workspace.name} is locked by someone else ({workspace.locked_by})")
    session.hcp.lock(hcp_ws, reason)
    session.say(f"locked HCP workspace {workspace.name}: {reason}")
    active = session.hcp.active_runs(hcp_ws)
    if active:
        session.console.error(f"WAIT runs still in flight: {', '.join(active)}; move-state refuses until they finish")
    return 0


def cmd_unlock(session: Session, args: argparse.Namespace) -> int:
    """Unlock the HCP workspace, only if the token's user holds the lock."""
    hcp_ws = check_hcp_workspace(args.hcp_workspace)
    workspace = session.hcp.workspace(hcp_ws)
    if not workspace.locked:
        session.say(f"HCP workspace {workspace.name} is not locked")
        return 0
    _require_our_lock(session, workspace)
    session.hcp.unlock(hcp_ws)
    session.say(f"unlocked HCP workspace {workspace.name}")
    return 0


def cmd_move_state(session: Session, args: argparse.Namespace) -> int:
    """Copy HCP's current state byte for byte into the plane's S3 key and prove it reads back unchanged."""
    environment = _environment(args.env)
    hcp_ws = check_hcp_workspace(args.hcp_workspace)
    plane_ws = check_plane_workspace(args.plane_workspace)
    key = state_key(plane_ws)
    workspace = session.hcp.workspace(hcp_ws)
    _require_our_lock(session, workspace)
    _require_idle(session, workspace)
    version = _require_state_version(session, workspace)
    if args.dry_run:
        session.say("dry run: plane API not called")
    else:
        problems = _plane_problems(session.deps.plane_factory(environment), plane_ws, version.terraform_version)
        if problems:
            raise Refused("; ".join(problems))
    store = session.deps.store_factory(environment)
    with PrivateDir(session.deps.private_parent) as private:
        body, meta = _download_meta(session, version)
        session.say(f"HCP state: {meta.describe()}")
        if meta.terraform_version != version.terraform_version:
            raise StateError(
                f"state body says terraform {meta.terraform_version}, the version says {version.terraform_version}"
            )
        engine = session.deps.resolver.resolve(version.terraform_version, args.engine_bin)
        session.say(f"engine: terraform {engine.version} at {engine.binary}")
        for existing in (key, f"{key}.tflock"):
            if store.exists(existing):
                raise Refused(f"s3://{environment.state_bucket}/{existing} already exists; refusing to overwrite")
        kms_key_arn = store.kms_key_arn()
        workdir = private.subdir("s3")
        (workdir / "backend.tf").write_text(
            s3_backend(environment.state_bucket, key, environment.region, kms_key_arn, f"workspaces/{plane_ws}/env")
        )
        env = engine_env({"AWS_PROFILE": environment.aws_profile, "AWS_REGION": environment.region})
        engine.init(workdir, env)
        session.say(f"engine init ok against s3://{environment.state_bucket}/{key}")
        if args.dry_run:
            session.say("dry run: stopping before the push; nothing was written")
            return 0
        store.put_new(key, body, kms_key_arn)
        session.say(f"wrote s3://{environment.state_bucket}/{key}")
        stored = store.get(key)
        stored_meta = parse_meta(stored)
        check_match(meta, stored_meta)
        if stored_meta.sha256 != meta.sha256:
            raise StateError("stored object differs from the downloaded state")
        pulled = parse_meta(engine.state_pull(workdir, env))
        check_match(meta, pulled)
        session.say(f"verified: S3 read back and terraform {engine.version} state pull both show {meta.describe()}")
    session.say("HCP stays locked; it is the rollback point until the plane's first apply is verified")
    return 0


def cmd_compare_vars(session: Session, args: argparse.Namespace) -> int:
    """Compare variable names, categories and flags; values are never fetched into the comparison."""
    hcp_ws = check_hcp_workspace(args.hcp_workspace)
    plane_ws = check_plane_workspace(args.plane_workspace)
    ignored = () if args.include_tfc else IGNORED_PREFIXES
    hcp_vars = [shape for shape in session.hcp.variables(hcp_ws) if not shape.key.startswith(ignored)]
    for shape in hcp_vars:
        session.say(f"HCP   {_describe(shape)}")
    if args.no_plane:
        session.say("plane comparison skipped (--no-plane)")
        return 0
    plane_vars = session.deps.plane_factory(_environment(args.env)).variables(plane_ws)
    plane_by_key = {(shape.category, shape.key): shape for shape in plane_vars}
    hcp_by_key = {(shape.category, shape.key): shape for shape in hcp_vars}
    failed = False
    for name, shape in sorted(hcp_by_key.items()):
        other = plane_by_key.get(name)
        if other is None:
            session.console.error(f"MISSING on plane: {shape.category} {shape.key}")
            failed = True
        elif (other.sensitive, other.hcl) != (shape.sensitive, shape.hcl):
            session.console.error(
                f"DIFFERS {shape.category} {shape.key}: HCP sensitive={shape.sensitive} hcl={shape.hcl}, "
                f"plane sensitive={other.sensitive} hcl={other.hcl}"
            )
            failed = True
    for name in sorted(set(plane_by_key) - set(hcp_by_key)):
        session.say(f"EXTRA on plane: {name[0]} {name[1]}")
    if failed:
        return 1
    session.say("variables match")
    return 0


def _describe(shape: VariableShape) -> str:
    """One variable line with no value."""
    return f"{shape.category:9} {shape.key} sensitive={shape.sensitive} hcl={shape.hcl} source={shape.source}"


def cmd_rollback_state(session: Session, args: argparse.Namespace) -> int:
    """Push the plane's newest state back to HCP with HCP's engine, verify it, then unlock HCP."""
    environment = _environment(args.env)
    hcp_ws = check_hcp_workspace(args.hcp_workspace)
    plane_ws = check_plane_workspace(args.plane_workspace)
    key = state_key(plane_ws)
    workspace = session.hcp.workspace(hcp_ws)
    _require_our_lock(session, workspace)
    _require_idle(session, workspace)
    version = _require_state_version(session, workspace)
    store = session.deps.store_factory(environment)
    plane_body = store.get(key)
    session.console.redactor.add_state(parse_state(plane_body))
    plane_meta = parse_meta(plane_body)
    session.say(f"plane state: {plane_meta.describe()}")
    with PrivateDir(session.deps.private_parent) as private:
        _hcp_body, hcp_meta = _download_meta(session, version)
        session.say(f"HCP state: {hcp_meta.describe()}")
        check_match(hcp_meta, plane_meta, exact=False)
        if plane_meta.serial == hcp_meta.serial:
            session.say("HCP already holds the plane's serial; nothing to push")
        else:
            engine = session.deps.resolver.resolve(version.terraform_version, args.engine_bin)
            session.say(f"engine: terraform {engine.version} at {engine.binary}")
            state_file = private.write("plane.tfstate", plane_body)
            workdir = private.subdir("hcp")
            (workdir / "backend.tf").write_text(cloud_backend(HCP_HOST, workspace.organization, workspace.name))
            env = engine_env({"TF_TOKEN_app_terraform_io": session.token})
            engine.init(workdir, env)
            if args.dry_run:
                session.say("dry run: stopping before the push; HCP stays locked")
                return 0
            engine.state_push(workdir, state_file, env, lock=False)
            pushed = session.hcp.current_state_version(hcp_ws)
            if pushed is None:
                raise StateError("HCP has no current state version after the push")
            _pushed_body, pushed_meta = _download_meta(session, pushed)
            check_match(plane_meta, pushed_meta, exact=False)
            session.say(f"verified HCP state: {pushed_meta.describe()}")
    if args.dry_run:
        session.say("dry run: HCP stays locked")
        return 0
    session.hcp.unlock(hcp_ws)
    session.say(f"unlocked HCP workspace {workspace.name}")
    return 0


def _parser() -> argparse.ArgumentParser:
    """The argument parser."""
    parser = argparse.ArgumentParser(prog="cutover", description="HCP Terraform to owned control plane cutover.")
    sub = parser.add_subparsers(dest="command", required=True)

    def pair(command: argparse.ArgumentParser) -> None:
        """The HCP and plane workspace ids and the environment."""
        command.add_argument("hcp_workspace", help="HCP workspace id, ws-...")
        command.add_argument("plane_workspace", help="control plane workspace id, ws-<ULID>")
        command.add_argument("--env", choices=sorted(ENVIRONMENTS), required=True)

    preflight = sub.add_parser("preflight", help="read only readiness checks")
    pair(preflight)
    preflight.add_argument("--no-plane", action="store_true", help="skip every plane API call")
    preflight.set_defaults(handler=cmd_preflight)

    lock = sub.add_parser("lock", help="lock the HCP workspace")
    lock.add_argument("hcp_workspace")
    lock.add_argument("--issue", required=True, help="Standupless issue key named in the lock reason")
    lock.set_defaults(handler=cmd_lock)

    unlock = sub.add_parser("unlock", help="unlock the HCP workspace")
    unlock.add_argument("hcp_workspace")
    unlock.set_defaults(handler=cmd_unlock)

    move = sub.add_parser("move-state", help="copy HCP's current state into the plane's S3 backend")
    pair(move)
    move.add_argument("--dry-run", action="store_true", help="everything except the write, and no plane calls")
    move.add_argument("--engine-bin", type=Path, help="a local terraform binary at exactly the state's version")
    move.set_defaults(handler=cmd_move_state)

    compare = sub.add_parser("compare-vars", help="compare variable names and flags, never values")
    pair(compare)
    compare.add_argument("--no-plane", action="store_true", help="list HCP's variables only")
    compare.add_argument("--include-tfc", action="store_true", help="also compare TFC_ dynamic credential variables")
    compare.set_defaults(handler=cmd_compare_vars)

    rollback = sub.add_parser("rollback-state", help="push the plane's newest state back to HCP and unlock it")
    pair(rollback)
    rollback.add_argument("--dry-run", action="store_true", help="everything except the push and the unlock")
    rollback.add_argument("--engine-bin", type=Path, help="a local terraform binary at HCP's state version")
    rollback.set_defaults(handler=cmd_rollback_state)
    return parser


def main(argv: Sequence[str] | None = None, deps: Deps | None = None) -> int:
    """Parse, run one command and map every failure to an exit code without a traceback."""
    args = _parser().parse_args(argv)
    session = Session(deps or Deps())
    handler: Callable[[Session, argparse.Namespace], int] = args.handler
    try:
        return handler(session, args)
    except UsageError as error:
        session.console.error(f"usage: {error}")
        return 2
    except Refused as error:
        session.console.error(f"REFUSED {error}")
        return 1
    except FAILURES as error:
        session.console.error(f"FAILED {type(error).__name__}: {error}")
        return 1
    except Exception as error:
        session.console.error(f"FAILED unexpected {type(error).__name__}: {error}")
        return 1
