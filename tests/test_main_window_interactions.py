from __future__ import annotations
from oh_my_ruyi.state_machine import ProvisionStateMachine

import os
import platform
import threading
import time
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QProcess, QTimer, Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication
from ruyi.config import GlobalConfig
from ruyi.utils.global_mode import EnvGlobalModeProvider

from oh_my_ruyi import first_use, host_storage, ruyi_facade, version_manager, workers
from oh_my_ruyi import main_window
from oh_my_ruyi.main_window import (
    ProvisionMainWindow,
    _VersionDownloadDialog,
)
from oh_my_ruyi.qt_logger import LogEmitter, QtRuyiLogger
from oh_my_ruyi.workers import FlashWorker


def _elf_header(machine: int, *, elf_class: int = 2) -> bytes:
    header = bytearray(64)
    header[:7] = b"\x7fELF" + bytes((elf_class, 1, 1))
    header[18:20] = machine.to_bytes(2, "little")
    return bytes(header)


def _macho_header(cpu_type: int) -> bytes:
    header = bytearray(64)
    header[:4] = b"\xcf\xfa\xed\xfe"
    header[4:8] = cpu_type.to_bytes(4, "little")
    return bytes(header)


def _binary_header_for_arch(architecture: str) -> bytes:
    normalized = version_manager.normalize_architecture(architecture) or architecture
    if normalized == "x86_64":
        return (
            _macho_header(0x01000007)
            if platform.system() == "Darwin"
            else _elf_header(62)
        )
    if normalized == "aarch64":
        return (
            _macho_header(0x0100000C)
            if platform.system() == "Darwin"
            else _elf_header(183)
        )
    if normalized == "riscv64":
        return _elf_header(243)
    return b"standalone ruyi"


def _host_binary_header() -> bytes:
    return _binary_header_for_arch(version_manager.host_architecture())


def _download_architecture_for_host() -> str:
    host = version_manager.host_architecture()
    if platform.system() == "Darwin" and host == "aarch64":
        return "macos-arm64"
    if host == "x86_64":
        return "amd64"
    return host


def _host_download_url(version: str) -> str:
    return (
        f"https://downloads.example/ruyi-{version}.{_download_architecture_for_host()}"
    )


@pytest.fixture
def window(qtbot, tmp_path) -> ProvisionMainWindow:
    _app = QApplication.instance() or QApplication([])
    gm = EnvGlobalModeProvider({}, [])
    emitter = LogEmitter()
    logger = QtRuyiLogger(gm, emitter)
    config = GlobalConfig(gm, logger)
    telemetry_installation = tmp_path / "state" / "installation.json"
    telemetry_installation.parent.mkdir(parents=True)
    telemetry_installation.write_text("{}")
    repo_config = tmp_path / "config" / "ruyi" / "config.toml"
    repo_config.parent.mkdir(parents=True)
    repo_config.write_text("[repo]\ndisabled = true\n")
    result = ProvisionMainWindow(
        config,
        logger,
        emitter,
        auto_start=False,
        versions_directory=tmp_path / "versions",
        activation_link=tmp_path / "bin" / "ruyi",
        telemetry_installation=telemetry_installation,
        system_ruyi_config=tmp_path / "etc" / "ruyi" / "config.toml",
        repo_config_path=repo_config,
    )
    qtbot.addWidget(result)
    return result


def test_feature_tabs_are_in_required_order(window: ProvisionMainWindow) -> None:
    assert [window._tabs.tabText(i) for i in range(window._tabs.count())] == [
        "Version Management",
        "Repo Management",
        "Device Provision",
        "About",
    ]
    assert window._tabs.currentIndex() == 0
    assert window._tabs.widget(2) is window._provision_tab
    assert window._tabs.widget(3) is window._about_tab
    assert window._repo_manager_tab.layout() is not None
    assert window._repo_manager_tab.preset_table.rowCount() == 1
    assert window._repo_manager_tab.configured_table.rowCount() == 1
    assert window._stack.widget(
        ProvisionStateMachine.STEP_WELCOME
    ).accessibleName() == ("RuyiSDK Device Provisioning")


def _first_use_window(qtbot, monkeypatch, tmp_path) -> ProvisionMainWindow:
    _app = QApplication.instance() or QApplication([])
    gm = EnvGlobalModeProvider({}, [])
    emitter = LogEmitter()
    logger = QtRuyiLogger(gm, emitter)
    config = GlobalConfig(gm, logger)
    data_dir = tmp_path / "share" / "oh-my-ruyi"
    repo_config = tmp_path / "config" / "ruyi" / "config.toml"
    repo_config.parent.mkdir(parents=True)
    monkeypatch.setattr(
        first_use,
        "should_offer_first_use_setup",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(ProvisionMainWindow, "_refresh_pm_catalog", lambda _self: None)
    result = ProvisionMainWindow(
        config,
        logger,
        emitter,
        versions_directory=data_dir / "versions",
        managed_data_directory=data_dir,
        activation_link=tmp_path / "bin" / "ruyi",
        telemetry_installation=tmp_path / "state" / "installation.json",
        system_ruyi_config=tmp_path / "etc" / "ruyi" / "config.toml",
        repo_config_path=repo_config,
    )
    qtbot.addWidget(result)
    qtbot.waitUntil(lambda: result._first_use_dialog is not None)
    return result


def test_first_use_catalog_prefers_latest_stable_release(
    qtbot, monkeypatch, tmp_path
) -> None:
    window = _first_use_window(qtbot, monkeypatch, tmp_path)

    window._on_pm_catalog_ready(
        version_manager.ReleaseCatalog(
            (
                version_manager.RuyiRelease(
                    "0.52.0-alpha.1",
                    "testing",
                    "2026-07-14",
                    ("https://example.test/testing",),
                    "x86_64",
                ),
                version_manager.RuyiRelease(
                    "0.50.0",
                    "stable",
                    "2026-06-23",
                    ("https://example.test/stable-old",),
                    "x86_64",
                ),
                version_manager.RuyiRelease(
                    "0.51.0",
                    "stable",
                    "2026-08-01",
                    ("https://example.test/stable-new",),
                    "x86_64",
                ),
            ),
            version_manager.PRIMARY_RELEASES_URL,
        )
    )

    dialog = window._first_use_dialog
    assert dialog is not None
    assert window._first_use_release is not None
    assert window._first_use_release.version == "0.51.0"
    assert dialog.current_label.text() == "Current step: Download a compatible ruyi"
    assert dialog.remaining_label.text() == "Remaining steps: 3"
    assert dialog.action_button.text() == "Download and activate"
    assert dialog.skip_button.text() == "Skip download"


def test_first_use_uses_testing_release_when_stable_is_unavailable(
    qtbot, monkeypatch, tmp_path
) -> None:
    window = _first_use_window(qtbot, monkeypatch, tmp_path)

    window._on_pm_catalog_ready(
        version_manager.ReleaseCatalog(
            (
                version_manager.RuyiRelease(
                    "0.52.0-alpha.1",
                    "testing",
                    "2026-07-14",
                    ("https://example.test/testing",),
                    "macos-arm64",
                ),
            ),
            version_manager.PRIMARY_RELEASES_URL,
        )
    )

    assert window._first_use_release is not None
    assert window._first_use_release.version == "0.52.0-alpha.1"
    assert window._first_use_dialog is not None
    assert "testing" in window._first_use_dialog.status.text()
    assert window._first_use_dialog.action_button.text() == "Download and activate"


def test_first_use_catalog_failure_can_continue_without_download(
    qtbot,
    monkeypatch,
    tmp_path,
) -> None:
    window = _first_use_window(qtbot, monkeypatch, tmp_path)
    failures: list[tuple[str, str]] = []
    monkeypatch.setattr(
        main_window.QMessageBox,
        "critical",
        lambda _parent, title, message: failures.append((title, message)),
    )
    window._pm_operation = "refresh"

    window._on_pm_worker_failed("catalog unavailable")

    dialog = window._first_use_dialog
    assert dialog is not None
    assert dialog.action_button.text() == "Retry"
    assert dialog.skip_button.text() == "Continue without download"
    assert failures == []


def test_first_use_skip_switches_to_repo_management(
    qtbot, monkeypatch, tmp_path
) -> None:
    window = _first_use_window(qtbot, monkeypatch, tmp_path)
    attempts: list[None] = []
    monkeypatch.setattr(
        window._repo_manager_tab,
        "choose_default_source_and_update",
        lambda: attempts.append(None) or False,
    )

    window._skip_first_use_download()

    qtbot.waitUntil(lambda: bool(attempts))
    dialog = window._first_use_dialog
    assert dialog is not None
    assert window._tabs.currentWidget() is window._repo_manager_tab
    assert dialog.step == 2
    assert (
        dialog.current_label.text()
        == "Current step: Choose and update the RuyiSDK mirror"
    )
    assert dialog.action_button.text() == "Choose mirror"


def test_first_use_downloads_activates_then_opens_repo_management(
    qtbot,
    monkeypatch,
    tmp_path,
) -> None:
    window = _first_use_window(qtbot, monkeypatch, tmp_path)
    release = version_manager.RuyiRelease(
        "0.51.0",
        "stable",
        "2026-08-01",
        ("https://example.test/stable",),
        version_manager.host_architecture(),
    )
    window._first_use_release = release
    source_dialog_attempted: list[None] = []

    def download_release(release, directory, **_kwargs):
        path = directory / f"ruyi-{release.version}"
        directory.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"standalone ruyi")
        path.chmod(0o755)
        return path

    monkeypatch.setattr(version_manager, "download_release", download_release)
    monkeypatch.setattr(
        window._repo_manager_tab,
        "choose_default_source_and_update",
        lambda: source_dialog_attempted.append(None) or False,
    )
    window._pm_activation_link.parent.mkdir(parents=True)

    window._start_first_use_download()

    download_dialog = window._pm_download_dialog
    assert isinstance(download_dialog, _VersionDownloadDialog)
    assert download_dialog.isVisible()
    download_dialog._download_button.click()

    qtbot.waitUntil(
        lambda: window._first_use_operation == "repository",
        timeout=3000,
    )
    qtbot.waitUntil(lambda: bool(source_dialog_attempted), timeout=1000)
    assert window._pm_activation_link.is_symlink()
    assert window._pm_activation_link.resolve() == (
        window._pm_versions_directory / "ruyi-0.51.0"
    )
    assert window._tabs.currentWidget() is window._repo_manager_tab


