"""Qt repository management backed by ruyi's imported Python APIs."""

from __future__ import annotations

import os
import re
import signal
import sys
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QProcess, QProcessEnvironment, QTimer, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from . import repo_manager

_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_REPO_ROLE = Qt.ItemDataRole.UserRole


class _RepoUpdateDialog(QDialog):
    """Show imported ruyi update output and request cancellation."""

    cancel_requested = Signal()

    def __init__(self, repo_id: str, parent=None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setWindowTitle("Repository update")
        self.resize(760, 440)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"Running: ruyi update --repo {repo_id}"))
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        layout.addWidget(self.log, 1)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(self.reject)
        row = QHBoxLayout()
        row.addStretch()
        row.addWidget(self.cancel_button)
        layout.addLayout(row)

    def append_output(self, text: str) -> None:
        self.log.moveCursor(self.log.textCursor().MoveOperation.End)
        self.log.insertPlainText(_ANSI_RE.sub("", text))
        self.log.ensureCursorVisible()

    def complete(self, success: bool, message: str = "") -> None:
        if message:
            self.append_output(f"\n{message}\n")
        self.cancel_button.setText("Close")
        self.cancel_button.setEnabled(True)
        self.cancel_button.clicked.disconnect()
        self.cancel_button.clicked.connect(self.accept)
        self.setWindowTitle(
            "Repository update complete" if success else "Repository update failed"
        )

    def reject(self) -> None:  # noqa: D401
        if self.cancel_button.text() == "Cancel":
            self.cancel_requested.emit()
            return
        super().reject()


