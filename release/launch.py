"""The packaged build's program: the app, or its self-check (`--self-check [report.json]`)."""
import sys

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--self-check":
        from disc_detective import selfcheck
        sys.exit(selfcheck.main(sys.argv[2:]))
    from disc_detective.app import main
    main()
