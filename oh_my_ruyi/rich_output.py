"""Rich and ANSI output adapters for Qt views.

Ruyi uses Rich renderables internally, while subprocesses expose the terminal
representation of those renderables.  This module keeps both representations
styled until they reach the UI and provides one small, stateful renderer for
streaming output.
"""

from __future__ import annotations

import codecs
import html
import io
import re
from typing import Any
from urllib.parse import quote, urlsplit

from PySide6.QtCore import QEvent
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import QTextBrowser, QTextEdit

from rich.ansi import AnsiDecoder
from rich.console import Console
from rich.terminal_theme import TerminalTheme


_ANSI_ESCAPE_RE = re.compile(
    r"(?:\x1b\[[0-?]*[ -/]*[@-~])|(?:\x1b\][^\x07\x1b]*(?:\x07|\x1b\\))|(?:\x1b.)"
)

RICH_TERMINAL_ENV = {
    "COLORTERM": "truecolor",
    "COLUMNS": "120",
    "FORCE_COLOR": "1",
    "TERM": "xterm-256color",
    "TTY_COMPATIBLE": "1",
    "TTY_INTERACTIVE": "0",
}

_LIGHT_ANSI = [
    (32, 33, 36),
    (183, 28, 28),
    (22, 120, 58),
    (145, 91, 0),
    (21, 101, 192),
    (123, 31, 162),
    (0, 121, 107),
    (66, 66, 66),
]
_LIGHT_BRIGHT = [
    (97, 97, 97),
    (211, 47, 47),
    (46, 125, 50),
    (175, 113, 0),
    (25, 118, 210),
    (142, 36, 170),
    (0, 137, 123),
    (33, 33, 33),
]
_DARK_ANSI = [
    (117, 117, 117),
    (239, 83, 80),
    (102, 187, 106),
    (255, 202, 40),
    (100, 181, 246),
    (206, 147, 216),
    (77, 208, 225),
    (224, 224, 224),
]
_DARK_BRIGHT = [
    (158, 158, 158),
    (255, 138, 128),
    (165, 214, 167),
    (255, 224, 130),
    (144, 202, 249),
    (225, 190, 231),
    (128, 222, 234),
    (255, 255, 255),
]


def _read_escape(text: str, start: int) -> tuple[str | None, int, bool]:
    """Read one terminal escape sequence from ``text``.

    The boolean marks an escape sequence split across stream chunks.  Keeping
    it in the view prevents a split SGR sequence from reaching Rich's decoder
    as ordinary text.
    """
    if start + 1 >= len(text):
        return None, len(text), True
    kind = text[start + 1]
    if kind == "[":
        index = start + 2
        while index < len(text):
            code = ord(text[index])
            if 0x40 <= code <= 0x7E:
                return text[start : index + 1], index + 1, False
            index += 1
        return None, len(text), True
    if kind in "]PX^_":
        index = start + 2
        while index < len(text):
            if kind == "]" and text[index] == "\x07":
                return text[start : index + 1], index + 1, False
            if text[index] == "\x1b":
                if index + 1 >= len(text):
                    return None, len(text), True
                if text[index + 1] == "\\":
                    return text[start : index + 2], index + 2, False
            index += 1
        return None, len(text), True
    return text[start : start + 2], start + 2, False


def _normalize_c1_controls(text: str) -> str:
    return (
        text.replace("\x90", "\x1bP")
        .replace("\x98", "\x1bX")
        .replace("\x9b", "\x1b[")
        .replace("\x9d", "\x1b]")
        .replace("\x9e", "\x1b^")
        .replace("\x9f", "\x1b_")
        .replace("\x9c", "\x1b\\")
    )


