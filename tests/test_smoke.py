"""Import-level smoke tests.

These run without a display by forcing the Qt offscreen platform. They only
check that the package wires up correctly — exercising the full wizard needs
a real ruyi metadata repo and is done manually.
"""

from __future__ import annotations

import os


# Force the offscreen Qt platform so the tests don't need a real display.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def test_package_imports_cleanly() -> None:
    import oh_my_ruyi  # noqa: F401
    from oh_my_ruyi import (  # noqa: F401
        app,
        about_tab,
        first_use,
        host_storage,
        main_window,
        qt_logger,
        repo_manager,
        repo_manager_tab,
        repo_news_child,
        repo_presets,
        repo_update_child,
        rich_output,
        ruyi_facade,
        download_child,
        state,
        version_manager,
        workers,
    )


def test_qt_logger_emits_signal(qtbot) -> None:
    """A QtRuyiLogger should re-emit every log call via the LogEmitter."""
    from oh_my_ruyi.qt_logger import LogEmitter, QtRuyiLogger
    from ruyi.utils.global_mode import EnvGlobalModeProvider

    gm = EnvGlobalModeProvider({}, [])
    emitter = LogEmitter()
    logger = QtRuyiLogger(gm, emitter)

    captured: list[tuple[str, str]] = []
    emitter.log_emitted.connect(lambda lvl, txt: captured.append((lvl, txt)))

    logger.I("hello info")
    logger.W("warn")
    logger.stdout("plain")

    assert ("I", "info: hello info") in captured  # ruyi prefixes with "info: "
    assert any(lvl == "W" and "warn" in txt for lvl, txt in captured)
    assert ("stdout", "plain") in captured


def test_qt_logger_preserves_rich_styles_and_links(qtbot) -> None:
    from PySide6.QtWidgets import QApplication
    from ruyi.utils.global_mode import EnvGlobalModeProvider

    from oh_my_ruyi.qt_logger import LogEmitter, QtRuyiLogger
    from oh_my_ruyi.rich_output import RichTextView

    _app = QApplication.instance() or QApplication([])
    emitter = LogEmitter()
    logger = QtRuyiLogger(EnvGlobalModeProvider({}, []), emitter)
    view = RichTextView()
    qtbot.addWidget(view)
    emitter.targeted_terminal_emitted.connect(
        lambda _target, text: view.feed_text(text)
    )
    emitter.start_terminal_delivery()

    logger.stdout("[bold blue]styled[/]")
    logger.I("[link=https://example.com]linked[/]")

    html = view.toHtml()
    assert view.toPlainText().splitlines() == ["styled", "info: linked"]
    assert "font-weight" in html
    assert "color:" in html
    assert "https://example.com" in html
    assert "\x1b" not in view.toPlainText()


def test_qt_logger_preserves_renderables_and_debug_messages(qtbot) -> None:
    from rich.text import Text
    from ruyi.utils.global_mode import EnvGlobalModeProvider

    from oh_my_ruyi.qt_logger import LogEmitter, QtRuyiLogger

    emitter = LogEmitter()
    logger = QtRuyiLogger(
        EnvGlobalModeProvider({"RUYI_DEBUG": "1"}, []),
        emitter,
    )
    captured: list[tuple[str, str]] = []
    terminal: list[str] = []
    emitter.log_emitted.connect(lambda level, text: captured.append((level, text)))
    emitter.terminal_emitted.connect(terminal.append)

    logger.I(Text("linked", style="link https://example.com"))
    logger.D("debug details")

    assert ("I", "info: linked") in captured
    assert ("D", "debug: debug details") in captured
    assert "https://example.com" in "".join(terminal)


def test_rich_text_view_handles_chunked_ansi_and_progress(qtbot) -> None:
    from PySide6.QtWidgets import QApplication

    from oh_my_ruyi.rich_output import RichTextView

    _app = QApplication.instance() or QApplication([])
    view = RichTextView()
    qtbot.addWidget(view)

    view.feed_bytes(b"plain\n\x1b[1;3")
    view.feed_bytes(b"1mred 10%\rgreen 100%\x1b[0m\n", final=True)

    assert view.toPlainText().splitlines() == ["plain", "green 100%"]
    assert "color:" in view.toHtml()
    assert "\x1b" not in view.toPlainText()


def test_rich_text_view_normalizes_bel_terminated_links(qtbot) -> None:
    from PySide6.QtWidgets import QApplication

    from oh_my_ruyi.rich_output import RichTextView

    _app = QApplication.instance() or QApplication([])
    view = RichTextView()
    qtbot.addWidget(view)

    view.feed_text("\x1b]8;;https://example.com\x07link\x1b]8;;\x07\n", final=True)

    assert view.toPlainText().strip() == "link"
    assert "https://example.com" in view.toHtml()
    assert "\x1b" not in view.toPlainText()


