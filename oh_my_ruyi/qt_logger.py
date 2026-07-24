"""Bridge :class:`ruyi.log.RuyiLogger` to Qt signals.

``ruyi`` writes its user-facing output through a :class:`RuyiLogger` instance
held on ``GlobalConfig.logger``.  :class:`QtRuyiLogger` gives Rich a terminal
stream backed by Qt signals, so colors, links, and text styles survive until a
GUI output view renders them.

The QObject is kept separate from the logger to side-step metaclass conflicts
between :class:`abc.ABCMeta` (used by ``RuyiLogger``) and the sip metaclass
used by ``QObject``.
"""

from __future__ import annotations

import io
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from PySide6.QtCore import QObject, Signal

from ruyi.log import RuyiLogger
from ruyi.i18n import _ as ruyi_tr
from ruyi.utils.global_mode import ProvidesGlobalMode

from .rich_output import strip_terminal_controls


class LogEmitter(QObject):
    """QObject owning the log signal.

    Having this on a dedicated object lets :class:`QtRuyiLogger` live on any
    thread (workers move their own emitter to the worker thread along with
    them).
    """

    #: Emitted for every log line. ``(level, text)`` where ``level`` is one of
    #: ``"stdout"``, ``"D"``, ``"I"``, ``"W"``, ``"F"`` and ``text`` is the
    #: plain text (without one trailing newline). Kept for non-visual clients.
    log_emitted = Signal(str, str)

    #: ANSI-styled terminal output produced by Rich. GUI output views consume
    #: this signal instead of the compatibility-only plain signal above.
    terminal_emitted = Signal(str)

    #: ANSI output together with the operation that owns it. This avoids
    #: sending delayed worker output to a newer operation's log view.
    targeted_terminal_emitted = Signal(str, str)

    #: Emitted when a fatal (``log.F``) message is produced.
    fatal_emitted = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._terminal_pending: list[tuple[str, str]] = []
        self._terminal_delivery_started = False
        self._terminal_targets = threading.local()

    def emit_terminal(self, text: str) -> None:
        target = getattr(self._terminal_targets, "current", "welcome")
        if not self._terminal_delivery_started:
            self._terminal_pending.append((target, text))
        self.terminal_emitted.emit(text)
        self.targeted_terminal_emitted.emit(target, text)

    def set_terminal_target(self, target: str) -> None:
        self._terminal_targets.current = target

    def terminal_target(self) -> str:
        return getattr(self._terminal_targets, "current", "welcome")

    def start_terminal_delivery(self) -> list[tuple[str, str]]:
        """Return output emitted before the GUI connected its terminal view."""
        self._terminal_delivery_started = True
        pending = list(self._terminal_pending)
        self._terminal_pending.clear()
        return pending


_LEVEL_PREFIXES: dict[str, str] = {
    "F": "[bold red]fatal error:[/] {message}",
    "I": "[bold green]info:[/] {message}",
    "W": "[bold yellow]warn:[/] {message}",
    "D": "[dim]debug:[/] {message}",
    "stdout": "",
}


class _QtConsoleStream(io.TextIOBase):
    """TTY-like stream that forwards Rich's ANSI output to a Qt signal."""

    def __init__(self, emitter: LogEmitter) -> None:
        super().__init__()
        self._emitter = emitter
        self._local = threading.local()

    @property
    def encoding(self) -> str:  # type: ignore[override]
        return "utf-8"

    def isatty(self) -> bool:
        return True

    def writable(self) -> bool:
        return True

    def begin_capture(self) -> None:
        self._local.capture = []

    def end_capture(self) -> str:
        captured = "".join(getattr(self._local, "capture", ()))
        self._local.capture = None
        return captured

    def write(self, text: str) -> int:
        if not text:
            return 0
        capture = getattr(self._local, "capture", None)
        if capture is not None:
            capture.append(text)
        self._emitter.emit_terminal(text)
        return len(text)

    def flush(self) -> None:
        return None


