# Double-click to open Disc Detective on Windows (.pyw = no console window).
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from disc_detective.app import main  # noqa: E402

main()