def test_rich_text_view_rejects_unsafe_links_and_preserves_erase_prefix(qtbot) -> None:
    from PySide6.QtWidgets import QApplication

    from oh_my_ruyi.rich_output import RichTextView, strip_terminal_controls

    _app = QApplication.instance() or QApplication([])
    view = RichTextView()
    qtbot.addWidget(view)

    view.feed_text(
        "\x1b]8;;javascript:alert(1)\x1b\\link\x1b]8;;\x1b\\\nnew 50%\x1b[K\n",
        final=True,
    )

    assert view.toPlainText().splitlines() == ["link", "new 50%"]
    assert "javascript:" not in view.toHtml()
    assert strip_terminal_controls("ok\x1bPsecret\x1b\\done") == "okdone"


def test_facade_exposes_expected_symbols() -> None:
    from oh_my_ruyi import ruyi_facade

    for name in [
        "list_devices",
        "sync_repo",
        "list_variants",
        "list_combos",
        "combo_package_atoms",
        "run_download",
        "prepare_provision",
        "compute_pretend_steps",
        "run_flash",
        "missing_cmds",
        "needs_fastboot_confirmation",
        "check_fastboot_devices",
        "part_description",
        "get_postinst_msg",
        "is_disk_or_child_mounted",
        "list_disks",
        "storage_platform_hint",
        "list_entity_types",
        "list_package_version_selections",
        "is_package_version_customization_possible",
    ]:
        assert hasattr(ruyi_facade, name), f"ruyi_facade missing {name}"


def test_main_window_constructs(qtbot, tmp_path) -> None:
    """The main window can be constructed with a stub config."""
    from PySide6.QtWidgets import QApplication
    from ruyi.config import GlobalConfig
    from ruyi.utils.global_mode import EnvGlobalModeProvider

    from oh_my_ruyi.qt_logger import LogEmitter, QtRuyiLogger
    from oh_my_ruyi.main_window import ProvisionMainWindow

    _app = QApplication.instance() or QApplication([])
    gm = EnvGlobalModeProvider({}, [])
    emitter = LogEmitter()
    logger = QtRuyiLogger(gm, emitter)
    config = GlobalConfig(gm, logger)

    window = ProvisionMainWindow(
        config,
        logger,
        emitter,
        auto_start=False,
        repo_config_path=tmp_path / "ruyi" / "config.toml",
    )
    assert window.windowTitle() == "Ohh My Ruyi"
    assert window._steps.count() == len(window.STEP_TITLES)
    assert window._stack.count() == len(window.STEP_TITLES)
    assert window._tabs.count() == 4


def test_flash_worker_adds_dd_progress_on_linux(monkeypatch) -> None:
    from oh_my_ruyi import workers
    from oh_my_ruyi.workers import FlashWorker

    monkeypatch.setattr(workers.platform, "system", lambda: "Linux")

    assert FlashWorker._argv_with_gui_progress(["dd", "if=a", "of=b", "bs=4096"]) == [
        "dd",
        "if=a",
        "of=b",
        "bs=4096",
        "status=progress",
    ]
    assert FlashWorker._argv_with_gui_progress(["sudo", "dd", "if=a", "of=b"]) == [
        "sudo",
        "dd",
        "if=a",
        "of=b",
        "status=progress",
    ]
    assert FlashWorker._argv_with_gui_progress(["fastboot", "devices"]) == [
        "fastboot",
        "devices",
    ]
    assert FlashWorker._argv_with_gui_progress(
        ["dd", "if=a", "of=b", "status=none"]
    ) == [
        "dd",
        "if=a",
        "of=b",
        "status=none",
    ]


def test_flash_worker_does_not_add_dd_progress_on_macos(monkeypatch) -> None:
    from oh_my_ruyi import workers
    from oh_my_ruyi.workers import FlashWorker

    monkeypatch.setattr(workers.platform, "system", lambda: "Darwin")

    assert FlashWorker._argv_with_gui_progress(["dd", "if=a", "of=b"]) == [
        "dd",
        "if=a",
        "of=b",
    ]


def test_flash_worker_emits_carriage_return_output() -> None:
    from oh_my_ruyi.workers import FlashWorker

    worker = FlashWorker(None, None, {}, {}, set())  # type: ignore[arg-type]
    captured: list[bytes] = []
    worker.process_output.connect(captured.append)

    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, b"1024 bytes\r2048 bytes\ndone")
        os.close(write_fd)
        write_fd = -1
        worker._emit_process_output(read_fd)
    finally:
        os.close(read_fd)
        if write_fd >= 0:
            os.close(write_fd)

    assert b"".join(captured) == b"1024 bytes\r2048 bytes\ndone"