def test_first_use_exits_and_cancels_repository_update(
    qtbot, monkeypatch, tmp_path
) -> None:
    window = _first_use_window(qtbot, monkeypatch, tmp_path)
    cancelled: list[None] = []
    monkeypatch.setattr(
        window._repo_manager_tab,
        "cancel_current_update",
        lambda: cancelled.append(None),
    )
    window._first_use_operation = "repository"

    dialog = window._first_use_dialog
    assert dialog is not None
    dialog.reject()

    assert not window._first_use_active
    assert cancelled == [None]


def test_first_use_repo_update_opens_about_and_completes_dialog(
    qtbot,
    monkeypatch,
    tmp_path,
) -> None:
    window = _first_use_window(qtbot, monkeypatch, tmp_path)
    window._first_use_operation = "repository"

    window._on_first_use_repo_update_finished("ruyisdk", True, "Updated ruyisdk.")

    dialog = window._first_use_dialog
    assert dialog is not None
    assert window._tabs.currentWidget() is window._about_tab
    assert dialog.step == 3
    assert dialog.remaining_label.text() == "Remaining steps: 0"
    assert dialog.action_button.text() == "Finish"


def test_repo_init_disables_repo_management(
    window: ProvisionMainWindow,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "oh_my_ruyi.worker_manager.WorkerTaskRunner.run_worker",
        lambda self, worker, *args, **kwargs: worker,
    )
    window._repo_manager_tab.preset_table.selectRow(0)
    assert window._repo_manager_tab.add_button.isEnabled()

    window._start_repo_init()

    assert window._worker is not None
    assert window._repo_manager_tab._external_busy
    assert not window._repo_manager_tab.preset_table.isEnabled()

    window._worker = None


def test_disabled_default_repo_stays_on_ready_page(
    qtbot,
    monkeypatch,
    tmp_path,
) -> None:
    _app = QApplication.instance() or QApplication([])
    gm = EnvGlobalModeProvider({}, [])
    emitter = LogEmitter()
    logger = QtRuyiLogger(gm, emitter)
    config = GlobalConfig(gm, logger)
    repo_config = tmp_path / "config" / "ruyi" / "config.toml"
    repo_config.parent.mkdir(parents=True)
    repo_config.write_text("[repo]\ndisabled = true\n")
    monkeypatch.setattr(ProvisionMainWindow, "_refresh_pm_catalog", lambda _self: None)

    window = ProvisionMainWindow(
        config,
        logger,
        emitter,
        versions_directory=tmp_path / "versions",
        activation_link=tmp_path / "bin" / "ruyi",
        telemetry_installation=tmp_path / "installation.json",
        system_ruyi_config=tmp_path / "etc" / "ruyi" / "config.toml",
        repo_config_path=repo_config,
    )
    qtbot.addWidget(window)
    window._tabs.setCurrentWidget(window._provision_tab)

    assert window._worker is None
    assert window._machine.current_step == ProvisionStateMachine.STEP_WELCOME
    assert window._welcome_status.text() == (
        "Enable the ruyisdk repository in Repo Management to load device metadata."
    )
    assert window._welcome_status.property("statusKind") == "warning"


def test_empty_device_repo_uses_detail_view_not_status_label(
    window: ProvisionMainWindow,
    monkeypatch,
) -> None:
    window.state.mr = object()  # type: ignore[assignment]
    monkeypatch.setattr(ruyi_facade, "list_devices", lambda _mr: [])
    monkeypatch.setattr(ruyi_facade, "list_entity_types", lambda _mr: ["package"])

    window._populate_devices()

    assert window._device_status.text() == (
        "No device provisioning data is available. See repository details."
    )
    assert "Available entity types: package" not in window._device_status.text()
    assert "Available entity types: package" in window._device_details.toPlainText()


def test_provision_update_waits_for_device_tab_switch(
    window: ProvisionMainWindow,
    monkeypatch,
) -> None:
    calls: list[None] = []
    monkeypatch.setattr(
        window._repo_manager_tab,
        "start_provision_update",
        lambda: calls.append(None),
    )

    assert calls == []
    window._tabs.setCurrentWidget(window._repo_manager_tab)
    assert calls == []
    window._tabs.setCurrentWidget(window._provision_tab)
    assert len(calls) == 1


def test_version_tables_separate_available_and_downloaded_versions(
    window: ProvisionMainWindow,
    monkeypatch,
) -> None:
    window._pm_versions_directory.mkdir(parents=True)
    binary = window._pm_versions_directory / "ruyi-0.50.0"
    binary.write_bytes(_host_binary_header())
    binary.chmod(0o755)
    window._pm_activation_link.parent.mkdir(parents=True)
    window._pm_activation_link.symlink_to(binary)
    monkeypatch.setenv("PATH", os.fspath(window._pm_activation_link.parent))
    window._pm_catalog_releases = [
        version_manager.RuyiRelease(
            "0.52.0-alpha.20260714",
            "testing",
            "2026-07-14T10:54:29Z",
            ("https://example.test/ruyi",),
            "x86_64",
        ),
        version_manager.RuyiRelease(
            "0.50.0",
            "stable",
            "2026-06-23T13:06:10Z",
            ("https://example.test/stable-ruyi",),
            "x86_64",
        ),
    ]

    window._refresh_pm_versions(select_installed_version="0.50.0")

    assert window._pm_available_table.columnCount() == 4
    assert window._pm_available_table.rowCount() == 2
    assert window._pm_available_table.item(0, 0).text() == "0.52.0-alpha.20260714"
    assert window._pm_available_table.item(1, 1).text() == "stable"
    assert window._pm_installed_table.columnCount() == 5
    assert window._pm_installed_table.rowCount() == 1
    assert window._pm_installed_table.item(0, 0).text() == "0.50.0"
    assert window._pm_installed_table.item(0, 1).text() == "stable"
    assert window._pm_installed_table.item(0, 2).text() == "Activate"
    assert window._pm_installed_table.item(0, 3).text() == "64 B"
    assert window._pm_installed_table.item(0, 4).text() == "Latest"
    assert window._pm_toggle_activation_btn.isEnabled()
    assert not window._pm_delete_btn.isEnabled()
    assert window._pm_toggle_activation_btn.text() == "Deactivate"
    assert "PATH ready" in window._pm_path_status.text()