class QtRuyiLogger(RuyiLogger):
    """:class:`RuyiLogger` implementation that forwards to a :class:`LogEmitter`.

    Calls render to ANSI through a private Rich console. The terminal stream is
    signalled to Qt unchanged; a plain companion signal remains available for
    tests and integrations that don't render styles. ``log_console`` returns
    the same console, which also captures Rich ``Progress`` and ``Live`` output
    used directly by imported ruyi APIs.
    """

    def __init__(
        self,
        gm: ProvidesGlobalMode,
        emitter: LogEmitter,
    ) -> None:
        super().__init__()
        self._gm = gm
        self._emitter = emitter

        from rich.console import Console

        self._stream = _QtConsoleStream(emitter)
        self._console = Console(
            file=self._stream,
            color_system="truecolor",
            force_terminal=True,
            force_interactive=False,
            highlight=False,
            no_color=False,
            soft_wrap=True,
            width=120,
        )

    @property
    def log_console(self) -> Any:  # type: ignore[override]
        return self._console

    def set_terminal_target(self, target: str) -> None:
        """Route subsequent Rich console output to one GUI output view."""
        self._emitter.set_terminal_target(target)

    @contextmanager
    def terminal_target(self, target: str) -> Iterator[None]:
        """Temporarily route Rich output on the current thread."""
        previous = self._emitter.terminal_target()
        self._emitter.set_terminal_target(target)
        try:
            yield
        finally:
            self._emitter.set_terminal_target(previous)

    def _emit(
        self,
        level: str,
        message: Any,
        *objects: Any,
        sep: str = " ",
        end: str = "\n",
    ) -> None:
        prefix = _LEVEL_PREFIXES.get(level, "")
        self._stream.begin_capture()
        try:
            if prefix:
                before, marker, after = ruyi_tr(prefix).partition("{message}")
                if marker:
                    self._console.print(before, end="")
                    self._console.print(message, end="")
                    self._console.print(after, end="")
                    for obj in objects:
                        self._stream.write(sep)
                        self._console.print(obj, end="")
                    self._stream.write(end)
                else:
                    self._console.print(
                        ruyi_tr(prefix), message, *objects, sep=sep, end=end
                    )
            else:
                self._console.print(message, *objects, sep=sep, end=end)
        finally:
            terminal_text = self._stream.end_capture()
        text = strip_terminal_controls(terminal_text)
        # Strip exactly one trailing newline (the one `end` adds); the log
        # compatibility signal's consumers append their own.
        if text.endswith("\n"):
            text = text[:-1]
        self._emitter.log_emitted.emit(level, text)

    def stdout(
        self,
        message: Any,
        *objects: Any,
        sep: str = " ",
        end: str = "\n",
    ) -> None:
        self._emit("stdout", message, *objects, sep=sep, end=end)

    def D(  # noqa: N802 - mirrors ruyi's API
        self,
        message: Any,
        *objects: Any,
        sep: str = " ",
        end: str = "\n",
        _stack_offset_delta: int = 0,
    ) -> None:
        if not self._gm.is_debug:
            return
        self._emit("D", message, *objects, sep=sep, end=end)

    def F(  # noqa: N802 - mirrors ruyi's API
        self,
        message: Any,
        *objects: Any,
        sep: str = " ",
        end: str = "\n",
    ) -> None:
        self._emit("F", message, *objects, sep=sep, end=end)
        self._emitter.fatal_emitted.emit()

    def I(  # noqa: N802, E743 - mirrors ruyi's API
        self,
        message: Any,
        *objects: Any,
        sep: str = " ",
        end: str = "\n",
    ) -> None:
        self._emit("I", message, *objects, sep=sep, end=end)

    def W(  # noqa: N802 - mirrors ruyi's API
        self,
        message: Any,
        *objects: Any,
        sep: str = " ",
        end: str = "\n",
    ) -> None:
        self._emit("W", message, *objects, sep=sep, end=end)
