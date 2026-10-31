# Double-click to open Disc Detective on Windows (.pyw = no console window).
# A Python environment in the project (.venv, venv, env, or temp/py*: a supported Python, the
# newest of them) runs it, with what's installed there (RapidOCR, which Python 3.15 can't have
# yet); double-clicking otherwise runs whichever Python Windows opens .pyw files with.
import glob
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
NEWEST = (3, 14)          # the newest Python the app supports (pyproject.toml's requires-python)


def project_env():
    """The project's own environment, as (its folder, its windowless Python), or None."""
    found = []
    for d in [os.path.join(HERE, n) for n in (".venv", "venv", "env")] + glob.glob(os.path.join(HERE, "temp", "py*")):
        cfg = os.path.join(d, "pyvenv.cfg")
        exe = os.path.join(d, "Scripts", "pythonw.exe") if os.name == "nt" else os.path.join(d, "bin", "python3")
        if not (os.path.isfile(cfg) and os.path.isfile(exe)):
            continue
        try:
            with open(cfg, encoding="utf-8") as fh:
                m = re.search(r"^version(?:_info)?\s*=\s*(\d+)\.(\d+)", fh.read(), re.M)
        except OSError:
            continue
        version = (int(m.group(1)), int(m.group(2))) if m else (0, 0)
        if version <= NEWEST:
            found.append((version, d, exe))
    return max(found)[1:] if found else None


env = project_env()
if env and not os.environ.get("DISC_DETECTIVE_ENV") and \
        os.path.normcase(os.path.abspath(sys.prefix)) != os.path.normcase(os.path.abspath(env[0])):
    # (once: the environment's Python opens this file again, and stays)
    subprocess.Popen([env[1], os.path.abspath(__file__)] + sys.argv[1:],
                     env=dict(os.environ, DISC_DETECTIVE_ENV=env[0]), cwd=HERE)
    sys.exit(0)

sys.path.insert(0, HERE)
from disc_detective.app import main  # noqa: E402

main()