@pytest.mark.parametrize(
    ("active_version", "api_channel", "api_latest", "api_older"),
    [
        ("0.50.0", "stable", "0.51.0", "0.50.0"),
        ("0.51.0-alpha.1", "testing", "0.52.0-alpha.1", "0.51.0-alpha.1"),
    ],
)
def test_active_version_color_compares_with_matching_api_channel(
    window: ProvisionMainWindow,
    monkeypatch,
    active_version: str,
    api_channel: str,
    api_latest: str,
    api_older: str,
) -> None:
    window._pm_versions_directory.mkdir(parents=True)
    active = window._pm_versions_directory / f"ruyi-{active_version}"
    active.write_bytes(_host_binary_header())
    window._pm_activation_link.parent.mkdir(parents=True)
    window._pm_activation_link.symlink_to(active)
    monkeypatch.setenv("PATH", os.fspath(window._pm_activation_link.parent))
    window._pm_catalog_releases = [
        version_manager.RuyiRelease(
            api_latest,
            api_channel,
            "2026-07-14T10:54:29Z",
            (f"https://api.example/ruyi-{api_latest}.amd64",),
            "x86_64",
        ),
        version_manager.RuyiRelease(
            api_older,
            api_channel,
            "2026-06-23T13:06:10Z",
            (f"https://api.example/ruyi-{api_older}.amd64",),
            "x86_64",
        ),
    ]

    window._refresh_pm_versions(select_installed_version=active_version)

    colors = window._theme_colors()
    row = window._pm_installed_table.currentRow()
    assert row >= 0
    activate_item = window._pm_installed_table.item(row, 2)
    assert activate_item.text() == "Activate"
    assert activate_item.foreground().color().name() == colors["error"]
    latest_row = next(
        row
        for row in range(window._pm_available_table.rowCount())
        if window._pm_available_table.item(row, 0).text() == api_latest
    )
    assert all(
        window._pm_available_table.item(latest_row, column).foreground().color().name()
        == colors["success"]
        for column in range(window._pm_available_table.columnCount())
    )


def test_active_latest_version_row_uses_default_foreground(
    window: ProvisionMainWindow,
    monkeypatch,
) -> None:
    window._pm_versions_directory.mkdir(parents=True)
    latest = window._pm_versions_directory / "ruyi-0.51.0"
    latest.write_bytes(_host_binary_header())
    window._pm_activation_link.parent.mkdir(parents=True)
    window._pm_activation_link.symlink_to(latest)
    monkeypatch.setenv("PATH", os.fspath(window._pm_activation_link.parent))
    window._pm_catalog_releases = [
        version_manager.RuyiRelease(
            "0.51.0",
            "stable",
            "2026-07-14T10:54:29Z",
            ("https://api.example/ruyi-0.51.0.amd64",),
            "x86_64",
        )
    ]

    window._refresh_pm_versions(select_installed_version="0.51.0")

    row = window._pm_installed_table.currentRow()
    assert row >= 0
    assert all(
        window._pm_installed_table.item(row, column).foreground().style()
        == Qt.BrushStyle.NoBrush
        for column in range(window._pm_installed_table.columnCount())
    )


def test_latest_downloaded_version_is_green_only_in_right_table(
    window: ProvisionMainWindow,
    monkeypatch,
) -> None:
    window._pm_versions_directory.mkdir(parents=True)
    active = window._pm_versions_directory / "ruyi-0.50.0"
    latest = window._pm_versions_directory / "ruyi-0.51.0"
    active.write_bytes(_host_binary_header())
    latest.write_bytes(_host_binary_header())
    window._pm_activation_link.parent.mkdir(parents=True)
    window._pm_activation_link.symlink_to(active)
    monkeypatch.setenv("PATH", os.fspath(window._pm_activation_link.parent))
    window._pm_catalog_releases = [
        version_manager.RuyiRelease(
            "0.51.0",
            "stable",
            "2026-07-14T10:54:29Z",
            ("https://api.example/ruyi-0.51.0.amd64",),
            "x86_64",
        )
    ]

    window._refresh_pm_versions(select_installed_version="0.50.0")

    colors = window._theme_colors()
    left_row = next(
        row
        for row in range(window._pm_available_table.rowCount())
        if window._pm_available_table.item(row, 0).text() == "0.51.0"
    )
    right_row = next(
        row
        for row in range(window._pm_installed_table.rowCount())
        if window._pm_installed_table.item(row, 0).text() == "0.51.0"
    )
    assert (
        window._pm_available_table.item(left_row, 0).foreground().color().name()
        != colors["success"]
    )
    assert all(
        window._pm_installed_table.item(right_row, column).foreground().color().name()
        == colors["success"]
        for column in range(window._pm_installed_table.columnCount())
    )


def test_external_system_management_keeps_tables_visible_but_disables_controls(
    qtbot,
    tmp_path,
) -> None:
    _app = QApplication.instance() or QApplication([])
    gm = EnvGlobalModeProvider({}, [])
    emitter = LogEmitter()
    logger = QtRuyiLogger(gm, emitter)
    config = GlobalConfig(gm, logger)
    system_config = tmp_path / "config.toml"
    system_config.write_text("[installation]\nexternally_managed = true\n")
    versions = tmp_path / "versions"
    versions.mkdir()
    (versions / "ruyi-0.50.0").write_bytes(_host_binary_header())
    telemetry_installation = tmp_path / "state" / "installation.json"
    telemetry_installation.parent.mkdir()
    telemetry_installation.write_text("{}")
    window = ProvisionMainWindow(
        config,
        logger,
        emitter,
        auto_start=False,
        versions_directory=versions,
        activation_link=tmp_path / "bin" / "ruyi",
        telemetry_installation=telemetry_installation,
        system_ruyi_config=system_config,
        repo_config_path=tmp_path / "config" / "ruyi" / "config.toml",
    )
    qtbot.addWidget(window)
    window._pm_catalog_releases = [
        version_manager.RuyiRelease(
            "0.50.0",
            "stable",
            "2026-06-23T13:06:10Z",
            ("https://example.test/ruyi",),
            "x86_64",
        )
    ]

    window._refresh_pm_versions()

    assert window._pm_available_table.rowCount() == 1
    assert window._pm_installed_table.rowCount() == 1
    assert not window._pm_available_table.isEnabled()
    assert not window._pm_installed_table.isEnabled()
    assert not window._pm_refresh_btn.isEnabled()
    assert not window._pm_download_btn.isEnabled()
    assert not window._pm_remove_url_btn.isEnabled()
    assert not window._pm_add_url_btn.isEnabled()
    assert not window._pm_local_refresh_btn.isEnabled()
    assert not window._pm_delete_btn.isEnabled()
    assert not window._pm_toggle_activation_btn.isEnabled()
    assert not window._pm_browse_btn.isEnabled()
    assert (
        window._pm_path_status.text()
        == "Version management issue: this system's ruyi package manager is "
        "configured to have its version managed by the system package manager."
    )
    assert window._pm_path_status.property("statusKind") == "error"


def test_loaded_ruyi_config_keeps_external_management_locked(
    qtbot,
    tmp_path,
) -> None:
    _app = QApplication.instance() or QApplication([])
    gm = EnvGlobalModeProvider({}, [])
    emitter = LogEmitter()
    logger = QtRuyiLogger(gm, emitter)
    config = GlobalConfig(gm, logger)
    config.is_installation_externally_managed = True
    system_config = tmp_path / "removed-config.toml"
    window = ProvisionMainWindow(
        config,
        logger,
        emitter,
        auto_start=False,
        versions_directory=tmp_path / "versions",
        activation_link=tmp_path / "bin" / "ruyi",
        telemetry_installation=tmp_path / "installation.json",
        system_ruyi_config=system_config,
        repo_config_path=tmp_path / "config" / "ruyi" / "config.toml",
    )
    qtbot.addWidget(window)

    window._refresh_pm_versions()

    assert not system_config.exists()
    assert window._pm_externally_managed
    assert not window._pm_available_table.isEnabled()
    assert not window._pm_refresh_btn.isEnabled()
    assert "system package manager" in window._pm_path_status.text()


def test_browse_opens_selected_binary_directory(
    window: ProvisionMainWindow,
    monkeypatch,
) -> None:
    window._pm_versions_directory.mkdir(parents=True)
    binary = window._pm_versions_directory / "ruyi-0.50.0"
    binary.write_bytes(_host_binary_header())
    started: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(main_window.platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        main_window.shutil,
        "which",
        lambda program: "/usr/bin/dolphin" if program == "dolphin" else None,
    )
    monkeypatch.setattr(
        main_window.QProcess,
        "startDetached",
        lambda program, arguments: (
            started.append((program, list(arguments))) or (True, 123)
        ),
    )
    window._refresh_pm_versions(select_installed_version="0.50.0")

    assert window._pm_browse_btn.isEnabled()
    window._pm_browse_btn.click()

    assert started == [("dolphin", ["--select", os.fspath(binary)])]


def test_version_statuses_align_and_button_stacks_are_centered(
    window: ProvisionMainWindow,
    qtbot,
) -> None:
    window.resize(1060, 720)
    window.show()
    window._pm_status.setText(
        "Release information loaded from https://api.ruyisdk.cn/releases/latest-pm."
    )
    window._set_status_kind(window._pm_status, "success")
    window._pm_path_status.setText("PATH ready: ruyi resolves to the managed version.")
    window._set_status_kind(window._pm_path_status, "success")

    qtbot.waitUntil(
        lambda: window._pm_status.height() == window._pm_path_status.height(),
        timeout=1000,
    )

    assert window._pm_status.objectName() == window._pm_path_status.objectName()
    assert window._pm_status.property("statusKind") == ""
    assert window._pm_path_status.property("statusKind") == ""
    assert window._pm_status.alignment() & Qt.AlignmentFlag.AlignTop
    assert window._pm_path_status.alignment() & Qt.AlignmentFlag.AlignTop
    assert window._pm_status.height() == window._pm_path_status.height()

    available_buttons_center = (
        window._pm_refresh_btn.geometry().top()
        + window._pm_add_url_btn.geometry().bottom()
    ) // 2
    installed_buttons_center = (
        window._pm_local_refresh_btn.geometry().top()
        + window._pm_browse_btn.geometry().bottom()
    ) // 2
    assert (
        abs(
            available_buttons_center
            - window._pm_available_table.geometry().center().y()
        )
        <= 2
    )
    assert (
        abs(
            installed_buttons_center
            - window._pm_installed_table.geometry().center().y()
        )
        <= 2
    )
    assert window._pm_installed_table.horizontalScrollBar().maximum() == 0


