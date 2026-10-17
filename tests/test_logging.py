"""Log files: everything in disc_detective.log, problems (with tracebacks) in errors.log."""
import logging
import os
import subprocess
import sys

import pytest

from disc_detective import core
import fixtures

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture
def logs(tmp_path):
    d = core.setup_logging(str(tmp_path / "logs"))
    yield d
    for h in list(core.logger.handlers):
        core.logger.removeHandler(h)
        h.close()


def read(d, name):
    for h in core.logger.handlers:
        h.flush()
    p = os.path.join(d, name)
    return open(p, encoding="utf-8").read() if os.path.exists(p) else ""


def test_scan_is_logged_and_problems_go_to_errors(logs, tmp_path, imdb_dir):
    disc = fixtures.make_tv_bluray(str(tmp_path / "JUSTIFIED"))
    args = core.default_args(imdb_dir=imdb_dir, no_discdb_online=True)   # index download fails: unreachable
    args.tmp_out = str(tmp_path / "t")
    args.library = core.library_open(args.db)
    core.identify(disc, args)
    args.library.close()
    main, errors = read(logs, "disc_detective.log"), read(logs, "errors.log")
    assert "scan " in main and "Justified" in main
    assert "index download failed" in errors and "scan " not in errors


def test_exceptions_keep_their_traceback(logs):
    try:
        raise ValueError("boom")
    except ValueError:
        core.logger.exception("rip failed")
    errors = read(logs, "errors.log")
    assert "rip failed" in errors and "Traceback" in errors and "ValueError: boom" in errors


def test_setting_up_twice_does_not_double_lines(logs):
    core.setup_logging(logs)
    core.logger.info("only once")
    assert read(logs, "disc_detective.log").count("only once") == 1
    assert sum(isinstance(h, logging.handlers.RotatingFileHandler) for h in core.logger.handlers) == 2


def test_command_line_writes_logs_in_the_home_folder(tmp_path, imdb_dir, isolated):
    disc = fixtures.make_tv_bluray(str(tmp_path / "JUSTIFIED"), with_thumb=False)
    env = dict(os.environ, PYTHONPATH=ROOT)
    out = subprocess.run([sys.executable, "-m", "disc_detective", disc, "--offline", "--no-record",
                          "--imdb-dir", imdb_dir], capture_output=True, text=True, env=env, timeout=120)
    assert out.returncode == 0, out.stderr
    text = open(os.path.join(str(isolated), "logs", "disc_detective.log"), encoding="utf-8").read()
    assert "disc-detective " in text and "scan " in text
