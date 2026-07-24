"""Locale resolution shared by the GUI, Qt, and imported ruyi APIs."""

from __future__ import annotations

import json
import os
import re
import threading
from importlib import resources
from typing import Mapping

_lock = threading.Lock()
_initialized = False
_active_locale: str | None = None
_catalog: dict[str, str] = {}
_qt_translators: list[object] = []
_template_patterns: list[tuple[re.Pattern[str], str]] = []


def preferred_locales(environ: Mapping[str, str]) -> tuple[str, ...]:
    """Resolve locale preferences using the same precedence as ruyi/gettext."""
    languages: list[str] = []
    for name in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
        if value := environ.get(name):
            languages = value.split(":")
            break
    if "C" not in languages:
        languages.append("C")
    return tuple(language.split(".", 1)[0] for language in languages)


def _app_catalog_path(locale: str):
    return resources.files("oh_my_ruyi") / "locales" / f"{locale}.json"


def _ruyi_supports(locale: str) -> bool:
    directory = resources.files("ruyi.resources") / "locale" / locale / "LC_MESSAGES"
    return all(
        (directory / f"{domain}.mo").is_file() for domain in ("argparse", "ruyi")
    )


def resolve_locale(environ: Mapping[str, str] | None = None) -> str | None:
    """Return a locale only when both ruyi and Oh My Ruyi translate it."""
    environ = os.environ if environ is None else environ
    for locale in preferred_locales(environ):
        if _ruyi_supports(locale) and _app_catalog_path(locale).is_file():
            return locale
    return None


def initialize(environ: Mapping[str, str] | None = None) -> str | None:
    """Initialize application and imported-ruyi translations once per process."""
    global _active_locale, _catalog, _initialized, _template_patterns
    if _initialized:
        return _active_locale
    with _lock:
        if _initialized:
            return _active_locale
        environ = os.environ if environ is None else environ
        _active_locale = resolve_locale(environ)
        if _active_locale is not None:
            _catalog = json.loads(
                _app_catalog_path(_active_locale).read_text(encoding="utf-8")
            )
            templates = (
                (source, translated)
                for source, translated in _catalog.items()
                if "{" in source
            )
            _template_patterns = [
                (_compile_template(source), translated)
                for source, translated in sorted(
                    templates,
                    key=lambda item: len(item[0]),
                    reverse=True,
                )
            ]

        from ruyi.i18n import ADAPTER

        if _active_locale is not None:
            ADAPTER.init_from_env({"LANGUAGE": _active_locale})
        ADAPTER.hook()
        _initialized = True
        return _active_locale


def active_locale() -> str | None:
    if not _initialized:
        initialize()
    return _active_locale


def _(source: str, /, **values: object) -> str:
    """Translate one source string and optionally format named placeholders."""
    if not _initialized:
        initialize()
    translated = _catalog.get(source)
    if translated is not None:
        return translated.format(**values) if values else translated
    if translated is None and source.startswith("<b>") and source.endswith("</b>"):
        return f"<b>{_(source[3:-4])}</b>"
    if translated is None and (match := re.fullmatch(r"(\d+\. )(.*)", source)):
        return match.group(1) + _(match.group(2))
    if translated is None:
        for pattern, template in _template_patterns:
            if match := pattern.fullmatch(source):
                format_values = {**match.groupdict(), **values}
                return template.format(**format_values)
    if values:
        return source.format(**values)
    return source


def _compile_template(template: str) -> re.Pattern[str]:
    parts: list[str] = []
    position = 0
    for match in re.finditer(r"\{([A-Za-z_][A-Za-z0-9_]*)(?::[^}]*)?\}", template):
        parts.append(re.escape(template[position : match.start()]))
        parts.append(f"(?P<{match.group(1)}>.+?)")
        position = match.end()
    parts.append(re.escape(template[position:]))
    return re.compile("".join(parts), re.DOTALL)


def locale_environment() -> dict[str, str]:
    """Return environment overrides that keep ruyi subprocess output aligned."""
    return {"LANGUAGE": active_locale() or "C"}