def test_version_statuses_shrink_to_current_text_height(
    window: ProvisionMainWindow,
    qtbot,
) -> None:
    window.resize(1060, 720)
    window.show()
    long_message = " ".join(["A long status message that wraps."] * 12)
    window._pm_status.setText(long_message)
    window._pm_path_status.setText(long_message)
    window._align_pm_status_heights()
    tall_height = window._pm_status.height()

    window._pm_status.setText("API ready.")
    window._pm_path_status.setText("PATH ready.")
    window._align_pm_status_heights()
    qtbot.wait(0)

    expected_height = max(
        label.heightForWidth(label.width())
        for label in (window._pm_status, window._pm_path_status)
    )
    assert window._pm_status.height() == expected_height
    assert window._pm_path_status.height() == expected_height
    assert window._pm_status.height() < tall_height


def test_local_refresh_rescans_versions_directory(
    window: ProvisionMainWindow,
) -> None:
    window._refresh_pm_versions()
    assert window._pm_installed_table.rowCount() == 0

    window._pm_versions_directory.mkdir(parents=True)
    binary = window._pm_versions_directory / "ruyi-0.50.0"
    binary.write_bytes(_host_binary_header())

    window._pm_local_refresh_btn.click()

    assert window._pm_installed_table.rowCount() == 1
    assert window._pm_installed_table.item(0, 0).text() == "0.50.0"


def test_download_dialog_requires_confirmation_even_for_one_url(
    window: ProvisionMainWindow,
    monkeypatch,
    qtbot,
) -> None:
    release = version_manager.RuyiRelease(
        "0.50.0",
        "stable",
        "2026-06-23T13:06:10Z",
        ("https://example.test/ruyi-0.50.0.amd64",),
        "x86_64",
    )
    window._pm_catalog_releases = [release]
    window._refresh_pm_versions(select_available_url=release.download_urls[0])
    started: list[tuple[version_manager.RuyiRelease, str]] = []
    monkeypatch.setattr(
        window,
        "_start_pm_download",
        lambda selected, url, _dialog: started.append((selected, url)),
    )

    window._download_selected_pm_version()

    dialog = window._pm_download_dialog
    assert dialog is not None
    qtbot.waitUntil(dialog.isVisible, timeout=1000)
    assert dialog._url_combo.count() == 1
    assert started == []

    dialog._download_button.click()

    assert started == [(release, release.download_urls[0])]
    assert dialog._progress.isVisible()
    assert dialog._progress.value() == 0
    assert dialog._progress.format() == "Connecting..."
    assert dialog._cancel_button.isEnabled()
    dialog.show_failure("test cleanup")
    dialog.reject()


def test_download_dialog_selects_url_and_tracks_success_or_failure(
    qtbot,
) -> None:
    release = version_manager.RuyiRelease(
        "0.50.0",
        "stable",
        "2026-06-23T13:06:10Z",
        (
            "https://primary.test/ruyi-0.50.0.amd64",
            "https://mirror.test/ruyi-0.50.0.amd64",
        ),
        "x86_64",
    )
    dialog = _VersionDownloadDialog(release)
    qtbot.addWidget(dialog)
    selected: list[str] = []
    dialog.download_requested.connect(selected.append)
    dialog.show()
    dialog._url_combo.setCurrentIndex(1)

    dialog._download_button.click()
    dialog.update_progress(50, 100)

    assert selected == [release.download_urls[1]]
    assert dialog._progress.value() == 50
    assert "50 B / 100 B" in dialog._progress.format()

    dialog.show_failure("mirror unavailable")

    assert dialog.isVisible()
    assert dialog._status.text() == "Download failed. See output below."
    assert "mirror unavailable" not in dialog._status.text()
    assert dialog._status.toolTip() == ""
    assert "mirror unavailable" in dialog._output.toPlainText()
    assert dialog._status.property("statusKind") == "error"
    assert dialog._url_combo.isEnabled()
    assert dialog._download_button.text() == "Retry"

    dialog._download_button.click()
    dialog.complete()

    assert selected == [release.download_urls[1], release.download_urls[1]]
    assert not dialog.isVisible()


def test_download_dialog_retries_another_url_and_closes_after_success(
    window: ProvisionMainWindow,
    monkeypatch,
    qtbot,
) -> None:
    release = version_manager.RuyiRelease(
        "0.50.0",
        "stable",
        "2026-06-23T13:06:10Z",
        (
            "https://primary.test/ruyi-0.50.0.amd64",
            "https://mirror.test/ruyi-0.50.0.amd64",
        ),
        "x86_64",
    )
    window._pm_catalog_releases = [release]
    window._refresh_pm_versions(select_available_url=release.download_urls[0])
    calls: list[str] = []

    def download_release(
        selected_release,
        directory,
        *,
        download_url,
        progress,
        cancelled,
        response_changed,
    ):
        assert selected_release is release
        assert not cancelled()
        response_changed(None)
        calls.append(download_url)
        progress(32, 64)
        if download_url == release.download_urls[0]:
            raise OSError("primary unavailable")
        directory.mkdir(parents=True, exist_ok=True)
        binary = directory / "ruyi-0.50.0"
        binary.write_bytes(_host_binary_header())
        return binary

    monkeypatch.setattr(version_manager, "download_release", download_release)

    window._download_selected_pm_version()
    dialog = window._pm_download_dialog
    assert dialog is not None
    dialog._download_button.click()

    qtbot.waitUntil(lambda: window._pm_worker is None, timeout=2000)
    assert dialog.isVisible()
    assert "primary unavailable" not in dialog._status.text()
    assert "primary unavailable" in dialog._output.toPlainText()
    assert dialog._status.property("statusKind") == "error"
    assert dialog._download_button.text() == "Retry"

    dialog._url_combo.setCurrentIndex(1)
    dialog._download_button.click()

    qtbot.waitUntil(lambda: window._pm_download_dialog is None, timeout=2000)
    assert calls == list(release.download_urls)
    assert dialog._progress.value() == 50
    assert not dialog.isVisible()
    assert window._pm_installed_table.rowCount() == 1


def test_download_dialog_cancel_requests_worker_and_cleans_up(
    window: ProvisionMainWindow,
    monkeypatch,
    qtbot,
) -> None:
    release = version_manager.RuyiRelease(
        "0.50.0",
        "stable",
        "2026-06-23T13:06:10Z",
        ("https://primary.test/ruyi-0.50.0.amd64",),
        "x86_64",
    )
    window._pm_catalog_releases = [release]
    window._refresh_pm_versions(select_available_url=release.download_urls[0])
    entered = threading.Event()

    def download_release(
        _release,
        directory,
        *,
        download_url,
        progress,
        cancelled,
        response_changed,
    ):
        assert download_url == release.download_urls[0]
        directory.mkdir(parents=True, exist_ok=True)
        partial = directory / ".ruyi-0.50.0.test.download"
        partial.write_bytes(b"partial")
        entered.set()
        while not cancelled():
            time.sleep(0.01)
        partial.unlink()
        raise version_manager.DownloadCancelledError("download cancelled")

    monkeypatch.setattr(version_manager, "download_release", download_release)

    window._download_selected_pm_version()
    dialog = window._pm_download_dialog
    assert dialog is not None
    dialog._download_button.click()
    qtbot.waitUntil(entered.is_set, timeout=1000)

    assert dialog._cancel_button.isEnabled()
    dialog._cancel_button.click()

    assert window._pm_download_dialog is None
    assert not dialog.isVisible()
    assert dialog._progress.value() == 0
    assert dialog._progress.format() == "Cancelling..."
    assert window._pm_status.text() == "Cancelling download..."
    qtbot.waitUntil(lambda: window._pm_worker is None, timeout=2000)
    assert window._pm_status.text() == "Download cancelled."
    assert not (window._pm_versions_directory / "ruyi-0.50.0").exists()
    assert not list(window._pm_versions_directory.glob("*.download"))


