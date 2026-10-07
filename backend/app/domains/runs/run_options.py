"""The plan options a run can carry beyond its workspace's settings.

`terraform plan` and `apply` through a cloud block send `-target`, `-replace`,
`-refresh=false`, `-refresh-only` and `-var` as attributes of the run they create.
They are checked here, stored on the run row and handed to the runner in the
bundle. Run variable values are HCL expressions, checked by the same scanner as a
workspace's HCL variables, and are sealed on the row because a `-var` may carry a
secret; only their keys stay readable.
"""

from __future__ import annotations

import json
from typing import Any, Final, Mapping

from ...common.composition.settings import Settings
from ...common.core import variable_cipher
from ...common.workspaces import hcl

MAX_ADDRESSES: Final = 100
"""How many `-target` or `-replace` addresses one run may name."""

MAX_ADDRESS_LENGTH: Final = 1024
"""The longest resource address accepted, well past any real one."""

MAX_RUN_VARIABLES: Final = 100
"""How many `-var` values one run may carry."""

SEALED_ATTRIBUTE: Final = "run_variables_sealed"
"""The row attribute holding the sealed run variables, never rendered."""

KEYS_ATTRIBUTE: Final = "run_variable_keys"
"""The row attribute naming the run variables, which is safe to show."""


class InvalidRunOptions(ValueError):
    """A run option the engine could not take, or one that could smuggle another flag."""


def _addresses(value: Any, name: str) -> list[str]:
    """A list of resource addresses, each a single line the engine reads as one flag value."""
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > MAX_ADDRESSES:
        raise InvalidRunOptions(f"{name} must be a list of at most {MAX_ADDRESSES} addresses")
    addresses: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise InvalidRunOptions(f"{name} must hold non-empty strings")
        if len(item) > MAX_ADDRESS_LENGTH or any(ord(character) < 32 for character in item):
            raise InvalidRunOptions(f"{name} holds an address that is not a single short line")
        addresses.append(item.strip())
    return addresses


def _variables(value: Any) -> dict[str, str]:
    """Run variables keyed by name, each value a checked HCL expression."""
    if value is None:
        return {}
    if not isinstance(value, Mapping) or len(value) > MAX_RUN_VARIABLES:
        raise InvalidRunOptions(f"run variables must be at most {MAX_RUN_VARIABLES} key and value pairs")
    variables: dict[str, str] = {}
    for key, expression in value.items():
        if not isinstance(key, str) or not isinstance(expression, str):
            raise InvalidRunOptions("run variables must be strings")
        try:
            hcl.validate_name(key)
            hcl.validate(expression)
        except hcl.InvalidHcl as error:
            raise InvalidRunOptions(f"run variable {key}: {error}") from error
        variables[key] = expression
    return variables


def parse(payload: Mapping[str, Any]) -> dict[str, Any]:
    """The run options in a create payload, checked, with only those that differ from a default.

    Raises:
        InvalidRunOptions: An option is malformed.
    """
    refresh = payload.get("refresh", True)
    refresh_only = payload.get("refresh_only", False)
    if not isinstance(refresh, bool) or not isinstance(refresh_only, bool):
        raise InvalidRunOptions("refresh and refresh_only must be booleans")
    options: dict[str, Any] = {}
    if targets := _addresses(payload.get("target_addrs"), "target_addrs"):
        options["target_addrs"] = targets
    if replacements := _addresses(payload.get("replace_addrs"), "replace_addrs"):
        options["replace_addrs"] = replacements
    if not refresh:
        options["refresh"] = False
    if refresh_only:
        options["refresh_only"] = True
    if variables := _variables(payload.get("run_variables")):
        options["run_variables"] = variables
    return options


def stored(options: Mapping[str, Any], *, workspace_id: str, run_id: str, settings: Settings) -> dict[str, Any]:
    """The row attributes for parsed options, the run variables sealed to this run.

    Raises:
        MasterKeyUnavailable: The run carries variables and no master key is configured.
    """
    item = {key: value for key, value in options.items() if key != "run_variables"}
    variables = options.get("run_variables")
    if variables:
        item[SEALED_ATTRIBUTE] = variable_cipher.seal(
            json.dumps(variables, sort_keys=True),
            workspace_id=workspace_id,
            key=f"run:{run_id}",
            settings=settings,
        )
        item[KEYS_ATTRIBUTE] = sorted(variables)
    return item


def bundle_fields(run: Mapping[str, Any], *, settings: Settings) -> dict[str, Any]:
    """The bundle's run options, opening the sealed run variables."""
    sealed = run.get(SEALED_ATTRIBUTE)
    variables: dict[str, str] = {}
    if sealed:
        opened = variable_cipher.open_sealed(
            sealed,
            workspace_id=str(run["workspace_id"]),
            key=f"run:{run['run_id']}",
            settings=settings,
        )
        variables = {str(key): str(value) for key, value in json.loads(opened).items()}
    return {
        "target_addrs": [str(address) for address in run.get("target_addrs") or []],
        "replace_addrs": [str(address) for address in run.get("replace_addrs") or []],
        "refresh": bool(run.get("refresh", True)),
        "refresh_only": bool(run.get("refresh_only", False)),
        "run_variables": variables,
    }


__all__ = [
    "KEYS_ATTRIBUTE",
    "MAX_ADDRESSES",
    "MAX_RUN_VARIABLES",
    "SEALED_ATTRIBUTE",
    "InvalidRunOptions",
    "bundle_fields",
    "parse",
    "stored",
]
