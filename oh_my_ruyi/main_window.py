"""Single-window provisioning frontend.

The original CLI is a linear wizard, but a GUI is easier to inspect when the
whole flow is visible at once. This window keeps a step list on the left and a
stable right-hand work area: a summary of choices made so far, followed by the
controls for the current step.
"""

from __future__ import annotations

import codecs
import os
import platform
import signal
import shutil
import sys
from pathlib import Path
from typing import Literal

from PySide6.QtCore import (
    QDir,
    QEvent,
    QProcess,
    QProcessEnvironment,
    QTimer,
    Qt,
    QUrl,
)
from PySide6.QtGui import QDesktopServices, QPalette, QTextCursor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QGroupBox,
    QHeaderView,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QStyle,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from . import host_storage, ruyi_facade, version_manager
from .qt_logger import LogEmitter, QtRuyiLogger
from .state import WizardState
from .workers import (
    FlashWorker,
    RepoInitWorker,
    RepoSyncWorker,
    StorageDiscoveryWorker,
    TelemetrySetupWorker,
    VersionActivationWorker,
    VersionCatalogWorker,
    VersionDeactivationWorker,
    VersionDeleteWorker,
    VersionDownloadWorker,
    run_worker_in_thread,
)

FASTBOOT_PROGRAM = "fastboot"
STORAGE_MOUNTED_ROLE = Qt.ItemDataRole.UserRole.value + 1
STORAGE_FINGERPRINT_ROLE = Qt.ItemDataRole.UserRole.value + 2


class _VersionTableItem(QTableWidgetItem):
    """Sort version cells by their semantic components instead of text."""

    def __init__(self, version: str) -> None:
        super().__init__(version)
        self._sort_key = version_manager.version_sort_key(version)

    def __lt__(self, other: QTableWidgetItem) -> bool:
        if isinstance(other, _VersionTableItem):
            return self._sort_key < other._sort_key
        return super().__lt__(other)


class _StreamingProcessOutput:
    """Turn chunked process bytes into terminal-style line updates."""

    def __init__(self) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._current = ""
        self._has_current = False
        self._pending_carriage_return = False

    def feed(
        self,
        data: bytes,
        *,
        final: bool = False,
    ) -> list[tuple[Literal["line", "progress"], str]]:
        text = self._decoder.decode(data, final=final)
        events: list[tuple[Literal["line", "progress"], str]] = []
        current_changed = False

        for char in text:
            if self._pending_carriage_return:
                self._pending_carriage_return = False
                if char == "\n":
                    events.append(("line", self._current))
                    self._current = ""
                    self._has_current = False
                    current_changed = False
                    continue
                self._current = ""
                self._has_current = False
                current_changed = True

            if char == "\r":
                self._pending_carriage_return = True
            elif char == "\n":
                events.append(("line", self._current))
                self._current = ""
                self._has_current = False
                current_changed = False
            elif char == "\b":
                self._current = self._current[:-1]
                self._has_current = bool(self._current)
                current_changed = True
            else:
                self._current += char
                self._has_current = True
                current_changed = True

        if final:
            self._pending_carriage_return = False
            if self._has_current:
                events.append(("line", self._current))
                self._current = ""
                self._has_current = False
        elif current_changed and self._has_current:
            events.append(("progress", self._current))

        return events


