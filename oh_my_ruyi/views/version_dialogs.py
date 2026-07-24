"""Version download dialog and version table sorting components."""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QProgressBar,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import version_manager
from ..i18n import _, translate_widget_tree
from ..rich_output import RichTextView


class VersionTableItem(QTableWidgetItem):
    """Sort version cells by their semantic components instead of text."""

    def __init__(self, version: str) -> None:
        super().__init__(version)
        self._sort_key = version_manager.version_sort_key(version)

    def __lt__(self, other: QTableWidgetItem) -> bool:
        if isinstance(other, VersionTableItem):
            return self._sort_key < other._sort_key
        return super().__lt__(other)


class VersionDownloadDialog(QDialog):
    """Select a release URL, then show that download's progress in place."""

    download_requested = Signal(str)
    cancel_requested = Signal()

    def __init__(
        self,
        release: version_manager.RuyiRelease,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(_("Download ruyi {version}", version=release.version))
        self.setModal(True)
        self.setMinimumWidth(560)
        self._downloading = False
        self._cancelling = False

        layout = QVBoxLayout(self)
        prompt = QLabel("Select a download URL:")
        self._url_combo = QComboBox()
        self._url_combo.setAccessibleName("Ruyi download URL")
        self._url_combo.addItems(release.download_urls)
        self._url_combo.currentTextChanged.connect(self._url_combo.setToolTip)
        self._url_combo.setToolTip(self._url_combo.currentText())
        prompt.setBuddy(self._url_combo)
        layout.addWidget(prompt)
        layout.addWidget(self._url_combo)

        self._progress = QProgressBar()
        self._progress.setVisible(False)
        self._status = QLabel("")
        self._status.setWordWrap(True)
        self._output = RichTextView()
        self._output.setMaximumHeight(100)
        self._output.hide()
        layout.addWidget(self._progress)
        layout.addWidget(self._status)
        layout.addWidget(self._output)

        self._buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self._download_button = self._buttons.addButton(
            "Download",
            QDialogButtonBox.ButtonRole.AcceptRole,
        )
        self._cancel_button = self._buttons.button(
            QDialogButtonBox.StandardButton.Cancel
        )
        self._download_button.clicked.connect(self._request_download)
        self._buttons.rejected.connect(self.reject)
        layout.addWidget(self._buttons)
        translate_widget_tree(self)

    def _request_download(self) -> None:
        url = self._url_combo.currentText()
        if self._downloading or not url:
            return
        self._downloading = True
        self._url_combo.setEnabled(False)
        self._download_button.setEnabled(False)
        self._cancel_button.setEnabled(True)
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        self._progress.setFormat(_("Connecting..."))
        self._progress.setVisible(True)
        self._output.clear()
        self._output.hide()
        self._status.setText(_("Downloading the selected ruyi release..."))
        self._status.setToolTip(url)
        self._set_status_kind(None)
        self.download_requested.emit(url)

    def update_progress(self, downloaded: int, total: int) -> None:
        if total > 0:
            percent = min(100, downloaded * 100 // total)
            self._progress.setRange(0, 100)
            self._progress.setValue(percent)
            self._progress.setFormat(
                _(
                    "%p% ({downloaded} / {total})",
                    downloaded=self._format_bytes(downloaded),
                    total=self._format_bytes(total),
                )
            )
        else:
            self._progress.setRange(0, 100)
            self._progress.setValue(0)
            self._progress.setFormat(
                _("{size} downloaded", size=self._format_bytes(downloaded))
            )

    def show_failure(self, message: str) -> None:
        self._downloading = False
        self._cancelling = False
        self._url_combo.setEnabled(True)
        self._download_button.setText(_("Retry"))
        self._download_button.setEnabled(True)
        self._cancel_button.setEnabled(True)
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        self._progress.setFormat(_("Download failed"))
        self._status.setText(_("Download failed. See output below."))
        self._status.setToolTip("")
        self._output.append_plain_status(message)
        self._output.show()
        self._set_status_kind("error")

    def complete(self) -> None:
        self._downloading = False
        self._cancelling = False
        self.accept()

    def complete_cancellation(self) -> None:
        self._downloading = False
        self._cancelling = False
        super().reject()

    def reject(self) -> None:
        if self._downloading:
            self._request_cancel()
            return
        super().reject()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt override
        if self._downloading:
            self._request_cancel()
            event.accept()
            return
        super().closeEvent(event)

    def _request_cancel(self) -> None:
        if self._cancelling:
            return
        self._cancelling = True
        self._cancel_button.setEnabled(False)
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        self._progress.setFormat(_("Cancelling..."))
        self._status.setText(_("Stopping the download and removing temporary data..."))
        self._set_status_kind(None)
        self.cancel_requested.emit()
        super().reject()

    def _set_status_kind(self, kind: str | None) -> None:
        self._status.setText(_(self._status.text()))
        self._status.setProperty("statusKind", kind or "")
        self._status.style().unpolish(self._status)
        self._status.style().polish(self._status)

    @staticmethod
    def _format_bytes(size: int) -> str:
        value = float(size)
        for unit in ("B", "KiB", "MiB", "GiB"):
            if value < 1024 or unit == "GiB":
                return f"{int(value)} {unit}" if unit == "B" else f"{value:.1f} {unit}"
            value /= 1024
        raise AssertionError("unreachable")
