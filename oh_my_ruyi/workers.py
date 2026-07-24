"""Background workers for the provisioning flow.

Three operations must not block the UI thread:

* cloning / updating the ruyi metadata repo on first launch,
* downloading and installing image packages,
* running the flashing strategies (which shell out to ``sudo dd`` / ``fastboot``).

Each operation is wrapped in a :class:`QObject` worker that is moved to a
:class:`QThread`. Results are reported via Qt signals. All workers log into
the same :class:`~oh_my_ruyi.qt_logger.QtRuyiLogger` that
``GlobalConfig`` was constructed with, so the main window's log view sees
every line in real time.
"""

from __future__ import annotations

import json
import os
import platform
import selectors
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import BinaryIO

from PySide6.QtCore import QObject, QThread, Qt, Signal, Slot

from ruyi.config import GlobalConfig
from ruyi.ruyipkg.composite_repo import CompositeRepo
from ruyi.ruyipkg.pkg_manifest import PartitionKind, PartitionMapDecl

from . import host_storage, ruyi_facade, version_manager
from .i18n import _, format_exception_message
from .ruyi_facade import PreparedProvision


def _set_terminal_target(config: GlobalConfig | None, target: str) -> None:
    logger = getattr(config, "logger", None)
    setter = getattr(logger, "set_terminal_target", None)
    if callable(setter):
        setter(target)


class _BaseWorker(QObject):
    """Common signal surface for every worker."""

    finished = Signal(object)  # worker-specific result type
    failed = Signal(str)  # error message

    def _fail(self, exc: Exception) -> None:
        self.failed.emit(format_exception_message(exc))


class RepoInitWorker(_BaseWorker):
    """Ensure the ruyi metadata repo is present and up to date."""

    def __init__(self, config: GlobalConfig) -> None:
        super().__init__()
        self._config = config

    @Slot()
    def run(self) -> None:
        _set_terminal_target(self._config, "welcome")
        try:
            mr = ruyi_facade.ensure_repo(self._config)
            self.finished.emit(mr)
        except Exception as exc:  # noqa: BLE001 - surface to UI
            self._fail(exc)


class RepoSyncWorker(_BaseWorker):
    """Sync metadata repositories, equivalent to the repo part of ``ruyi update``."""

    def __init__(self, config: GlobalConfig, mr: CompositeRepo) -> None:
        super().__init__()
        self._config = config
        self._mr = mr

    @Slot()
    def run(self) -> None:
        _set_terminal_target(self._config, "device")
        try:
            mr = ruyi_facade.sync_repo(self._config, self._mr)
            self.finished.emit(mr)
        except Exception as exc:  # noqa: BLE001
            self._fail(exc)


class StorageDiscoveryWorker(_BaseWorker):
    """Discover host disks without blocking the GUI event loop."""

    @Slot()
    def run(self) -> None:
        try:
            self.finished.emit(host_storage.list_disks())
        except Exception as exc:  # noqa: BLE001
            self._fail(exc)


class VersionCatalogWorker(_BaseWorker):
    """Fetch the latest stable and testing package manager releases."""

    @Slot()
    def run(self) -> None:
        try:
            self.finished.emit(version_manager.fetch_release_catalog())
        except Exception as exc:  # noqa: BLE001
            self._fail(exc)


class VersionDownloadWorker(_BaseWorker):
    """Download one standalone ruyi binary into the user's version directory."""

    progress = Signal(int, int)
    cancelled = Signal()

    def __init__(
        self,
        release: version_manager.RuyiRelease,
        directory: Path,
        download_url: str,
    ) -> None:
        super().__init__()
        self._release = release
        self._directory = directory
        self._download_url = download_url
        self._cancel_requested = threading.Event()
        self._response_lock = threading.Lock()
        self._response: BinaryIO | None = None

    def request_cancel(self) -> None:
        self._cancel_requested.set()
        with self._response_lock:
            response = self._response
        if response is not None:
            threading.Thread(
                target=self._close_response,
                args=(response,),
                daemon=True,
            ).start()

    @staticmethod
    def _close_response(response: BinaryIO) -> None:
        try:
            response.close()
        except Exception:  # noqa: BLE001 - cancellation must not block the UI
            pass

    def _set_response(self, response: BinaryIO | None) -> None:
        with self._response_lock:
            self._response = response
        if response is not None and self._cancel_requested.is_set():
            self._close_response(response)

    @Slot()
    def run(self) -> None:
        try:
            self.finished.emit(
                version_manager.download_release(
                    self._release,
                    self._directory,
                    download_url=self._download_url,
                    progress=self.progress.emit,
                    cancelled=self._cancel_requested.is_set,
                    response_changed=self._set_response,
                )
            )
        except version_manager.DownloadCancelledError:
            self.cancelled.emit()
        except Exception as exc:  # noqa: BLE001
            self._fail(exc)


