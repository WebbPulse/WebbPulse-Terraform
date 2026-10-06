"""State identity checks, the private directory and shredding."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from cutover.statefile import (
    LineageMismatch,
    PrivateDir,
    SerialMismatch,
    StateError,
    check_match,
    parse_meta,
    shred_file,
)

from .fakes import LINEAGE, OUTPUT_SECRET, make_state


def test_meta_reads_identity_only() -> None:
    """Serial, lineage and version are read, and the description holds no state contents."""
    meta = parse_meta(make_state(serial=12))
    assert (meta.serial, meta.lineage, meta.terraform_version) == (12, LINEAGE, "1.16.4")
    assert OUTPUT_SECRET not in meta.describe()


@pytest.mark.parametrize(
    "body",
    [b"not json " + OUTPUT_SECRET.encode(), b"[]", b'{"lineage": "x", "terraform_version": "1.0.0"}', b'{"serial": 1}'],
)
def test_bad_bodies_never_quote_the_body(body: bytes) -> None:
    """A malformed body is refused without echoing it."""
    with pytest.raises(StateError) as raised:
        parse_meta(body)
    assert OUTPUT_SECRET not in str(raised.value)


def test_lineage_must_match() -> None:
    """Another history is refused whatever the serial."""
    expected = parse_meta(make_state())
    other = parse_meta(make_state(lineage="00000000-0000-0000-0000-000000000000"))
    with pytest.raises(LineageMismatch):
        check_match(expected, other)
    with pytest.raises(LineageMismatch):
        check_match(expected, other, exact=False)


def test_serial_exact_and_at_least() -> None:
    """Exact mode needs the same serial; the relaxed mode only refuses going backwards."""
    base = parse_meta(make_state(serial=10))
    ahead = parse_meta(make_state(serial=11))
    with pytest.raises(SerialMismatch):
        check_match(base, ahead)
    check_match(base, ahead, exact=False)
    with pytest.raises(SerialMismatch):
        check_match(ahead, base, exact=False)
    check_match(base, parse_meta(make_state(serial=10)))


def test_private_dir_modes_and_cleanup(tmp_path: Path) -> None:
    """The directory is 0700, files 0600, and nothing survives exit."""
    with PrivateDir(tmp_path) as private:
        root = private.path
        assert stat.S_IMODE(root.stat().st_mode) == 0o700
        written = private.write("state.json", make_state())
        assert stat.S_IMODE(written.stat().st_mode) == 0o600
        sub = private.subdir("work")
        assert stat.S_IMODE(sub.stat().st_mode) == 0o700
        (sub / "nested").write_bytes(b"x")
    assert not root.exists()
    assert list(tmp_path.iterdir()) == []


def test_private_dir_is_shredded_after_an_error(tmp_path: Path) -> None:
    """An exception inside the block still removes every file."""
    with pytest.raises(RuntimeError), PrivateDir(tmp_path) as private:
        private.write("state.json", make_state())
        raise RuntimeError("boom")
    assert list(tmp_path.iterdir()) == []


def test_private_write_refuses_to_replace_or_escape(tmp_path: Path) -> None:
    """O_EXCL refuses an existing name, and paths outside the directory are refused."""
    with PrivateDir(tmp_path) as private:
        private.write("state.json", b"{}")
        with pytest.raises(FileExistsError):
            private.write("state.json", b"{}")
        with pytest.raises(ValueError):
            private.write("../escape.json", b"{}")


def test_private_path_closed_outside_context() -> None:
    """The path is unavailable before entry."""
    with pytest.raises(RuntimeError):
        _ = PrivateDir().path


def test_shred_zeroes_the_blocks_before_unlinking(tmp_path: Path) -> None:
    """A hard link to the same inode shows zeros after the shred, proving the bytes were overwritten."""
    target = tmp_path / "state.json"
    body = make_state()
    target.write_bytes(body)
    witness = tmp_path / "witness"
    os.link(target, witness)
    shred_file(target)
    assert not target.exists()
    assert witness.read_bytes() == b"\0" * len(body)


def test_shred_missing_file_is_quiet(tmp_path: Path) -> None:
    """Shredding something already gone is not an error."""
    shred_file(tmp_path / "absent")


def test_private_dir_shreds_rather_than_only_deleting(tmp_path: Path) -> None:
    """A file in the private directory is zeroed on exit, seen through a hard link outside it."""
    witness = tmp_path / "witness"
    parent = tmp_path / "parent"
    parent.mkdir()
    body = make_state()
    with PrivateDir(parent) as private:
        written = private.write("state.json", body)
        os.link(written, witness)
    assert witness.read_bytes() == b"\0" * len(body)
