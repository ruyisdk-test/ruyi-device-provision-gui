from __future__ import annotations

from types import SimpleNamespace

import pytest
from PySide6.QtCore import QProcess
from PySide6.QtWidgets import QApplication
from ruyi.config import GlobalConfig
from ruyi.utils.global_mode import EnvGlobalModeProvider

from ruyi_device_provision_gui import host_storage, ruyi_facade
from ruyi_device_provision_gui.main_window import ProvisionMainWindow
from ruyi_device_provision_gui.qt_logger import LogEmitter, QtRuyiLogger


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