def strip_terminal_controls(text: str) -> str:
    """Return readable text without terminal cursor/control sequences."""
    text = _normalize_c1_controls(text)
    output: list[str] = []
    index = 0
    while index < len(text):
        if text[index] == "\x1b":
            _sequence, next_index, incomplete = _read_escape(text, index)
            if incomplete:
                break
            index = next_index
            continue
        char = text[index]
        index += 1
        code = ord(char)
        if char in "\n\t" or 32 <= code < 127 or code >= 160:
            output.append(char)
    return "".join(output)


def _safe_link(link: str) -> str | None:
    try:
        parsed = urlsplit(link)
    except ValueError:
        return None
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.netloc:
        return None
    return quote(link, safe=":/?#[]@!$&()*+,;=%-._~")


def _sanitize_osc8_sequence(sequence: str) -> str:
    terminator = "\x07" if sequence.endswith("\x07") else "\x1b\\"
    payload = sequence[2 : -len(terminator)]
    if not payload.startswith("8;"):
        return ""
    _params, separator, link = payload[2:].partition(";")
    if not separator or not link:
        return "\x1b]8;;\x1b\\"
    safe_link = _safe_link(link)
    return f"\x1b]8;;{safe_link}\x1b\\" if safe_link is not None else "\x1b]8;;\x1b\\"


def _sanitize_ansi_links(text: str) -> str:
    output: list[str] = []
    index = 0
    while index < len(text):
        if text[index] != "\x1b":
            output.append(text[index])
            index += 1
            continue
        sequence, next_index, incomplete = _read_escape(text, index)
        if incomplete:
            break
        assert sequence is not None
        if sequence.startswith("\x1b]8;"):
            output.append(_sanitize_osc8_sequence(sequence))
        elif not sequence.startswith(("\x1b]", "\x1bP", "\x1bX", "\x1b^", "\x1b_")):
            output.append(sequence)
        index = next_index
    return "".join(output)


def rich_to_html(
    renderable: Any,
    *objects: Any,
    sep: str = " ",
    end: str = "\n",
    width: int = 120,
) -> str:
    """Render a Rich value as a Qt-friendly HTML fragment."""
    output = io.StringIO()
    console = Console(
        file=output,
        color_system="truecolor",
        force_terminal=True,
        force_interactive=False,
        highlight=False,
        no_color=False,
        soft_wrap=True,
        width=width,
    )
    console.print(renderable, *objects, sep=sep, end=end)
    return ansi_to_html(output.getvalue())


def ansi_to_html(
    text: str,
    *,
    decoder: AnsiDecoder | None = None,
    theme: TerminalTheme | None = None,
) -> str:
    """Convert ANSI-styled text into a Qt-friendly HTML fragment."""
    if not text:
        return ""
    text = _sanitize_ansi_links(text)
    decoder = decoder or AnsiDecoder()
    output = io.StringIO()
    console = Console(
        file=output,
        record=True,
        force_terminal=False,
        force_interactive=False,
        highlight=False,
        no_color=False,
        soft_wrap=True,
        width=120,
    )
    parts = text.splitlines(keepends=True)
    if not parts:
        parts = [text]
    for part in parts:
        has_newline = part.endswith(("\n", "\r"))
        line = part.rstrip("\r\n") if has_newline else part
        console.print(decoder.decode_line(line), end="\n" if has_newline else "")
    return console.export_html(
        clear=True,
        inline_styles=True,
        code_format="{code}",
        theme=theme,
    )


