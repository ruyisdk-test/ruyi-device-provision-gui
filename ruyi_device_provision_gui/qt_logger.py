"""Bridge :class:`ruyi.log.RuyiLogger` to Qt signals.

``ruyi`` writes all of its user-facing output through a :class:`RuyiLogger`
instance held on ``GlobalConfig.logger``. To get that output into the GUI in
real time we provide :class:`QtRuyiLogger` — a drop-in subclass that renders
each log line through ``rich`` (so the markup ruyi uses keeps working) and
emits the resulting plain text via :class:`LogEmitter.log_emitted`.

The QObject is kept separate from the logger to side-step metaclass conflicts
between :class:`abc.ABCMeta` (used by ``RuyiLogger``) and the sip metaclass
used by ``QObject``.
"""

from __future__ import annotations

import io
from typing import Any

from PySide6.QtCore import QObject, Signal

from ruyi.log import RuyiLogger
from ruyi.utils.global_mode import ProvidesGlobalMode


class LogEmitter(QObject):
    """QObject owning the log signal.

    Having this on a dedicated object lets :class:`QtRuyiLogger` live on any
    thread (workers move their own emitter to the worker thread along with
    them).
    """

    #: Emitted for every log line. ``(level, text)`` where ``level`` is one of
    #: ``"stdout"``, ``"D"``, ``"I"``, ``"W"``, ``"F"`` and ``text`` is the
    #: already-rendered plain text (without trailing newline).
    log_emitted = Signal(str, str)

    #: Emitted when a fatal (``log.F``) message is produced.
    fatal_emitted = Signal()


_LEVEL_PREFIXES: dict[str, str] = {
    "F": "fatal error: ",
    "I": "info: ",
    "W": "warn: ",
    "D": "debug: ",
    "stdout": "",
}


class QtRuyiLogger(RuyiLogger):
    """:class:`RuyiLogger` implementation that forwards to a :class:`LogEmitter`.

    Each call renders the message into a plain-text string using a private
    ``rich.console.Console`` instance pointed at an in-memory buffer, then
    emits the captured text. Level prefixes (``info: ``, ``warn: ``,
    ``fatal error: ``) are inserted manually to match what ruyi's CLI
    logger prints, so the GUI log view shows the same text the CLI would.
    The rendering cost is negligible compared to the work ruyi does on each
    log call.
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

        self._buf = io.StringIO()
        self._console = Console(
            file=self._buf,
            highlight=False,
            soft_wrap=True,
            no_color=True,
            width=100,
        )

    @property
    def log_console(self) -> Any:  # type: ignore[override]
        return self._console

    def _emit(
        self,
        level: str,
        message: Any,
        *objects: Any,
        sep: str = " ",
        end: str = "\n",
    ) -> None:
        prefix = _LEVEL_PREFIXES.get(level, "")
        if prefix and isinstance(message, str):
            self._console.print(f"{prefix}{message}", *objects, sep=sep, end=end)
        elif prefix:
            self._console.print(prefix, message, *objects, sep="", end=end)
        else:
            self._console.print(message, *objects, sep=sep, end=end)
        text = self._buf.getvalue()
        # Reset the buffer in-place to avoid reallocating.
        self._buf.seek(0)
        self._buf.truncate(0)
        # Strip exactly one trailing newline (the one `end` adds); the log
        # view appends its own.
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
