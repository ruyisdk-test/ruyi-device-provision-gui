"""Read or mark repository news through ruyi's imported APIs."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 2 or argv[1] not in {"read", "mark"}:
        raise SystemExit("usage: repo_news_child CONFIG_PATH read|mark")
    config_path = Path(argv[0])
    action = argv[1]
    if config_path.name != "config.toml" or config_path.parent.name != "ruyi":
        raise SystemExit("ruyi config path must end in ruyi/config.toml")
    os.environ["XDG_CONFIG_HOME"] = os.fspath(config_path.parent.parent)
    if hasattr(os, "setpgrp"):
        os.setpgrp()

    from .i18n import initialize, localize_config

    initialize()

    from ruyi.config import GlobalConfig
    from ruyi.log import RuyiConsoleLogger
    from ruyi.ruyipkg.news import do_news_read
    from ruyi.utils.global_mode import EnvGlobalModeProvider

    command_argv = ["ruyi", "news", "read"]
    gm = EnvGlobalModeProvider(os.environ, command_argv)
    logger = RuyiConsoleLogger(gm)
    config = localize_config(GlobalConfig.load_from_config(gm, logger))
    return do_news_read(config, quiet=action == "mark", items_strs=[])


if __name__ == "__main__":
    raise SystemExit(main())
