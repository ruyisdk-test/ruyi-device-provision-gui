"""Application bootstrap: build ``GlobalConfig`` and launch the main window."""

from __future__ import annotations

import os
import sys
from typing import Callable, Optional

from .i18n import initialize, install_qt_translations, localize_config

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


def _build_config_with_loader() -> tuple[
    GlobalConfig,
    QtRuyiLogger,
    LogEmitter,
    Callable[[], GlobalConfig],
]:
    """Construct the ruyi ``GlobalConfig`` wired to a :class:`QtRuyiLogger`."""
    initialize()
    gm = _make_global_mode()
    emitter = LogEmitter()
    logger = QtRuyiLogger(gm, emitter)
    config = localize_config(GlobalConfig.load_from_config(gm, logger))
    return (
        config,
        logger,
        emitter,
        lambda: localize_config(GlobalConfig.load_from_config(gm, logger)),
    )


def build_config() -> tuple[GlobalConfig, QtRuyiLogger, LogEmitter]:
    """Construct the public application configuration tuple."""
    config, logger, emitter, _loader = _build_config_with_loader()
    return config, logger, emitter


def run(argv: Optional[list[str]] = None) -> int:
    """Run the application. Returns the exit code."""
    if argv is None:
        argv = sys.argv
    app = QApplication(argv)
    app.setApplicationName("oh-my-ruyi")
    install_qt_translations(app)

    config, _logger, emitter, config_loader = _build_config_with_loader()
    window = ProvisionMainWindow(
        config,
        _logger,
        emitter,
        config_loader=config_loader,
    )
    window.show()
    return app.exec()


__all__ = ["build_config", "run"]
