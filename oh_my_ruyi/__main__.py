"""``python -m oh_my_ruyi`` entry point."""

from __future__ import annotations

import sys

from .app import run


def main() -> int:
    return run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
