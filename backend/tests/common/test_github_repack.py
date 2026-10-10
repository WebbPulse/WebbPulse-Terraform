"""The shared rewrite of GitHub's repository archive and its limits."""

from __future__ import annotations

import io
import tarfile
from pathlib import Path

import pytest

from app.common.github import repack


def _archive(path: Path, files: dict[str, bytes]) -> Path:
    """A gzipped tar shaped like GitHub's, every file under one top level directory."""
    with tarfile.open(path, "w:gz") as archive:
        for name, body in files.items():
            info = tarfile.TarInfo(f"owner-repo-sha/{name}")
            info.size = len(body)
            archive.addfile(info, io.BytesIO(body))
    return path


def _names(path: Path) -> list[str]:
    """The member names of a gzipped tar."""
    with tarfile.open(path, "r:gz") as archive:
        return archive.getnames()


def test_the_top_level_directory_and_excluded_paths_are_dropped(tmp_path):
    source = _archive(
        tmp_path / "in.tar.gz",
        {"main.tf": b"", "modules/a.tf": b"", ".git/config": b"", "terraform.tfstate": b"{}"},
    )
    count = repack.repack(source, tmp_path / "out.tar.gz")
    assert count == 1
    assert sorted(_names(tmp_path / "out.tar.gz")) == ["main.tf", "modules/a.tf"]


def test_a_reserved_first_part_is_dropped(tmp_path):
    source = _archive(tmp_path / "in.tar.gz", {"main.tf": b"", ".webbpulse/paths": b"x"})
    repack.repack(source, tmp_path / "out.tar.gz", reserved=frozenset({".webbpulse"}))
    assert _names(tmp_path / "out.tar.gz") == ["main.tf"]


def test_a_path_escaping_the_root_is_rejected(tmp_path):
    source = _archive(tmp_path / "in.tar.gz", {"../evil.tf": b""})
    with pytest.raises(repack.ArchiveRejected):
        repack.repack(source, tmp_path / "out.tar.gz")


def test_too_many_members_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(repack, "MAX_MEMBERS", 2)
    source = _archive(tmp_path / "in.tar.gz", {"a.tf": b"", "b.tf": b"", "c.tf": b""})
    with pytest.raises(repack.ArchiveRejected):
        repack.repack(source, tmp_path / "out.tar.gz")


def test_too_many_unpacked_bytes_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(repack, "MAX_UNPACKED_BYTES", 10)
    source = _archive(tmp_path / "in.tar.gz", {"a.tf": b"x" * 8, "b.tf": b"y" * 8})
    with pytest.raises(repack.ArchiveRejected):
        repack.repack(source, tmp_path / "out.tar.gz")


def test_bytes_that_are_not_a_gzipped_tar_are_rejected(tmp_path):
    source = tmp_path / "in.tar.gz"
    source.write_bytes(b"not an archive")
    with pytest.raises(repack.ArchiveRejected):
        repack.repack(source, tmp_path / "out.tar.gz")
