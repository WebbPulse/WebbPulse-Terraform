"""Finding a workspace's README inside a config tarball."""

import io
import tarfile

from app.common.workspaces import readme


def pack(files: dict[str, bytes | str], *, prefix: str = "./") -> io.BytesIO:
    """A gzipped tarball holding `files`, each name under `prefix`."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, body in files.items():
            data = body.encode() if isinstance(body, str) else body
            info = tarfile.TarInfo(f"{prefix}{name}")
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    buffer.seek(0)
    return buffer


def test_the_root_readme_is_found():
    """With no working directory the root README is the one shown."""
    found = readme.find_readme(pack({"main.tf": "", "README.md": "# Root\n"}))
    assert found == {"path": "README.md", "content": "# Root\n", "truncated": False}


def test_the_working_directory_readme_wins_over_the_root():
    """HCP shows the README beside the configuration the workspace runs."""
    archive = pack({"README.md": "# Root\n", "stacks/app/README.md": "# App\n", "stacks/app/main.tf": ""})
    found = readme.find_readme(archive, "./stacks/app/")
    assert found is not None
    assert found["path"] == "stacks/app/README.md"
    assert found["content"] == "# App\n"


def test_the_root_readme_is_the_fallback_for_a_working_directory():
    """A working directory with no README of its own shows the repository's."""
    found = readme.find_readme(pack({"README.md": "# Root\n", "stacks/app/main.tf": ""}), "stacks/app")
    assert found is not None
    assert found["path"] == "README.md"


def test_names_are_matched_without_case_and_markdown_is_preferred():
    """`readme.md` in any case wins over a bare `README` in the same directory."""
    found = readme.find_readme(pack({"README": "plain\n", "Readme.MD": "# Markdown\n"}))
    assert found is not None
    assert found["path"] == "Readme.MD"


def test_a_readme_in_another_directory_is_ignored():
    """Only the working directory and the root are looked at."""
    assert readme.find_readme(pack({"modules/vpc/README.md": "# VPC\n", "main.tf": ""})) is None


def test_members_without_a_dot_prefix_are_read():
    """Tarballs packed without `./` names read the same."""
    found = readme.find_readme(pack({"README.md": "# Root\n"}, prefix=""))
    assert found is not None
    assert found["path"] == "README.md"


def test_a_long_readme_is_cut_on_a_line_break():
    """The stored text stays inside the bound and ends on a whole line."""
    line = "x" * 99 + "\n"
    body = line * (readme.MAX_README_BYTES // len(line) + 50)
    found = readme.find_readme(pack({"README.md": body}))
    assert found is not None
    assert found["truncated"] is True
    assert len(found["content"].encode()) <= readme.MAX_README_BYTES
    assert found["content"].endswith("\n")


def test_an_oversized_readme_is_skipped(monkeypatch):
    """A README past the scan ceiling is not read at all."""
    monkeypatch.setattr(readme, "MAX_SCANNED_README_BYTES", 10)
    assert readme.find_readme(pack({"README.md": "# far too long\n"})) is None


def test_a_broken_archive_reads_as_no_readme():
    """Bytes that are not a gzipped tarball are no README, not an error."""
    assert readme.find_readme(io.BytesIO(b"not a tarball")) is None


def test_readme_fields_mark_the_scan_either_way():
    """A tarball with no README is still marked, so it is not streamed again."""
    assert readme.readme_fields(None) == {"readme_scanned": True}
    fields = readme.readme_fields({"path": "README.md", "content": "# Hi\n", "truncated": False})
    assert fields["readme"] == {"path": "README.md", "content": "# Hi\n", "truncated": False}
