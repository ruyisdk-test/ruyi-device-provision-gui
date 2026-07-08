"""Application bootstrap: build ``GlobalConfig`` and launch the main window."""

from __future__ import annotations

import os
import sys
from typing import Optional

from PySide6.QtWidgets import QApplication

from ruyi.config import GlobalConfig
from ruyi.utils.global_mode import EnvGlobalModeProvider

from .qt_logger import LogEmitter, QtRuyiLogger
from .main_window import ProvisionMainWindow


def _make_global_mode() -> EnvGlobalModeProvider:
    """Construct an :class:`EnvGlobalModeProvider`.

    The GUI benefits from ruyi's debug logs (download progress, fastboot
    invocations, ...), so we honour ``RUYI_DEBUG`` like the CLI does. Other
    env vars (``RUYI_EXPERIMENTAL`` etc.) are also forwarded.
    """
    return EnvGlobalModeProvider(os.environ, list(sys.argv))


def build_config() -> tuple[GlobalConfig, QtRuyiLogger, LogEmitter]:
    """Construct the ruyi ``GlobalConfig`` wired to a :class:`QtRuyiLogger`."""
    gm = _make_global_mode()
    emitter = LogEmitter()
    logger = QtRuyiLogger(gm, emitter)
    config = GlobalConfig.load_from_config(gm, logger)
    return config, logger, emitter


def run(argv: Optional[list[str]] = None) -> int:
    """Run the application. Returns the exit code."""
    if argv is None:
        argv = sys.argv
    app = QApplication(argv)
    app.setApplicationName("ruyi-device-provision-gui")

    config, _logger, emitter = build_config()
    window = ProvisionMainWindow(config, _logger, emitter)
    window.show()
    return app.exec()


__all__ = ["build_config", "run"]