def test_add_url_is_transient_and_survives_refresh(
    window: ProvisionMainWindow,
    monkeypatch,
) -> None:
    custom_url = _host_download_url("0.53.0-beta.1")
    monkeypatch.setattr(
        main_window.QInputDialog,
        "getText",
        lambda *_args, **_kwargs: (custom_url, True),
    )

    window._add_pm_download_url()

    assert window._pm_available_table.rowCount() == 1
    assert window._pm_available_table.item(0, 0).text() == "0.53.0-beta.1"
    assert window._pm_available_table.item(0, 1).text() == "custom"
    assert (
        window._pm_available_table.item(0, 2).text()
        == version_manager.host_architecture()
    )
    assert window._pm_download_btn.isEnabled()
    assert window._pm_remove_url_btn.isEnabled()

    window._on_pm_catalog_ready(
        version_manager.ReleaseCatalog(
            (
                version_manager.RuyiRelease(
                    "0.50.0",
                    "stable",
                    "2026-06-23T13:06:10Z",
                    ("https://example.test/ruyi",),
                    "x86_64",
                ),
            ),
            version_manager.PRIMARY_RELEASES_URL,
        )
    )

    assert window._pm_available_table.rowCount() == 2
    assert {window._pm_available_table.item(row, 1).text() for row in range(2)} == {
        "custom",
        "stable",
    }


def test_remove_does_not_remove_api_release_with_same_version(
    window: ProvisionMainWindow,
    monkeypatch,
) -> None:
    custom_url = _host_download_url("0.53.0-beta.1")
    monkeypatch.setattr(
        main_window.QInputDialog,
        "getText",
        lambda *_args, **_kwargs: (custom_url, True),
    )
    window._add_pm_download_url()
    window._pm_catalog_releases = [
        version_manager.RuyiRelease(
            "0.53.0-beta.1",
            "testing",
            "2026-07-14T10:54:29Z",
            ("https://api.example/ruyi-0.53.0-beta.1.amd64",),
            "x86_64",
        )
    ]
    window._refresh_pm_versions(select_available_url=custom_url)

    assert window._pm_available_table.rowCount() == 2
    assert window._pm_remove_url_btn.isEnabled()

    for row in range(window._pm_available_table.rowCount()):
        if window._pm_available_table.item(row, 1).text() == "testing":
            window._pm_available_table.selectRow(row)
            break
    assert not window._pm_remove_url_btn.isEnabled()
    window._remove_selected_pm_download_url()
    assert len(window._pm_custom_releases) == 1
    assert window._pm_available_table.rowCount() == 2

    for row in range(window._pm_available_table.rowCount()):
        release = window._pm_available_table.item(row, 0).data(Qt.ItemDataRole.UserRole)
        if release is window._pm_custom_releases[0]:
            window._pm_available_table.selectRow(row)
            break
    assert window._pm_remove_url_btn.isEnabled()
    window._pm_remove_url_btn.click()

    assert window._pm_custom_releases == []
    assert window._pm_available_table.rowCount() == 1
    assert window._pm_available_table.item(0, 1).text() == "testing"
    assert not window._pm_remove_url_btn.isEnabled()


def test_add_url_rejects_incompatible_architecture(
    window: ProvisionMainWindow,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        main_window.QInputDialog,
        "getText",
        lambda *_args, **_kwargs: (
            "https://downloads.example/ruyi-0.53.0-beta.1.riscv64",
            True,
        ),
    )
    warnings: list[tuple[str, str]] = []
    monkeypatch.setattr(
        main_window.QMessageBox,
        "warning",
        lambda _parent, title, message: warnings.append((title, message)),
    )

    window._add_pm_download_url()

    assert window._pm_custom_releases == []
    assert window._pm_available_table.rowCount() == 0
    assert warnings == [
        (
            "Incompatible ruyi architecture",
            f"The URL provides a riscv64 binary, but this computer uses {version_manager.host_architecture()}.",
        )
    ]


@pytest.mark.parametrize("machine", ["x86_64", "aarch64"])
def test_installed_table_hides_incompatible_binary(
    window: ProvisionMainWindow,
    monkeypatch,
    machine: str,
) -> None:
    monkeypatch.setattr(version_manager.platform, "machine", lambda: machine)
    window._pm_versions_directory.mkdir(parents=True)
    compatible = window._pm_versions_directory / "ruyi-0.50.0"
    incompatible = window._pm_versions_directory / "ruyi-0.51.0"
    compatible.write_bytes(_binary_header_for_arch(machine))
    incompatible.write_bytes(_binary_header_for_arch("riscv64"))

    window._refresh_pm_versions()

    assert window._pm_installed_table.rowCount() == 1
    assert window._pm_installed_table.item(0, 0).text() == "0.50.0"
    assert window._pm_installed_table.item(0, 2).text() == ""


def test_latest_note_ignores_transient_custom_releases(
    window: ProvisionMainWindow,
) -> None:
    window._pm_versions_directory.mkdir(parents=True)
    stable = window._pm_versions_directory / "ruyi-0.50.0"
    custom = window._pm_versions_directory / "ruyi-0.53.0-beta.1"
    stable.write_bytes(_host_binary_header())
    custom.write_bytes(_host_binary_header())
    window._pm_catalog_releases = [
        version_manager.RuyiRelease(
            "0.50.0",
            "stable",
            "2026-06-23T13:06:10Z",
            ("https://example.test/ruyi",),
            "x86_64",
        )
    ]
    window._pm_custom_releases = [
        version_manager.RuyiRelease(
            "0.53.0-beta.1",
            "custom",
            "",
            ("https://example.test/custom-ruyi",),
            "x86_64",
        )
    ]

    window._refresh_pm_versions()

    notes = {
        window._pm_installed_table.item(row, 0).text(): window._pm_installed_table.item(
            row, 4
        ).text()
        for row in range(window._pm_installed_table.rowCount())
    }
    assert notes == {"0.53.0-beta.1": "", "0.50.0": "Latest"}


def test_deactivate_requires_selected_active_version(
    window: ProvisionMainWindow,
    monkeypatch,
) -> None:
    window._pm_versions_directory.mkdir(parents=True)
    binary = window._pm_versions_directory / "ruyi-0.50.0"
    binary.write_bytes(_host_binary_header())
    window._pm_activation_link.parent.mkdir(parents=True)
    window._pm_activation_link.symlink_to(binary)
    questions: list[bool] = []
    monkeypatch.setattr(
        main_window.QMessageBox,
        "question",
        lambda *_args, **_kwargs: (
            questions.append(True) or main_window.QMessageBox.StandardButton.Yes
        ),
    )

    window._refresh_pm_versions()

    assert window._pm_installed_table.currentRow() == -1
    assert not window._pm_toggle_activation_btn.isEnabled()
    window._toggle_selected_pm_version_activation()
    assert not questions
    assert window._pm_activation_link.is_symlink()

    window._pm_installed_table.selectRow(0)
    assert window._pm_toggle_activation_btn.isEnabled()
    assert window._pm_toggle_activation_btn.text() == "Deactivate"


def test_activation_confirms_and_backs_up_unmanaged_command(
    window: ProvisionMainWindow,
    monkeypatch,
    qtbot,
) -> None:
    window._pm_versions_directory.mkdir(parents=True)
    binary = window._pm_versions_directory / "ruyi-0.50.0"
    binary.write_bytes(b"new")
    window._pm_activation_link.parent.mkdir(parents=True)
    window._pm_activation_link.write_bytes(b"old")
    monkeypatch.setattr(
        main_window.QMessageBox,
        "question",
        lambda *_args, **_kwargs: main_window.QMessageBox.StandardButton.Yes,
    )
    window._refresh_pm_versions(select_installed_version="0.50.0")

    window._toggle_selected_pm_version_activation()

    qtbot.waitUntil(lambda: window._pm_worker is None, timeout=2000)
    assert window._pm_activation_link.is_symlink()
    assert window._pm_activation_link.resolve() == binary
    assert window._pm_activation_link.with_name("ruyi.bak").read_bytes() == b"old"


def test_downloaded_versions_can_switch_delete_and_deactivate(
    window: ProvisionMainWindow,
    monkeypatch,
    qtbot,
) -> None:
    window._pm_versions_directory.mkdir(parents=True)
    stable = window._pm_versions_directory / "ruyi-0.50.0"
    testing = window._pm_versions_directory / "ruyi-0.52.0-alpha.20260714"
    stable.write_bytes(b"stable")
    testing.write_bytes(b"testing")
    window._pm_activation_link.parent.mkdir(parents=True)
    window._pm_activation_link.symlink_to(stable)
    monkeypatch.setattr(
        main_window.QMessageBox,
        "question",
        lambda *_args, **_kwargs: main_window.QMessageBox.StandardButton.Yes,
    )

    window._refresh_pm_versions(select_installed_version="0.52.0-alpha.20260714")
    assert window._pm_toggle_activation_btn.isEnabled()
    assert window._pm_toggle_activation_btn.text() == "Activate"
    assert window._pm_delete_btn.isEnabled()
    window._toggle_selected_pm_version_activation()
    qtbot.waitUntil(lambda: window._pm_worker is None, timeout=2000)
    assert window._pm_activation_link.resolve() == testing

    window._toggle_selected_pm_version_activation()
    qtbot.waitUntil(lambda: window._pm_worker is None, timeout=2000)
    assert not os.path.lexists(window._pm_activation_link)
    assert stable.exists() and testing.exists()

    window._refresh_pm_versions(select_installed_version="0.50.0")
    window._delete_selected_pm_version()
    qtbot.waitUntil(lambda: window._pm_worker is None, timeout=2000)
    assert not stable.exists()
    assert testing.exists()


