from __future__ import annotations

import os
import time
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QProcess, QTimer
from PySide6.QtWidgets import QApplication
from ruyi.config import GlobalConfig
from ruyi.utils.global_mode import EnvGlobalModeProvider

from ruyi_device_provision_gui import host_storage, ruyi_facade, workers
from ruyi_device_provision_gui import main_window
from ruyi_device_provision_gui.main_window import ProvisionMainWindow
from ruyi_device_provision_gui.qt_logger import LogEmitter, QtRuyiLogger
from ruyi_device_provision_gui.workers import FlashWorker


@pytest.fixture
def window(qtbot) -> ProvisionMainWindow:
    app = QApplication.instance() or QApplication([])
    gm = EnvGlobalModeProvider({}, [])
    emitter = LogEmitter()
    logger = QtRuyiLogger(gm, emitter)
    config = GlobalConfig(gm, logger)
    result = ProvisionMainWindow(config, logger, emitter, auto_start=False)
    qtbot.addWidget(result)
    return result


def test_sidebar_cannot_skip_forward_steps(window: ProvisionMainWindow) -> None:
    window._set_step(window.STEP_PACKAGES)

    window._steps.setCurrentRow(window.STEP_REVIEW)

    assert window._current_step == window.STEP_PACKAGES
    assert window._steps.currentRow() == window.STEP_PACKAGES


def test_storage_requires_explicit_target(
    window: ProvisionMainWindow,
    monkeypatch,
) -> None:
    window.state.prepared = SimpleNamespace(
        requested_host_blkdevs=["disk"],
        needed_cmds=set(),
    )
    monkeypatch.setattr(ruyi_facade, "part_description", lambda _part: "Whole disk")
    monkeypatch.setattr(
        host_storage,
        "list_disks",
        lambda: [
            host_storage.BlockDeviceChoice(
                path="/dev/test-disk",
                display_name="/dev/test-disk - 32.0 GiB",
            )
        ],
    )

    window._populate_storage()
    target = window._storage_inputs["disk"]

    assert target.currentIndex() == -1
    assert target.currentText() == ""
    assert not window._storage_complete()


def test_flash_revalidates_mount_state(
    window: ProvisionMainWindow,
    monkeypatch,
    tmp_path,
) -> None:
    target = tmp_path / "target.img"
    target.touch()
    window.state.prepared = SimpleNamespace(
        requested_host_blkdevs=["disk"],
        needed_cmds=set(),
    )
    window.state.host_blkdev_map = {"disk": str(target)}
    window._set_step(window.STEP_REVIEW)
    monkeypatch.setattr(ruyi_facade, "part_description", lambda _part: "Whole disk")
    monkeypatch.setattr(host_storage, "list_disks", lambda: [])
    monkeypatch.setattr(host_storage, "is_disk_or_child_mounted", lambda _path: True)
    monkeypatch.setattr(host_storage, "device_fingerprint", lambda _path: "target-v1")
    window.state.host_blkdev_fingerprints = {"disk": "target-v1"}

    window._start_flash()

    assert window._current_step == window.STEP_STORAGE
    assert "now mounted" in window._storage_error.text()
    assert window._storage_mount_warnings["disk"].isVisibleTo(window)
    assert not window._storage_mount_confirmations["disk"].isChecked()
    assert window._thread is None


def test_failed_download_start_releases_busy_state(window: ProvisionMainWindow) -> None:
    window.state.pkg_atoms = ["board-image/test"]
    window._set_step(window.STEP_DOWNLOAD)
    window._download_process = QProcess(window)

    window._on_download_process_error(QProcess.ProcessError.FailedToStart)

    assert window._download_process is None
    assert not window._is_busy()
    assert window._download_recoverable
    assert window._download_recovery_row.isVisibleTo(window)


