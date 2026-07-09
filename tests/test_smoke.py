"""Import-level smoke tests.

These run without a display by forcing the Qt offscreen platform. They only
check that the package wires up correctly — exercising the full wizard needs
a real ruyi metadata repo and is done manually.
"""

from __future__ import annotations

import os

import pytest

# Force the offscreen Qt platform so the tests don't need a real display.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def test_package_imports_cleanly() -> None:
    import ruyi_device_provision_gui  # noqa: F401
    from ruyi_device_provision_gui import (  # noqa: F401
        app,
        main_window,
        qt_logger,
        ruyi_facade,
        download_child,
        state,
        workers,
    )


def test_qt_logger_emits_signal(qtbot) -> None:
    """A QtRuyiLogger should re-emit every log call via the LogEmitter."""
    from ruyi_device_provision_gui.qt_logger import LogEmitter, QtRuyiLogger
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


def test_facade_exposes_expected_symbols() -> None:
    from ruyi_device_provision_gui import ruyi_facade

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


def test_main_window_constructs(qtbot) -> None:
    """The main window can be constructed with a stub config."""
    from PySide6.QtWidgets import QApplication
    from ruyi.config import GlobalConfig
    from ruyi.utils.global_mode import EnvGlobalModeProvider

    from ruyi_device_provision_gui.qt_logger import LogEmitter, QtRuyiLogger
    from ruyi_device_provision_gui.main_window import ProvisionMainWindow

    app = QApplication.instance() or QApplication([])
    gm = EnvGlobalModeProvider({}, [])
    emitter = LogEmitter()
    logger = QtRuyiLogger(gm, emitter)
    config = GlobalConfig(gm, logger)

    window = ProvisionMainWindow(config, logger, emitter, auto_start=False)
    assert window.windowTitle() == "RuyiSDK Device Provisioning"
    assert window._steps.count() == len(window.STEP_TITLES)
    assert window._stack.count() == len(window.STEP_TITLES)


def test_flash_worker_adds_dd_progress_on_linux(monkeypatch) -> None:
    from ruyi_device_provision_gui import workers
    from ruyi_device_provision_gui.workers import FlashWorker

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
    assert FlashWorker._argv_with_gui_progress(["fastboot", "devices"]) == ["fastboot", "devices"]
    assert FlashWorker._argv_with_gui_progress(["dd", "if=a", "of=b", "status=none"]) == [
        "dd",
        "if=a",
        "of=b",
        "status=none",
    ]


def test_flash_worker_does_not_add_dd_progress_on_macos(monkeypatch) -> None:
    from ruyi_device_provision_gui import workers
    from ruyi_device_provision_gui.workers import FlashWorker

    monkeypatch.setattr(workers.platform, "system", lambda: "Darwin")

    assert FlashWorker._argv_with_gui_progress(["dd", "if=a", "of=b"]) == ["dd", "if=a", "of=b"]


def test_disk_mount_detection_checks_children(monkeypatch) -> None:
    from ruyi_device_provision_gui import ruyi_facade

    monkeypatch.setattr(ruyi_facade, "_disk_child_paths", lambda path: [f"{path}1"])
    monkeypatch.setattr(ruyi_facade, "is_path_mounted_blkdev", lambda path: path == "/dev/sda1")

    assert ruyi_facade.is_disk_or_child_mounted("/dev/sda")


def test_list_disks_keeps_mounted_disks(monkeypatch) -> None:
    from ruyi_device_provision_gui import ruyi_facade

    class FakePath:
        def __init__(self, name: str) -> None:
            self.name = name

        def is_block_device(self) -> bool:
            return True

        def __truediv__(self, _name):
            return self

    fake_paths = [FakePath("sdc"), FakePath("sda"), FakePath("sdb"), FakePath("sdd")]
    monkeypatch.setattr(ruyi_facade.platform, "system", lambda: "Linux")
    monkeypatch.setattr(ruyi_facade.pathlib.Path, "iterdir", lambda _path: fake_paths)
    monkeypatch.setattr(ruyi_facade.pathlib.Path, "is_block_device", lambda _path: True)
    monkeypatch.setattr(ruyi_facade, "_skip_block_device_name", lambda _name: False)
    monkeypatch.setattr(ruyi_facade, "_sysfs_disk_size", lambda _dev: "32.0 GiB")
    monkeypatch.setattr(ruyi_facade, "_read_sysfs_text", lambda _path: "Test Disk")
    monkeypatch.setattr(ruyi_facade, "is_disk_or_child_mounted", lambda path: path in {"/dev/sda", "/dev/sdc"})

    disks = ruyi_facade.list_disks()

    assert [disk.path for disk in disks] == ["/dev/sdb", "/dev/sdd", "/dev/sda", "/dev/sdc"]
    assert [disk.mounted for disk in disks] == [False, False, True, True]
    assert "mounted" in disks[2].display_name


def test_darwin_list_disks_keeps_mounted_disks(monkeypatch) -> None:
    from ruyi_device_provision_gui import ruyi_facade

    def fake_diskutil(*args: str):
        if args == ("list", "-plist"):
            return {"WholeDisks": ["disk2", "disk1"]}
        if args == ("info", "-plist", "disk1"):
            return {"TotalSize": 1024, "MediaName": "Mounted", "VirtualOrPhysical": "Physical"}
        if args == ("info", "-plist", "disk2"):
            return {"TotalSize": 2048, "MediaName": "Unmounted", "VirtualOrPhysical": "Physical"}
        return None

    monkeypatch.setattr(ruyi_facade.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(ruyi_facade, "_darwin_diskutil_plist", fake_diskutil)
    monkeypatch.setattr(ruyi_facade, "_darwin_disk_or_child_mounted", lambda path: path == "/dev/rdisk1")

    disks = ruyi_facade.list_disks()

    assert [disk.path for disk in disks] == ["/dev/rdisk2", "/dev/rdisk1"]
    assert [disk.mounted for disk in disks] == [False, True]


def test_flash_worker_emits_carriage_return_output() -> None:
    from ruyi_device_provision_gui.workers import FlashWorker

    worker = FlashWorker(None, None, {})  # type: ignore[arg-type]
    captured: list[str] = []
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

    assert captured == ["1024 bytes", "2048 bytes", "done"]


def test_worker_run_executes_in_worker_thread(qtbot) -> None:
    from PySide6.QtCore import QThread, Signal

    from ruyi_device_provision_gui.workers import _BaseWorker, run_worker_in_thread

    class ProbeWorker(_BaseWorker):
        finished = Signal(object)  # type: ignore[assignment]

        def run(self) -> None:
            self.finished.emit(QThread.currentThread())

    main_thread = QThread.currentThread()
    worker = ProbeWorker()
    with qtbot.waitSignal(worker.finished, timeout=1000) as blocker:
        thread = run_worker_in_thread(worker)
    try:
        assert blocker.args[0] is thread
        assert blocker.args[0] is not main_thread
    finally:
        thread.quit()
        thread.wait()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
