from __future__ import annotations

import datetime as dt
import importlib.metadata
import json
from pathlib import Path

from PySide6.QtWidgets import QApplication

from oh_my_ruyi import __version__
from oh_my_ruyi.about_tab import AboutTab, application_version, telemetry_summary


def test_application_version_matches_installed_distribution() -> None:
    assert application_version() == importlib.metadata.version("oh-my-ruyi")
    assert application_version() == __version__


def test_telemetry_summary_for_off_and_local_modes() -> None:
    class Config:
        state_root = "/unused"

        def __init__(self, mode: str) -> None:
            self.telemetry_mode = mode

    assert telemetry_summary(Config("off")) == (
        "Off",
        "No periodic upload is scheduled.",
    )
    assert telemetry_summary(Config("local")) == (
        "Local only",
        "No periodic upload is scheduled.",
    )


def test_telemetry_summary_calculates_next_window(tmp_path: Path) -> None:
    installation = tmp_path / "telemetry" / "installation.json"
    installation.parent.mkdir()
    installation.write_text(json.dumps({"report_uuid": "0000000100000000"}))

    class Config:
        telemetry_mode = "on"
        state_root = tmp_path

    summary = telemetry_summary(
        Config(), now=dt.datetime(2026, 7, 19, tzinfo=dt.UTC).timestamp()
    )

    assert summary[0] == "On"
    assert "Next upload window:" in summary[1]


def test_about_tab_shows_two_version_panels(qtbot, tmp_path: Path, monkeypatch) -> None:
    _app = QApplication.instance() or QApplication([])

    class Config:
        telemetry_mode = "off"
        state_root = tmp_path

    monkeypatch.setattr(
        "oh_my_ruyi.about_tab.version_manager.read_path_state",
        lambda *_args, **_kwargs: type("State", (), {"command": None})(),
    )
    tab = AboutTab(
        Config(),
        activation_link=tmp_path / "ruyi",
        versions_directory=tmp_path / "versions",
    )
    qtbot.addWidget(tab)

    assert "Ruyi " in tab.bundled_version.toPlainText()
    assert tab._title.text() == "<b>About Oh My Ruyi</b>"
    assert tab._version_label.text() == f"Version {application_version()}"
    tab.start_path_probe()
    assert "No executable named ruyi" in tab.path_version.toPlainText()
    assert tab.telemetry_mode.text() == "Off"
