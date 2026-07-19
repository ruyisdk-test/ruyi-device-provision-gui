"""Run one repository update through ruyi in an interruptible child process."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 2:
        raise SystemExit("usage: repo_update_child CONFIG_PATH REPO_ID")
    config_path = Path(argv[0])
    repo_id = argv[1]
    if config_path.name != "config.toml" or config_path.parent.name != "ruyi":
        raise SystemExit("ruyi config path must end in ruyi/config.toml")
    os.environ["XDG_CONFIG_HOME"] = os.fspath(config_path.parent.parent)
    if hasattr(os, "setpgrp"):
        os.setpgrp()

    import argparse

    from ruyi.config import GlobalConfig
    from ruyi.log import RuyiConsoleLogger
    from ruyi.ruyipkg.composite_repo import CompositeRepo
    from ruyi.ruyipkg.update_cli import UpdateCommand
    from ruyi.utils.global_mode import EnvGlobalModeProvider

    command_argv = ["ruyi", "update", "--repo", repo_id]
    gm = EnvGlobalModeProvider(os.environ, command_argv)
    logger = RuyiConsoleLogger(gm)
    config = GlobalConfig.load_from_config(gm, logger)
    entries = [entry for entry in config.repo_entries if entry.id == repo_id]
    if not entries or not entries[0].active:
        logger.F(f"no active repo with id '{repo_id}'")
        return 1
    # Keep the native update command, but scope every operation in this child
    # process to the repository selected by the GUI.
    config.__dict__["repo"] = CompositeRepo(entries, config)
    return UpdateCommand.main(config, argparse.Namespace(repo=repo_id))


if __name__ == "__main__":
    raise SystemExit(main())