def test_path_status_warns_when_another_ruyi_shadows_managed_version(
    window: ProvisionMainWindow,
    monkeypatch,
    tmp_path,
) -> None:
    window._pm_versions_directory.mkdir(parents=True)
    managed = window._pm_versions_directory / "ruyi-0.50.0"
    managed.write_bytes(b"managed")
    managed.chmod(0o755)
    window._pm_activation_link.parent.mkdir(parents=True)
    window._pm_activation_link.symlink_to(managed)
    shadow_bin = tmp_path / "shadow-bin"
    shadow_bin.mkdir()
    shadow = shadow_bin / "ruyi"
    shadow.write_text("#!/bin/sh\n")
    shadow.chmod(0o755)
    monkeypatch.setenv(
        "PATH",
        os.pathsep.join(
            [os.fspath(shadow_bin), os.fspath(window._pm_activation_link.parent)]
        ),
    )

    window._refresh_pm_versions()

    assert "resolves first" in window._pm_path_status.text()
    assert os.fspath(shadow) in window._pm_path_status.text()
    assert os.fspath(managed) not in window._pm_path_status.text()
    assert os.fspath(window._pm_activation_link) in window._pm_path_status.text()
    assert window._pm_path_status.property("statusKind") == "error"


@pytest.mark.parametrize(
    ("answers", "expected"),
    [
        ([main_window.QMessageBox.StandardButton.Yes], "consent"),
        (
            [
                main_window.QMessageBox.StandardButton.No,
                main_window.QMessageBox.StandardButton.Yes,
            ],
            "optout",
        ),
        (
            [
                main_window.QMessageBox.StandardButton.No,
                main_window.QMessageBox.StandardButton.No,
            ],
            "local",
        ),
    ],
)
def test_first_install_telemetry_choices_are_graphical(
    window: ProvisionMainWindow,
    monkeypatch,
    answers: list,
    expected: str,
) -> None:
    remaining = iter(answers)
    monkeypatch.setattr(
        main_window.QMessageBox,
        "question",
        lambda *_args, **_kwargs: next(remaining),
    )

    assert window._ask_for_pm_telemetry_mode() == expected


def test_first_install_runs_selected_mode_and_telemetry_status(
    window: ProvisionMainWindow,
    monkeypatch,
    qtbot,
    tmp_path,
) -> None:
    window._pm_telemetry_installation.unlink()
    log = tmp_path / "telemetry-commands.log"
    binary = window._pm_versions_directory / "ruyi-0.50.0"
    binary.parent.mkdir(parents=True)
    binary.write_text(
        "#!/bin/sh\n"
        f"printf '%s|%s\\n' \"$0\" \"$*\" >> '{log}'\n"
        "read upload\n"
        "if [ \"$upload\" = 'y' ]; then printf 'on\\n'; exit 0; fi\n"
        "read optout\n"
        "if [ \"$optout\" = 'y' ]; then printf 'off\\n'; else printf 'local\\n'; fi\n"
    )
    binary.chmod(0o755)
    window._pm_activation_link.parent.mkdir(parents=True)
    window._pm_activation_link.symlink_to(binary)
    answers = iter(
        [
            main_window.QMessageBox.StandardButton.No,
            main_window.QMessageBox.StandardButton.No,
        ]
    )
    monkeypatch.setattr(
        main_window.QMessageBox,
        "question",
        lambda *_args, **_kwargs: next(answers),
    )

    window._maybe_start_pm_telemetry()

    qtbot.waitUntil(lambda: window._pm_worker is None, timeout=2000)
    assert log.read_text().splitlines() == [
        f"{window._pm_activation_link}|telemetry status"
    ]
    assert window._pm_status.text() == "Telemetry mode: local"


def test_pm_failure_uses_error_dialog_without_output_panel(
    window: ProvisionMainWindow,
    monkeypatch,
) -> None:
    errors: list[tuple[str, str]] = []
    monkeypatch.setattr(
        main_window.QMessageBox,
        "critical",
        lambda _parent, title, message: errors.append((title, message)),
    )
    window._pm_operation = "activate"

    window._on_pm_worker_failed("activation failed")

    assert errors == [("Operation failed", "activation failed")]
    assert window._pm_status.text() == "Operation failed. See the error dialog."
    assert not hasattr(window, "_pm_output")


def _contrast_ratio(foreground: str, background: str) -> float:
    def luminance(color_name: str) -> float:
        color = QColor(color_name)
        channels = [color.redF(), color.greenF(), color.blueF()]
        linear = [
            channel / 12.92
            if channel <= 0.04045
            else ((channel + 0.055) / 1.055) ** 2.4
            for channel in channels
        ]
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

    first = luminance(foreground)
    second = luminance(background)
    lighter, darker = max(first, second), min(first, second)
    return (lighter + 0.05) / (darker + 0.05)


def _test_palette(*, dark: bool) -> QPalette:
    palette = QPalette()
    values = (
        {
            QPalette.ColorRole.Window: "#202124",
            QPalette.ColorRole.WindowText: "#f1f3f4",
            QPalette.ColorRole.Base: "#121212",
            QPalette.ColorRole.Text: "#f1f3f4",
            QPalette.ColorRole.Button: "#303134",
            QPalette.ColorRole.ButtonText: "#f1f3f4",
            QPalette.ColorRole.Mid: "#5f6368",
            QPalette.ColorRole.Highlight: "#8ab4f8",
            QPalette.ColorRole.HighlightedText: "#202124",
        }
        if dark
        else {
            QPalette.ColorRole.Window: "#f8f9fa",
            QPalette.ColorRole.WindowText: "#202124",
            QPalette.ColorRole.Base: "#ffffff",
            QPalette.ColorRole.Text: "#202124",
            QPalette.ColorRole.Button: "#f1f3f4",
            QPalette.ColorRole.ButtonText: "#202124",
            QPalette.ColorRole.Mid: "#bdc1c6",
            QPalette.ColorRole.Highlight: "#1967d2",
            QPalette.ColorRole.HighlightedText: "#ffffff",
        }
    )
    for role, value in values.items():
        palette.setColor(role, QColor(value))
    palette.setColor(
        QPalette.ColorGroup.Disabled,
        QPalette.ColorRole.Text,
        QColor("#9aa0a6" if dark else "#80868b"),
    )
    palette.setColor(
        QPalette.ColorGroup.Disabled,
        QPalette.ColorRole.Button,
        QColor("#3c4043" if dark else "#e8eaed"),
    )
    return palette


def test_sidebar_cannot_skip_forward_steps(window: ProvisionMainWindow) -> None:
    print("BEFORE SET_STEP: current=", window._machine.current_step)
    window._set_step(ProvisionStateMachine.STEP_PACKAGES)
    print("AFTER SET_STEP: current=", window._machine.current_step)

    window._steps.setCurrentRow(ProvisionStateMachine.STEP_REVIEW)
    print("AFTER SETCURRENTROW: current=", window._machine.current_step)

    assert window._machine.current_step == ProvisionStateMachine.STEP_PACKAGES
    assert window._steps.currentRow() == ProvisionStateMachine.STEP_PACKAGES


def test_device_step_is_clickable_after_returning_to_ready(
    window: ProvisionMainWindow,
) -> None:
    window.state.mr = object()
    window._set_step(ProvisionStateMachine.STEP_DEVICE)

    window._go_back()

    device_item = window._steps.item(ProvisionStateMachine.STEP_DEVICE)
    assert device_item.flags() & Qt.ItemFlag.ItemIsEnabled

    window._steps.setCurrentRow(ProvisionStateMachine.STEP_DEVICE)

    assert window._machine.current_step == ProvisionStateMachine.STEP_DEVICE


