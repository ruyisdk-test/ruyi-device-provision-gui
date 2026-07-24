from __future__ import annotations

from typing import Callable, Any

from PySide6.QtCore import QObject, QThread, Qt

from .workers import _BaseWorker


class WorkerTaskRunner(QObject):
    """
    A unified manager for running _BaseWorker instances in QThreads.
    This encapsulates the boilerplate of connecting signals, starting the thread,
    and cleaning up (deleteLater) upon finish, failure, or cancellation.
    """

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        # Keep references to prevent garbage collection while running
        self._active_threads: list[QThread] = []
        self._active_workers: list[_BaseWorker] = []

    def run_worker(
        self,
        worker: _BaseWorker,
        on_finished: Callable[[Any], None] | None = None,
        on_failed: Callable[[str], None] | None = None,
        on_cancelled: Callable[[], None] | None = None,
        extra_connections: list[tuple[Any, Callable]] | None = None,
    ) -> _BaseWorker:
        """
        Starts the worker in a new thread.
        Connects basic signals (finished, failed) and any extra signals.
        Returns the worker instance (e.g. for cancellation).
        """
        thread = QThread(self)
        worker.moveToThread(thread)

        # Basic signal routing
        if on_finished:
            worker.finished.connect(on_finished)
        if on_failed:
            worker.failed.connect(on_failed)

        # Handle cancellation if the worker supports it (e.g., VersionDownloadWorker, FlashWorker)
        if on_cancelled and hasattr(worker, "cancelled"):
            getattr(worker, "cancelled").connect(on_cancelled)

        # Extra signal connections (e.g. progress, password_requested)
        if extra_connections:
            for signal_obj, slot in extra_connections:
                signal_obj.connect(slot)

        # Cleanup connections
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        if hasattr(worker, "cancelled"):
            getattr(worker, "cancelled").connect(thread.quit)

        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)

        # Cleanup internal references when thread finishes
        def _on_thread_finished() -> None:
            if thread in self._active_threads:
                self._active_threads.remove(thread)
            if worker in self._active_workers:
                self._active_workers.remove(worker)

        thread.finished.connect(_on_thread_finished)

        self._active_threads.append(thread)
        self._active_workers.append(worker)

        thread.started.connect(worker.run, type=Qt.ConnectionType.QueuedConnection)
        thread.start()

        return worker

    def request_cancel_all(self) -> None:
        """Attempt to cancel all active workers that support cancellation."""
        for worker in self._active_workers:
            if hasattr(worker, "request_cancel"):
                getattr(worker, "request_cancel")()

    def safe_stop_all(self) -> None:
        """Forcefully quit and terminate all running threads if necessary."""
        for thread in list(self._active_threads):
            if thread.isRunning():
                thread.quit()
                if not thread.wait(3000):
                    thread.terminate()
                    thread.wait(1000)
        self._active_threads.clear()
        self._active_workers.clear()