class RichTextView(QTextBrowser):
    """Read-only Qt view that appends Rich HTML and streamed ANSI output."""

    def __init__(self, parent=None, max_blocks: int = 2000) -> None:
        super().__init__(parent)
        self.setReadOnly(True)
        self.setAcceptRichText(True)
        self.setOpenExternalLinks(True)
        self.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        if max_blocks > 0:
            self.document().setMaximumBlockCount(max_blocks)
        self._max_blocks = max_blocks
        self._utf8_decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._segments: list[tuple[str, str]] = []
        self._terminal_current = ""
        self._active_terminal_style = ""
        self._escape_pending = ""
        self._pending_carriage_return = False
        self._render_decoder = AnsiDecoder()
        self._rendered_segment_count = 0
        self._current_start = 0

    def append_rich_html(
        self, fragment: str, *, replace_progress: bool = False
    ) -> None:
        """Append an HTML fragment, optionally replacing the current line."""
        if not fragment:
            return
        if replace_progress:
            self._terminal_current = ""
        elif self._terminal_current:
            self._segments.append(("ansi", self._terminal_current + "\n"))
            self._terminal_current = ""
        self._segments.append(("html", fragment))
        self._render_document()

    def append_rich(self, renderable: Any, *objects: Any, **kwargs: Any) -> None:
        self.append_rich_html(rich_to_html(renderable, *objects, **kwargs))

    def append_ansi(self, text: str, *, replace_progress: bool = False) -> None:
        """Append ANSI output while discarding unsupported terminal controls."""
        if not text:
            return
        if replace_progress:
            self._terminal_current = ""
        self.feed_text(text, final=False)

    def append_ansi_line(
        self,
        text: str,
        *,
        complete: bool,
        replace_progress: bool = False,
    ) -> None:
        """Append one already-delimited terminal line."""
        if replace_progress:
            self._terminal_current = ""
        self.feed_text(text + ("\n" if complete else ""), final=False)

    @staticmethod
    def _preformatted(fragment: str) -> str:
        return (
            f'<span style="white-space: pre; font-family: monospace;">{fragment}</span>'
        )

    def _render_document(self, *, replay: bool = False) -> None:
        if replay:
            super().clear()
            self._render_decoder = AnsiDecoder()
            self._rendered_segment_count = 0
            self._current_start = 0

        theme = self._terminal_theme()
        cursor = QTextCursor(self.document())
        cursor.setPosition(self._current_start)
        cursor.movePosition(
            QTextCursor.MoveOperation.End,
            QTextCursor.MoveMode.KeepAnchor,
        )
        cursor.removeSelectedText()

        for kind, content in self._segments[self._rendered_segment_count :]:
            if kind == "ansi":
                fragment = ansi_to_html(
                    content,
                    decoder=self._render_decoder,
                    theme=theme,
                )
            else:
                fragment = content
            if fragment:
                cursor.insertHtml(self._preformatted(fragment))
            self._rendered_segment_count += 1

        self._current_start = cursor.position()
        if self._terminal_current:
            preview_decoder = AnsiDecoder()
            preview_decoder.style = self._render_decoder.style
            fragment = ansi_to_html(
                self._terminal_current,
                decoder=preview_decoder,
                theme=theme,
            )
            if fragment:
                cursor.insertHtml(self._preformatted(fragment))

        cursor.movePosition(QTextCursor.MoveOperation.End)
        self.setTextCursor(cursor)
        self.ensureCursorVisible()

    def _terminal_theme(self) -> TerminalTheme:
        background = self.palette().base().color()
        foreground = self.palette().text().color()
        dark = background.lightness() < 128
        return TerminalTheme(
            background.getRgb()[:3],
            foreground.getRgb()[:3],
            _DARK_ANSI if dark else _LIGHT_ANSI,
            _DARK_BRIGHT if dark else _LIGHT_BRIGHT,
        )

    @staticmethod
    def _erase_last_character(text: str) -> str:
        tokens = list(_ANSI_ESCAPE_RE.finditer(text))
        protected = {
            index for token in tokens for index in range(token.start(), token.end())
        }
        for index in range(len(text) - 1, -1, -1):
            if index not in protected:
                return text[:index] + text[index + 1 :]
        return text

    def _handle_escape(self, sequence: str) -> None:
        if sequence.startswith("\x1b]"):
            if sequence.startswith("\x1b]8;"):
                sequence = _sanitize_osc8_sequence(sequence)
                self._terminal_current += sequence
                self._active_terminal_style += sequence
            return
        if not sequence.startswith("\x1b["):
            return
        final = sequence[-1]
        if final == "m":
            self._terminal_current += sequence
            if sequence in {"\x1b[m", "\x1b[0m"}:
                self._active_terminal_style = sequence
            else:
                self._active_terminal_style += sequence
            return
        if final == "K":
            params = sequence[2:-1]
            mode = int(params or "0") if (params or "0").isdigit() else 0
            if mode in {1, 2}:
                self._terminal_current = self._active_terminal_style
        elif final in "G`":
            params = sequence[2:-1]
            column = int(params or "1") if (params or "1").isdigit() else 1
            if column <= 1:
                self._terminal_current = self._active_terminal_style
        elif final in "JAHf":
            # Cursor movement and screen clearing have no direct QTextDocument
            # equivalent; dropping them keeps the captured output readable.
            return

    def _commit_terminal_line(self) -> None:
        self._segments.append(("ansi", self._terminal_current + "\n"))
        self._terminal_current = ""

    def feed_text(self, text: str, *, final: bool = False) -> None:
        """Append decoded terminal text, stabilizing CR progress updates."""
        text = _normalize_c1_controls(self._escape_pending + text)
        self._escape_pending = ""
        index = 0
        while index < len(text):
            if self._pending_carriage_return:
                self._pending_carriage_return = False
                if text[index] == "\n":
                    index += 1
                    self._commit_terminal_line()
                    continue
                self._terminal_current = self._active_terminal_style

            if text[index] == "\x1b":
                sequence, next_index, incomplete = _read_escape(text, index)
                if incomplete:
                    self._escape_pending = text[index:]
                    break
                assert sequence is not None
                self._handle_escape(sequence)
                index = next_index
                continue

            char = text[index]
            index += 1
            if char == "\r":
                self._pending_carriage_return = True
            elif char == "\n":
                self._commit_terminal_line()
            elif char == "\b":
                self._terminal_current = self._erase_last_character(
                    self._terminal_current
                )
            elif char == "\t" or 32 <= ord(char) < 127 or ord(char) >= 160:
                self._terminal_current += char

        if final:
            self._escape_pending = ""
            self._pending_carriage_return = False
        self._render_document()

    def feed_bytes(self, data: bytes, *, final: bool = False) -> None:
        """Decode and append a subprocess chunk, handling CR progress updates."""
        text = self._utf8_decoder.decode(data, final=final)
        self.feed_text(text, final=final)
        if final:
            self._utf8_decoder.reset()

    def set_ansi(self, text: str) -> None:
        self.clear()
        self.feed_text(text, final=True)

    def append_plain_status(self, text: str) -> None:
        """Append a non-terminal status without interpreting it as HTML."""
        text = strip_terminal_controls(text).strip()
        if not text or text in self.toPlainText():
            return
        self.append_rich_html(html.escape(text) + "\n")

    def appendPlainText(self, text: str) -> None:  # noqa: N802 - Qt-compatible API
        self.feed_text(text if text.endswith("\n") else text + "\n")

    def setPlainText(self, text: str) -> None:  # noqa: N802 - Qt-compatible API
        self.clear()
        self.feed_text(text, final=True)

    def clear(self) -> None:  # noqa: D401 - Qt API override
        super().clear()
        self._utf8_decoder.reset()
        self._segments.clear()
        self._terminal_current = ""
        self._active_terminal_style = ""
        self._escape_pending = ""
        self._pending_carriage_return = False
        self._render_decoder = AnsiDecoder()
        self._rendered_segment_count = 0
        self._current_start = 0

    def changeEvent(self, event) -> None:  # noqa: N802 - Qt API override
        super().changeEvent(event)
        if event.type() in {
            QEvent.Type.ApplicationPaletteChange,
            QEvent.Type.PaletteChange,
        } and hasattr(self, "_segments"):
            self._render_document(replay=True)


__all__ = [
    "RichTextView",
    "RICH_TERMINAL_ENV",
    "ansi_to_html",
    "rich_to_html",
    "strip_terminal_controls",
]