class ProvisionMainWindow(QMainWindow):
    """One-screen GUI for the device provisioning flow."""

    STEP_WELCOME = 0
    STEP_DEVICE = 1
    STEP_VARIANT = 2
    STEP_COMBO = 3
    STEP_VERSIONS = 4
    STEP_PACKAGES = 5
    STEP_DOWNLOAD = 6
    STEP_STORAGE = 7
    STEP_REVIEW = 8
    STEP_FLASH = 9
    STEP_DONE = 10

    STEP_TITLES = [
        "Ready",
        "Device",
        "Variant",
        "Image",
        "Versions",
        "Packages",
        "Download",
        "Storage",
        "Review",
        "Flash",
        "Done",
    ]

    def __init__(
        self,
        config,
        logger: QtRuyiLogger,
        emitter: LogEmitter,
        *,
        auto_start: bool = True,
        versions_directory: Path | None = None,
        activation_link: Path | None = None,
        telemetry_installation: Path | None = None,
        system_ruyi_config: Path | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Ohh My Ruyi")
        self.resize(1060, 720)

        self.state = WizardState(config=config, emitter=emitter)
        self._logger = logger
        self._worker = None
        self._thread = None
        self._download_process: QProcess | None = None
        self._download_output = _StreamingProcessOutput()
        self._download_progress_line_active = False
        self._fastboot_process: QProcess | None = None
        self._fastboot_output = ""
        self._fastboot_error_output = ""
        self._fastboot_timed_out = False
        self._fastboot_timer = QTimer(self)
        self._fastboot_timer.setSingleShot(True)
        self._fastboot_timer.setInterval(10_000)
        self._fastboot_timer.timeout.connect(self._on_fastboot_timeout)
        self._download_cancelled = False
        self._download_recoverable = False
        self._flash_recoverable = False
        self._flash_cancel_requested = False
        self._applying_styles = False
        self._current_step = self.STEP_WELCOME
        self._download_ok = False
        self._versions_visited = False
        self._pm_versions_directory = (
            version_manager.versions_dir()
            if versions_directory is None
            else Path(versions_directory)
        )
        self._pm_activation_link = (
            version_manager.DEFAULT_ACTIVATION_LINK
            if activation_link is None
            else Path(activation_link)
        )
        self._pm_telemetry_installation = (
            version_manager.telemetry_installation_path()
            if telemetry_installation is None
            else Path(telemetry_installation)
        )
        self._pm_system_config = (
            version_manager.DEFAULT_SYSTEM_CONFIG
            if system_ruyi_config is None
            else Path(system_ruyi_config)
        )
        self._pm_config_externally_managed = bool(
            getattr(config, "is_installation_externally_managed", False)
        )
        self._pm_externally_managed = self._pm_config_externally_managed or (
            version_manager.is_ruyi_externally_managed(self._pm_system_config)
        )
        self._pm_catalog_releases: list[version_manager.RuyiRelease] = []
        self._pm_custom_releases: list[version_manager.RuyiRelease] = []
        self._pm_worker = None
        self._pm_thread = None
        self._pm_operation = ""
        self._pm_first_run_check_pending = auto_start

        self._device_choices = {}
        self._variant_choices = {}
        self._combo_choices = {}
        self._version_combos: list[QComboBox] = []
        self._storage_inputs: dict[str, QComboBox] = {}
        self._storage_mount_warnings: dict[str, QLabel] = {}
        self._storage_mount_confirmations: dict[str, QCheckBox] = {}
        self._storage_discovery_paths: dict[str, str] = {}

        self._build_ui()
        self._connect_logs()
        self._set_step(self.STEP_WELCOME)
        if auto_start:
            self._refresh_pm_catalog()
            self._start_repo_init()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt override
        self._stop_fastboot_check()
        if self._download_process is not None:
            ret = QMessageBox.question(
                self,
                "Cancel download?",
                "A download or package installation is still running. Cancel it and close?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if ret != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self._download_cancelled = True
            self._terminate_download_process()
            event.accept()
            return

        if self._thread is not None:
            QMessageBox.warning(
                self,
                "Operation in progress",
                "An operation is still running. Wait for it to finish before closing this window.",
            )
            event.ignore()
            return

        if self._pm_thread is not None:
            QMessageBox.warning(
                self,
                "Operation in progress",
                "A package manager version operation is still running. Wait for it to finish before closing this window.",
            )
            event.ignore()
            return

        event.accept()

    # ------------------------------------------------------------------ UI

    def _build_ui(self) -> None:
        provision_tab = QWidget()
        root_layout = QHBoxLayout(provision_tab)
        root_layout.setContentsMargins(12, 12, 12, 12)
        root_layout.setSpacing(12)

        self._steps = QListWidget()
        self._steps.setFixedWidth(180)
        self._steps.setObjectName("stepList")
        self._steps.setAccessibleName("Provisioning steps")
        self._steps.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        for i, title in enumerate(self.STEP_TITLES):
            item = QListWidgetItem(f"{i + 1}. {title}")
            item.setData(Qt.ItemDataRole.UserRole, i)
            item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            self._steps.addItem(item)
        self._steps.currentRowChanged.connect(self._on_step_clicked)
        root_layout.addWidget(self._steps)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(10)
        root_layout.addWidget(right, 1)

        self._summary = QGroupBox("Selected options")
        summary_layout = QVBoxLayout(self._summary)
        self._summary_device = QLabel("Device: -")
        self._summary_variant = QLabel("Variant: -")
        self._summary_combo = QLabel("Image: -")
        self._summary_packages = QLabel("Packages: -")
        self._summary_storage = QLabel("Storage: -")
        for label in [
            self._summary_device,
            self._summary_variant,
            self._summary_combo,
            self._summary_packages,
            self._summary_storage,
        ]:
            label.setWordWrap(True)
            summary_layout.addWidget(label)
        right_layout.addWidget(self._summary)

        self._stack = QStackedWidget()
        right_layout.addWidget(self._stack, 1)
        self._build_pages()

        button_row = QHBoxLayout()
        button_row.addStretch()
        self._back_btn = QPushButton("Back")
        self._next_btn = QPushButton("Next")
        self._back_btn.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_ArrowBack)
        )
        self._next_btn.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_ArrowForward)
        )
        self._next_btn.setObjectName("primaryButton")
        self._back_btn.clicked.connect(self._go_back)
        self._next_btn.clicked.connect(self._go_next)
        button_row.addWidget(self._back_btn)
        button_row.addWidget(self._next_btn)
        right_layout.addLayout(button_row)

        self._tabs = QTabWidget()
        self._tabs.setObjectName("featureTabs")
        self._version_manager_tab = self._build_version_manager_tab()
        self._repo_manager_tab = QWidget()
        self._provision_tab = provision_tab
        self._config_manager_tab = QWidget()
        self._tabs.addTab(self._version_manager_tab, "Version Management")
        self._tabs.addTab(self._repo_manager_tab, "Repo Management")
        self._tabs.addTab(self._provision_tab, "Device Provision")
        self._tabs.addTab(self._config_manager_tab, "Config Management")
        self.setCentralWidget(self._tabs)
        self._apply_styles()

    def _build_version_manager_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        title = QLabel("<b>Ruyi Package Manager Versions</b>")
        title.setObjectName("pageTitle")
        layout.addWidget(title)
        description = QLabel(
            "Download standalone ruyi releases into your home directory and choose "
            "which version /usr/local/bin/ruyi activates."
        )
        description.setWordWrap(True)
        layout.addWidget(description)

        self._pm_splitter = QSplitter(Qt.Orientation.Horizontal)
        self._pm_splitter.setChildrenCollapsible(False)
        self._pm_splitter.addWidget(self._build_available_versions_panel())
        self._pm_splitter.addWidget(self._build_installed_versions_panel())
        self._pm_splitter.setStretchFactor(0, 1)
        self._pm_splitter.setStretchFactor(1, 1)
        self._pm_splitter.splitterMoved.connect(self._align_pm_status_heights)
        layout.addWidget(self._pm_splitter, 1)

        self._refresh_pm_versions()
        return tab

    def _build_available_versions_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 6, 0)
        layout.addWidget(QLabel("<b>Available downloads</b>"))

        content = QHBoxLayout()
        self._pm_available_table = QTableWidget(0, 4)
        self._configure_pm_table(
            self._pm_available_table,
            ["Version", "Channel", "Architecture", "Released"],
            stretch_column=0,
        )
        self._pm_available_table.setObjectName("availableVersionTable")
        self._pm_available_table.setAccessibleName("Available ruyi versions")
        self._pm_available_table.itemSelectionChanged.connect(self._refresh_pm_buttons)
        content.addWidget(self._pm_available_table, 1)

        buttons = QVBoxLayout()
        buttons.addStretch()
        self._pm_refresh_btn = QPushButton("Refresh")
        self._pm_download_btn = QPushButton("Download")
        self._pm_remove_url_btn = QPushButton("Remove")
        self._pm_add_url_btn = QPushButton("Add URL")
        self._pm_refresh_btn.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_BrowserReload)
        )
        self._pm_refresh_btn.clicked.connect(self._refresh_pm_catalog)
        self._pm_download_btn.clicked.connect(self._download_selected_pm_version)
        self._pm_remove_url_btn.clicked.connect(self._remove_selected_pm_download_url)
        buttons.addWidget(self._pm_refresh_btn)
        buttons.addWidget(self._pm_download_btn)
        buttons.addWidget(self._pm_remove_url_btn)
        buttons.addWidget(self._pm_add_url_btn)
        buttons.addStretch()
        self._pm_add_url_btn.clicked.connect(self._add_pm_download_url)
        content.addLayout(buttons)
        layout.addLayout(content, 1)

        self._pm_status = self._make_pm_status_label(
            "Showing versions already downloaded on this computer."
        )
        layout.addWidget(self._pm_status)
        return panel

    def _build_installed_versions_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(6, 0, 0, 0)
        layout.addWidget(QLabel("<b>Downloaded versions</b>"))

        content = QHBoxLayout()
        self._pm_installed_table = QTableWidget(0, 5)
        self._configure_pm_table(
            self._pm_installed_table,
            ["Version", "Channel", "State", "Size", "Note"],
            stretch_column=0,
        )
        self._pm_installed_table.setObjectName("installedVersionTable")
        self._pm_installed_table.setAccessibleName("Downloaded ruyi versions")
        self._pm_installed_table.itemSelectionChanged.connect(self._refresh_pm_buttons)
        content.addWidget(self._pm_installed_table, 1)

        buttons = QVBoxLayout()
        buttons.addStretch()
        self._pm_local_refresh_btn = QPushButton("Refresh")
        self._pm_delete_btn = QPushButton("Delete")
        self._pm_activate_btn = QPushButton("Activate")
        self._pm_deactivate_btn = QPushButton("Deactivate")
        self._pm_browse_btn = QPushButton("Browse")
        self._pm_local_refresh_btn.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_BrowserReload)
        )
        self._pm_local_refresh_btn.setToolTip(
            "Rescan downloaded ruyi binaries from the file system"
        )
        self._pm_activate_btn.setObjectName("primaryButton")
        self._pm_browse_btn.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_DirOpenIcon)
        )
        self._pm_browse_btn.setToolTip(
            "Open the folder containing the selected downloaded binary"
        )
        self._pm_local_refresh_btn.clicked.connect(self._refresh_pm_local_versions)
        self._pm_delete_btn.clicked.connect(self._delete_selected_pm_version)
        self._pm_activate_btn.clicked.connect(self._activate_selected_pm_version)
        self._pm_deactivate_btn.clicked.connect(self._deactivate_selected_pm_version)
        self._pm_browse_btn.clicked.connect(self._browse_selected_pm_version)
        buttons.addWidget(self._pm_local_refresh_btn)
        buttons.addWidget(self._pm_delete_btn)
        buttons.addWidget(self._pm_activate_btn)
        buttons.addWidget(self._pm_deactivate_btn)
        buttons.addWidget(self._pm_browse_btn)
        buttons.addStretch()
        content.addLayout(buttons)
        layout.addLayout(content, 1)

        self._pm_path_status = self._make_pm_status_label()
        layout.addWidget(self._pm_path_status)
        return panel

    @staticmethod
    def _make_pm_status_label(text: str = "") -> QLabel:
        label = QLabel(text)
        label.setObjectName("versionStatus")
        label.setFrameShape(QFrame.Shape.NoFrame)
        label.setWordWrap(True)
        label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        return label

    @staticmethod
    def _configure_pm_table(
        table: QTableWidget,
        headers: list[str],
        *,
        stretch_column: int,
    ) -> None:
        table.setHorizontalHeaderLabels(headers)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setAlternatingRowColors(True)
        table.setSortingEnabled(True)
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        table.horizontalHeader().setSectionResizeMode(
            stretch_column,
            QHeaderView.ResizeMode.Stretch,
        )

    def _build_pages(self) -> None:
        self._welcome_status = QLabel("Preparing the RuyiSDK metadata repository...")
        self._welcome_status.setWordWrap(True)
        self._add_page(
            "RuyiSDK Device Provisioning",
            [
                QLabel(
                    "This screen walks through the same flow as `ruyi device provision`. "
                    "The left side shows the whole process; the right side keeps your "
                    "choices visible while showing the current step."
                ),
                self._welcome_status,
            ],
        )

        self._device_list = QListWidget()
        self._device_list.setAccessibleName("Devices")
        self._device_list.currentRowChanged.connect(self._refresh_buttons)
        self._device_list.itemDoubleClicked.connect(self._activate_current_step)
        self._device_status = QLabel("")
        self._device_status.setWordWrap(True)
        self._device_status.setProperty("statusKind", "warning")
        self._update_repo_btn = QPushButton("Update metadata")
        self._update_repo_btn.clicked.connect(self._start_repo_sync)
        self._add_page(
            "Pick your device",
            [self._device_status, self._update_repo_btn, self._device_list],
        )

        self._variant_list = QListWidget()
        self._variant_list.setAccessibleName("Device variants")
        self._variant_list.currentRowChanged.connect(self._refresh_buttons)
        self._variant_list.itemDoubleClicked.connect(self._activate_current_step)
        self._add_page("Pick the device variant", [self._variant_list])

        self._combo_list = QListWidget()
        self._combo_list.setAccessibleName("System images")
        self._combo_list.currentRowChanged.connect(self._refresh_buttons)
        self._combo_list.itemDoubleClicked.connect(self._activate_current_step)
        self._add_page("Pick the system image", [self._combo_list])

        self._versions_box = QWidget()
        self._versions_layout = QVBoxLayout(self._versions_box)
        self._versions_layout.setContentsMargins(0, 0, 0, 0)
        self._versions_status = QLabel("")
        self._versions_status.setWordWrap(True)
        self._add_page(
            "Customize package versions",
            [
                QLabel(
                    "By default, ruyi installs the latest version of each package. "
                    "When other versions are available, choose them here."
                ),
                self._versions_status,
                self._versions_box,
            ],
        )

        self._packages_list = QListWidget()
        self._packages_list.setAccessibleName("Packages to install")
        self._packages_list.itemDoubleClicked.connect(self._activate_current_step)
        self._add_page(
            "Confirm packages",
            [
                QLabel("The following packages will be downloaded and installed:"),
                self._packages_list,
            ],
        )

        self._download_status = QLabel("Download has not started.")
        self._download_log = self._make_log_view()
        self._cancel_download_btn = QPushButton("Cancel download")
        self._cancel_download_btn.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_DialogCancelButton)
        )
        self._cancel_download_btn.clicked.connect(self._cancel_download)
        self._resume_download_btn = QPushButton("Resume download")
        self._resume_download_btn.clicked.connect(self._resume_download)
        self._reselect_versions_btn = QPushButton("Reselect versions")
        self._reselect_versions_btn.clicked.connect(self._reselect_versions)
        self._restart_btn = QPushButton("Start over")
        self._restart_btn.clicked.connect(self._restart_flow)
        self._download_recovery_row = QWidget()
        recovery_layout = QHBoxLayout(self._download_recovery_row)
        recovery_layout.setContentsMargins(0, 0, 0, 0)
        recovery_layout.addWidget(self._resume_download_btn)
        recovery_layout.addWidget(self._reselect_versions_btn)
        recovery_layout.addWidget(self._restart_btn)
        self._add_page(
            "Download and install packages",
            [
                self._download_status,
                self._cancel_download_btn,
                self._download_recovery_row,
                self._download_log,
            ],
        )

        self._storage_box = QWidget()
        self._storage_layout = QVBoxLayout(self._storage_box)
        self._storage_layout.setContentsMargins(0, 0, 0, 0)
        self._storage_error = QLabel("")
        self._storage_error.setWordWrap(True)
        self._storage_error.setProperty("statusKind", "warning")
        self._refresh_storage_btn = QPushButton("Refresh disks")
        self._refresh_storage_btn.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_BrowserReload)
        )
        self._refresh_storage_btn.setToolTip("Scan for newly connected storage devices")
        self._refresh_storage_btn.clicked.connect(self._refresh_storage_disks)
        self._add_page(
            "Provide storage paths",
            [
                QLabel(host_storage.storage_platform_hint()),
                self._refresh_storage_btn,
                self._storage_box,
                self._storage_error,
            ],
        )

        self._review_steps = QPlainTextEdit()
        self._review_steps.setReadOnly(True)
        self._review_missing = QLabel("")
        self._review_missing.setWordWrap(True)
        self._review_missing.setProperty("statusKind", "error")
        self._fastboot_ok = False
        self._fastboot_status = QLabel("")
        self._fastboot_status.setWordWrap(True)
        self._check_fastboot_btn = QPushButton("Check fastboot devices")
        self._check_fastboot_btn.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_BrowserReload)
        )
        self._check_fastboot_btn.clicked.connect(self._check_fastboot_devices)
        self._proceed_cb = QCheckBox("Proceed with flashing.")
        self._proceed_cb.toggled.connect(self._refresh_buttons)
        self._add_page(
            "Review flashing actions",
            [
                self._review_steps,
                self._review_missing,
                self._fastboot_status,
                self._check_fastboot_btn,
                self._proceed_cb,
            ],
        )

        self._flash_status = QLabel("Flash has not started.")
        self._interrupt_flash_btn = QPushButton("Interrupt flash")
        self._interrupt_flash_btn.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_BrowserStop)
        )
        self._interrupt_flash_btn.clicked.connect(self._interrupt_flash)
        self._retry_flash_btn = QPushButton("Retry flash")
        self._retry_flash_btn.clicked.connect(self._retry_flash)
        self._review_flash_btn = QPushButton("Review settings")
        self._review_flash_btn.clicked.connect(self._review_flash_settings)
        self._restart_flash_btn = QPushButton("Start over")
        self._restart_flash_btn.clicked.connect(self._restart_flow)
        self._flash_recovery_row = QWidget()
        flash_recovery_layout = QHBoxLayout(self._flash_recovery_row)
        flash_recovery_layout.setContentsMargins(0, 0, 0, 0)
        flash_recovery_layout.addWidget(self._retry_flash_btn)
        flash_recovery_layout.addWidget(self._review_flash_btn)
        flash_recovery_layout.addWidget(self._restart_flash_btn)
        self._flash_log = self._make_log_view()
        self._add_page(
            "Flash device",
            [
                self._flash_status,
                self._interrupt_flash_btn,
                self._flash_recovery_row,
                self._flash_log,
            ],
        )

        self._done_label = QLabel("")
        self._done_label.setWordWrap(True)
        self._postinst_label = QLabel("")
        self._postinst_label.setWordWrap(True)
        self._postinst_label.setFrameShape(QFrame.Shape.Box)
        self._postinst_label.setObjectName("postInstallMessage")
        self._add_page("Done", [self._done_label, self._postinst_label])

    def _add_page(self, title: str, widgets: list[QWidget]) -> None:
        page = QWidget()
        layout = QVBoxLayout(page)
        title_label = QLabel(f"<b>{title}</b>")
        title_label.setObjectName("pageTitle")
        title_label.setWordWrap(True)
        page.setAccessibleName(title)
        layout.addWidget(title_label)
        has_expanding_widget = False
        for widget in widgets:
            if isinstance(widget, QLabel):
                widget.setWordWrap(True)
            expands_vertically = widget.sizePolicy().verticalPolicy() in {
                QSizePolicy.Policy.Expanding,
                QSizePolicy.Policy.MinimumExpanding,
            }
            layout.addWidget(widget, 1 if expands_vertically else 0)
            has_expanding_widget = has_expanding_widget or expands_vertically
        if not has_expanding_widget:
            layout.addStretch()
        self._stack.addWidget(page)

    def _make_log_view(self) -> QPlainTextEdit:
        view = QPlainTextEdit()
        view.setReadOnly(True)
        view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        font = view.font()
        font.setFamily("Monospace")
        view.setFont(font)
        return view

    def _apply_styles(self) -> None:
        if self._applying_styles:
            return
        self._applying_styles = True
        colors = self._theme_colors()
        try:
            self.setStyleSheet(
                f"""
            QMainWindow {{ background: {colors["window"]}; color: {colors["window_text"]}; }}
            QWidget {{ color: {colors["window_text"]}; }}
            QListWidget#stepList {{
                background: {colors["base"]};
                color: {colors["text"]};
                border: 1px solid {colors["border"]};
                border-radius: 6px;
                padding: 4px;
            }}
            QListWidget#stepList::item {{ min-height: 34px; padding: 3px 7px; }}
            QListWidget#stepList::item:selected {{
                background: {colors["highlight"]};
                color: {colors["highlighted_text"]};
            }}
            QListWidget#stepList::item:disabled {{ color: {colors["disabled_text"]}; }}
            QGroupBox {{
                background: {colors["base"]};
                color: {colors["text"]};
                border: 1px solid {colors["border"]};
                border-radius: 6px;
                margin-top: 9px;
                padding: 8px;
            }}
            QGroupBox::title {{ subcontrol-origin: margin; left: 8px; padding: 0 4px; }}
            QGroupBox QLabel {{ color: {colors["text"]}; }}
            QLabel#pageTitle {{ font-size: 17px; color: {colors["window_text"]}; }}
            QLabel#postInstallMessage {{
                padding: 8px;
                background: {colors["base"]};
                color: {colors["text"]};
                border: 1px solid {colors["border"]};
            }}
            QLabel[statusKind="success"] {{ color: {colors["success"]}; font-weight: 600; }}
            QLabel[statusKind="warning"] {{ color: {colors["warning"]}; font-weight: 600; }}
            QLabel[statusKind="error"] {{ color: {colors["error"]}; font-weight: 600; }}
            QPushButton {{
                min-height: 30px;
                padding: 2px 10px;
                background: {colors["button"]};
                color: {colors["button_text"]};
                border: 1px solid {colors["border"]};
                border-radius: 4px;
            }}
            QPushButton#primaryButton {{
                background: {colors["highlight"]};
                color: {colors["highlighted_text"]};
                border-color: {colors["highlight"]};
            }}
            QPushButton:disabled {{
                background: {colors["disabled_button"]};
                color: {colors["disabled_text"]};
            }}
            QPushButton#primaryButton:disabled {{
                background: {colors["disabled_button"]};
                color: {colors["disabled_text"]};
                border-color: {colors["border"]};
            }}
            QLineEdit, QComboBox, QListWidget, QTableWidget, QPlainTextEdit {{
                background: {colors["base"]};
                color: {colors["text"]};
                selection-background-color: {colors["highlight"]};
                selection-color: {colors["highlighted_text"]};
                border: 1px solid {colors["border"]};
            }}
            QLineEdit:disabled, QComboBox:disabled, QListWidget:disabled,
            QTableWidget:disabled,
            QPlainTextEdit:disabled, QCheckBox:disabled {{
                background: {colors["disabled_button"]};
                color: {colors["disabled_text"]};
            }}
            QLabel#versionStatus {{
                padding: 0;
                background: transparent;
                color: {colors["window_text"]};
                border: none;
                font-weight: normal;
            }}
            QLabel#versionStatus[statusKind="error"] {{ color: {colors["error"]}; font-weight: normal; }}
            """
            )
        finally:
            self._applying_styles = False

    def _theme_colors(self) -> dict[str, str]:
        app = QApplication.instance()
        palette = app.palette() if app is not None else self.palette()

        def color(role: QPalette.ColorRole) -> str:
            return palette.color(role).name()

        is_dark = palette.color(QPalette.ColorRole.Window).lightness() < 128
        return {
            "window": color(QPalette.ColorRole.Window),
            "window_text": color(QPalette.ColorRole.WindowText),
            "base": color(QPalette.ColorRole.Base),
            "text": color(QPalette.ColorRole.Text),
            "button": color(QPalette.ColorRole.Button),
            "button_text": color(QPalette.ColorRole.ButtonText),
            "border": color(QPalette.ColorRole.Mid),
            "highlight": color(QPalette.ColorRole.Highlight),
            "highlighted_text": color(QPalette.ColorRole.HighlightedText),
            "disabled_button": palette.color(
                QPalette.ColorGroup.Disabled,
                QPalette.ColorRole.Button,
            ).name(),
            "disabled_text": palette.color(
                QPalette.ColorGroup.Disabled,
                QPalette.ColorRole.Text,
            ).name(),
            "success": "#7ee787" if is_dark else "#1a7f37",
            "warning": "#f2cc60" if is_dark else "#9a6700",
            "error": "#ff7b72" if is_dark else "#cf222e",
        }

    def changeEvent(self, event) -> None:  # noqa: N802 - Qt override
        super().changeEvent(event)
        if event.type() in {
            QEvent.Type.ApplicationPaletteChange,
            QEvent.Type.PaletteChange,
        } and hasattr(self, "_steps"):
            self._apply_styles()

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)
        if hasattr(self, "_pm_status") and hasattr(self, "_pm_path_status"):
            QTimer.singleShot(0, self._align_pm_status_heights)

    def _set_status_kind(self, label: QLabel, kind: str | None) -> None:
        if label.objectName() == "versionStatus":
            kind = "error" if kind in {"warning", "error"} else None
        label.setProperty("statusKind", kind or "")
        label.style().unpolish(label)
        label.style().polish(label)
        label.update()
        if label in {
            getattr(self, "_pm_status", None),
            getattr(self, "_pm_path_status", None),
        }:
            QTimer.singleShot(0, self._align_pm_status_heights)

    # -------------------------------------------------------------- actions

    def _refresh_pm_catalog(self) -> None:
        if self._pm_thread is not None:
            return
        self._pm_operation = "refresh"
        self._pm_status.setText("Checking the latest ruyi releases...")
        self._set_status_kind(self._pm_status, None)
        self._pm_worker = VersionCatalogWorker()
        self._pm_worker.finished.connect(self._on_pm_catalog_ready)
        self._pm_worker.failed.connect(self._on_pm_worker_failed)
        self._pm_thread = run_worker_in_thread(self._pm_worker)
        self._refresh_pm_buttons()

    def _download_selected_pm_version(self) -> None:
        release = self._selected_pm_release()
        if release is None or self._pm_thread is not None:
            return
        self._pm_operation = "download"
        self._pm_status.setText(f"Downloading ruyi {release.version}...")
        self._set_status_kind(self._pm_status, None)
        self._pm_worker = VersionDownloadWorker(
            release,
            self._pm_versions_directory,
        )
        self._pm_worker.finished.connect(self._on_pm_download_finished)
        self._pm_worker.failed.connect(self._on_pm_worker_failed)
        self._pm_thread = run_worker_in_thread(self._pm_worker)
        self._refresh_pm_buttons()

    def _refresh_pm_local_versions(self) -> None:
        if self._pm_thread is not None:
            return
        self._refresh_pm_versions()

    def _remove_selected_pm_download_url(self) -> None:
        if self._pm_thread is not None or self._pm_externally_managed:
            return
        release = self._selected_pm_release()
        custom_release = next(
            (item for item in self._pm_custom_releases if item is release),
            None,
        )
        if custom_release is None:
            return
        self._pm_custom_releases.remove(custom_release)
        self._pm_status.setText(
            f"Removed transient download URL for ruyi {custom_release.version}."
        )
        self._set_status_kind(self._pm_status, "success")
        self._refresh_pm_versions()

    def _add_pm_download_url(self) -> None:
        if self._pm_thread is not None:
            return
        url, ok = QInputDialog.getText(
            self,
            "Add ruyi download URL",
            "URL ending in ruyi-<semver version>.<arch>:",
        )
        if not ok or not url.strip():
            return
        try:
            release = version_manager.release_from_url(url)
        except version_manager.VersionManagerError as exc:
            QMessageBox.warning(self, "Invalid ruyi URL", str(exc))
            return
        if not version_manager.architecture_is_compatible(release.architecture):
            QMessageBox.warning(
                self,
                "Incompatible ruyi architecture",
                f"The URL provides a {release.architecture} binary, but this "
                f"computer uses {version_manager.host_architecture()}.",
            )
            return
        all_releases = [*self._pm_catalog_releases, *self._pm_custom_releases]
        if any(
            item.download_urls[0] == release.download_urls[0] for item in all_releases
        ):
            self._pm_status.setText("That download URL is already in the table.")
            self._set_status_kind(self._pm_status, "warning")
        else:
            self._pm_custom_releases.append(release)
            self._pm_status.setText(
                f"Added transient download URL for ruyi {release.version}."
            )
            self._set_status_kind(self._pm_status, "success")
        self._refresh_pm_versions(select_available_url=release.download_urls[0])

    def _activate_selected_pm_version(self) -> None:
        installed = self._selected_pm_installed_version()
        if installed is None or self._pm_thread is not None:
            return
        binary = installed.path

        state = version_manager.read_activation_state(
            self._pm_activation_link,
            self._pm_versions_directory,
        )
        backup_unmanaged = state.exists and not state.managed
        if backup_unmanaged:
            existing = (
                f"a symbolic link to {state.target}"
                if state.is_symlink
                else "an existing file"
            )
            answer = QMessageBox.question(
                self,
                "Replace existing ruyi command?",
                f"{self._pm_activation_link} is {existing} and is not managed by "
                "Oh My Ruyi.\n\nIf you continue, it will be preserved as a .bak "
                "backup before the selected version is activated.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return

        self._pm_operation = "activate"
        self._pm_status.setText(f"Activating ruyi {installed.version}...")
        self._set_status_kind(self._pm_status, None)
        self._pm_worker = VersionActivationWorker(
            binary,
            self._pm_versions_directory,
            self._pm_activation_link,
            backup_unmanaged=backup_unmanaged,
        )
        self._pm_worker.finished.connect(self._on_pm_activation_finished)
        self._pm_worker.failed.connect(self._on_pm_worker_failed)
        self._pm_worker.password_requested.connect(
            self._on_pm_password_requested,
            Qt.ConnectionType.BlockingQueuedConnection,
        )
        self._pm_thread = run_worker_in_thread(self._pm_worker)
        self._refresh_pm_buttons()

    def _delete_selected_pm_version(self) -> None:
        installed = self._selected_pm_installed_version()
        if installed is None or self._pm_thread is not None:
            return
        answer = QMessageBox.question(
            self,
            "Delete downloaded ruyi?",
            f"Delete ruyi {installed.version} from {installed.path}?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._pm_operation = "delete"
        self._pm_status.setText(f"Deleting ruyi {installed.version}...")
        self._set_status_kind(self._pm_status, None)
        self._pm_worker = VersionDeleteWorker(
            installed.path,
            self._pm_versions_directory,
            self._pm_activation_link,
        )
        self._pm_worker.finished.connect(self._on_pm_delete_finished)
        self._pm_worker.failed.connect(self._on_pm_worker_failed)
        self._pm_thread = run_worker_in_thread(self._pm_worker)
        self._refresh_pm_buttons()

    def _deactivate_selected_pm_version(self) -> None:
        if self._pm_thread is not None:
            return
        installed = self._selected_pm_installed_version()
        if installed is None:
            return
        state = version_manager.read_activation_state(
            self._pm_activation_link,
            self._pm_versions_directory,
        )
        if not state.managed or state.target != installed.path.resolve(strict=False):
            return
        answer = QMessageBox.question(
            self,
            "Deactivate ruyi?",
            f"Remove the managed link {self._pm_activation_link}?\n\n"
            "Downloaded versions and existing backups will not be removed.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._pm_operation = "deactivate"
        self._pm_status.setText(f"Deactivating ruyi {state.version}...")
        self._set_status_kind(self._pm_status, None)
        self._pm_worker = VersionDeactivationWorker(
            self._pm_versions_directory,
            self._pm_activation_link,
        )
        self._pm_worker.finished.connect(self._on_pm_deactivation_finished)
        self._pm_worker.failed.connect(self._on_pm_worker_failed)
        self._pm_worker.password_requested.connect(
            self._on_pm_password_requested,
            Qt.ConnectionType.BlockingQueuedConnection,
        )
        self._pm_thread = run_worker_in_thread(self._pm_worker)
        self._refresh_pm_buttons()

    def _browse_selected_pm_version(self) -> None:
        installed = self._selected_pm_installed_version()
        if installed is None or self._pm_thread is not None:
            return
        if not self._reveal_pm_file(installed.path):
            QMessageBox.warning(
                self,
                "Could not browse downloaded ruyi",
                f"Could not show {installed.path} in the file manager.",
            )

    @staticmethod
    def _reveal_pm_file(path: Path) -> bool:
        """Show a downloaded binary in the platform's file manager."""
        path = Path(path)
        system = platform.system()
        if system == "Windows":
            started, _ = QProcess.startDetached(
                "explorer.exe",
                [f"/select,{os.fspath(path)}"],
            )
            if started:
                return True
        elif system == "Darwin":
            started, _ = QProcess.startDetached(
                "open",
                ["-R", os.fspath(path)],
            )
            if started:
                return True
        else:
            for program in ("dolphin", "nautilus"):
                if shutil.which(program) is None:
                    continue
                started, _ = QProcess.startDetached(
                    program,
                    ["--select", os.fspath(path)],
                )
                if started:
                    return True

        return QDesktopServices.openUrl(QUrl.fromLocalFile(os.fspath(path.parent)))

    def _start_repo_init(self) -> None:
        self._next_btn.setEnabled(False)
        self._worker = RepoInitWorker(self.state.config)
        self._worker.finished.connect(self._on_repo_ready)
        self._worker.failed.connect(self._on_worker_failed)
        self._thread = run_worker_in_thread(self._worker)

    def _start_repo_sync(self) -> None:
        assert self.state.mr is not None
        self._device_status.setText("Updating metadata repositories...")
        self._device_list.clear()
        self._worker = RepoSyncWorker(self.state.config, self.state.mr)
        self._worker.finished.connect(self._on_repo_synced)
        self._worker.failed.connect(self._on_worker_failed)
        self._thread = run_worker_in_thread(self._worker)
        self._refresh_buttons()

    def _start_download(self) -> None:
        assert self.state.mr is not None
        self._download_ok = False
        self._download_cancelled = False
        self._download_recoverable = False
        self._download_output = _StreamingProcessOutput()
        self._download_progress_line_active = False
        self._download_log.clear()
        self._download_status.setText("Downloading and installing packages...")
        self._set_step(self.STEP_DOWNLOAD)
        self._download_process = QProcess(self)
        self._download_process.setProgram(sys.executable)
        self._download_process.setArguments(
            ["-m", "oh_my_ruyi.download_child", *self.state.pkg_atoms]
        )
        env = QProcessEnvironment.systemEnvironment()
        env.insert("PYTHONUNBUFFERED", "1")
        self._download_process.setProcessEnvironment(env)
        self._download_process.setProcessChannelMode(
            QProcess.ProcessChannelMode.MergedChannels
        )
        self._download_process.readyReadStandardOutput.connect(self._on_download_output)
        self._download_process.finished.connect(self._on_download_process_finished)
        self._download_process.errorOccurred.connect(self._on_download_process_error)
        self._download_process.start()
        self._refresh_buttons()

    def _start_flash(self) -> None:
        assert self.state.prepared is not None
        storage_error = self._flash_storage_error()
        if storage_error is not None:
            self._populate_storage()
            self._storage_error.setText(storage_error)
            self._set_step(self.STEP_STORAGE)
            return
        self._flash_recoverable = False
        self._flash_cancel_requested = False
        self._flash_log.clear()
        self._flash_status.setText("Flashing the device...")
        self._set_step(self.STEP_FLASH)
        self._worker = FlashWorker(
            self.state.config,
            self.state.prepared,
            self.state.host_blkdev_map,
            self.state.host_blkdev_fingerprints,
            {
                part
                for part, confirmation in self._storage_mount_confirmations.items()
                if confirmation.isChecked()
            },
        )
        self._worker.finished.connect(self._on_flash_finished)
        self._worker.cancelled.connect(self._on_flash_cancelled)
        self._worker.failed.connect(self._on_worker_failed)
        self._worker.yes_no_requested.connect(
            self._on_flash_yes_no_requested, Qt.ConnectionType.BlockingQueuedConnection
        )
        self._worker.password_requested.connect(
            self._on_flash_password_requested,
            Qt.ConnectionType.BlockingQueuedConnection,
        )
        self._worker.process_output.connect(self._on_flash_process_output)
        self._thread = run_worker_in_thread(self._worker)
        self._refresh_buttons()

    def _check_fastboot_devices(self) -> None:
        self._stop_fastboot_check()
        self._fastboot_ok = False
        self._fastboot_output = ""
        self._fastboot_error_output = ""
        self._fastboot_timed_out = False
        self._set_status_kind(self._fastboot_status, None)
        self._fastboot_status.setText("Checking fastboot devices...")
        self._check_fastboot_btn.setEnabled(False)

        process = QProcess(self)
        self._fastboot_process = process
        process.setProgram(FASTBOOT_PROGRAM)
        process.setArguments(["devices"])
        process.readyReadStandardOutput.connect(
            lambda p=process: self._on_fastboot_output(p)
        )
        process.readyReadStandardError.connect(
            lambda p=process: self._on_fastboot_stderr(p)
        )
        process.finished.connect(
            lambda ret, _status, p=process: self._on_fastboot_finished(p, ret)
        )
        process.errorOccurred.connect(
            lambda error, p=process: self._on_fastboot_error(p, error)
        )
        process.start()
        self._fastboot_timer.start()
        self._refresh_buttons()

    def _on_fastboot_output(self, process: QProcess) -> None:
        if process is not self._fastboot_process:
            return
        self._fastboot_output += bytes(process.readAllStandardOutput()).decode(
            errors="replace"
        )

    def _on_fastboot_stderr(self, process: QProcess) -> None:
        if process is not self._fastboot_process:
            return
        self._fastboot_error_output += bytes(process.readAllStandardError()).decode(
            errors="replace"
        )

    def _on_fastboot_finished(self, process: QProcess, ret: int) -> None:
        if process is not self._fastboot_process:
            process.deleteLater()
            return
        self._on_fastboot_output(process)
        self._on_fastboot_stderr(process)
        stdout = self._fastboot_output.strip()
        stderr = self._fastboot_error_output.strip()
        output = "\n".join(part for part in (stdout, stderr) if part)
        if self._fastboot_timed_out:
            self._complete_fastboot_check(process, False, "fastboot devices timed out.")
        elif ret != 0:
            self._complete_fastboot_check(
                process,
                False,
                stderr or stdout or f"fastboot devices exited with code {ret}.",
            )
        elif not output:
            self._complete_fastboot_check(process, False, "No fastboot devices found.")
        else:
            self._complete_fastboot_check(
                process,
                True,
                "fastboot devices output:\n" + output,
            )

    def _on_fastboot_error(
        self,
        process: QProcess,
        error: QProcess.ProcessError,
    ) -> None:
        if process is not self._fastboot_process:
            return
        if self._fastboot_timed_out:
            message = "fastboot devices timed out."
        elif error == QProcess.ProcessError.FailedToStart:
            message = "fastboot command was not found."
        else:
            message = f"fastboot check failed: {error.name}."
        self._complete_fastboot_check(process, False, message)

    def _on_fastboot_timeout(self) -> None:
        process = self._fastboot_process
        if process is None:
            return
        self._fastboot_timed_out = True
        process.kill()

    def _complete_fastboot_check(
        self,
        process: QProcess,
        ok: bool,
        message: str,
    ) -> None:
        if process is not self._fastboot_process:
            return
        self._fastboot_timer.stop()
        self._fastboot_process = None
        process.deleteLater()
        self._fastboot_ok = ok
        self._set_status_kind(self._fastboot_status, "success" if ok else "error")
        self._fastboot_status.setText(message)
        self._check_fastboot_btn.setEnabled(True)
        self._refresh_buttons()

    def _stop_fastboot_check(self) -> None:
        self._fastboot_timer.stop()
        process = self._fastboot_process
        self._fastboot_process = None
        if process is None:
            return
        process.blockSignals(True)
        if process.state() != QProcess.ProcessState.NotRunning:
            process.terminate()
            if not process.waitForFinished(1000):
                process.kill()
                process.waitForFinished(1000)
        process.deleteLater()
        self._check_fastboot_btn.setEnabled(True)

    def _cancel_download(self) -> None:
        if self._download_process is None:
            return
        self._download_cancelled = True
        self._download_status.setText("Cancelling download...")
        self._terminate_download_process()
        self._refresh_buttons()

    def _resume_download(self) -> None:
        if not self.state.pkg_atoms:
            return
        self._start_download()

    def _reselect_versions(self) -> None:
        self.state.prepared = None
        self.state.host_blkdev_map = {}
        self.state.host_blkdev_fingerprints = {}
        self._download_ok = False
        self._download_recoverable = False
        if (
            self._versions_visited
            and self.state.mr is not None
            and self.state.combo is not None
        ):
            self.state.pkg_atoms = ruyi_facade.combo_package_atoms(
                self.state.combo.entity
            )
            self._populate_versions()
            self._set_step(self.STEP_VERSIONS)
        else:
            self._populate_packages()
            self._set_step(self.STEP_PACKAGES)

    def _restart_flow(self) -> None:
        self._download_ok = False
        self._download_recoverable = False
        self._flash_recoverable = False
        self._versions_visited = False
        self.state.device = None
        self.state.variant = None
        self.state.combo = None
        self.state.pkg_atoms = []
        self.state.prepared = None
        self.state.host_blkdev_map = {}
        self.state.host_blkdev_fingerprints = {}
        self.state.flash_ret = None
        self._populate_devices()
        self._set_step(self.STEP_DEVICE)

    def _terminate_download_process(self) -> None:
        proc = self._download_process
        if proc is None:
            return
        pid = proc.processId()
        if pid > 0 and platform.system() != "Windows":
            try:
                os.killpg(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            except PermissionError:
                os.kill(pid, signal.SIGTERM)
        proc.terminate()
        if not proc.waitForFinished(3000):
            if pid > 0 and platform.system() != "Windows":
                try:
                    os.killpg(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except PermissionError:
                    os.kill(pid, signal.SIGKILL)
            proc.kill()

    # --------------------------------------------------------------- slots

    def _on_pm_catalog_ready(self, catalog: version_manager.ReleaseCatalog) -> None:
        self._pm_catalog_releases = list(catalog.releases)
        self._cleanup_pm_thread()
        self._pm_status.setText(
            f"Release information loaded from {catalog.source_url}."
        )
        self._set_status_kind(self._pm_status, "success")
        self._refresh_pm_versions()
        self._run_pending_pm_first_run_check()

    def _on_pm_download_finished(self, path: Path) -> None:
        version = path.name.removeprefix("ruyi-")
        self._cleanup_pm_thread()
        self._pm_status.setText(f"Downloaded ruyi {version} to {path}.")
        self._set_status_kind(self._pm_status, "success")
        self._refresh_pm_versions(select_installed_version=version)

    def _on_pm_activation_finished(
        self,
        result: version_manager.ActivationResult,
    ) -> None:
        self._cleanup_pm_thread()
        message = (
            f"Activated ruyi {result.state.version} at {self._pm_activation_link}."
        )
        if result.backup_path is not None:
            message += f" Previous command backed up to {result.backup_path}."
        self._pm_status.setText(message)
        self._set_status_kind(self._pm_status, "success")
        self._refresh_pm_versions(select_installed_version=result.state.version)
        if result.state.target is not None:
            self._maybe_start_pm_telemetry(result.state.target)

    def _on_pm_delete_finished(
        self,
        installed: version_manager.InstalledVersion,
    ) -> None:
        self._cleanup_pm_thread()
        self._pm_status.setText(f"Deleted ruyi {installed.version}.")
        self._set_status_kind(self._pm_status, "success")
        self._refresh_pm_versions()

    def _on_pm_deactivation_finished(
        self,
        _state: version_manager.ActivationState,
    ) -> None:
        self._cleanup_pm_thread()
        self._pm_status.setText(f"Deactivated {self._pm_activation_link}.")
        self._set_status_kind(self._pm_status, "success")
        self._refresh_pm_versions()

    def _on_pm_telemetry_finished(
        self,
        result: version_manager.TelemetrySetupResult,
    ) -> None:
        self._cleanup_pm_thread()
        self._pm_status.setText(f"Telemetry mode: {result.status}")
        self._set_status_kind(self._pm_status, "success")
        self._refresh_pm_versions()

    def _on_pm_worker_failed(self, msg: str) -> None:
        operation = self._pm_operation
        self._cleanup_pm_thread()
        self._pm_status.setText(f"Failed: {msg}")
        self._set_status_kind(self._pm_status, "error")
        self._refresh_pm_versions()
        if operation != "refresh":
            QMessageBox.critical(self, "Operation failed", msg)
        else:
            self._run_pending_pm_first_run_check()

    def _on_pm_password_requested(self, prompt: str, response: dict) -> None:
        password, ok = QInputDialog.getText(
            self,
            "sudo password required",
            prompt,
            QLineEdit.EchoMode.Password,
        )
        response["password"] = password if ok else None

    def _run_pending_pm_first_run_check(self) -> None:
        if not self._pm_first_run_check_pending:
            return
        self._pm_first_run_check_pending = False
        self._maybe_start_pm_telemetry()

    def _maybe_start_pm_telemetry(self, binary: Path | None = None) -> None:
        if self._pm_telemetry_installation.exists() or self._pm_thread is not None:
            return
        if binary is None:
            state = version_manager.read_activation_state(
                self._pm_activation_link,
                self._pm_versions_directory,
            )
            if not state.managed or state.target is None:
                return
            binary = state.target
        if not binary.is_file():
            return

        mode = self._ask_for_pm_telemetry_mode()
        self._pm_operation = "telemetry"
        self._pm_status.setText("Saving telemetry preference and checking status...")
        self._set_status_kind(self._pm_status, None)
        self._pm_worker = TelemetrySetupWorker(binary, mode)
        self._pm_worker.finished.connect(self._on_pm_telemetry_finished)
        self._pm_worker.failed.connect(self._on_pm_worker_failed)
        self._pm_thread = run_worker_in_thread(self._pm_worker)
        self._refresh_pm_buttons()

    def _ask_for_pm_telemetry_mode(self) -> version_manager.TelemetryMode:
        upload = QMessageBox.question(
            self,
            "Ruyi telemetry",
            "This appears to be the first ruyi installation. RuyiSDK sends a "
            "one-time anonymous installation report and keeps additional usage data "
            "on this computer by default. With your permission, non-tracking usage "
            "data will also be uploaded periodically to RuyiSDK team-managed servers "
            "in the Chinese mainland.\n\nAllow periodic telemetry uploads?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if upload == QMessageBox.StandardButton.Yes:
            return "consent"

        opt_out = QMessageBox.question(
            self,
            "Ruyi telemetry",
            "Do you want to opt out of telemetry collection entirely? Choose No "
            "to keep telemetry data locally without uploading it.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return "optout" if opt_out == QMessageBox.StandardButton.Yes else "local"

    def _on_repo_ready(self, mr) -> None:
        self.state.mr = mr
        self._welcome_status.setText("RuyiSDK metadata repository is ready.")
        self._cleanup_thread()
        self._populate_devices()
        self._set_step(self.STEP_DEVICE)

    def _on_repo_synced(self, mr) -> None:
        self.state.mr = mr
        self._cleanup_thread()
        self._populate_devices()
        self._set_step(self.STEP_DEVICE)

    def _on_download_output(self) -> None:
        if self._download_process is None:
            return
        self._consume_download_output(
            bytes(self._download_process.readAllStandardOutput())
        )

    def _consume_download_output(self, data: bytes, *, final: bool = False) -> None:
        for kind, text in self._download_output.feed(data, final=final):
            self._render_download_output(text, complete=kind == "line")

    def _render_download_output(self, text: str, *, complete: bool) -> None:
        cursor = self._download_log.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        if self._download_progress_line_active:
            cursor.movePosition(
                QTextCursor.MoveOperation.StartOfBlock,
                QTextCursor.MoveMode.KeepAnchor,
            )
            cursor.insertText(text)
        else:
            self._download_log.appendPlainText(text)
        self._download_progress_line_active = not complete
        cursor = self._download_log.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        self._download_log.setTextCursor(cursor)
        self._download_log.ensureCursorVisible()

    def _on_download_process_error(self, error) -> None:
        self._download_status.setText(f"Download process error: {error.name}.")
        self._download_ok = False
        self._download_recoverable = True
        if (
            error == QProcess.ProcessError.FailedToStart
            and self._download_process is not None
        ):
            self._download_process.deleteLater()
            self._download_process = None
        self._refresh_buttons()

    def _on_download_process_finished(self, ret: int, _status) -> None:
        if self._download_process is not None:
            self._consume_download_output(
                bytes(self._download_process.readAllStandardOutput())
            )
            self._consume_download_output(b"", final=True)
            self._download_process.deleteLater()
            self._download_process = None
        if self._download_cancelled:
            self._download_status.setText("Download cancelled.")
            self._download_ok = False
            self._download_recoverable = True
            self._refresh_buttons()
            return
        self._on_download_finished(ret)

    def _on_download_finished(self, ret: int) -> None:
        if ret != 0:
            self.state.config.logger.F("failed to download and install packages")
            self._download_status.setText(f"Download failed (exit code {ret}).")
            self._download_ok = False
            self._download_recoverable = True
            self._refresh_buttons()
            return
        try:
            assert self.state.mr is not None
            self.state.prepared = ruyi_facade.prepare_provision(
                self.state.config,
                self.state.mr,
                self.state.pkg_atoms,
            )
        except Exception as exc:  # noqa: BLE001
            self._download_status.setText(f"Preparing flash failed: {exc}")
            self._download_ok = False
            self._download_recoverable = True
        else:
            self._download_status.setText("Download complete.")
            self._download_ok = True
            self._download_recoverable = False
        self._refresh_buttons()
        if self._download_ok:
            self._advance_after_download()

    def _on_flash_finished(self, ret: int) -> None:
        self._flash_cancel_requested = False
        self.state.flash_ret = ret
        self._flash_recoverable = ret != 0
        self._flash_status.setText(
            "Flash complete." if ret == 0 else f"Flash failed (exit code {ret})."
        )
        self._cleanup_thread()
        if ret == 0:
            self._populate_done()
            self._set_step(self.STEP_DONE)
        else:
            self._refresh_buttons()

    def _on_flash_cancelled(self) -> None:
        self._flash_cancel_requested = False
        self.state.flash_ret = None
        self._flash_recoverable = True
        self._flash_status.setText("Flash interrupted.")
        self._cleanup_thread()
        self._refresh_buttons()

    def _on_worker_failed(self, msg: str) -> None:
        QMessageBox.critical(self, "Operation failed", msg)
        if self._current_step == self.STEP_DOWNLOAD:
            self._download_status.setText(f"Failed: {msg}")
        elif self._current_step == self.STEP_FLASH:
            self._flash_cancel_requested = False
            self._flash_status.setText(f"Failed: {msg}")
            self._flash_recoverable = True
        elif self._current_step == self.STEP_DEVICE:
            self._device_status.setText(f"Failed: {msg}")
        else:
            self._welcome_status.setText(f"Failed: {msg}")
        self._cleanup_thread()
        self._refresh_buttons()

    def _on_flash_yes_no_requested(
        self, prompt: str, default: bool, response: dict
    ) -> None:
        ret = QMessageBox.question(
            self,
            "Flashing needs confirmation",
            prompt,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes
            if default
            else QMessageBox.StandardButton.No,
        )
        response["answer"] = ret == QMessageBox.StandardButton.Yes

    def _on_flash_password_requested(self, prompt: str, response: dict) -> None:
        password, ok = QInputDialog.getText(
            self,
            "sudo password required",
            prompt,
            QLineEdit.EchoMode.Password,
        )
        response["password"] = password if ok else None

    def _on_flash_process_output(self, text: str) -> None:
        self._flash_log.appendPlainText(text)

    def _on_log(self, level: str, text: str) -> None:
        target = (
            self._flash_log
            if self._current_step == self.STEP_FLASH
            else self._download_log
        )
        target.appendPlainText(text)

    # -------------------------------------------------------------- helpers

    def _connect_logs(self) -> None:
        self.state.emitter.log_emitted.connect(self._on_log)

    def _cleanup_thread(self) -> None:
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait()
            self._thread.deleteLater()
        self._thread = None
        self._worker = None

    def _cleanup_pm_thread(self) -> None:
        if self._pm_thread is not None:
            self._pm_thread.quit()
            self._pm_thread.wait()
            self._pm_thread.deleteLater()
        self._pm_thread = None
        self._pm_worker = None
        self._pm_operation = ""

    def _selected_pm_release(self) -> version_manager.RuyiRelease | None:
        row = self._pm_available_table.currentRow()
        item = self._pm_available_table.item(row, 0) if row >= 0 else None
        release = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        return release if isinstance(release, version_manager.RuyiRelease) else None

    def _selected_pm_installed_version(
        self,
    ) -> version_manager.InstalledVersion | None:
        row = self._pm_installed_table.currentRow()
        item = self._pm_installed_table.item(row, 0) if row >= 0 else None
        installed = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        return (
            installed
            if isinstance(installed, version_manager.InstalledVersion)
            else None
        )

    def _refresh_pm_versions(
        self,
        *,
        select_available_url: str | None = None,
        select_installed_version: str | None = None,
    ) -> None:
        self._pm_externally_managed = self._pm_config_externally_managed or (
            version_manager.is_ruyi_externally_managed(self._pm_system_config)
        )
        selected_release = self._selected_pm_release()
        previous_available_url = select_available_url or (
            selected_release.download_urls[0] if selected_release is not None else None
        )
        selected_installed = self._selected_pm_installed_version()
        previous_installed_version = select_installed_version or (
            selected_installed.version if selected_installed is not None else None
        )
        try:
            installed = version_manager.list_installed_versions(
                self._pm_versions_directory
            )
            installed = tuple(
                item
                for item in installed
                if item.architecture == "unknown"
                or version_manager.architecture_is_compatible(item.architecture)
            )
            active = version_manager.read_activation_state(
                self._pm_activation_link,
                self._pm_versions_directory,
            )
        except OSError as exc:
            self._pm_status.setText(f"Failed to inspect installed versions: {exc}")
            self._set_status_kind(self._pm_status, "error")
            installed = ()
            active = version_manager.ActivationState(
                self._pm_activation_link,
                False,
                False,
                False,
                None,
                None,
            )

        self._populate_pm_available_table(previous_available_url)
        self._populate_pm_installed_table(
            installed,
            active,
            previous_installed_version,
        )
        self._refresh_pm_path_status(active)
        self._refresh_pm_buttons()

    def _populate_pm_available_table(self, selected_url: str | None) -> None:
        table = self._pm_available_table
        releases = [*self._pm_catalog_releases, *self._pm_custom_releases]
        table.blockSignals(True)
        table.setSortingEnabled(False)
        table.setRowCount(len(releases))
        for row, release in enumerate(releases):
            version_item = _VersionTableItem(release.version)
            version_item.setData(Qt.ItemDataRole.UserRole, release)
            table.setItem(row, 0, version_item)
            table.setItem(row, 1, QTableWidgetItem(release.channel))
            architecture = (
                version_manager.normalize_architecture(release.architecture)
                or release.architecture
            )
            table.setItem(row, 2, QTableWidgetItem(architecture))
            table.setItem(row, 3, QTableWidgetItem(release.release_date[:10]))
        table.setSortingEnabled(True)
        table.sortItems(0, Qt.SortOrder.DescendingOrder)
        table.clearSelection()
        if selected_url is not None:
            for row in range(table.rowCount()):
                item = table.item(row, 0)
                release = item.data(Qt.ItemDataRole.UserRole)
                if (
                    isinstance(release, version_manager.RuyiRelease)
                    and release.download_urls[0] == selected_url
                ):
                    table.selectRow(row)
                    break
        table.blockSignals(False)

    def _populate_pm_installed_table(
        self,
        installed: tuple[version_manager.InstalledVersion, ...],
        active: version_manager.ActivationState,
        selected_version: str | None,
    ) -> None:
        table = self._pm_installed_table
        table.blockSignals(True)
        table.setSortingEnabled(False)
        table.setRowCount(len(installed))
        latest_versions = {release.version for release in self._pm_catalog_releases}
        for row, item in enumerate(installed):
            version_item = _VersionTableItem(item.version)
            version_item.setData(Qt.ItemDataRole.UserRole, item)
            table.setItem(row, 0, version_item)
            is_active = active.managed and active.target == item.path.resolve(
                strict=False
            )
            table.setItem(row, 1, QTableWidgetItem(item.channel))
            table.setItem(row, 2, QTableWidgetItem("Activate" if is_active else ""))
            table.setItem(row, 3, QTableWidgetItem(self._format_file_size(item.size)))
            table.setItem(
                row,
                4,
                QTableWidgetItem("Latest" if item.version in latest_versions else ""),
            )
        table.setSortingEnabled(True)
        table.sortItems(0, Qt.SortOrder.DescendingOrder)
        table.clearSelection()
        if selected_version is not None:
            for row in range(table.rowCount()):
                item = table.item(row, 0).data(Qt.ItemDataRole.UserRole)
                if (
                    isinstance(item, version_manager.InstalledVersion)
                    and item.version == selected_version
                ):
                    table.selectRow(row)
                    break
        table.blockSignals(False)

    @staticmethod
    def _format_file_size(size: int) -> str:
        value = float(size)
        for unit in ("B", "KiB", "MiB", "GiB"):
            if value < 1024 or unit == "GiB":
                return f"{int(value)} {unit}" if unit == "B" else f"{value:.1f} {unit}"
            value /= 1024
        raise AssertionError("unreachable")

    def _align_pm_status_heights(self) -> None:
        labels = (self._pm_status, self._pm_path_status)
        required_heights = []
        for label in labels:
            label.setMinimumHeight(0)
            label.setMaximumHeight(16777215)
            label.updateGeometry()
            required = label.heightForWidth(max(1, label.width()))
            required_heights.append(
                required if required >= 0 else label.sizeHint().height()
            )
        height = max(required_heights)
        for label in labels:
            label.setFixedHeight(height)

    def _refresh_pm_path_status(
        self,
        active: version_manager.ActivationState,
    ) -> None:
        if self._pm_externally_managed:
            self._pm_path_status.setText(
                "Version management issue: this system's ruyi package manager is "
                "configured to have its version managed by the system package manager."
            )
            self._set_status_kind(self._pm_path_status, "error")
            return
        path_state = version_manager.read_path_state(
            self._pm_versions_directory,
            link=self._pm_activation_link,
        )
        if path_state.correct:
            self._pm_path_status.setText(
                "PATH ready: ruyi resolves to the managed command at "
                f"{self._pm_activation_link}."
            )
            self._set_status_kind(self._pm_path_status, None)
        elif path_state.command is None:
            message = "PATH issue: no executable named ruyi was found."
            if active.managed:
                message += f" Add {self._pm_activation_link.parent} to PATH."
            self._pm_path_status.setText(message)
            self._set_status_kind(self._pm_path_status, "error")
        elif active.managed:
            self._pm_path_status.setText(
                f"PATH issue: ruyi resolves first to {path_state.command}, which is "
                f"ahead of the managed command at {self._pm_activation_link}."
            )
            self._set_status_kind(self._pm_path_status, "error")
        else:
            self._pm_path_status.setText(
                f"PATH issue: ruyi resolves to {path_state.command}, but no Oh My "
                "Ruyi-managed version is active."
            )
            self._set_status_kind(self._pm_path_status, "error")

    def _refresh_pm_buttons(self) -> None:
        busy = self._pm_thread is not None
        controls_enabled = not busy and not self._pm_externally_managed
        release = self._selected_pm_release()
        installed = self._selected_pm_installed_version()
        try:
            active = version_manager.read_activation_state(
                self._pm_activation_link,
                self._pm_versions_directory,
            )
        except OSError:
            active = version_manager.ActivationState(
                self._pm_activation_link,
                False,
                False,
                False,
                None,
                None,
            )
        release_is_installed = (
            version_manager.binary_path(
                release.version,
                self._pm_versions_directory,
            ).is_file()
            if release is not None
            else False
        )
        selected_is_active = (
            installed is not None
            and active.managed
            and active.target == installed.path.resolve(strict=False)
        )
        self._pm_available_table.setEnabled(controls_enabled)
        self._pm_installed_table.setEnabled(controls_enabled)
        self._pm_refresh_btn.setEnabled(controls_enabled)
        self._pm_local_refresh_btn.setEnabled(controls_enabled)
        self._pm_add_url_btn.setEnabled(controls_enabled)
        self._pm_remove_url_btn.setEnabled(
            controls_enabled
            and release is not None
            and any(item is release for item in self._pm_custom_releases)
        )
        self._pm_download_btn.setEnabled(
            controls_enabled and release is not None and not release_is_installed
        )
        self._pm_activate_btn.setEnabled(
            controls_enabled and installed is not None and not selected_is_active
        )
        self._pm_delete_btn.setEnabled(
            controls_enabled and installed is not None and not selected_is_active
        )
        self._pm_deactivate_btn.setEnabled(controls_enabled and selected_is_active)
        self._pm_browse_btn.setEnabled(controls_enabled and installed is not None)

    def _set_step(self, step: int) -> None:
        if self._current_step == self.STEP_REVIEW and step != self.STEP_REVIEW:
            self._stop_fastboot_check()
        if step < self._current_step:
            self._invalidate_downstream(step)
        self._current_step = step
        self._steps.blockSignals(True)
        self._steps.setCurrentRow(step)
        self._steps.blockSignals(False)
        self._stack.setCurrentIndex(step)
        self._refresh_step_items()
        self._refresh_summary()
        self._refresh_buttons()
        QTimer.singleShot(0, self._focus_current_step)

    def _refresh_step_items(self) -> None:
        for row in range(self._steps.count()):
            item = self._steps.item(row)
            flags = Qt.ItemFlag.ItemIsSelectable
            if row == self._current_step or (
                (row < self._current_step or self._is_completed_flash_history_step(row))
                and self._can_open_step(row)
            ):
                flags |= Qt.ItemFlag.ItemIsEnabled
            item.setFlags(flags)

    def _focus_current_step(self) -> None:
        target: QWidget | None = None
        if self._current_step == self.STEP_DEVICE:
            target = self._device_list
        elif self._current_step == self.STEP_VARIANT:
            target = self._variant_list
        elif self._current_step == self.STEP_COMBO:
            target = self._combo_list
        elif self._current_step == self.STEP_VERSIONS and self._version_combos:
            target = self._version_combos[0]
        elif self._current_step == self.STEP_PACKAGES:
            target = self._packages_list
        elif self._current_step == self.STEP_DOWNLOAD:
            target = self._download_log
        elif self._current_step == self.STEP_STORAGE and self._storage_inputs:
            target = next(iter(self._storage_inputs.values()))
        elif self._current_step == self.STEP_REVIEW:
            target = self._proceed_cb
        elif self._current_step == self.STEP_FLASH:
            target = self._flash_log
        elif self._current_step == self.STEP_DONE:
            target = self._next_btn
        if target is not None and target.isEnabled():
            target.setFocus(Qt.FocusReason.OtherFocusReason)

    def _invalidate_downstream(self, dest_step: int) -> None:
        if dest_step < self.STEP_FLASH:
            self.state.flash_ret = None
            self._flash_recoverable = False
        if dest_step < self.STEP_STORAGE:
            self.state.host_blkdev_map = {}
            self.state.host_blkdev_fingerprints = {}
        if dest_step < self.STEP_DOWNLOAD:
            self.state.prepared = None
            self._download_ok = False
            self._download_recoverable = False
        if dest_step < self.STEP_VERSIONS:
            # Re-derive pkg_atoms from the combo, discarding any version
            # customization the user may have done.
            if self.state.combo is not None:
                self.state.pkg_atoms = ruyi_facade.combo_package_atoms(
                    self.state.combo.entity
                )
            self._versions_visited = False
        if dest_step < self.STEP_COMBO:
            self.state.combo = None
            self.state.pkg_atoms = []
            self._versions_visited = False
        if dest_step < self.STEP_VARIANT:
            self.state.variant = None
        if dest_step < self.STEP_DEVICE:
            self.state.device = None

    def _on_step_clicked(self, row: int) -> None:
        if row < 0 or row == self._current_step:
            return
        if self._is_busy() or (
            row > self._current_step and not self._is_completed_flash_history_step(row)
        ):
            self._steps.setCurrentRow(self._current_step)
            return
        if self._can_open_step(row):
            if row == self.STEP_REVIEW:
                self._populate_review()
            self._set_step(row)
        else:
            self._steps.setCurrentRow(self._current_step)

    def _can_open_step(self, step: int) -> bool:
        if step == self.STEP_WELCOME:
            return True
        if step == self.STEP_DEVICE:
            return self.state.mr is not None
        if step == self.STEP_VARIANT:
            return self.state.device is not None
        if step == self.STEP_COMBO:
            return self.state.variant is not None
        if step == self.STEP_VERSIONS:
            # Only allow jumping here if the TUI would actually have offered
            # customization; otherwise the page is unpopulated and would be
            # blank/confusing.
            return (
                self.state.combo is not None
                and bool(self.state.pkg_atoms)
                and self.state.mr is not None
                and ruyi_facade.is_package_version_customization_possible(
                    self.state.config,
                    self.state.mr,
                    self.state.pkg_atoms,
                )
            )
        if step == self.STEP_PACKAGES:
            return self.state.combo is not None
        if step == self.STEP_DOWNLOAD:
            return bool(self.state.pkg_atoms)
        if step == self.STEP_STORAGE:
            return (
                self._download_ok
                and self.state.prepared is not None
                and bool(self.state.prepared.requested_host_blkdevs)
            )
        if step == self.STEP_REVIEW:
            return self._download_ok and self.state.prepared is not None
        if step == self.STEP_FLASH:
            return self.state.flash_ret is not None
        if step == self.STEP_DONE:
            return self.state.flash_ret == 0 or (
                self.state.combo is not None and not self.state.pkg_atoms
            )
        return False

    def _is_completed_flash_history_step(self, step: int) -> bool:
        return self.state.flash_ret == 0 and step in {self.STEP_FLASH, self.STEP_DONE}

    def _review_complete_if_possible(self) -> bool:
        if self.state.prepared is None:
            return False
        return self._review_complete()

    def _refresh_summary(self) -> None:
        self._summary_device.setText(
            f"Device: {self.state.device.display_name if self.state.device else '-'}"
        )
        self._summary_variant.setText(
            f"Variant: {self.state.variant.display_name if self.state.variant else '-'}"
        )
        self._summary_combo.setText(
            f"Image: {self.state.combo.display_name if self.state.combo else '-'}"
        )
        pkgs = ", ".join(self.state.pkg_atoms) if self.state.pkg_atoms else "-"
        self._summary_packages.setText(f"Packages: {pkgs}")
        if self.state.host_blkdev_map:
            storage = ", ".join(
                f"{k}: {v}" for k, v in self.state.host_blkdev_map.items()
            )
        else:
            storage = "-"
        self._summary_storage.setText(f"Storage: {storage}")

    def _refresh_buttons(self) -> None:
        busy = self._is_busy()
        self._back_btn.setEnabled(
            not busy
            and self._current_step
            not in {self.STEP_WELCOME, self.STEP_DOWNLOAD, self.STEP_FLASH}
        )
        self._next_btn.setEnabled(not busy and self._can_go_next())
        if self._current_step == self.STEP_DONE:
            self._next_btn.setText("Close")
        elif self._current_step == self.STEP_PACKAGES:
            self._next_btn.setText("Proceed")
        else:
            self._next_btn.setText("Next")
        self._update_repo_btn.setEnabled(not busy and self.state.mr is not None)
        self._cancel_download_btn.setVisible(
            self._current_step == self.STEP_DOWNLOAD
            and self._download_process is not None
        )
        self._cancel_download_btn.setEnabled(self._download_process is not None)
        self._download_recovery_row.setVisible(
            self._current_step == self.STEP_DOWNLOAD
            and self._download_recoverable
            and not busy
        )
        self._resume_download_btn.setEnabled(bool(self.state.pkg_atoms))
        self._reselect_versions_btn.setEnabled(self.state.combo is not None)
        self._reselect_versions_btn.setText(
            "Reselect versions" if self._versions_visited else "Reselect packages"
        )
        self._restart_btn.setEnabled(self.state.mr is not None)
        self._refresh_storage_btn.setEnabled(
            not busy
            and self._current_step == self.STEP_STORAGE
            and self.state.prepared is not None
        )
        flash_recoverable = (
            self._current_step == self.STEP_FLASH
            and self._flash_recoverable
            and not busy
        )
        flash_running = (
            self._current_step == self.STEP_FLASH
            and isinstance(self._worker, FlashWorker)
            and self._thread is not None
        )
        self._interrupt_flash_btn.setVisible(flash_running)
        self._interrupt_flash_btn.setEnabled(
            flash_running and not self._flash_cancel_requested
        )
        self._flash_recovery_row.setVisible(flash_recoverable)
        self._retry_flash_btn.setEnabled(self.state.prepared is not None)
        self._review_flash_btn.setEnabled(self.state.prepared is not None)
        self._restart_flash_btn.setEnabled(self.state.mr is not None)

    def _interrupt_flash(self) -> None:
        worker = self._worker
        if not isinstance(worker, FlashWorker) or self._flash_cancel_requested:
            return
        self._flash_cancel_requested = True
        self._flash_status.setText("Interrupting flash...")
        worker.request_cancel()
        self._refresh_buttons()

    def _retry_flash(self) -> None:
        if self.state.prepared is None or self._is_busy():
            return
        self.state.flash_ret = None
        self._start_flash()

    def _review_flash_settings(self) -> None:
        if self.state.prepared is None or self._is_busy():
            return
        self.state.flash_ret = None
        self._flash_recoverable = False
        if self.state.prepared.requested_host_blkdevs:
            self._populate_storage()
            self._set_step(self.STEP_STORAGE)
        else:
            self._populate_review()
            self._set_step(self.STEP_REVIEW)

    def _is_busy(self) -> bool:
        return (
            self._thread is not None
            or self._download_process is not None
            or self._fastboot_process is not None
        )

    def _activate_current_step(self, _item=None) -> None:
        if self._is_busy() or not self._can_go_next():
            return
        self._go_next()

    def _advance_after_download(self) -> None:
        assert self.state.prepared is not None
        if self.state.prepared.requested_host_blkdevs:
            self._populate_storage()
            self._set_step(self.STEP_STORAGE)
        else:
            self.state.host_blkdev_map = {}
            self.state.host_blkdev_fingerprints = {}
            self._populate_review()
            self._set_step(self.STEP_REVIEW)

    def _can_go_next(self) -> bool:
        step = self._current_step
        if step == self.STEP_WELCOME:
            return self.state.mr is not None
        if step == self.STEP_DEVICE:
            item = self._device_list.currentItem()
            if item is None:
                return False
            choice_id = item.data(Qt.ItemDataRole.UserRole)
            return choice_id in self._device_choices
        if step == self.STEP_VARIANT:
            return self._variant_list.currentItem() is not None
        if step == self.STEP_COMBO:
            return self._combo_list.currentItem() is not None
        if step == self.STEP_VERSIONS:
            return True
        if step == self.STEP_PACKAGES:
            return True
        if step == self.STEP_DOWNLOAD:
            return self._download_ok
        if step == self.STEP_STORAGE:
            return self._storage_complete()
        if step == self.STEP_REVIEW:
            return self._review_complete()
        if step == self.STEP_FLASH:
            return self.state.flash_ret == 0
        return True

    def _go_next(self) -> None:
        step = self._current_step
        if step == self.STEP_WELCOME:
            self._set_step(self.STEP_DEVICE)
        elif step == self.STEP_DEVICE:
            self._choose_device()
            self._populate_variants()
            self._set_step(self.STEP_VARIANT)
        elif step == self.STEP_VARIANT:
            self._choose_variant()
            self._populate_combos()
            self._set_step(self.STEP_COMBO)
        elif step == self.STEP_COMBO:
            self._choose_combo()
            if ruyi_facade.is_package_version_customization_possible(
                self.state.config,
                self.state.mr,
                self.state.pkg_atoms,
            ):
                self._populate_versions()
                self._versions_visited = True
                self._set_step(self.STEP_VERSIONS)
            else:
                self._populate_packages()
                self._set_step(self.STEP_PACKAGES)
        elif step == self.STEP_VERSIONS:
            self._commit_versions()
            self._populate_packages()
            self._set_step(self.STEP_PACKAGES)
        elif step == self.STEP_PACKAGES:
            if not self.state.pkg_atoms:
                self._populate_done()
                self._set_step(self.STEP_DONE)
            else:
                self._start_download()
        elif step == self.STEP_DOWNLOAD:
            self._advance_after_download()
        elif step == self.STEP_STORAGE:
            if self._commit_storage():
                self._populate_review()
                self._set_step(self.STEP_REVIEW)
        elif step == self.STEP_REVIEW:
            self._start_flash()
        elif step == self.STEP_FLASH:
            self._populate_done()
            self._set_step(self.STEP_DONE)
        elif step == self.STEP_DONE:
            self.close()

    def _go_back(self) -> None:
        step = self._current_step
        if step == self.STEP_DEVICE:
            prev = self.STEP_WELCOME
        elif step == self.STEP_VARIANT:
            prev = self.STEP_DEVICE
        elif step == self.STEP_COMBO:
            prev = self.STEP_VARIANT
        elif step == self.STEP_VERSIONS:
            prev = self.STEP_COMBO
        elif step == self.STEP_PACKAGES:
            prev = self.STEP_VERSIONS if self._versions_visited else self.STEP_COMBO
        elif step == self.STEP_STORAGE:
            prev = self.STEP_DOWNLOAD
        elif step == self.STEP_REVIEW:
            if self.state.prepared and self.state.prepared.requested_host_blkdevs:
                prev = self.STEP_STORAGE
            else:
                prev = self.STEP_DOWNLOAD
        elif step == self.STEP_DONE:
            if self.state.flash_ret is not None:
                prev = self.STEP_FLASH
            elif self.state.pkg_atoms and self.state.prepared is not None:
                self._populate_review()
                prev = self.STEP_REVIEW
            else:
                prev = self.STEP_PACKAGES
        else:
            prev = None
        if prev is not None:
            self._set_step(prev)

    # ----------------------------------------------------------- step setup

    def _populate_devices(self) -> None:
        assert self.state.mr is not None
        devices = ruyi_facade.list_devices(self.state.mr)
        self._device_choices = {d.id: d for d in devices}
        self._device_list.clear()
        self._device_status.setText("")
        self._update_repo_btn.setVisible(not devices)
        for d in devices:
            item = QListWidgetItem(d.display_name)
            item.setData(Qt.ItemDataRole.UserRole, d.id)
            self._device_list.addItem(item)
        if not devices:
            entity_types = ruyi_facade.list_entity_types(self.state.mr)
            types_text = ", ".join(entity_types) if entity_types else "(none)"
            repo_entries = []
            for entry in self.state.config.repo_entries:
                if entry.id != ruyi_facade.PROVISION_REPO_ID:
                    continue
                source = entry.local_path or entry.remote or "(no source)"
                repo_entries.append(f"{entry.id}: {source}")
            repos_text = (
                "\n".join(f" * {entry}" for entry in repo_entries) or " * (none)"
            )

            workspace_ruyinews = (
                Path(__file__).resolve().parents[2]
                / "ruyisdk-ruyisdk-website"
                / "news"
                / "ruyinews"
            )
            local_hint = ""
            if (workspace_ruyinews / "entities" / "device").is_dir():
                local_hint = (
                    "\n\nA local metadata tree with device data was detected at:\n"
                    f"{workspace_ruyinews}\n\n"
                    "To make the CLI and GUI use it, configure ruyi's repo.local "
                    "to this absolute path."
                )
            self._device_status.setText(
                "The current ruyi metadata repository does not contain device "
                "provisioning entities (`device`, `device-variant`, `image-combo`). "
                "This GUI follows `ruyi device provision`, so it cannot continue "
                "without those entities.\n\n"
                f"Available entity types: {types_text}.\n\n"
                "Configured repositories:\n"
                f"{repos_text}"
                f"{local_hint}"
            )
            item = QListWidgetItem(
                "No device provisioning data is available in this repository."
            )
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            self._device_list.addItem(item)
        elif self._device_list.count() > 0:
            self._device_list.setCurrentRow(0)

    def _choose_device(self) -> None:
        item = self._device_list.currentItem()
        assert item is not None
        choice_id = item.data(Qt.ItemDataRole.UserRole)
        self.state.device = self._device_choices[choice_id]
        self.state.variant = None
        self.state.combo = None
        self.state.pkg_atoms = []

    def _populate_variants(self) -> None:
        assert self.state.mr is not None and self.state.device is not None
        variants = ruyi_facade.list_variants(self.state.mr, self.state.device.entity)
        self._variant_choices = {v.id: v for v in variants}
        self._variant_list.clear()
        for v in variants:
            item = QListWidgetItem(v.display_name)
            item.setData(Qt.ItemDataRole.UserRole, v.id)
            self._variant_list.addItem(item)

    def _choose_variant(self) -> None:
        item = self._variant_list.currentItem()
        assert item is not None
        self.state.variant = self._variant_choices[item.data(Qt.ItemDataRole.UserRole)]
        self.state.combo = None
        self.state.pkg_atoms = []

    def _populate_combos(self) -> None:
        assert self.state.mr is not None and self.state.variant is not None
        combos = ruyi_facade.list_combos(self.state.mr, self.state.variant.entity)
        self._combo_choices = {c.id: c for c in combos}
        self._combo_list.clear()
        for c in combos:
            item = QListWidgetItem(c.display_name)
            item.setData(Qt.ItemDataRole.UserRole, c.id)
            self._combo_list.addItem(item)

    def _choose_combo(self) -> None:
        item = self._combo_list.currentItem()
        assert item is not None
        self.state.combo = self._combo_choices[item.data(Qt.ItemDataRole.UserRole)]
        self.state.pkg_atoms = ruyi_facade.combo_package_atoms(self.state.combo.entity)

    def _populate_versions(self) -> None:
        assert self.state.mr is not None
        while self._versions_layout.count():
            item = self._versions_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._version_combos.clear()
        selections = ruyi_facade.list_package_version_selections(
            self.state.config,
            self.state.mr,
            self.state.pkg_atoms,
        )
        self._versions_status.setText(
            "This mirrors the TUI's package version customization step. "
            "Leave the default selection to install the latest version."
        )
        for sel in selections:
            label = QLabel(sel.package_name)
            combo = QComboBox()
            combo.setAccessibleName(f"Version for {sel.package_name}")
            label.setBuddy(combo)
            for option in sel.options:
                combo.addItem(option.display_name, option.atom)
            combo.setEnabled(sel.locked_reason is None and len(sel.options) > 1)
            if sel.locked_reason:
                label.setText(f"{sel.package_name} ({sel.locked_reason})")
            row = QHBoxLayout()
            row.addWidget(label, 2)
            row.addWidget(combo, 3)
            wrapper = QWidget()
            wrapper.setLayout(row)
            self._versions_layout.addWidget(wrapper)
            self._version_combos.append(combo)
        self._versions_layout.addStretch()

    def _commit_versions(self) -> None:
        if not self._version_combos:
            return
        self.state.pkg_atoms = [
            combo.currentData(Qt.ItemDataRole.UserRole)
            for combo in self._version_combos
        ]

    def _populate_packages(self) -> None:
        self._packages_list.clear()
        if self.state.pkg_atoms:
            for atom in self.state.pkg_atoms:
                self._packages_list.addItem(atom)
        else:
            self._packages_list.addItem(
                "No packages. The selected image only contains a post-install message."
            )

    def _populate_storage(
        self,
        disks: list[host_storage.BlockDeviceChoice] | None = None,
        selected_paths: dict[str, str] | None = None,
    ) -> None:
        assert self.state.prepared is not None
        if selected_paths is None:
            selected_paths = dict(self.state.host_blkdev_map)
        while self._storage_layout.count():
            item = self._storage_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._storage_inputs.clear()
        self._storage_mount_warnings.clear()
        self._storage_mount_confirmations.clear()
        self._storage_error.setText("")
        discover_async = disks is None and host_storage.validation_is_slow()
        if disks is None:
            disks = [] if discover_async else host_storage.list_disks()
        for part in self.state.prepared.requested_host_blkdevs:
            previous_path = selected_paths.get(part)
            desc = ruyi_facade.part_description(part)
            label = QLabel(f"{desc} ({part})")
            edit = QComboBox()
            edit.setEditable(True)
            edit.setAccessibleName(f"Target disk for {desc}")
            label.setBuddy(edit)
            edit.lineEdit().setPlaceholderText("/dev/...")
            for disk in disks:
                edit.addItem(disk.display_name, disk.path)
                index = edit.count() - 1
                edit.setItemData(index, disk.mounted, STORAGE_MOUNTED_ROLE)
                edit.setItemData(
                    index,
                    disk.fingerprint,
                    STORAGE_FINGERPRINT_ROLE,
                )
            warning = QLabel("The selected disk or one of its partitions is mounted.")
            warning.setProperty("statusKind", "error")
            warning.setVisible(False)
            confirm = QCheckBox("I understand flashing may overwrite mounted data.")
            confirm.setVisible(False)
            confirm.toggled.connect(self._refresh_buttons)
            edit.currentTextChanged.connect(
                lambda _text, e=edit, w=warning, c=confirm: (
                    self._on_storage_target_changed(e, w, c)
                )
            )
            browse = QPushButton()
            browse.setIcon(
                self.style().standardIcon(QStyle.StandardPixmap.SP_DialogOpenButton)
            )
            browse.setToolTip(f"Choose target disk or image file for {desc}")
            browse.setAccessibleName(f"Choose target disk or image file for {desc}")
            browse.clicked.connect(lambda _=False, e=edit: self._browse_storage(e))
            row = QHBoxLayout()
            row.addWidget(label, 2)
            row.addWidget(edit, 3)
            row.addWidget(browse)
            wrapper = QWidget()
            wrapper_layout = QVBoxLayout(wrapper)
            wrapper_layout.setContentsMargins(0, 0, 0, 0)
            wrapper_layout.addLayout(row)
            wrapper_layout.addWidget(warning)
            wrapper_layout.addWidget(confirm)
            self._storage_layout.addWidget(wrapper)
            self._storage_inputs[part] = edit
            self._storage_mount_warnings[part] = warning
            self._storage_mount_confirmations[part] = confirm
            if previous_path:
                idx = edit.findData(previous_path)
                if idx < 0:
                    edit.addItem(previous_path, previous_path)
                    idx = edit.count() - 1
                edit.setCurrentIndex(idx)
            else:
                edit.setCurrentIndex(-1)
                edit.lineEdit().clear()
            self._refresh_storage_mount_warning(edit, warning, confirm)
        self._storage_layout.addStretch()
        if discover_async:
            self._start_storage_discovery(selected_paths)
        else:
            self._storage_box.setEnabled(True)

    def _refresh_storage_disks(self) -> None:
        if self.state.prepared is None or self._is_busy():
            return
        selected_paths = {
            part: path
            for part, edit in self._storage_inputs.items()
            if (path := self._storage_path(edit))
        }
        self._start_storage_discovery(selected_paths)

    def _start_storage_discovery(
        self,
        selected_paths: dict[str, str] | None = None,
    ) -> None:
        self._storage_discovery_paths = dict(selected_paths or {})
        self._storage_box.setEnabled(False)
        self._storage_error.setText("Detecting disks...")
        self._worker = StorageDiscoveryWorker()
        self._worker.finished.connect(self._on_storage_disks_ready)
        self._worker.failed.connect(self._on_storage_discovery_failed)
        self._thread = run_worker_in_thread(self._worker)
        self._refresh_buttons()

    def _on_storage_disks_ready(self, disks: object) -> None:
        selected_paths = self._storage_discovery_paths
        self._storage_discovery_paths = {}
        self._cleanup_thread()
        self._populate_storage(list(disks), selected_paths)
        self._refresh_buttons()

    def _on_storage_discovery_failed(self, message: str) -> None:
        self._storage_discovery_paths = {}
        self._cleanup_thread()
        self._storage_box.setEnabled(True)
        self._storage_error.setText(
            f"Automatic disk detection failed: {message}. Use the file chooser to select a target."
        )
        self._refresh_buttons()

    def _browse_storage(self, edit: QComboBox) -> None:
        dialog = QFileDialog(
            self,
            "Select disk or image file",
            host_storage.DEFAULT_DEVICE_ROOT,
        )
        dialog.setOption(QFileDialog.Option.DontUseNativeDialog, True)
        dialog.setFileMode(QFileDialog.FileMode.AnyFile)
        dialog.setNameFilter("All entries (*)")
        dialog.setFilter(
            QDir.Filter.AllEntries
            | QDir.Filter.System
            | QDir.Filter.Hidden
            | QDir.Filter.NoDotAndDotDot
        )
        if dialog.exec() != QFileDialog.DialogCode.Accepted:
            return
        selected = dialog.selectedFiles()
        path = selected[0].strip() if selected else ""
        if not path:
            return
        idx = edit.findData(path)
        if idx < 0:
            idx = edit.findText(path)
        if idx < 0:
            edit.addItem(path, path)
            idx = edit.count() - 1
        edit.setCurrentIndex(idx)
        self._refresh_storage_controls()

    def _storage_path(self, edit: QComboBox) -> str:
        data = edit.currentData(Qt.ItemDataRole.UserRole)
        if data and edit.currentText() == edit.itemText(edit.currentIndex()):
            return str(data).strip()
        return edit.currentText().strip()

    def _refresh_storage_mount_warning(
        self, edit: QComboBox, warning: QLabel, confirm: QCheckBox
    ) -> None:
        path = self._storage_path(edit)
        mounted_data = self._storage_item_data(edit, STORAGE_MOUNTED_ROLE)
        if mounted_data is not None:
            mounted = bool(mounted_data)
        elif path and os.path.exists(path) and host_storage.is_native_disk_path(path):
            mounted = (
                True
                if host_storage.validation_is_slow()
                else host_storage.is_disk_or_child_mounted(path)
            )
        else:
            mounted = bool(
                path
                and os.path.exists(path)
                and host_storage.is_disk_or_child_mounted(path)
            )
        if not mounted:
            confirm.setChecked(False)
        warning.setVisible(mounted)
        confirm.setVisible(mounted)
        confirm.setEnabled(mounted)
        self._refresh_buttons()

    def _on_storage_target_changed(
        self, edit: QComboBox, warning: QLabel, confirm: QCheckBox
    ) -> None:
        confirm.setChecked(False)
        self._refresh_storage_mount_warning(edit, warning, confirm)

    def _storage_item_data(self, edit: QComboBox, role: int) -> object | None:
        index = edit.currentIndex()
        if index < 0 or edit.currentText() != edit.itemText(index):
            return None
        return edit.itemData(index, role)

    def _refresh_storage_controls(self) -> None:
        for part, edit in self._storage_inputs.items():
            self._refresh_storage_mount_warning(
                edit,
                self._storage_mount_warnings[part],
                self._storage_mount_confirmations[part],
            )

    def _storage_complete(self) -> bool:
        for part, edit in self._storage_inputs.items():
            path = self._storage_path(edit)
            if not path or not os.path.exists(path):
                return False
            if (
                self._storage_mount_warnings[part].isVisible()
                and not self._storage_mount_confirmations[part].isChecked()
            ):
                return False
        return True

    def _commit_storage(self) -> bool:
        host_blkdev_map = {}
        fingerprints: dict[str, str] = {}
        for part, edit in self._storage_inputs.items():
            path = self._storage_path(edit)
            if not os.path.exists(path):
                self._storage_error.setText(f"'{path}' does not exist.")
                return False
            if (
                self._storage_mount_warnings[part].isVisible()
                and not self._storage_mount_confirmations[part].isChecked()
            ):
                self._storage_error.setText(
                    f"'{path}' is mounted. Confirm the mounted-device warning before continuing."
                )
                return False
            fingerprint_data = self._storage_item_data(
                edit,
                STORAGE_FINGERPRINT_ROLE,
            )
            fingerprint = (
                str(fingerprint_data)
                if fingerprint_data
                else host_storage.device_fingerprint(path)
            )
            if fingerprint is None:
                self._storage_error.setText(
                    f"Could not verify the identity of '{path}'. Select the target again."
                )
                return False
            host_blkdev_map[part] = path
            fingerprints[part] = fingerprint
        self.state.host_blkdev_map = host_blkdev_map
        self.state.host_blkdev_fingerprints = fingerprints
        self._refresh_summary()
        return True

    def _flash_storage_error(self) -> str | None:
        if self.state.prepared is None:
            return "Flash preparation is incomplete."
        for part in self.state.prepared.requested_host_blkdevs:
            path = self.state.host_blkdev_map.get(part, "").strip()
            if not path or not os.path.exists(path):
                return f"The selected target for {part} is no longer available. Select it again."
            expected_fingerprint = self.state.host_blkdev_fingerprints.get(part)
            if host_storage.validation_is_slow():
                if expected_fingerprint is None:
                    return (
                        f"The identity of '{path}' was not recorded. Select it again."
                    )
                continue
            current_fingerprint = host_storage.device_fingerprint(path)
            if (
                expected_fingerprint is None
                or current_fingerprint is None
                or current_fingerprint != expected_fingerprint
            ):
                return (
                    f"The device at '{path}' has changed since review. "
                    "Select and confirm the target again."
                )
            confirmation = self._storage_mount_confirmations.get(part)
            if host_storage.is_disk_or_child_mounted(path) and (
                confirmation is None or not confirmation.isChecked()
            ):
                return (
                    f"'{path}' is now mounted. Review the target and confirm the "
                    "mounted-device warning before flashing."
                )
        return None

    def _populate_review(self) -> None:
        assert self.state.prepared is not None
        steps = ruyi_facade.compute_pretend_steps(
            self.state.prepared, self.state.host_blkdev_map
        )
        self._review_steps.setPlainText("\n".join(f" * {s}" for s in steps))
        missing = ruyi_facade.missing_cmds(self.state.prepared)
        self._review_missing.setText(
            "Missing required commands: " + ", ".join(missing) + "." if missing else ""
        )
        needs_fastboot = ruyi_facade.needs_fastboot_confirmation(self.state.prepared)
        self._fastboot_ok = not needs_fastboot
        self._fastboot_status.setVisible(needs_fastboot)
        self._check_fastboot_btn.setVisible(needs_fastboot)
        if needs_fastboot:
            self._fastboot_status.setText("Checking fastboot devices...")
            self._set_status_kind(self._fastboot_status, None)
            self._check_fastboot_devices()
        else:
            self._fastboot_status.setText("")
        self._proceed_cb.setChecked(False)

    def _review_complete(self) -> bool:
        assert self.state.prepared is not None
        if ruyi_facade.missing_cmds(self.state.prepared):
            return False
        if (
            ruyi_facade.needs_fastboot_confirmation(self.state.prepared)
            and not self._fastboot_ok
        ):
            return False
        return self._proceed_cb.isChecked()

    def _populate_done(self) -> None:
        if self.state.flash_ret is None and not self.state.pkg_atoms:
            self._done_label.setText(
                "No flashing was required. See the message below for next steps."
            )
            self._set_status_kind(self._done_label, "success")
        elif self.state.flash_ret == 0:
            self._done_label.setText(
                "It seems the flashing has finished without errors. Happy hacking!"
            )
            self._set_status_kind(self._done_label, "success")
        else:
            self._done_label.setText(
                f"Flashing failed (exit code {self.state.flash_ret}). Check the device right now."
            )
            self._set_status_kind(self._done_label, "error")

        msg = ""
        if self.state.combo is not None and self.state.mr is not None:
            msg = (
                ruyi_facade.get_postinst_msg(
                    self.state.mr,
                    self.state.combo.entity,
                    self.state.config.lang_code,
                )
                or ""
            )
        self.state.postinst_msg = msg or None
        self._postinst_label.setText(msg)
        self._postinst_label.setVisible(bool(msg))
