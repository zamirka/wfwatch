"""``python -m wfwatch`` – the same as the ``wfwatch`` command."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
