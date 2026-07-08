"""Single-window provisioning frontend.

The original CLI is a linear wizard, but a GUI is easier to inspect when the
whole flow is visible at once. This window keeps a step list on the left and a
stable right-hand work area: a summary of choices made so far, followed by the
controls for the current step.
"""

from __future__ import annotations

import os
import signal
import sys
from pathlib import Path

from PySide6.QtCore import QDir, QProcess, QProcessEnvironment, Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QGroupBox,
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
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from . import ruyi_facade
from .qt_logger import LogEmitter, QtRuyiLogger
from .state import WizardState
from .workers import FlashWorker, RepoInitWorker, RepoSyncWorker, run_worker_in_thread


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
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("RuyiSDK Device Provisioning")
        self.resize(1060, 720)

        self.state = WizardState(config=config, emitter=emitter)
        self._logger = logger
        self._worker = None
        self._thread = None
        self._download_process: QProcess | None = None
        self._download_cancelled = False
        self._download_recoverable = False
        self._flash_recoverable = False
        self._current_step = self.STEP_WELCOME
        self._download_ok = False
        self._versions_visited = False

        self._device_choices = {}
        self._variant_choices = {}
        self._combo_choices = {}
        self._version_combos: list[QComboBox] = []
        self._storage_inputs: dict[str, QComboBox] = {}
        self._storage_mount_warnings: dict[str, QLabel] = {}
        self._storage_mount_confirmations: dict[str, QCheckBox] = {}

        self._build_ui()
        self._connect_logs()
        self._set_step(self.STEP_WELCOME)
        if auto_start:
            self._start_repo_init()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt override
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

        event.accept()

    # ------------------------------------------------------------------ UI

    def _build_ui(self) -> None:
        root = QWidget()
        root_layout = QHBoxLayout(root)
        root_layout.setContentsMargins(12, 12, 12, 12)
        root_layout.setSpacing(12)

        self._steps = QListWidget()
        self._steps.setFixedWidth(180)
        self._steps.setFocusPolicy(Qt.FocusPolicy.NoFocus)
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
        self._back_btn.clicked.connect(self._go_back)
        self._next_btn.clicked.connect(self._go_next)
        button_row.addWidget(self._back_btn)
        button_row.addWidget(self._next_btn)
        right_layout.addLayout(button_row)

        self.setCentralWidget(root)

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
        self._device_list.currentRowChanged.connect(self._refresh_buttons)
        self._device_list.itemDoubleClicked.connect(self._activate_current_step)
        self._device_status = QLabel("")
        self._device_status.setWordWrap(True)
        self._device_status.setStyleSheet("color: #b15a00;")
        self._update_repo_btn = QPushButton("Update metadata")
        self._update_repo_btn.clicked.connect(self._start_repo_sync)
        self._add_page(
            "Pick your device",
            [self._device_status, self._update_repo_btn, self._device_list],
        )

        self._variant_list = QListWidget()
        self._variant_list.currentRowChanged.connect(self._refresh_buttons)
        self._variant_list.itemDoubleClicked.connect(self._activate_current_step)
        self._add_page("Pick the device variant", [self._variant_list])

        self._combo_list = QListWidget()
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
        self._storage_error.setStyleSheet("color: #b15a00;")
        self._add_page(
            "Provide storage paths",
            [
                QLabel(
                    "Enter block device paths such as /dev/sdX or /dev/nvme0n1. "
                    "Mounted block devices are rejected for safety."
                ),
                self._storage_box,
                self._storage_error,
            ],
        )

        self._review_steps = QPlainTextEdit()
        self._review_steps.setReadOnly(True)
        self._review_missing = QLabel("")
        self._review_missing.setWordWrap(True)
        self._review_missing.setStyleSheet("color: #c01c28;")
        self._fastboot_ok = False
        self._fastboot_status = QLabel("")
        self._fastboot_status.setWordWrap(True)
        self._check_fastboot_btn = QPushButton("Check fastboot devices")
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
            [self._flash_status, self._flash_recovery_row, self._flash_log],
        )

        self._done_label = QLabel("")
        self._done_label.setWordWrap(True)
        self._postinst_label = QLabel("")
        self._postinst_label.setWordWrap(True)
        self._postinst_label.setFrameShape(QFrame.Shape.Box)
        self._postinst_label.setStyleSheet("padding: 8px;")
        self._add_page("Done", [self._done_label, self._postinst_label])

    def _add_page(self, title: str, widgets: list[QWidget]) -> None:
        page = QWidget()
        layout = QVBoxLayout(page)
        title_label = QLabel(f"<b>{title}</b>")
        title_label.setWordWrap(True)
        layout.addWidget(title_label)
        for widget in widgets:
            if isinstance(widget, QLabel):
                widget.setWordWrap(True)
            layout.addWidget(widget)
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

    # -------------------------------------------------------------- actions

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
        self._download_log.clear()
        self._download_status.setText("Downloading and installing packages...")
        self._set_step(self.STEP_DOWNLOAD)
        self._download_process = QProcess(self)
        self._download_process.setProgram(sys.executable)
        self._download_process.setArguments(
            ["-m", "ruyi_device_provision_gui.download_child", *self.state.pkg_atoms]
        )
        env = QProcessEnvironment.systemEnvironment()
        env.insert("PYTHONUNBUFFERED", "1")
        self._download_process.setProcessEnvironment(env)
        self._download_process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self._download_process.readyReadStandardOutput.connect(self._on_download_output)
        self._download_process.finished.connect(self._on_download_process_finished)
        self._download_process.errorOccurred.connect(self._on_download_process_error)
        self._download_process.start()
        self._refresh_buttons()

    def _start_flash(self) -> None:
        assert self.state.prepared is not None
        self._flash_recoverable = False
        self._flash_log.clear()
        self._flash_status.setText("Flashing the device...")
        self._set_step(self.STEP_FLASH)
        self._worker = FlashWorker(
            self.state.config,
            self.state.prepared,
            self.state.host_blkdev_map,
        )
        self._worker.finished.connect(self._on_flash_finished)
        self._worker.failed.connect(self._on_worker_failed)
        self._worker.yes_no_requested.connect(self._on_flash_yes_no_requested, Qt.ConnectionType.BlockingQueuedConnection)
        self._worker.password_requested.connect(self._on_flash_password_requested, Qt.ConnectionType.BlockingQueuedConnection)
        self._worker.process_output.connect(self._on_flash_process_output)
        self._thread = run_worker_in_thread(self._worker)
        self._refresh_buttons()

    def _check_fastboot_devices(self) -> None:
        ok, output = ruyi_facade.check_fastboot_devices()
        self._fastboot_ok = ok
        if ok:
            self._fastboot_status.setStyleSheet("color: #1a7f37;")
            self._fastboot_status.setText("fastboot devices found:\n" + output)
        else:
            self._fastboot_status.setStyleSheet("color: #c01c28;")
            self._fastboot_status.setText(output)
        self._refresh_buttons()

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
        self._download_ok = False
        self._download_recoverable = False
        if self._versions_visited and self.state.mr is not None and self.state.combo is not None:
            self.state.pkg_atoms = ruyi_facade.combo_package_atoms(self.state.combo.entity)
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
        self.state.flash_ret = None
        self._populate_devices()
        self._set_step(self.STEP_DEVICE)

    def _terminate_download_process(self) -> None:
        proc = self._download_process
        if proc is None:
            return
        pid = proc.processId()
        if pid > 0:
            try:
                os.killpg(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            except PermissionError:
                os.kill(pid, signal.SIGTERM)
        proc.terminate()
        if not proc.waitForFinished(3000):
            if pid > 0:
                try:
                    os.killpg(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except PermissionError:
                    os.kill(pid, signal.SIGKILL)
            proc.kill()

    # --------------------------------------------------------------- slots

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
        data = bytes(self._download_process.readAllStandardOutput()).decode(errors="replace")
        if data:
            self._download_log.appendPlainText(data.rstrip("\n"))

    def _on_download_process_error(self, error) -> None:
        self._download_status.setText(f"Download process error: {error.name}.")
        self._download_ok = False
        self._download_recoverable = True
        self._refresh_buttons()

    def _on_download_process_finished(self, ret: int, _status) -> None:
        if self._download_process is not None:
            leftover = bytes(self._download_process.readAllStandardOutput()).decode(errors="replace")
            if leftover:
                self._download_log.appendPlainText(leftover.rstrip("\n"))
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
        self.state.flash_ret = ret
        self._flash_recoverable = ret != 0
        self._flash_status.setText("Flash complete." if ret == 0 else f"Flash failed (exit code {ret}).")
        self._cleanup_thread()
        self._refresh_buttons()

    def _on_worker_failed(self, msg: str) -> None:
        QMessageBox.critical(self, "Operation failed", msg)
        if self._current_step == self.STEP_DOWNLOAD:
            self._download_status.setText(f"Failed: {msg}")
        elif self._current_step == self.STEP_FLASH:
            self._flash_status.setText(f"Failed: {msg}")
            self._flash_recoverable = True
        elif self._current_step == self.STEP_DEVICE:
            self._device_status.setText(f"Failed: {msg}")
        else:
            self._welcome_status.setText(f"Failed: {msg}")
        self._cleanup_thread()
        self._refresh_buttons()

    def _on_flash_yes_no_requested(self, prompt: str, default: bool, response: dict) -> None:
        ret = QMessageBox.question(
            self,
            "Flashing needs confirmation",
            prompt,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes if default else QMessageBox.StandardButton.No,
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
        target = self._flash_log if self._current_step == self.STEP_FLASH else self._download_log
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

    def _set_step(self, step: int) -> None:
        if step < self._current_step:
            self._invalidate_downstream(step)
        self._current_step = step
        self._steps.blockSignals(True)
        self._steps.setCurrentRow(step)
        self._steps.blockSignals(False)
        self._stack.setCurrentIndex(step)
        self._refresh_summary()
        self._refresh_buttons()

    def _invalidate_downstream(self, dest_step: int) -> None:
        if dest_step < self.STEP_DONE:
            self.state.flash_ret = None
            self._flash_recoverable = False
        if dest_step < self.STEP_STORAGE:
            self.state.host_blkdev_map = {}
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
        if self._is_busy():
            self._steps.setCurrentRow(self._current_step)
            return
        if self._can_open_step(row):
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
            return self._download_ok and self.state.prepared is not None and bool(self.state.prepared.requested_host_blkdevs)
        if step == self.STEP_REVIEW:
            return self._download_ok and self.state.prepared is not None
        if step == self.STEP_FLASH:
            return self.state.flash_ret is not None or self._review_complete_if_possible()
        if step == self.STEP_DONE:
            return self.state.flash_ret is not None or (self.state.combo is not None and not self.state.pkg_atoms)
        return False

    def _review_complete_if_possible(self) -> bool:
        if self.state.prepared is None:
            return False
        return self._review_complete()

    def _refresh_summary(self) -> None:
        self._summary_device.setText(f"Device: {self.state.device.display_name if self.state.device else '-'}")
        self._summary_variant.setText(f"Variant: {self.state.variant.display_name if self.state.variant else '-'}")
        self._summary_combo.setText(f"Image: {self.state.combo.display_name if self.state.combo else '-'}")
        pkgs = ", ".join(self.state.pkg_atoms) if self.state.pkg_atoms else "-"
        self._summary_packages.setText(f"Packages: {pkgs}")
        if self.state.host_blkdev_map:
            storage = ", ".join(f"{k}: {v}" for k, v in self.state.host_blkdev_map.items())
        else:
            storage = "-"
        self._summary_storage.setText(f"Storage: {storage}")

    def _refresh_buttons(self) -> None:
        busy = self._is_busy()
        self._back_btn.setEnabled(not busy and self._current_step not in {self.STEP_WELCOME, self.STEP_DOWNLOAD, self.STEP_FLASH})
        self._next_btn.setEnabled(not busy and self._can_go_next())
        if self._current_step == self.STEP_DONE:
            self._next_btn.setText("Close")
        elif self._current_step == self.STEP_PACKAGES:
            self._next_btn.setText("Proceed")
        else:
            self._next_btn.setText("Next")
        self._update_repo_btn.setEnabled(not busy and self.state.mr is not None)
        self._cancel_download_btn.setVisible(self._current_step == self.STEP_DOWNLOAD and self._download_process is not None)
        self._cancel_download_btn.setEnabled(self._download_process is not None)
        self._download_recovery_row.setVisible(
            self._current_step == self.STEP_DOWNLOAD and self._download_recoverable and not busy
        )
        self._resume_download_btn.setEnabled(bool(self.state.pkg_atoms))
        self._reselect_versions_btn.setEnabled(self.state.combo is not None)
        self._reselect_versions_btn.setText(
            "Reselect versions" if self._versions_visited else "Reselect packages"
        )
        self._restart_btn.setEnabled(self.state.mr is not None)
        flash_recoverable = self._current_step == self.STEP_FLASH and self._flash_recoverable and not busy
        self._flash_recovery_row.setVisible(flash_recoverable)
        self._retry_flash_btn.setEnabled(self.state.prepared is not None)
        self._review_flash_btn.setEnabled(self.state.prepared is not None)
        self._restart_flash_btn.setEnabled(self.state.mr is not None)

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
        return self._thread is not None or self._download_process is not None

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
            prev = self.STEP_FLASH if self.state.pkg_atoms else self.STEP_PACKAGES
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
                source = entry.local_path or entry.remote or "(no source)"
                repo_entries.append(f"{entry.id}: {source}")
            repos_text = "\n".join(f" * {entry}" for entry in repo_entries) or " * (none)"

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
            item = QListWidgetItem("No device provisioning data is available in this repository.")
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
            combo.currentData(Qt.ItemDataRole.UserRole) for combo in self._version_combos
        ]

    def _populate_packages(self) -> None:
        self._packages_list.clear()
        if self.state.pkg_atoms:
            for atom in self.state.pkg_atoms:
                self._packages_list.addItem(atom)
        else:
            self._packages_list.addItem("No packages. The selected image only contains a post-install message.")

    def _populate_storage(self) -> None:
        assert self.state.prepared is not None
        while self._storage_layout.count():
            item = self._storage_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._storage_inputs.clear()
        self._storage_mount_warnings.clear()
        self._storage_mount_confirmations.clear()
        self._storage_error.setText("")
        disks = ruyi_facade.list_disks()
        for part in self.state.prepared.requested_host_blkdevs:
            desc = ruyi_facade.part_description(part)
            label = QLabel(f"{desc} ({part})")
            edit = QComboBox()
            edit.setEditable(True)
            edit.lineEdit().setPlaceholderText("/dev/...")
            edit.currentTextChanged.connect(self._refresh_buttons)
            for disk in disks:
                edit.addItem(disk.display_name, disk.path)
            warning = QLabel("The selected disk or one of its partitions is mounted.")
            warning.setStyleSheet("color: #c01c28;")
            warning.setVisible(False)
            confirm = QCheckBox("I understand flashing may overwrite mounted data.")
            confirm.setVisible(False)
            confirm.toggled.connect(self._refresh_buttons)
            edit.currentTextChanged.connect(
                lambda _text, e=edit, w=warning, c=confirm: self._refresh_storage_mount_warning(e, w, c)
            )
            browse = QPushButton("...")
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
            self._refresh_storage_mount_warning(edit, warning, confirm)
        self._storage_layout.addStretch()

    def _browse_storage(self, edit: QComboBox) -> None:
        dialog = QFileDialog(self, "Select disk or image file", "/dev")
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

    def _refresh_storage_mount_warning(self, edit: QComboBox, warning: QLabel, confirm: QCheckBox) -> None:
        path = self._storage_path(edit)
        mounted = bool(path and os.path.exists(path) and ruyi_facade.is_disk_or_child_mounted(path))
        if not mounted:
            confirm.setChecked(False)
        warning.setVisible(mounted)
        confirm.setVisible(mounted)
        confirm.setEnabled(mounted)
        self._refresh_buttons()

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
            if ruyi_facade.is_disk_or_child_mounted(path) and not self._storage_mount_confirmations[part].isChecked():
                return False
        return True

    def _commit_storage(self) -> bool:
        self.state.host_blkdev_map = {}
        for part, edit in self._storage_inputs.items():
            path = self._storage_path(edit)
            if not os.path.exists(path):
                self._storage_error.setText(f"'{path}' does not exist.")
                return False
            if ruyi_facade.is_disk_or_child_mounted(path) and not self._storage_mount_confirmations[part].isChecked():
                self._storage_error.setText(
                    f"'{path}' is mounted. Confirm the mounted-device warning before continuing."
                )
                return False
            self.state.host_blkdev_map[part] = path
        self._refresh_summary()
        return True

    def _populate_review(self) -> None:
        assert self.state.prepared is not None
        steps = ruyi_facade.compute_pretend_steps(self.state.prepared, self.state.host_blkdev_map)
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
            self._fastboot_status.setStyleSheet("")
            self._check_fastboot_devices()
        else:
            self._fastboot_status.setText("")
        self._proceed_cb.setChecked(False)

    def _review_complete(self) -> bool:
        assert self.state.prepared is not None
        if ruyi_facade.missing_cmds(self.state.prepared):
            return False
        if ruyi_facade.needs_fastboot_confirmation(self.state.prepared) and not self._fastboot_ok:
            return False
        return self._proceed_cb.isChecked()

    def _populate_done(self) -> None:
        if self.state.flash_ret is None and not self.state.pkg_atoms:
            self._done_label.setText("No flashing was required. See the message below for next steps.")
            self._done_label.setStyleSheet("color: #1a7f37;")
        elif self.state.flash_ret == 0:
            self._done_label.setText("It seems the flashing has finished without errors. Happy hacking!")
            self._done_label.setStyleSheet("color: #1a7f37;")
        else:
            self._done_label.setText(f"Flashing failed (exit code {self.state.flash_ret}). Check the device right now.")
            self._done_label.setStyleSheet("color: #c01c28;")

        msg = ""
        if self.state.combo is not None and self.state.mr is not None:
            msg = ruyi_facade.get_postinst_msg(
                self.state.mr,
                self.state.combo.entity,
                self.state.config.lang_code,
            ) or ""
        self.state.postinst_msg = msg or None
        self._postinst_label.setText(msg)
        self._postinst_label.setVisible(bool(msg))