def test_fastboot_check_runs_without_blocking_ui(
    window: ProvisionMainWindow,
    monkeypatch,
    qtbot,
    tmp_path,
) -> None:
    fastboot = tmp_path / "fastboot"
    fastboot.write_text("#!/bin/sh\nsleep 0.1\nprintf 'SERIAL\\tfastboot\\n'\n")
    fastboot.chmod(0o755)
    monkeypatch.setattr(main_window, "FASTBOOT_PROGRAM", os.fspath(fastboot))
    event_loop_ran: list[bool] = []

    window._check_fastboot_devices()
    QTimer.singleShot(0, lambda: event_loop_ran.append(True))

    qtbot.waitUntil(lambda: bool(event_loop_ran), timeout=500)
    assert window._fastboot_process is not None
    qtbot.waitUntil(lambda: window._fastboot_process is None, timeout=2000)
    assert window._fastboot_ok
    assert "SERIAL" in window._fastboot_status.text()


def test_fastboot_check_reports_missing_command(
    window: ProvisionMainWindow,
    monkeypatch,
    qtbot,
    tmp_path,
) -> None:
    monkeypatch.setattr(
        main_window,
        "FASTBOOT_PROGRAM",
        os.fspath(tmp_path / "missing-fastboot"),
    )

    window._check_fastboot_devices()

    qtbot.waitUntil(lambda: window._fastboot_process is None, timeout=1000)
    assert not window._fastboot_ok
    assert window._fastboot_status.text() == "fastboot command was not found."
    assert window._check_fastboot_btn.isEnabled()


def test_fastboot_check_reports_no_devices(
    window: ProvisionMainWindow,
    monkeypatch,
    qtbot,
    tmp_path,
) -> None:
    fastboot = tmp_path / "fastboot"
    fastboot.write_text("#!/bin/sh\nexit 0\n")
    fastboot.chmod(0o755)
    monkeypatch.setattr(main_window, "FASTBOOT_PROGRAM", os.fspath(fastboot))

    window._check_fastboot_devices()

    qtbot.waitUntil(lambda: window._fastboot_process is None, timeout=1000)
    assert not window._fastboot_ok
    assert window._fastboot_status.text() == "No fastboot devices found."


def test_fastboot_check_ignores_stderr_only_output(
    window: ProvisionMainWindow,
    monkeypatch,
    qtbot,
    tmp_path,
) -> None:
    fastboot = tmp_path / "fastboot"
    fastboot.write_text("#!/bin/sh\nprintf 'warning only\\n' >&2\nexit 0\n")
    fastboot.chmod(0o755)
    monkeypatch.setattr(main_window, "FASTBOOT_PROGRAM", os.fspath(fastboot))

    window._check_fastboot_devices()

    qtbot.waitUntil(lambda: window._fastboot_process is None, timeout=1000)
    assert not window._fastboot_ok
    assert "No fastboot devices found." in window._fastboot_status.text()


def test_fastboot_check_rejects_malformed_stdout(
    window: ProvisionMainWindow,
    monkeypatch,
    qtbot,
    tmp_path,
) -> None:
    fastboot = tmp_path / "fastboot"
    fastboot.write_text("#!/bin/sh\nprintf 'warning fastboot unavailable\\n'\n")
    fastboot.chmod(0o755)
    monkeypatch.setattr(main_window, "FASTBOOT_PROGRAM", os.fspath(fastboot))

    window._check_fastboot_devices()

    qtbot.waitUntil(lambda: window._fastboot_process is None, timeout=1000)
    assert not window._fastboot_ok
    assert "No fastboot devices found." in window._fastboot_status.text()


def test_flash_rejects_replaced_target(
    window: ProvisionMainWindow,
    monkeypatch,
    tmp_path,
) -> None:
    target = tmp_path / "target.img"
    target.touch()
    window.state.prepared = SimpleNamespace(
        requested_host_blkdevs=["disk"],
        needed_cmds=set(),
    )
    window.state.host_blkdev_map = {"disk": str(target)}
    window.state.host_blkdev_fingerprints = {"disk": "old-device"}
    monkeypatch.setattr(host_storage, "device_fingerprint", lambda _path: "new-device")
    monkeypatch.setattr(ruyi_facade, "part_description", lambda _part: "Whole disk")
    monkeypatch.setattr(host_storage, "list_disks", lambda: [])

    window._start_flash()

    assert window._current_step == window.STEP_STORAGE
    assert "has changed" in window._storage_error.text()


