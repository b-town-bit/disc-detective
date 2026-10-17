"""python -m disc_detective          -> the app
python -m disc_detective D:\\ ...  -> the command line (same options as disc-detective)"""
import sys


def main():
    if len(sys.argv) > 1:
        from .core import main as cli
        cli()
    else:
        from .app import main as gui
        gui()


if __name__ == "__main__":
    main()
