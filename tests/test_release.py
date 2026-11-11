"""The packaged build's parts that can be checked without building: its self-check, and the licences it gathers."""
import os
import sys

import pytest

from disc_detective import selfcheck

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_the_self_check_passes_from_source():
    """What a build's --self-check asks, asked of this copy: modules, files, RapidOCR reading the app's own font,
    OpenCV decoding, a window drawn (where RapidOCR and OpenCV are installed: Python 3.14 or older)."""
    pytest.importorskip("rapidocr")
    pytest.importorskip("cv2")
    got = {c["name"]: c for c in selfcheck.run()}
    failed = {n: c["said"] for n, c in got.items() if c["required"] and not c["ok"]}
    assert not failed
    assert "DETECTIVE" in got["rapidocr"]["said"].upper()


def test_the_self_check_reports_a_failure(monkeypatch, tmp_path):
    """A required check that fails: the report says so, with why, and the exit code is 1 (the build stops)."""
    def broken():
        raise AssertionError("read ''")
    monkeypatch.setattr(selfcheck, "CHECKS", (("rapidocr", broken, True), ("windows_ocr", lambda: "n/a", False)))
    report = tmp_path / "r.json"
    assert selfcheck.main([str(report)]) == 1
    import json
    got = json.loads(report.read_text(encoding="utf-8"))
    assert got["ok"] is False and got["checks"][0]["said"].startswith("AssertionError: read ''")


@pytest.mark.skipif(sys.version_info < (3, 11), reason="the build reads pyproject.toml with tomllib (3.11+)")
def test_the_build_gathers_each_bundled_packages_licence(tmp_path):
    """Every package the build carries, its licence files copied (one with none of its own: the text of the licence
    it names), Python's own, and a list; the app's own build tools (PyInstaller's helpers) aren't carried."""
    pytest.importorskip("rapidocr")
    pytest.importorskip("packaging")
    sys.path.insert(0, os.path.join(ROOT, "release"))
    try:
        import build
    finally:
        sys.path.pop(0)
    if not any(d.metadata["Name"].lower() == "pyinstaller" for d in _installed()):
        pytest.skip("PyInstaller isn't installed here")
    names = {build._key(d.metadata["Name"]) for d in build.bundled_packages()}
    assert {"pillow", "rapidocr", "onnxruntime", "hdmv-menus", "numpy", "opencv-python-headless"} <= names
    assert "pyinstaller" in names and not names & {"altgraph", "pefile", "setuptools", "pyinstaller-hooks-contrib"}
    rows = build.gather_licences(str(tmp_path / "licenses"))
    assert rows[0][0] == "Python" and os.path.isfile(tmp_path / "licenses" / "python" / "LICENSE.txt")
    for n in names:
        assert os.listdir(tmp_path / "licenses" / n), n
    listed = (tmp_path / "licenses" / "README.txt").read_text(encoding="utf-8")
    assert "rapidocr" in listed and "Apache-2.0" in listed


def _installed():
    import importlib.metadata as md
    return list(md.distributions())