def test_done_back_returns_to_fresh_review(window: ProvisionMainWindow, monkeypatch) -> None:
    window.state.pkg_atoms = ["image/pkg"]
    window.state.prepared = SimpleNamespace(requested_host_blkdevs=[], needed_cmds=set())
    window._proceed_cb.setChecked(True)
    window._fastboot_ok = True
    monkeypatch.setattr(
        window,
        "_populate_review",
        lambda: (window._proceed_cb.setChecked(False), setattr(window, "_fastboot_ok", False)),
    )
    window._set_step(window.STEP_DONE)

    window._go_back()

    assert window._current_step == window.STEP_REVIEW
    assert not window._proceed_cb.isChecked()
    assert not window._fastboot_ok


def test_flash_worker_revalidates_dd_target_before_spawn(monkeypatch, tmp_path) -> None:
    target = tmp_path / "target.img"
    target.touch()
    worker = FlashWorker(
        None,
        None,
        {"disk": os.fspath(target)},
        {"disk": "reviewed-device"},
        set(),
    )  # type: ignore[arg-type]
    monkeypatch.setattr(host_storage, "device_fingerprint", lambda _path: "replacement")
    spawned: list[bool] = []
    monkeypatch.setattr(
        workers.subprocess,
        "Popen",
        lambda *_args, **_kwargs: spawned.append(True),
    )

    with pytest.raises(RuntimeError, match="changed after review"):
        worker._call_subprocess(["dd", "if=image", f"of={target}"])

    assert not spawned


def test_flash_worker_rejects_multiple_dd_outputs(monkeypatch, tmp_path) -> None:
    target = tmp_path / "target.img"
    other = tmp_path / "other.img"
    target.touch()
    other.touch()
    worker = FlashWorker(
        None,
        None,
        {"disk": os.fspath(target)},
        {"disk": "reviewed-device"},
        set(),
    )  # type: ignore[arg-type]
    monkeypatch.setattr(host_storage, "device_fingerprint", lambda _path: "reviewed-device")

    with pytest.raises(RuntimeError, match="exactly one"):
        worker._call_subprocess(
            ["dd", "if=image", f"of={target}", f"of={other}"]
        )


def test_slow_storage_discovery_does_not_block_ui(
    window: ProvisionMainWindow,
    monkeypatch,
    qtbot,
) -> None:
    window.state.prepared = SimpleNamespace(
        requested_host_blkdevs=["disk"],
        needed_cmds=set(),
    )
    monkeypatch.setattr(ruyi_facade, "part_description", lambda _part: "Whole disk")
    monkeypatch.setattr(host_storage, "validation_is_slow", lambda: True)

    def slow_discovery():
        time.sleep(0.1)
        return [
            host_storage.BlockDeviceChoice(
                path="/dev/rdisk2",
                display_name="/dev/rdisk2 - 32.0 GiB",
                fingerprint="darwin:disk2",
            )
        ]

    monkeypatch.setattr(host_storage, "list_disks", slow_discovery)
    event_loop_ran: list[bool] = []

    window._populate_storage()
    QTimer.singleShot(0, lambda: event_loop_ran.append(True))

    qtbot.waitUntil(lambda: bool(event_loop_ran), timeout=500)
    assert window._thread is not None
    qtbot.waitUntil(lambda: window._thread is None, timeout=2000)
    assert window._storage_box.isEnabled()
    assert window._storage_inputs["disk"].count() == 1
def test_storage_controls_have_accessible_labels(
    window: ProvisionMainWindow,
    monkeypatch,
) -> None:
    window.state.prepared = SimpleNamespace(
        requested_host_blkdevs=["disk"],
        needed_cmds=set(),
    )
    monkeypatch.setattr(ruyi_facade, "part_description", lambda _part: "Whole disk")
    monkeypatch.setattr(host_storage, "list_disks", lambda: [])

    window._populate_storage()
    target = window._storage_inputs["disk"]
    labels = target.parentWidget().findChildren(type(window._storage_error))
    browse_buttons = target.parentWidget().findChildren(type(window._next_btn))

    assert target.accessibleName() == "Target disk for Whole disk"
    assert any(label.buddy() is target for label in labels)
    assert any(
        button.accessibleName() == "Choose target disk or image file for Whole disk"
        for button in browse_buttons
    )