class VersionActivationWorker(_BaseWorker):
    """Activate a downloaded binary, requesting sudo credentials when needed."""

    password_requested = Signal(str, object)

    def __init__(
        self,
        binary: Path,
        directory: Path,
        link: Path,
        *,
        backup_unmanaged: bool,
    ) -> None:
        super().__init__()
        self._binary = binary
        self._directory = directory
        self._link = link
        self._backup_unmanaged = backup_unmanaged

    @Slot()
    def run(self) -> None:
        try:
            if os.access(self._link.parent, os.W_OK):
                result = version_manager.activate_version(
                    self._binary,
                    self._directory,
                    link=self._link,
                    backup_unmanaged=self._backup_unmanaged,
                )
            else:
                result = self._activate_with_sudo()
            self.finished.emit(result)
        except Exception as exc:  # noqa: BLE001
            self._fail(exc)

    def _activate_with_sudo(self) -> version_manager.ActivationResult:
        if platform.system() == "Windows":
            raise RuntimeError(
                "activating /usr/local/bin/ruyi is unsupported on Windows"
            )

        response: dict[str, str | None] = {"password": None}
        self.password_requested.emit(
            _(
                "sudo password is required to update {path}.",
                path=self._link,
            ),
            response,
        )
        password = response["password"]
        if password is None:
            raise RuntimeError("activation was cancelled")

        command = [
            "sudo",
            "-S",
            "-p",
            "",
            sys.executable,
            "-m",
            "oh_my_ruyi.version_manager",
            "activate",
            os.fspath(self._binary),
            os.fspath(self._directory),
            os.fspath(self._link),
        ]
        if self._backup_unmanaged:
            command.append("--backup-unmanaged")
        completed = subprocess.run(
            command,
            input=password + "\n",
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            message = completed.stderr.strip() or completed.stdout.strip()
            raise RuntimeError(
                message or f"sudo exited with code {completed.returncode}"
            )
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError("activation helper returned invalid output") from exc
        backup = payload.get("backup_path")
        return version_manager.ActivationResult(
            version_manager.read_activation_state(self._link, self._directory),
            Path(backup) if isinstance(backup, str) else None,
        )


class VersionDeleteWorker(_BaseWorker):
    """Delete one inactive binary from the user's version directory."""

    def __init__(self, binary: Path, directory: Path, link: Path) -> None:
        super().__init__()
        self._binary = binary
        self._directory = directory
        self._link = link

    @Slot()
    def run(self) -> None:
        try:
            self.finished.emit(
                version_manager.delete_version(
                    self._binary,
                    self._directory,
                    link=self._link,
                )
            )
        except Exception as exc:  # noqa: BLE001
            self._fail(exc)


class VersionDeactivationWorker(_BaseWorker):
    """Remove the managed activation symlink, requesting sudo when needed."""

    password_requested = Signal(str, object)

    def __init__(self, directory: Path, link: Path) -> None:
        super().__init__()
        self._directory = directory
        self._link = link

    @Slot()
    def run(self) -> None:
        try:
            if os.access(self._link.parent, os.W_OK):
                result = version_manager.deactivate_version(
                    self._directory,
                    link=self._link,
                )
            else:
                result = self._deactivate_with_sudo()
            self.finished.emit(result)
        except Exception as exc:  # noqa: BLE001
            self._fail(exc)

    def _deactivate_with_sudo(self) -> version_manager.ActivationState:
        response: dict[str, str | None] = {"password": None}
        self.password_requested.emit(
            _(
                "sudo password is required to update {path}.",
                path=self._link,
            ),
            response,
        )
        password = response["password"]
        if password is None:
            raise RuntimeError("deactivation was cancelled")
        completed = subprocess.run(
            [
                "sudo",
                "-S",
                "-p",
                "",
                sys.executable,
                "-m",
                "oh_my_ruyi.version_manager",
                "deactivate",
                os.fspath(self._directory),
                os.fspath(self._link),
            ],
            input=password + "\n",
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            message = completed.stderr.strip() or completed.stdout.strip()
            raise RuntimeError(
                message or f"sudo exited with code {completed.returncode}"
            )
        return version_manager.read_activation_state(self._link, self._directory)


class TelemetrySetupWorker(_BaseWorker):
    """Apply the user's first-install telemetry choice using the activated ruyi."""

    process_output = Signal(str)

    def __init__(
        self,
        binary: Path,
        mode: version_manager.TelemetryMode,
    ) -> None:
        super().__init__()
        self._binary = binary
        self._mode = mode

    @Slot()
    def run(self) -> None:
        try:
            self.finished.emit(
                version_manager.run_telemetry_setup(self._binary, self._mode)
            )
        except version_manager.TelemetryCommandError as exc:
            if exc.output:
                self.process_output.emit(exc.output)
            self._fail(exc)
        except Exception as exc:  # noqa: BLE001
            self._fail(exc)


class FlashWorker(_BaseWorker):
    """Run every flashing strategy in priority order."""

    finished = Signal(int)  # type: ignore[assignment]
    cancelled = Signal()
    yes_no_requested = Signal(str, bool, object)
    password_requested = Signal(str, object)
    process_output = Signal(bytes)

    def __init__(
        self,
        config: GlobalConfig,
        prepared: PreparedProvision,
        host_blkdev_map: PartitionMapDecl,
        host_blkdev_fingerprints: dict[str, str],
        confirmed_mounted_parts: set[PartitionKind],
    ) -> None:
        super().__init__()
        self._config = config
        self._prepared = prepared
        self._host_blkdev_map = host_blkdev_map
        self._host_blkdev_fingerprints = host_blkdev_fingerprints
        self._confirmed_mounted_parts = confirmed_mounted_parts
        self._cancel_requested = threading.Event()
        self._process_lock = threading.Lock()
        self._process: subprocess.Popen[bytes] | None = None

    @Slot()
    def run(self) -> None:
        _set_terminal_target(self._config, "flash")
        try:
            ret = self._run_with_gui_prompts()
            if self._cancel_requested.is_set():
                self.cancelled.emit()
            else:
                self.finished.emit(ret)
        except Exception as exc:  # noqa: BLE001
            if self._cancel_requested.is_set():
                self.cancelled.emit()
            else:
                self._fail(exc)

    def request_cancel(self) -> None:
        """Request cancellation and terminate the active command process group."""
        self._cancel_requested.set()
        with self._process_lock:
            proc = self._process
        if proc is not None and proc.poll() is None:
            self._signal_process_group(proc, signal.SIGTERM)

    def _run_with_gui_prompts(self) -> int:
        if platform.system() == "Windows":
            raise RuntimeError(
                "Native Windows flashing is not supported. Run this GUI inside WSL2 with usbipd-attached USB devices."
            )
        from ruyi.pluginhost import api as plugin_api

        original_ask = plugin_api.RuyiHostAPI.cli_ask_for_yesno_confirmation
        original_call = plugin_api.RuyiHostAPI.call_subprocess_argv

        def ask(api_self, prompt: str, default: bool = False) -> bool:  # noqa: ANN001
            response: dict[str, bool] = {"answer": default}
            self.yes_no_requested.emit(prompt, default, response)
            return response["answer"]

        def call_subprocess(api_self, argv: list[str]) -> int:  # noqa: ANN001
            return self._call_subprocess(argv)

        plugin_api.RuyiHostAPI.cli_ask_for_yesno_confirmation = ask
        plugin_api.RuyiHostAPI.call_subprocess_argv = call_subprocess
        try:
            return ruyi_facade.run_flash(
                self._config,
                self._prepared,
                self._host_blkdev_map,
            )
        finally:
            plugin_api.RuyiHostAPI.cli_ask_for_yesno_confirmation = original_ask
            plugin_api.RuyiHostAPI.call_subprocess_argv = original_call

    def _call_subprocess(self, argv: list[str]) -> int:
        if self._cancel_requested.is_set():
            return 130
        original_argv = argv
        argv = self._argv_with_gui_progress(argv)
        if argv and argv[0] == "sudo":
            response: dict[str, str | None] = {"password": None}
            self.password_requested.emit(
                _("sudo password is required for flashing."), response
            )
            password = response["password"]
            if password is None:
                self.process_output.emit(
                    (_("sudo password prompt was cancelled.") + "\n").encode()
                )
                return 1
            if self._cancel_requested.is_set():
                return 130
            argv = ["sudo", "-S", "-p", "", *argv[1:]]
            stdin_data = password + "\n"
        else:
            stdin_data = None

        self._validate_dd_target(original_argv)
        self.process_output.emit(("$ " + " ".join(argv) + "\n").encode())
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE if stdin_data is not None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
            start_new_session=platform.system() != "Windows",
        )
        with self._process_lock:
            self._process = proc
        try:
            if self._cancel_requested.is_set():
                self._signal_process_group(proc, signal.SIGTERM)
            if stdin_data is not None:
                assert proc.stdin is not None
                try:
                    proc.stdin.write(stdin_data.encode())
                    proc.stdin.close()
                except BrokenPipeError:
                    pass
            assert proc.stdout is not None
            self._emit_process_output(proc.stdout.fileno(), proc)
            return self._wait_for_process(proc)
        finally:
            with self._process_lock:
                if self._process is proc:
                    self._process = None

    def _wait_for_process(self, proc: subprocess.Popen[bytes]) -> int:
        if not self._cancel_requested.is_set():
            return proc.wait()
        try:
            return proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            self._signal_process_group(proc, signal.SIGKILL)
        try:
            return proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            return 130

    @staticmethod
    def _signal_process_group(
        proc: subprocess.Popen[bytes],
        sig: signal.Signals,
    ) -> None:
        try:
            if platform.system() == "Windows":
                proc.send_signal(sig)
            else:
                os.killpg(proc.pid, sig)
        except (OSError, ProcessLookupError):
            try:
                proc.send_signal(sig)
            except (OSError, ProcessLookupError):
                pass

    def _validate_dd_target(self, argv: list[str]) -> None:
        command = argv[1:] if argv and argv[0] == "sudo" else argv
        if not command or command[0] != "dd":
            return
        output_paths = [
            arg.removeprefix("of=") for arg in command[1:] if arg.startswith("of=")
        ]
        if len(output_paths) != 1 or not output_paths[0]:
            raise RuntimeError(
                "refusing to run dd without exactly one explicit output target"
            )
        output_path = output_paths[0]
        part = next(
            (
                candidate
                for candidate, path in self._host_blkdev_map.items()
                if path == output_path
            ),
            None,
        )
        if part is None:
            raise RuntimeError(
                f"refusing to write to unreviewed dd target '{output_path}'"
            )
        expected = self._host_blkdev_fingerprints.get(part)
        current = host_storage.device_fingerprint(output_path)
        if expected is None or current is None or current != expected:
            raise RuntimeError(
                f"the dd target '{output_path}' changed after review; flashing was stopped"
            )
        if (
            host_storage.is_disk_or_child_mounted(output_path)
            and part not in self._confirmed_mounted_parts
        ):
            raise RuntimeError(
                f"the dd target '{output_path}' became mounted after review; flashing was stopped"
            )

    @staticmethod
    def _argv_with_gui_progress(argv: list[str]) -> list[str]:
        if platform.system() != "Linux":
            return argv
        if not argv:
            return argv
        prefix: list[str]
        cmd_index: int
        if argv[0] == "sudo":
            prefix = [argv[0]]
            cmd_index = 1
        else:
            prefix = []
            cmd_index = 0
        if cmd_index >= len(argv) or argv[cmd_index] != "dd":
            return argv
        if any(arg.startswith("status=") for arg in argv[cmd_index + 1 :]):
            return argv
        return [*prefix, "dd", *argv[cmd_index + 1 :], "status=progress"]

    def _emit_process_output(
        self,
        stdout_fd: int,
        proc: subprocess.Popen[bytes] | None = None,
    ) -> None:
        sel = selectors.DefaultSelector()
        sel.register(stdout_fd, selectors.EVENT_READ)
        try:
            cancel_started: float | None = None
            kill_sent_at: float | None = None
            while True:
                if proc is not None and self._cancel_requested.is_set():
                    now = time.monotonic()
                    if cancel_started is None:
                        cancel_started = now
                        self._signal_process_group(proc, signal.SIGTERM)
                    elif kill_sent_at is None and now - cancel_started >= 1:
                        kill_sent_at = now
                        self._signal_process_group(proc, signal.SIGKILL)
                    elif kill_sent_at is not None and now - kill_sent_at >= 2:
                        break
                events = sel.select(0.1 if proc is not None else None)
                if not events:
                    continue
                chunk = os.read(stdout_fd, 4096)
                if not chunk:
                    break
                self.process_output.emit(chunk)
        finally:
            sel.unregister(stdout_fd)


def run_worker_in_thread(worker: _BaseWorker) -> QThread:
    """Move ``worker`` to a fresh :class:`QThread`, start it, and return the thread.

    The caller is responsible for wiring ``worker.finished`` / ``worker.failed``
    to whatever ends the work, and for cleaning up — typically:

    .. code-block:: python

        thread = run_worker_in_thread(worker)
        worker.finished.connect(thread.quit)
        thread.finished.connect(thread.deleteLater)

    The worker's ``run()`` slot is invoked via a queued connection once the
    thread's event loop starts.
    """
    thread = QThread()
    worker.moveToThread(thread)
    thread.started.connect(worker.run, type=Qt.ConnectionType.QueuedConnection)
    thread.start()
    return thread


def safe_stop_thread(thread: QThread | None, timeout_ms: int = 3000) -> None:
    """Safely quit and wait for a worker thread, terminating if it hangs."""
    if thread is None or not thread.isRunning():
        return
    thread.quit()
    if not thread.wait(timeout_ms):
        thread.terminate()
        thread.wait(1000)