@pytest.mark.parametrize(
    ("step", "widget_name"),
    [
        (ProvisionStateMachine.STEP_DEVICE, "_device_list"),
        (ProvisionStateMachine.STEP_VARIANT, "_variant_list"),
        (ProvisionStateMachine.STEP_COMBO, "_combo_list"),
        (ProvisionStateMachine.STEP_PACKAGES, "_packages_list"),
        (ProvisionStateMachine.STEP_DOWNLOAD, "_download_log"),
        (ProvisionStateMachine.STEP_FLASH, "_flash_log"),
    ],
)
def test_primary_step_content_fills_page_height(
    window: ProvisionMainWindow,
    qtbot,
    step: int,
    widget_name: str,
) -> None:
    window.resize(1060, 720)
    window._stack.setCurrentIndex(step)
    window.show()
    qtbot.waitUntil(lambda: window._stack.height() > 400, timeout=1000)

    page = window._stack.widget(step)
    widget = getattr(window, widget_name)
    bottom_gap = page.height() - widget.geometry().bottom() - 1

    assert bottom_gap <= page.layout().contentsMargins().bottom() + 1


@pytest.mark.parametrize("dark", [False, True])
def test_theme_uses_application_palette(
    window: ProvisionMainWindow,
    qtbot,
    dark: bool,
) -> None:
    app = QApplication.instance()
    assert app is not None
    original = app.palette()
    try:
        app.setPalette(_test_palette(dark=dark))
        expected_window = "#202124" if dark else "#f8f9fa"
        qtbot.waitUntil(
            lambda: expected_window in window.styleSheet(),
            timeout=1000,
        )
        colors = window._theme_colors()
        stylesheet = window.styleSheet()

        assert colors["window"] in stylesheet
        assert colors["window_text"] in stylesheet
        assert colors["base"] in stylesheet
        assert colors["highlight"] in stylesheet
        assert colors["disabled_text"] in stylesheet
        assert _contrast_ratio(colors["window_text"], colors["window"]) >= 4.5
        assert _contrast_ratio(colors["text"], colors["base"]) >= 4.5
        assert _contrast_ratio(colors["success"], colors["window"]) >= 4.5
        assert _contrast_ratio(colors["warning"], colors["window"]) >= 4.5
        assert _contrast_ratio(colors["error"], colors["window"]) >= 4.5
    finally:
        app.setPalette(original)


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
    monkeypatch.setattr(host_storage, "validation_is_slow", lambda: False)
    window._tabs.setCurrentIndex(2)
    target = tmp_path / "target.img"
    target.touch()
    window.state.prepared = SimpleNamespace(
        requested_host_blkdevs=["disk"],
        needed_cmds=set(),
    )
    window.state.host_blkdev_map = {"disk": str(target)}
    window._set_step(ProvisionStateMachine.STEP_REVIEW)
    monkeypatch.setattr(ruyi_facade, "part_description", lambda _part: "Whole disk")
    monkeypatch.setattr(host_storage, "list_disks", lambda: [])
    monkeypatch.setattr(host_storage, "is_disk_or_child_mounted", lambda _path: True)
    monkeypatch.setattr(host_storage, "device_fingerprint", lambda _path: "target-v1")
    window.state.host_blkdev_fingerprints = {"disk": "target-v1"}

    window._start_flash()

    assert window._machine.current_step == ProvisionStateMachine.STEP_STORAGE
    assert "now mounted" in window._storage_error.text()
    assert window._storage_mount_warnings["disk"].isVisibleTo(window)
    assert not window._storage_mount_confirmations["disk"].isChecked()
    assert window._worker is None


def test_failed_download_start_releases_busy_state(window: ProvisionMainWindow) -> None:
    window._tabs.setCurrentIndex(2)
    window.state.pkg_atoms = ["board-image/test"]
    window._set_step(ProvisionStateMachine.STEP_DOWNLOAD)
    window._download_process = QProcess(window)

    window._on_download_process_error(QProcess.ProcessError.FailedToStart)

    assert window._download_process is None
    assert not window._is_busy()
    assert window._machine.download_recoverable
    assert window._download_recovery_row.isVisibleTo(window)


def test_download_log_replaces_progress_line(window: ProvisionMainWindow) -> None:
    window._download_log.clear()

    window._download_log.feed_bytes(b"Connecting...\nfile 10%\r")
    window._download_log.feed_bytes(b"file 100%\nSaved\n", final=True)

    assert window._download_log.toPlainText().splitlines() == [
        "Connecting...",
        "file 100%",
        "Saved",
    ]


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
    qtbot.waitUntil(lambda: window._fastboot_process is None, timeout=5000)
    assert window._fastboot_ok
    assert window._fastboot_status.text() == "Fastboot device check completed."
    assert "SERIAL" not in window._fastboot_status.text()
    assert "SERIAL" in window._fastboot_log.toPlainText()


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

    qtbot.waitUntil(lambda: window._fastboot_process is None, timeout=5000)
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

    qtbot.waitUntil(lambda: window._fastboot_process is None, timeout=5000)
    assert not window._fastboot_ok
    assert window._fastboot_status.text() == "No fastboot devices found."


def test_fastboot_check_accepts_nonempty_stderr_without_parsing(
    window: ProvisionMainWindow,
    monkeypatch,
    qtbot,
    tmp_path,
) -> None:
    fastboot = tmp_path / "fastboot"
    fastboot.write_text("#!/bin/sh\nprintf 'device output\\n' >&2\nexit 0\n")
    fastboot.chmod(0o755)
    monkeypatch.setattr(main_window, "FASTBOOT_PROGRAM", os.fspath(fastboot))

    window._check_fastboot_devices()

    qtbot.waitUntil(lambda: window._fastboot_process is None, timeout=5000)
    assert window._fastboot_ok
    assert "device output" not in window._fastboot_status.text()
    assert "device output" in window._fastboot_log.toPlainText()


def test_fastboot_check_accepts_device_record_on_stderr(
    window: ProvisionMainWindow,
    monkeypatch,
    qtbot,
    tmp_path,
) -> None:
    fastboot = tmp_path / "fastboot"
    fastboot.write_text("#!/bin/sh\nprintf 'SERIAL\\tfastboot\\n' >&2\nexit 0\n")
    fastboot.chmod(0o755)
    monkeypatch.setattr(main_window, "FASTBOOT_PROGRAM", os.fspath(fastboot))

    window._check_fastboot_devices()

    qtbot.waitUntil(lambda: window._fastboot_process is None, timeout=5000)
    assert window._fastboot_ok
    assert "SERIAL" not in window._fastboot_status.text()
    assert "SERIAL" in window._fastboot_log.toPlainText()


def test_fastboot_check_accepts_dfu_download_output(
    window: ProvisionMainWindow,
    monkeypatch,
    qtbot,
    tmp_path,
) -> None:
    fastboot = tmp_path / "fastboot"
    fastboot.write_text("#!/bin/sh\nprintf 'dfu-device       DFU download\\n'\n")
    fastboot.chmod(0o755)
    monkeypatch.setattr(main_window, "FASTBOOT_PROGRAM", os.fspath(fastboot))

    window._check_fastboot_devices()

    qtbot.waitUntil(lambda: window._fastboot_process is None, timeout=5000)
    assert window._fastboot_ok
    assert "dfu-device       DFU download" not in window._fastboot_status.text()
    assert "dfu-device       DFU download" in window._fastboot_log.toPlainText()


def test_fastboot_check_accepts_nonempty_stdout_without_parsing(
    window: ProvisionMainWindow,
    monkeypatch,
    qtbot,
    tmp_path,
) -> None:
    fastboot = tmp_path / "fastboot"
    fastboot.write_text("#!/bin/sh\nprintf 'unrecognized device format\\n'\n")
    fastboot.chmod(0o755)
    monkeypatch.setattr(main_window, "FASTBOOT_PROGRAM", os.fspath(fastboot))

    window._check_fastboot_devices()

    qtbot.waitUntil(lambda: window._fastboot_process is None, timeout=5000)
    assert window._fastboot_ok
    assert "unrecognized device format" not in window._fastboot_status.text()
    assert "unrecognized device format" in window._fastboot_log.toPlainText()