class _RepoSourceDialog(QDialog):
    """Edit repository fields supported by ruyi's repository implementation."""

    def __init__(
        self,
        title: str,
        *,
        name: str = "",
        remote: str = "",
        local: str = "",
        branch: str = "",
        priority: int = 10,
        source_options: tuple[repo_manager.RepoSource, ...] = (),
        name_enabled: bool = True,
        source_enabled: bool = True,
        priority_enabled: bool = True,
        allow_empty_source: bool = False,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(560, 300)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self._allow_empty_source = allow_empty_source
        self._custom_source_enabled = source_enabled
        self._custom_remote = remote
        self._custom_branch = branch
        self._source_index = -1
        self.name_edit = QLineEdit(name)
        self.remote_edit = QLineEdit(remote)
        self.local_edit = QLineEdit(local)
        self.branch_edit = QLineEdit(branch)
        self.priority_edit = QLineEdit(str(priority))
        self.priority_edit.setPlaceholderText("Integer")
        self.name_edit.setEnabled(name_enabled)
        self.local_edit.setEnabled(False)
        self.priority_edit.setEnabled(priority_enabled)
        form.addRow("Name", self.name_edit)
        self.source_combo = QComboBox()
        initial_source = repo_manager.RepoSource(
            remote or None,
            local or None,
            branch or None,
        )
        selected_index: int | None = None
        for index, option in enumerate(source_options):
            label = repo_manager.source_label(option)
            if option.branch:
                label += f" [{option.branch}]"
            self.source_combo.addItem(label or f"Preset {index + 1}", option)
            if repo_manager.source_matches_preset(initial_source, option):
                selected_index = index
        custom_index = self.source_combo.count()
        self.source_combo.addItem("Custom", None)
        self.source_combo.currentIndexChanged.connect(self._select_source_option)
        self.source_combo.setCurrentIndex(
            custom_index if selected_index is None else selected_index
        )
        self._select_source_option(self.source_combo.currentIndex())
        form.addRow("Source preset", self.source_combo)
        form.addRow("Remote URL", self.remote_edit)
        form.addRow("Local path", self.local_edit)
        form.addRow("Branch", self.branch_edit)
        form.addRow("Priority", self.priority_edit)
        layout.addLayout(form)
        self.help_label = QLabel(
            "Use a remote URL, an absolute local path, or both. Repository ID and "
            "name come from the preset list for additional repositories."
        )
        self.help_label.setWordWrap(True)
        layout.addWidget(self.help_label)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _select_source_option(self, index: int) -> None:
        if (
            self._source_index >= 0
            and self.source_combo.itemData(self._source_index) is None
        ):
            self._custom_remote = self.remote_edit.text()
            self._custom_branch = self.branch_edit.text()
        option = self.source_combo.itemData(index)
        is_preset = isinstance(option, repo_manager.RepoSource)
        if is_preset:
            self.remote_edit.setText(option.remote or "")
            self.branch_edit.setText(option.branch or "")
        else:
            self.remote_edit.setText(self._custom_remote)
            self.branch_edit.setText(self._custom_branch)
        self.remote_edit.setEnabled(self._custom_source_enabled and not is_preset)
        self.branch_edit.setEnabled(self._custom_source_enabled and not is_preset)
        self._source_index = index

    def values(self) -> tuple[repo_manager.RepoSource, int | None, str]:
        try:
            priority = int(self.priority_edit.text().strip())
        except ValueError:
            priority = None
        return (
            repo_manager.RepoSource(
                self.remote_edit.text().strip() or None,
                self.local_edit.text().strip() or None,
                self.branch_edit.text().strip() or None,
            ),
            priority,
            self.name_edit.text().strip(),
        )

    def accept(self) -> None:  # noqa: D401
        source, priority, _name = self.values()
        if priority is None:
            QMessageBox.warning(
                self, "Invalid priority", "Priority must be an integer."
            )
            return
        if (
            source.remote is None
            and source.local is None
            and not self._allow_empty_source
        ):
            QMessageBox.warning(
                self, "Missing source", "Enter a remote URL or local path."
            )
            return
        if source.local is not None and not Path(source.local).is_absolute():
            QMessageBox.warning(
                self, "Invalid local path", "Local path must be absolute."
            )
            return
        super().accept()


class RepoManagementTab(QWidget):
    """Manage user-local repositories without invoking a managed ruyi binary."""

    configuration_changed = Signal(str)
    repository_updated = Signal(str)
    busy_changed = Signal(bool)
    provision_update_finished = Signal(bool, str)

    def __init__(
        self,
        *,
        config_path: Path | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._config_path = (
            repo_manager.user_config_path()
            if config_path is None
            else Path(config_path)
        )
        self._repos: tuple[repo_manager.ConfiguredRepo, ...] = ()
        self._process: QProcess | None = None
        self._updating_repo_id: str | None = None
        self._update_success_message = ""
        self._provision_update = False
        self._provision_update_succeeded = False
        self._cancel_requested = False
        self._process_output: list[str] = []
        self._external_busy = False
        self._update_dialog: _RepoUpdateDialog | None = None
        self._kill_timer = QTimer(self)
        self._kill_timer.setSingleShot(True)
        self._kill_timer.setInterval(1500)
        self._kill_timer.timeout.connect(self._force_kill_process)
        self._build_ui()
        self.reload()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(10)
        title = QLabel("<b>Ruyi Repository Management</b>")
        title.setObjectName("pageTitle")
        root.addWidget(title)
        description = QLabel(
            "Choose a preset repository on the left to add it to the local configuration. "
            "The right side shows repositories in their configuration order."
        )
        description.setWordWrap(True)
        root.addWidget(description)

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.addWidget(self._build_presets_panel())
        self.splitter.addWidget(self._build_configured_panel())
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 2)
        self.splitter.setSizes([320, 640])
        root.addWidget(self.splitter, 1)
        self.status = QLabel("")
        self.status.setObjectName("repoStatus")
        self.status.setWordWrap(True)
        root.addWidget(self.status)

    def _build_presets_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 6, 0)
        layout.addWidget(QLabel("<b>Preset repositories</b>"))
        content = QHBoxLayout()
        self.preset_table = QTableWidget(0, 2)
        self._configure_table(
            self.preset_table,
            ["ID", "Name"],
            stretch_column=1,
        )
        self.preset_table.setAccessibleName("Preset repositories")
        self.preset_table.itemSelectionChanged.connect(self._refresh_buttons)
        content.addWidget(self.preset_table, 1)
        buttons = QVBoxLayout()
        buttons.addStretch()
        self.add_button = QPushButton("Add")
        self.add_button.clicked.connect(self._add_selected_preset)
        buttons.addWidget(self.add_button)
        buttons.addStretch()
        content.addLayout(buttons)
        layout.addLayout(content, 1)
        return panel

    def _build_configured_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(6, 0, 0, 0)
        layout.addWidget(QLabel("<b>Configured repositories</b>"))
        content = QHBoxLayout()
        self.configured_table = QTableWidget(0, 6)
        self._configure_table(
            self.configured_table,
            ["ID", "Name", "Source", "Branch", "Priority", "State"],
            stretch_column=2,
        )
        self.configured_table.setAccessibleName("Configured repositories")
        self.configured_table.itemSelectionChanged.connect(self._refresh_buttons)
        content.addWidget(self.configured_table, 1)
        buttons = QVBoxLayout()
        buttons.addStretch()
        self.refresh_button = QPushButton("Refresh")
        self.edit_button = QPushButton("Edit")
        self.remove_button = QPushButton("Remove")
        self.toggle_button = QPushButton("Enable")
        self.update_button = QPushButton("Update")
        self.refresh_button.clicked.connect(self.reload)
        self.edit_button.clicked.connect(self._edit_selected)
        self.remove_button.clicked.connect(self._remove_selected)
        self.toggle_button.clicked.connect(self._toggle_selected)
        self.update_button.clicked.connect(self._update_selected)
        for button in (
            self.refresh_button,
            self.edit_button,
            self.remove_button,
            self.toggle_button,
            self.update_button,
        ):
            buttons.addWidget(button)
        buttons.addStretch()
        content.addLayout(buttons)
        layout.addLayout(content, 1)
        return panel

    @staticmethod
    def _configure_table(
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
        table.setSortingEnabled(False)
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        table.horizontalHeader().setSectionResizeMode(
            stretch_column, QHeaderView.ResizeMode.Stretch
        )

    @property
    def is_busy(self) -> bool:
        return self._process is not None

    @property
    def can_cancel(self) -> bool:
        return self.is_busy

    @property
    def default_repo_active(self) -> bool:
        return any(repo.is_default and repo.active for repo in self._repos)

    @property
    def provision_update_succeeded(self) -> bool:
        return self._provision_update_succeeded

    @property
    def can_start_provision_update(self) -> bool:
        return not (
            self._provision_update_succeeded or self.is_busy or self._external_busy
        )

    def set_external_busy(self, busy: bool) -> None:
        self._external_busy = busy
        self._refresh_buttons()

    def start_provision_update(self) -> None:
        """Update the active default repository before provisioning starts."""
        if not self.can_start_provision_update:
            return
        default = next((repo for repo in self._repos if repo.is_default), None)
        if default is None or not default.active:
            self.provision_update_finished.emit(
                False,
                "Enable the ruyisdk repository in Repo Management to load device metadata.",
            )
            return
        self._provision_update = True
        self._start_update(
            default.id,
            "RuyiSDK metadata repository is ready.",
        )

    def reload(self) -> None:
        if self.is_busy:
            return
        try:
            self._repos = repo_manager.read_configured_repos(self._config_path)
        except repo_manager.RepoManagerError as exc:
            self._repos = ()
            self._set_status(f"Failed to read repository configuration: {exc}", "error")
        else:
            self._populate_tables()
            self._set_status("Repository configuration loaded.", None)
        self._refresh_buttons()

    def _populate_tables(self) -> None:
        configured_ids = {repo.id for repo in self._repos}
        self.preset_table.setRowCount(0)
        for preset in repo_manager.PRESET_REPOS:
            row = self.preset_table.rowCount()
            self.preset_table.insertRow(row)
            values = (preset.id, preset.name)
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 0:
                    item.setData(_REPO_ROLE, preset)
                tooltip = value
                if preset.id in configured_ids:
                    tooltip += "\nAlready present in the local configuration."
                item.setToolTip(tooltip)
                self.preset_table.setItem(row, column, item)

        self.configured_table.setRowCount(0)
        for repo in self._repos:
            row = self.configured_table.rowCount()
            self.configured_table.insertRow(row)
            source = repo_manager.source_label(repo)
            values = (
                repo.id,
                repo.name,
                source,
                repo.branch or "",
                str(repo.priority),
                "Active" if repo.active else "Disabled",
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                if column == 0:
                    item.setData(_REPO_ROLE, repo)
                self.configured_table.setItem(row, column, item)
            if repo.is_default:
                for column in range(self.configured_table.columnCount()):
                    self.configured_table.item(row, column).setToolTip(
                        f"{values[column]}\nThe default ruyisdk repository cannot be removed."
                    )

    def _selected_preset(self) -> repo_manager.RepoPreset | None:
        row = self.preset_table.currentRow()
        item = self.preset_table.item(row, 0) if row >= 0 else None
        value = item.data(_REPO_ROLE) if item is not None else None
        return value if isinstance(value, repo_manager.RepoPreset) else None

    def _selected_repo(self) -> repo_manager.ConfiguredRepo | None:
        row = self.configured_table.currentRow()
        item = self.configured_table.item(row, 0) if row >= 0 else None
        value = item.data(_REPO_ROLE) if item is not None else None
        return value if isinstance(value, repo_manager.ConfiguredRepo) else None

    def _refresh_buttons(self) -> None:
        busy = self.is_busy or self._external_busy
        preset = self._selected_preset()
        repo = self._selected_repo()
        configured_ids = {item.id for item in self._repos}
        self.preset_table.setEnabled(not busy)
        self.configured_table.setEnabled(not busy)
        self.add_button.setEnabled(
            not busy and preset is not None and preset.id not in configured_ids
        )
        self.refresh_button.setEnabled(not busy)
        self.edit_button.setEnabled(not busy and repo is not None)
        self.remove_button.setEnabled(
            not busy and repo is not None and not repo.is_default
        )
        self.toggle_button.setEnabled(not busy and repo is not None)
        self.toggle_button.setText(
            "Disable" if repo is not None and repo.active else "Enable"
        )
        self.update_button.setEnabled(not busy and repo is not None and repo.active)

    def _add_selected_preset(self) -> None:
        preset = self._selected_preset()
        if preset is None or self.is_busy or self._external_busy:
            return
        source = preset.sources[0] if preset.sources else repo_manager.RepoSource()
        dialog = _RepoSourceDialog(
            f"Add {preset.name}",
            name=preset.name,
            remote=source.remote or "",
            local=source.local or "",
            branch=source.branch or "",
            priority=10,
            source_options=preset.sources,
            name_enabled=False,
            parent=self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        selected, priority, _name = dialog.values()
        assert priority is not None
        self._apply_mutation(
            preset.id,
            f"Added {preset.id} as a disabled repository.",
            lambda: repo_manager.add_repo(
                self._config_path, preset, selected, priority
            ),
        )

    def _edit_selected(self) -> None:
        repo = self._selected_repo()
        if repo is None or self.is_busy or self._external_busy:
            return
        if repo.is_default:
            configured = repo.configured_source or repo_manager.RepoSource()
            dialog = _RepoSourceDialog(
                "Edit ruyisdk repository",
                remote=configured.remote or "",
                local=configured.local or "",
                branch=configured.branch or "",
                priority=0,
                source_options=repo_manager.DEFAULT_REPO_SOURCES,
                name_enabled=False,
                priority_enabled=False,
                allow_empty_source=True,
                parent=self,
            )
            dialog.help_label.setText(
                "Empty fields remove user overrides. Ruyi then uses its built-in remote "
                "and main branch; local is optional. ID, name, and priority are fixed."
            )
            dialog.remote_edit.setPlaceholderText(repo.remote or "")
            dialog.branch_edit.setPlaceholderText(repo.branch or "")
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            source, _priority, _name = dialog.values()
            self._apply_mutation(
                repo.id,
                "Updated the default repository configuration.",
                lambda: repo_manager.edit_default_repo(self._config_path, repo, source),
                require_change=True,
            )
            return

        configured = repo.configured_source or repo_manager.RepoSource(
            repo.remote,
            repo.local,
            repo.branch,
        )
        dialog = _RepoSourceDialog(
            f"Edit {repo.id}",
            name=repo.name,
            remote=configured.remote or "",
            local=configured.local or "",
            branch=configured.branch or "",
            priority=repo.priority,
            source_options=tuple(
                option
                for preset in repo_manager.PRESET_REPOS
                if preset.id == repo.id
                for option in preset.sources
            ),
            name_enabled=False,
            parent=self,
        )
        dialog.help_label.setText(
            "ID and name come from the preset. Remote, local path, branch, and "
            "priority are stored through ruyi's configuration API. Local path is "
            "read-only."
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        _source, priority, _name = dialog.values()
        assert priority is not None
        if priority == repo.priority and _source == configured:
            return
        self._apply_mutation(
            repo.id,
            f"Updated {repo.id}.",
            lambda: repo_manager.edit_repo(self._config_path, repo, _source, priority),
        )

    def _remove_selected(self) -> None:
        repo = self._selected_repo()
        if repo is None or repo.is_default or self.is_busy or self._external_busy:
            return
        answer = QMessageBox.question(
            self,
            "Remove repository",
            f"Remove '{repo.id}' from the local ruyi configuration? Cached data will be kept.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._apply_mutation(
            repo.id,
            f"Removed {repo.id}.",
            lambda: repo_manager.remove_repo(self._config_path, repo),
        )

    def _toggle_selected(self) -> None:
        repo = self._selected_repo()
        if repo is None or self.is_busy or self._external_busy:
            return
        enabled = not repo.active
        if not self._apply_mutation(
            repo.id,
            f"{'Enabled' if enabled else 'Disabled'} {repo.id}.",
            lambda: repo_manager.set_enabled(self._config_path, repo, enabled),
        ):
            return
        if enabled:
            self._start_update(repo.id, f"Enabled and updated {repo.id}.")

    def _update_selected(self) -> None:
        repo = self._selected_repo()
        if repo is None or not repo.active or self.is_busy or self._external_busy:
            return
        self._start_update(repo.id, f"Updated {repo.id}.")

    def _apply_mutation(
        self,
        repo_id: str,
        success_message: str,
        operation: Callable[[], object],
        *,
        require_change: bool = False,
    ) -> bool:
        try:
            result = operation()
        except repo_manager.RepoManagerError as exc:
            self._set_status(str(exc), "error")
            QMessageBox.critical(self, "Repository operation failed", str(exc))
            return False
        if require_change and result is False:
            return False
        if repo_id == repo_manager.DEFAULT_REPO_ID:
            self._provision_update_succeeded = False
        self.reload()
        self.configuration_changed.emit(repo_id)
        self._set_status(success_message, None)
        return True

    def _start_update(self, repo_id: str, success_message: str) -> None:
        if self.is_busy or self._external_busy:
            return
        self._updating_repo_id = repo_id
        self._update_success_message = success_message
        self._cancel_requested = False
        self._process_output = []
        dialog = _RepoUpdateDialog(repo_id, self)
        self._update_dialog = dialog
        dialog.cancel_requested.connect(self._cancel_process)

        process = QProcess(self)
        self._process = process
        process.setProgram(sys.executable)
        process.setArguments(
            [
                "-m",
                "oh_my_ruyi.repo_update_child",
                os.fspath(self._config_path),
                repo_id,
            ]
        )
        env = QProcessEnvironment.systemEnvironment()
        env.insert("PYTHONUNBUFFERED", "1")
        env.insert("RUYI_TELEMETRY_OPTOUT", "1")
        process.setProcessEnvironment(env)
        process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        process.readyReadStandardOutput.connect(self._read_process_output)
        process.finished.connect(
            lambda code, status, p=process: self._on_process_finished(p, code, status)
        )
        process.errorOccurred.connect(
            lambda error, p=process: self._on_process_error(p, error)
        )
        self._set_status(f"Updating repository {repo_id}...", None)
        self.busy_changed.emit(True)
        self._refresh_buttons()
        process.start()
        dialog.open()

    def _read_process_output(self) -> None:
        if self._process is None:
            return
        text = bytes(self._process.readAllStandardOutput()).decode(errors="replace")
        plain = _ANSI_RE.sub("", text)
        self._process_output.append(plain)
        if self._update_dialog is not None:
            self._update_dialog.append_output(text)

    def _on_process_finished(self, process: QProcess, code: int, _status) -> None:
        if process is not self._process:
            return
        self._kill_timer.stop()
        self._read_process_output()
        self._process = None
        process.deleteLater()
        if self._cancel_requested:
            self._finish_update(False, "Repository update cancelled.")
            return
        if code != 0:
            output = "".join(self._process_output).strip()
            self._finish_update(
                False, output or f"ruyi update exited with code {code}."
            )
            return
        self._finish_update(True, self._update_success_message)

    def _on_process_error(
        self, process: QProcess, error: QProcess.ProcessError
    ) -> None:
        if process is not self._process:
            return
        if error == QProcess.ProcessError.FailedToStart:
            self._finish_update(False, "Failed to start the repository update process.")
        # Terminating an update reports Crashed before finished. The finished
        # handler owns cancellation classification and final output collection.

    def _cancel_process(self) -> None:
        process = self._process
        if process is None or self._cancel_requested:
            return
        self._cancel_requested = True
        if self._update_dialog is not None:
            self._update_dialog.cancel_button.setEnabled(False)
        pid = process.processId()
        if pid > 0 and hasattr(os, "killpg"):
            try:
                os.killpg(pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                process.terminate()
        else:
            process.terminate()
        self._kill_timer.start()

    def _force_kill_process(self) -> None:
        process = self._process
        if process is None:
            return
        pid = process.processId()
        if pid > 0 and hasattr(os, "killpg"):
            try:
                os.killpg(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        process.kill()

    def _finish_update(self, success: bool, message: str) -> None:
        self._kill_timer.stop()
        process = self._process
        self._process = None
        if process is not None:
            process.deleteLater()
        repo_id = self._updating_repo_id
        provision_update = self._provision_update
        self._updating_repo_id = None
        self._provision_update = False
        self._cancel_requested = False
        self._process_output = []
        dialog = self._update_dialog
        self._update_dialog = None
        self.busy_changed.emit(False)
        self._refresh_buttons()
        self._set_status(message, None if success else "error")
        if dialog is not None:
            dialog.complete(success, message)
        if success and (
            repo_id == repo_manager.DEFAULT_REPO_ID or provision_update
        ):
            self._provision_update_succeeded = True
        if success and repo_id is not None and not provision_update:
            self.repository_updated.emit(repo_id)
        if provision_update:
            self.provision_update_finished.emit(success, message)

    def _set_status(self, text: str, kind: str | None) -> None:
        self.status.setText(text)
        self.status.setProperty("statusKind", kind or "")
        self.status.style().unpolish(self.status)
        self.status.style().polish(self.status)
        self.status.update()


__all__ = ["RepoManagementTab"]
