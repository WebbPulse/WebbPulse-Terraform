"""The documentation a module page shows, read from a published version's tarball.

Like HCP Terraform's module page: the README, the inputs and outputs, the
provider requirements, the resources, and the same for each submodule under
`modules/`, which a source reaches with `//modules/<name>`. It is extracted once
when a version publishes and stored beside the tarball as JSON, so a page view
never parses HCL. A version published before extraction existed is extracted on
its first view.

The tarball is a module author's content, so every read is bounded: file sizes,
file counts and the README length. A file that does not parse is named in
`parse_errors` and left out rather than failing the version, since the engine,
not this reader, is the authority on whether the module is valid.
"""

from __future__ import annotations

import json
import logging
import posixpath
import tarfile
from pathlib import Path
from typing import Any, Final, Mapping, Optional, cast

_log = logging.getLogger(__name__)

DOCS_SCHEMA: Final = 1
"""Bumped when the extracted shape changes, so older documents are extracted again."""

MAX_TF_FILE_BYTES: Final = 1_000_000
MAX_TF_BYTES: Final = 5_000_000
MAX_TF_FILES: Final = 400
MAX_README_BYTES: Final = 256_000
MAX_SUBMODULES: Final = 100
README_NAMES: Final = ("readme.md", "readme.markdown", "readme")


def _unquote(value: str) -> str:
    """A string attribute as its text: quotes, escapes and heredoc markers removed."""
    if len(value) >= 2 and value.startswith('"') and value.endswith('"'):
        inner = value[1:-1]
        if inner.startswith("<<"):
            return _heredoc(inner)
        try:
            decoded = json.loads(value)
        except ValueError:
            return inner
        return decoded if isinstance(decoded, str) else inner
    return value


def _heredoc(text: str) -> str:
    """A heredoc's body, dedented for the `<<-` form as the engine does."""
    lines = text.split("\n")
    marker = lines[0]
    body = lines[1:-1] if len(lines) > 1 else []
    if marker.startswith("<<-"):
        indents = [len(line) - len(line.lstrip()) for line in body if line.strip()]
        cut = min(indents) if indents else 0
        body = [line[cut:] for line in body]
    return "\n".join(body)


def _expression(value: str) -> str:
    """An interpolation wrapper removed, so `${list(string)}` reads `list(string)`."""
    if value.startswith("${") and value.endswith("}"):
        return value[2:-1]
    return value


def render(value: Any, indent: int = 0) -> str:
    """A parsed value as HCL text, for a default or a type the page prints."""
    pad = "  " * indent
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        if value.startswith('"') and value.endswith('"') and value[1:].startswith("<<"):
            return json.dumps(_heredoc(value[1:-1]))
        return _expression(value)
    if isinstance(value, list):
        items = cast(list[Any], value)
        if not items:
            return "[]"
        inner = [f"{pad}  {render(item, indent + 1)}," for item in items]
        return "[\n" + "\n".join(inner) + f"\n{pad}]"
    if isinstance(value, dict):
        entries = {str(key): item for key, item in cast(dict[str, Any], value).items() if not key.startswith("__")}
        if not entries:
            return "{}"
        inner = [f"{pad}  {_unquote(key)} = {render(item, indent + 1)}" for key, item in entries.items()]
        return "{\n" + "\n".join(inner) + f"\n{pad}}}"
    return str(value)


def _entries(value: Any) -> list[dict[str, Any]]:
    """The mappings in a parsed block list, anything else dropped."""
    if not isinstance(value, list):
        return []
    return [cast(dict[str, Any], entry) for entry in cast(list[Any], value) if isinstance(entry, dict)]


def _blocks(document: Mapping[str, Any], kind: str) -> list[tuple[str, dict[str, Any]]]:
    """The labelled blocks of one kind, as `(label, body)` pairs."""
    found: list[tuple[str, dict[str, Any]]] = []
    for entry in _entries(document.get(kind)):
        for label, body in entry.items():
            if isinstance(body, dict):
                found.append((_unquote(label), cast(dict[str, Any], body)))
    return found


def _text(body: Mapping[str, Any], key: str) -> Optional[str]:
    """A string attribute's text, or `None` when absent or not a string."""
    value = body.get(key)
    return _unquote(value) if isinstance(value, str) else None