def localize_config(config):
    """Align repository message selection with the active ruyi translation."""
    if hasattr(config, "_lang_code"):
        setattr(config, "_lang_code", active_locale() or "en_US")
    else:
        config.__dict__["_lang_code"] = active_locale() or "en_US"
    if hasattr(config, "babel_locale"):
        delattr(config, "babel_locale")
    elif "babel_locale" in getattr(config, "__dict__", {}):
        config.__dict__.pop("babel_locale", None)
    return config


def format_exception_message(exc: Exception) -> str:
    """Format an exception into a human-readable, localized error message."""
    exc_type = type(exc).__name__
    if isinstance(exc, PermissionError):
        return f"{exc_type}: {_('Permission denied: {detail}', detail=str(exc))}"
    if isinstance(exc, FileNotFoundError):
        return (
            f"{exc_type}: {_('File or directory not found: {detail}', detail=str(exc))}"
        )
    if isinstance(exc, TimeoutError):
        return f"{exc_type}: {_('Operation timed out: {detail}', detail=str(exc))}"
    return f"{exc_type}: {_(str(exc))}"


def apply_qprocess_locale(environment) -> None:
    for name, value in locale_environment().items():
        environment.insert(name, value)


def install_qt_translations(app) -> None:
    """Install Qt's standard-widget catalog for the active ruyi locale."""
    locale = active_locale()
    if locale is None:
        return
    from PySide6.QtCore import QLibraryInfo, QTranslator

    translator = QTranslator(app)
    translations_path = QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)
    if translator.load(f"qtbase_{locale}", translations_path):
        app.installTranslator(translator)
        _qt_translators.append(translator)


def translate_widget_tree(root) -> None:
    """Translate static widget properties after a programmatic UI is built."""
    if active_locale() is None:
        return
    from PySide6.QtWidgets import (
        QAbstractButton,
        QComboBox,
        QGroupBox,
        QLabel,
        QLineEdit,
        QListWidget,
        QProgressBar,
        QTabWidget,
        QTableWidget,
        QWidget,
    )

    widgets = [root, *root.findChildren(QWidget)]
    for widget in widgets:
        if widget.property("isDynamic"):
            continue
        if widget.windowTitle():
            widget.setWindowTitle(_(widget.windowTitle()))
        if widget.toolTip():
            widget.setToolTip(_(widget.toolTip()))
        if widget.accessibleName():
            widget.setAccessibleName(_(widget.accessibleName()))
        if isinstance(widget, QLabel) and widget.text():
            widget.setText(_(widget.text()))
        if isinstance(widget, QAbstractButton) and widget.text():
            widget.setText(_(widget.text()))
        if isinstance(widget, QGroupBox) and widget.title():
            widget.setTitle(_(widget.title()))
        if isinstance(widget, QLineEdit) and widget.placeholderText():
            widget.setPlaceholderText(_(widget.placeholderText()))
        if isinstance(widget, QProgressBar) and widget.format():
            widget.setFormat(_(widget.format()))
        if isinstance(widget, QTabWidget):
            for index in range(widget.count()):
                widget.setTabText(index, _(widget.tabText(index)))
        if isinstance(widget, QListWidget):
            for index in range(widget.count()):
                item = widget.item(index)
                item.setText(_(item.text()))
        if isinstance(widget, QTableWidget):
            for column in range(widget.columnCount()):
                item = widget.horizontalHeaderItem(column)
                if item is not None:
                    item.setText(_(item.text()))
        if isinstance(widget, QComboBox):
            for index in range(widget.count()):
                widget.setItemText(index, _(widget.itemText(index)))


__all__ = [
    "active_locale",
    "format_exception_message",
    "initialize",
    "install_qt_translations",
    "apply_qprocess_locale",
    "localize_config",
    "locale_environment",
    "preferred_locales",
    "resolve_locale",
    "_",
    "translate_widget_tree",
]