def test_flash_rejects_replaced_target(
    window: ProvisionMainWindow,
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(host_storage, "validation_is_slow", lambda: False)
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

    assert window._machine.current_step == ProvisionStateMachine.STEP_STORAGE
    assert "has changed" in window._storage_error.text()


def test_review_steps_render_ruyi_rich_markup(
    window: ProvisionMainWindow,
    monkeypatch,
) -> None:
    window.state.prepared = SimpleNamespace(
        requested_host_blkdevs=[], needed_cmds=set()
    )
    monkeypatch.setattr(
        ruyi_facade,
        "compute_pretend_steps",
        lambda *_args: ["write [yellow]/path/to/image.img[/] to [green]/dev/rdisk4[/]"],
    )
    monkeypatch.setattr(ruyi_facade, "missing_cmds", lambda _prepared: [])
    monkeypatch.setattr(
        ruyi_facade,
        "needs_fastboot_confirmation",
        lambda _prepared: False,
    )

    window._populate_review()

    assert window._review_steps.toPlainText().strip() == (
        "* write /path/to/image.img to /dev/rdisk4"
    )
    assert "[yellow]" not in window._review_steps.toPlainText()
    assert "color:" in window._review_steps.toHtml()


def test_flash_confirmation_renders_ruyi_rich_markup(
    window: ProvisionMainWindow,
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_exec(box) -> int:  # noqa: ANN001
        captured["text"] = box.text()
        captured["format"] = box.textFormat()
        captured["default"] = box.standardButton(box.defaultButton())
        return int(main_window.QMessageBox.StandardButton.Yes)

    monkeypatch.setattr(main_window.QMessageBox, "exec", fake_exec)
    response: dict[str, bool] = {}

    window._on_flash_yes_no_requested(
        "Do you want to retry the command with [yellow]sudo[/]?",
        False,
        response,
    )

    assert "[yellow]" not in str(captured["text"])
    assert "sudo" in str(captured["text"])
    assert "color:" in str(captured["text"])
    assert captured["format"] == Qt.TextFormat.RichText
    assert captured["default"] == main_window.QMessageBox.StandardButton.No
    assert response["answer"] is True


def test_successful_flash_advances_to_done_and_can_return_to_flash(
    window: ProvisionMainWindow,
) -> None:
    window.state.pkg_atoms = ["image/pkg"]
    window.state.prepared = SimpleNamespace(
        requested_host_blkdevs=[], needed_cmds=set()
    )
    window._flash_log.setPlainText("fastboot flash complete")
    window._set_step(ProvisionStateMachine.STEP_FLASH)

    window._on_flash_finished(0)

    assert window._machine.current_step == ProvisionStateMachine.STEP_DONE
    assert window.state.flash_ret == 0
    assert window._done_label.text() == (
        "It seems the flashing has finished without errors. Happy hacking!"
    )

    window._go_back()

    assert window._machine.current_step == ProvisionStateMachine.STEP_FLASH
    assert window._flash_status.text() == "Flash complete."
    assert window._flash_log.toPlainText() == "fastboot flash complete"
    assert window._next_btn.isEnabled()
    assert (
        window._steps.item(ProvisionStateMachine.STEP_DONE).flags()
        & Qt.ItemFlag.ItemIsEnabled
    )

    window._go_next()

    assert window._machine.current_step == ProvisionStateMachine.STEP_DONE
    assert (
        window._steps.item(ProvisionStateMachine.STEP_FLASH).flags()
        & Qt.ItemFlag.ItemIsEnabled
    )

    window._steps.setCurrentRow(ProvisionStateMachine.STEP_FLASH)
    assert window._machine.current_step == ProvisionStateMachine.STEP_FLASH

    window._steps.setCurrentRow(ProvisionStateMachine.STEP_DONE)
    assert window._machine.current_step == ProvisionStateMachine.STEP_DONE


def test_failed_flash_stays_on_flash_page(window: ProvisionMainWindow) -> None:
    window.state.prepared = SimpleNamespace(
        requested_host_blkdevs=[], needed_cmds=set()
    )
    window._set_step(ProvisionStateMachine.STEP_FLASH)

    window._on_flash_finished(1)

    assert window._machine.current_step == ProvisionStateMachine.STEP_FLASH
    assert window.state.flash_ret == 1
    assert window._flash_status.text() == "Flash failed (exit code 1)."
    assert window._machine.flash_recoverable
    assert not (
        window._steps.item(ProvisionStateMachine.STEP_DONE).flags()
        & Qt.ItemFlag.ItemIsEnabled
    )


def test_interrupt_flash_requests_worker_cancellation(
    window: ProvisionMainWindow,
    monkeypatch,
) -> None:
    worker = FlashWorker(None, None, {}, {}, set())  # type: ignore[arg-type]
    requests: list[bool] = []
    monkeypatch.setattr(worker, "request_cancel", lambda: requests.append(True))
    window._worker = worker
    window._set_step(ProvisionStateMachine.STEP_FLASH)

    window._interrupt_flash_btn.click()

    assert requests == [True]
    assert window._flash_cancel_requested
    assert window._flash_status.text() == "Interrupting flash..."
    assert not window._interrupt_flash_btn.isEnabled()

    window._worker = None


def test_interrupted_flash_becomes_recoverable(window: ProvisionMainWindow) -> None:
    window._tabs.setCurrentIndex(2)
    window.state.flash_ret = 0
    window._flash_cancel_requested = True
    window._set_step(ProvisionStateMachine.STEP_FLASH)

    window._on_flash_cancelled()

    assert window._machine.current_step == ProvisionStateMachine.STEP_FLASH
    assert window.state.flash_ret is None
    assert window._flash_status.text() == "Flash interrupted."
    assert window._machine.flash_recoverable
    assert window._flash_recovery_row.isVisibleTo(window)


@pytest.mark.skipif(
    platform.system() == "Windows", reason="native Windows flashing is unsupported"
)
@pytest.mark.parametrize("command", ["dd", "fastboot"])
def test_flash_worker_interrupts_active_command(
    monkeypatch,
    tmp_path,
    command: str,
) -> None:
    executable = tmp_path / command
    executable.write_text("#!/bin/sh\n/bin/sleep 30\n")
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", os.fspath(tmp_path))
    target = tmp_path / "target.img"
    target.touch()
    worker = FlashWorker(
        None,
        None,
        {"disk": os.fspath(target)},
        {"disk": "reviewed-device"},
        set(),
    )  # type: ignore[arg-type]
    monkeypatch.setattr(
        host_storage, "device_fingerprint", lambda _path: "reviewed-device"
    )
    monkeypatch.setattr(host_storage, "is_disk_or_child_mounted", lambda _path: False)
    argv = ["dd", f"of={target}"] if command == "dd" else ["fastboot", "flash"]
    result: list[int] = []
    thread = threading.Thread(
        target=lambda: result.append(worker._call_subprocess(argv))
    )

    thread.start()
    deadline = time.monotonic() + 2
    while worker._process is None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert worker._process is not None

    worker.request_cancel()
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert result and result[0] != 0


def test_unflashed_done_back_returns_to_fresh_review(
    window: ProvisionMainWindow,
    monkeypatch,
) -> None:
    window.state.pkg_atoms = ["image/pkg"]
    window.state.prepared = SimpleNamespace(
        requested_host_blkdevs=[], needed_cmds=set()
    )
    window._proceed_cb.setChecked(True)
    window._fastboot_ok = True
    monkeypatch.setattr(
        window,
        "_populate_review",
        lambda: (
            window._proceed_cb.setChecked(False),
            setattr(window, "_fastboot_ok", False),
        ),
    )
    window._set_step(ProvisionStateMachine.STEP_DONE)

    window._go_back()

    assert window._machine.current_step == ProvisionStateMachine.STEP_REVIEW
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
    monkeypatch.setattr(
        host_storage, "device_fingerprint", lambda _path: "reviewed-device"
    )

    with pytest.raises(RuntimeError, match="exactly one"):
        worker._call_subprocess(["dd", "if=image", f"of={target}", f"of={other}"])


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
    assert window._worker is not None
    qtbot.waitUntil(lambda: window._worker is None, timeout=2000)
    assert window._storage_box.isEnabled()
    assert window._storage_inputs["disk"].count() == 1


def test_storage_refresh_discovers_new_disk_and_preserves_selection(
    window: ProvisionMainWindow,
    monkeypatch,
    qtbot,
) -> None:
    window.state.prepared = SimpleNamespace(
        requested_host_blkdevs=["disk"],
        needed_cmds=set(),
    )
    monkeypatch.setattr(ruyi_facade, "part_description", lambda _part: "Whole disk")
    monkeypatch.setattr(host_storage, "validation_is_slow", lambda: False)
    first_disk = host_storage.BlockDeviceChoice(
        path="/dev/disk-old",
        display_name="/dev/disk-old - 16.0 GiB",
        fingerprint="old-disk",
    )
    new_disk = host_storage.BlockDeviceChoice(
        path="/dev/disk-new",
        display_name="/dev/disk-new - 32.0 GiB",
        fingerprint="new-disk",
    )
    discoveries = iter([[first_disk], [first_disk, new_disk]])
    monkeypatch.setattr(host_storage, "list_disks", lambda: next(discoveries))

    window._set_step(ProvisionStateMachine.STEP_STORAGE)
    window._populate_storage()
    target = window._storage_inputs["disk"]
    target.setCurrentIndex(0)

    window._refresh_storage_btn.click()

    qtbot.waitUntil(lambda: window._worker is None, timeout=1000)
    target = window._storage_inputs["disk"]
    assert target.count() == 2
    assert target.findData("/dev/disk-new") >= 0
    assert window._storage_path(target) == "/dev/disk-old"
    assert window._refresh_storage_btn.isEnabled()


def test_storage_controls_have_accessible_labels(
    window: ProvisionMainWindow,
    monkeypatch,
) -> None:
    monkeypatch.setattr(host_storage, "validation_is_slow", lambda: False)
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
