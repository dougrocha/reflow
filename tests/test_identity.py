import os

from reflow import identity


def test_tool_versions_cover_the_parsers_and_the_ocr_programs():
    versions = identity.tool_versions()
    for name in ("pdfminer.six", "pillow", "pdfplumber", "pypdfium2", "python",
                 "ocrmypdf-program", "tesseract-program"):
        assert name in versions
    assert identity.tool_key() == ";".join(f"{k}={v}" for k, v in versions.items())


def test_dependency_versions_add_the_renderer_s_packages_to_the_stage_keys_tools():
    assert identity.dependency_versions().keys() - identity.tool_versions().keys() == {"markupsafe"}


def test_a_program_off_path_is_absent_and_one_that_prints_nothing_is_unknown(monkeypatch):
    monkeypatch.setattr(identity.shutil, "which", lambda name: None)
    assert identity.program_version("tesseract") == "absent"
    monkeypatch.setattr(identity.shutil, "which", lambda name: "/usr/bin/tesseract")
    monkeypatch.setattr(identity, "program_output", lambda *command: "")
    assert identity.program_version("tesseract") == "unknown"
    monkeypatch.setattr(identity, "program_output", lambda *command: "tesseract 5.5\nleptonica 1.8\n")
    assert identity.program_version("tesseract") == "tesseract 5.5"


def test_a_file_is_hashed_again_only_when_its_size_or_mtime_changes(tmp_path):
    path = tmp_path / "f"
    path.write_bytes(b"one")
    first = identity.file_sha256(path)
    stat = path.stat()
    path.write_bytes(b"two")
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert identity.file_sha256(path) == first   # same size and mtime: remembered
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1))
    assert identity.file_sha256(path) != first