def _collect(documents: list[dict[str, Any]]) -> dict[str, Any]:
    """Inputs, outputs, provider requirements and resources from one directory's files."""
    inputs: list[dict[str, Any]] = []
    outputs: list[dict[str, Any]] = []
    providers: dict[str, dict[str, Optional[str]]] = {}
    resources: list[dict[str, str]] = []
    for document in documents:
        for name, body in _blocks(document, "variable"):
            has_default = "default" in body
            inputs.append(
                {
                    "name": name,
                    "type": _expression(body["type"]) if isinstance(body.get("type"), str) else None,
                    "description": _text(body, "description"),
                    "default": render(body["default"]) if has_default else None,
                    "required": not has_default,
                    "sensitive": body.get("sensitive") is True,
                }
            )
        for name, body in _blocks(document, "output"):
            outputs.append(
                {"name": name, "description": _text(body, "description"), "sensitive": body.get("sensitive") is True}
            )
        for terraform in _entries(document.get("terraform")):
            for required in _entries(terraform.get("required_providers")):
                for local, spec in required.items():
                    if local.startswith("__"):
                        continue
                    source = version = None
                    if isinstance(spec, dict):
                        source = _text(cast(dict[str, Any], spec), "source")
                        version = _text(cast(dict[str, Any], spec), "version")
                    elif isinstance(spec, str):
                        version = _unquote(spec)
                    providers[local] = {"source": source, "version": version}
        for entry in _entries(document.get("resource")):
            for kind, named in entry.items():
                if not isinstance(named, dict):
                    continue
                for label in cast(dict[str, Any], named):
                    if not label.startswith("__"):
                        resources.append({"type": _unquote(kind), "name": _unquote(label)})
    inputs.sort(key=lambda item: (not item["required"], item["name"]))
    outputs.sort(key=lambda item: item["name"])
    resources.sort(key=lambda item: (item["type"], item["name"]))
    return {
        "inputs": inputs,
        "outputs": outputs,
        "providers": [{"name": name, **spec} for name, spec in sorted(providers.items())],
        "resources": resources,
    }


def _parse(text: str) -> dict[str, Any]:
    """One `.tf` file's blocks. Imported late so the parser loads only when extracting."""
    import hcl2

    parsed: Any = hcl2.loads(text)
    return cast(dict[str, Any], parsed) if isinstance(parsed, dict) else {}


def extract(tarball: Path) -> dict[str, Any]:
    """The documentation of the module packed at `tarball`, bounded throughout."""
    directories: dict[str, list[dict[str, Any]]] = {}
    readmes: dict[str, str] = {}
    parse_errors: list[str] = []
    files = 0
    parsed_bytes = 0
    with tarfile.open(tarball, "r:gz") as archive:
        for member in archive:
            if not member.isfile():
                continue
            name = member.name[2:] if member.name.startswith("./") else member.name
            directory, filename = posixpath.split(name)
            if directory and not (directory.startswith("modules/") and directory.count("/") == 1):
                continue
            if filename.lower() in README_NAMES and member.size <= MAX_README_BYTES:
                handle = archive.extractfile(member)
                if handle is not None and (directory not in readmes or filename.lower() == "readme.md"):
                    readmes[directory] = handle.read().decode("utf-8", errors="replace")
                continue
            if not filename.endswith(".tf"):
                continue
            files += 1
            if files > MAX_TF_FILES or member.size > MAX_TF_FILE_BYTES or parsed_bytes + member.size > MAX_TF_BYTES:
                parse_errors.append(name)
                continue
            parsed_bytes += member.size
            handle = archive.extractfile(member)
            if handle is None:
                continue
            try:
                document = _parse(handle.read().decode("utf-8"))
            except Exception:  # noqa: BLE001
                parse_errors.append(name)
                continue
            directories.setdefault(directory, []).append(document)
    root = _collect(directories.get("", []))
    submodules: list[dict[str, Any]] = []
    for directory in sorted(key for key in directories if key)[:MAX_SUBMODULES]:
        submodules.append(
            {
                "name": directory.split("/", 1)[1],
                "path": directory,
                "readme": readmes.get(directory),
                **_collect(directories[directory]),
            }
        )
    return {
        "schema": DOCS_SCHEMA,
        "readme": readmes.get(""),
        **root,
        "submodules": submodules,
        "parse_errors": sorted(parse_errors),
    }


__all__ = ["DOCS_SCHEMA", "extract", "render"]
