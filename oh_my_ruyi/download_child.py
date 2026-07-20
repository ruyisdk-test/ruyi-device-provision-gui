"""Subprocess entry point for package download/install.

Running the download in a separate process lets the GUI capture stdout/stderr
and terminate the whole operation cleanly if the user cancels or closes the
window while curl/wget/tar is active.
"""

from __future__ import annotations

import os
import sys

from .i18n import initialize, localize_config

from ruyi.config import GlobalConfig
from ruyi.log import RuyiConsoleLogger
from ruyi.utils.global_mode import EnvGlobalModeProvider

from . import ruyi_facade


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]

    if hasattr(os, "setpgrp"):
        os.setpgrp()

    initialize()
    gm = EnvGlobalModeProvider(os.environ, list(sys.argv))
    logger = RuyiConsoleLogger(gm)
    config = localize_config(GlobalConfig.load_from_config(gm, logger))
    mr = ruyi_facade.ensure_repo(config)
    return ruyi_facade.run_download(config, mr, argv)


if __name__ == "__main__":
    raise SystemExit(main())
