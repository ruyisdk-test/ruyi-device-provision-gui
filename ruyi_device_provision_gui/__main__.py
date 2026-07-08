"""``python -m ruyi_device_provision_gui`` entry point."""

from __future__ import annotations

import sys

from .app import run


def main() -> int:
    return run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
