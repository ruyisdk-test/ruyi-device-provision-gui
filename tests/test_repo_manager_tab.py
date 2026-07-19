from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path

import pygit2
from PySide6.QtCore import QProcess
from PySide6.QtWidgets import QApplication, QDialog

from oh_my_ruyi import repo_manager
from oh_my_ruyi import repo_manager_tab as repo_manager_tab_module
from oh_my_ruyi.repo_manager_tab import (
    RepoManagementTab,
    _RepoSourceDialog,
    _RepoUpdateDialog,
)


class _FakeProcess:
    def __init__(self) -> None:
        self.terminated = False
        self.killed = False

    def processId(self) -> int:  # noqa: N802 - mirrors QProcess
        return 4242

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True

    def deleteLater(self) -> None:  # noqa: N802 - mirrors QObject
        pass


class _FakeLabel:
    def __init__(self) -> None:
        self._text = ""

    def setText(self, text: str) -> None:  # noqa: N802 - mirrors QLabel
        self._text = text

    def text(self) -> str:
        return self._text

    def setPlaceholderText(self, _text: str) -> None:  # noqa: N802 - mirrors QLineEdit
        pass


def test_tables_keep_default_first_and_toml_order(qtbot, tmp_path: Path) -> None:
    _app = QApplication.instance() or QApplication([])
    config = tmp_path / "ruyi" / "config.toml"
    config.parent.mkdir()
    config.write_text(
        """
[repo]
remote = "https://example.test/default.git"

[[repos]]
id = "z-last"
name = "Configured first"
remote = "https://example.test/z.git"
priority = -100
active = false

[[repos]]
id = "a-first"
name = "Configured second"
local = "/srv/a"
priority = 100
active = true
""".strip()
        + "\n"
    )
    tab = RepoManagementTab(config_path=config)
    qtbot.addWidget(tab)

    assert tab.preset_table.rowCount() == 1
    assert tab.preset_table.columnCount() == 2
    assert [
        tab.preset_table.horizontalHeaderItem(column).text()
        for column in range(tab.preset_table.columnCount())
    ] == ["ID", "Name"]
    assert [
        tab.configured_table.item(row, 0).text()
        for row in range(tab.configured_table.rowCount())
    ] == ["ruyisdk", "z-last", "a-first"]
    assert tab.configured_table.item(1, 5).text() == "Disabled"
    assert tab.configured_table.item(2, 5).text() == "Active"
    assert tab.configured_table.item(0, 1).text() == "Ruyi default packages-index"

    tab.configured_table.selectRow(0)
    assert not tab.remove_button.isEnabled()
    assert tab.update_button.isEnabled()
    tab.configured_table.selectRow(1)
    assert tab.remove_button.isEnabled()
    assert tab.toggle_button.text() == "Enable"
    assert not tab.update_button.isEnabled()


def test_update_dialog_news_actions_disable_together(qtbot) -> None:
    _app = QApplication.instance() or QApplication([])
    dialog = _RepoUpdateDialog("ruyisdk")
    qtbot.addWidget(dialog)
    read_requests: list[None] = []
    dialog.read_news_requested.connect(lambda: read_requests.append(None))

    dialog.complete(True, "Updated ruyisdk.")
    assert dialog.read_news_button.isEnabled()
    assert dialog.mark_all_news_read_button.isEnabled()

    dialog.read_news_button.click()

    assert read_requests == [None]
    assert not dialog.read_news_button.isEnabled()
    assert not dialog.mark_all_news_read_button.isEnabled()


def test_source_dialog_locks_presets_and_enables_custom(qtbot) -> None:
    _app = QApplication.instance() or QApplication([])
    preset = repo_manager.PRESET_REPOS[0]
    source = preset.sources[0]
    dialog = _RepoSourceDialog(
        "Add repository",
        remote=source.remote or "",
        branch=source.branch or "",
        source_options=preset.sources,
    )
    qtbot.addWidget(dialog)

    assert dialog.source_combo.currentData() == source
    assert not dialog.remote_edit.isEnabled()
    assert not dialog.branch_edit.isEnabled()
    assert not dialog.local_edit.isEnabled()


def test_source_dialog_matches_preset_when_main_branch_is_implicit(qtbot) -> None:
    _app = QApplication.instance() or QApplication([])
    source = repo_manager.PRESET_REPOS[0].sources[0]
    dialog = _RepoSourceDialog(
        "Edit repository",
        remote=source.remote or "",
        branch="",
        source_options=repo_manager.PRESET_REPOS[0].sources,
    )
    qtbot.addWidget(dialog)

    assert dialog.source_combo.currentData() == source
    assert dialog.source_combo.currentText() != "Custom"
    assert dialog.remote_edit.text() == source.remote
    assert dialog.branch_edit.text() == "main"
    assert not dialog.remote_edit.isEnabled()
    assert not dialog.branch_edit.isEnabled()

    dialog.source_combo.setCurrentIndex(dialog.source_combo.count() - 1)
    assert dialog.source_combo.currentText() == "Custom"
    assert dialog.remote_edit.isEnabled()
    assert dialog.branch_edit.isEnabled()
    assert not dialog.local_edit.isEnabled()


def test_source_dialog_matches_normalized_preset_url(qtbot) -> None:
    _app = QApplication.instance() or QApplication([])
    source = repo_manager.PRESET_REPOS[0].sources[0]
    dialog = _RepoSourceDialog(
        "Edit repository",
        remote=f"{source.remote}/",
        branch="main",
        source_options=repo_manager.PRESET_REPOS[0].sources,
    )
    qtbot.addWidget(dialog)

    assert dialog.source_combo.currentData() == source
    assert dialog.source_combo.currentText() != "Custom"


def test_source_dialog_selects_custom_when_config_does_not_match(qtbot) -> None:
    _app = QApplication.instance() or QApplication([])
    dialog = _RepoSourceDialog(
        "Edit repository",
        remote="https://custom.test/repo.git",
        local="/srv/repo",
        branch="custom",
        source_options=repo_manager.PRESET_REPOS[0].sources,
    )
    qtbot.addWidget(dialog)

    assert dialog.source_combo.currentText() == "Custom"
    assert dialog.remote_edit.isEnabled()
    assert dialog.branch_edit.isEnabled()
    assert not dialog.local_edit.isEnabled()
    assert dialog.local_edit.text() == "/srv/repo"

    custom_index = dialog.source_combo.currentIndex()
    dialog.source_combo.setCurrentIndex(0)
    assert dialog.remote_edit.text() != "https://custom.test/repo.git"
    dialog.source_combo.setCurrentIndex(custom_index)
    assert dialog.remote_edit.text() == "https://custom.test/repo.git"
    assert dialog.branch_edit.text() == "custom"


def test_default_repo_without_configured_source_stays_custom(qtbot) -> None:
    _app = QApplication.instance() or QApplication([])
    dialog = _RepoSourceDialog(
        "Edit ruyisdk",
        source_options=repo_manager.DEFAULT_REPO_SOURCES,
    )
    qtbot.addWidget(dialog)

    assert dialog.source_combo.currentText() == "Custom"


def test_provision_update_skips_disabled_default_repo(
    qtbot,
    monkeypatch,
    tmp_path: Path,
) -> None:
    _app = QApplication.instance() or QApplication([])
    config = tmp_path / "ruyi" / "config.toml"
    config.parent.mkdir()
    config.write_text("[repo]\ndisabled = true\n")
    tab = RepoManagementTab(config_path=config)
    qtbot.addWidget(tab)
    started: list[tuple[str, str]] = []
    finished: list[tuple[bool, str]] = []
    monkeypatch.setattr(
        tab,
        "_start_update",
        lambda repo_id, message: started.append((repo_id, message)),
    )
    tab.provision_update_finished.connect(
        lambda success, message: finished.append((success, message))
    )

    tab.start_provision_update()

    assert not tab.default_repo_active
    assert started == []
    assert finished == [
        (
            False,
            "Enable the ruyisdk repository in Repo Management to load device metadata.",
        )
    ]


def test_provision_update_uses_active_default_repo(
    qtbot,
    monkeypatch,
    tmp_path: Path,
) -> None:
    _app = QApplication.instance() or QApplication([])
    tab = RepoManagementTab(config_path=tmp_path / "ruyi" / "config.toml")
    qtbot.addWidget(tab)
    started: list[tuple[str, str]] = []
    monkeypatch.setattr(
        tab,
        "_start_update",
        lambda repo_id, message: started.append((repo_id, message)),
    )

    tab.start_provision_update()

    assert tab.default_repo_active
    assert started == [
        ("ruyisdk", "RuyiSDK metadata repository is ready."),
    ]
    assert tab._provision_update


def test_provision_update_retries_after_failure_but_not_after_success(
    qtbot,
    monkeypatch,
    tmp_path: Path,
) -> None:
    _app = QApplication.instance() or QApplication([])
    tab = RepoManagementTab(config_path=tmp_path / "ruyi" / "config.toml")
    qtbot.addWidget(tab)
    started: list[tuple[str, str]] = []
    monkeypatch.setattr(
        tab,
        "_start_update",
        lambda repo_id, message: started.append((repo_id, message)),
    )

    tab.start_provision_update()
    tab._finish_update(False, "update failed")
    tab.start_provision_update()
    tab._finish_update(True, "update succeeded")
    tab.start_provision_update()

    assert started == [
        ("ruyisdk", "RuyiSDK metadata repository is ready."),
        ("ruyisdk", "RuyiSDK metadata repository is ready."),
    ]
    assert tab.provision_update_succeeded


def test_successful_default_repo_update_satisfies_provision_update(
    qtbot,
    monkeypatch,
    tmp_path: Path,
) -> None:
    _app = QApplication.instance() or QApplication([])
    tab = RepoManagementTab(config_path=tmp_path / "ruyi" / "config.toml")
    qtbot.addWidget(tab)
    started: list[tuple[str, str]] = []
    monkeypatch.setattr(
        tab,
        "_start_update",
        lambda repo_id, message: started.append((repo_id, message)),
    )

    tab._updating_repo_id = repo_manager.DEFAULT_REPO_ID
    tab._finish_update(True, "Updated ruyisdk.")
    tab.start_provision_update()

    assert tab.provision_update_succeeded
    assert started == []


def test_default_repo_configuration_change_rearms_provision_update(
    qtbot,
    tmp_path: Path,
) -> None:
    _app = QApplication.instance() or QApplication([])
    config = tmp_path / "ruyi" / "config.toml"
    tab = RepoManagementTab(config_path=config)
    qtbot.addWidget(tab)
    tab._updating_repo_id = repo_manager.DEFAULT_REPO_ID
    tab._finish_update(True, "Updated ruyisdk.")
    current = repo_manager.read_configured_repos(config)[0]

    assert tab.provision_update_succeeded
    assert tab._apply_mutation(
        current.id,
        "Disabled ruyisdk.",
        lambda: repo_manager.set_enabled(config, current, False),
    )
    assert not tab.provision_update_succeeded


def test_ruyisdk_presets_keep_declared_order_and_custom_last(qtbot) -> None:
    _app = QApplication.instance() or QApplication([])
    dialog = _RepoSourceDialog(
        "Edit ruyisdk",
        remote="https://gitee.com/ruyisdk/packages-index.git",
        branch="main",
        source_options=repo_manager.DEFAULT_REPO_SOURCES,
    )
    qtbot.addWidget(dialog)

    assert [
        dialog.source_combo.itemData(index).remote
        for index in range(dialog.source_combo.count() - 1)
    ] == [
        "https://github.com/ruyisdk/packages-index.git",
        "https://mirror.iscas.ac.cn/git/ruyisdk/packages-index.git",
        "https://gitee.com/ruyisdk/packages-index.git",
    ]
    assert dialog.source_combo.currentIndex() == 2
    assert dialog.source_combo.itemText(dialog.source_combo.count() - 1) == "Custom"


def test_repo_panels_use_one_to_two_width_ratio(qtbot, tmp_path: Path) -> None:
    _app = QApplication.instance() or QApplication([])
    tab = RepoManagementTab(config_path=tmp_path / "ruyi" / "config.toml")
    qtbot.addWidget(tab)
    tab.resize(1200, 720)
    tab.show()
    qtbot.wait(0)

    left, right = tab.splitter.sizes()
    assert abs((right / left) - 2) < 0.1


def test_api_mutation_updates_table_without_managed_binary(
    qtbot, tmp_path: Path
) -> None:
    _app = QApplication.instance() or QApplication([])
    config = tmp_path / "ruyi" / "config.toml"
    tab = RepoManagementTab(config_path=config)
    qtbot.addWidget(tab)
    changed: list[str] = []
    tab.configuration_changed.connect(changed.append)
    preset = repo_manager.PRESET_REPOS[0]

    assert tab._apply_mutation(
        preset.id,
        "Added.",
        lambda: repo_manager.add_repo(config, preset, preset.sources[0], 10),
    )

    assert [
        tab.configured_table.item(row, 0).text()
        for row in range(tab.configured_table.rowCount())
    ] == ["ruyisdk", "ruyi-addons-loongson"]
    assert tab.configured_table.item(1, 5).text() == "Disabled"
    assert changed == ["ruyi-addons-loongson"]
    assert tab.status.text() == "Added."


def test_activate_updates_config_before_starting_update(
    qtbot,
    monkeypatch,
    tmp_path: Path,
) -> None:
    _app = QApplication.instance() or QApplication([])
    config = tmp_path / "ruyi" / "config.toml"
    preset = repo_manager.PRESET_REPOS[0]
    repo_manager.add_repo(config, preset, preset.sources[0], 10)
    tab = RepoManagementTab(config_path=config)
    qtbot.addWidget(tab)
    updates: list[tuple[str, str]] = []
    monkeypatch.setattr(
        tab,
        "_start_update",
        lambda repo_id, message: updates.append((repo_id, message)),
    )
    tab.configured_table.selectRow(1)

    tab._toggle_selected()

    assert repo_manager.read_configured_repos(config)[1].active
    assert updates == [
        ("ruyi-addons-loongson", "Enabled and updated ruyi-addons-loongson.")
    ]

    tab.configured_table.selectRow(1)
    tab._toggle_selected()
    assert not repo_manager.read_configured_repos(config)[1].active
    assert len(updates) == 1


def test_source_only_edit_is_applied(
    qtbot,
    monkeypatch,
    tmp_path: Path,
) -> None:
    _app = QApplication.instance() or QApplication([])
    config = tmp_path / "ruyi" / "config.toml"
    preset = repo_manager.PRESET_REPOS[0]
    repo_manager.add_repo(config, preset, preset.sources[0], 10)
    tab = RepoManagementTab(config_path=config)
    qtbot.addWidget(tab)

    class FakeDialog:
        def __init__(self, *_args, **_kwargs) -> None:
            self.help_label = _FakeLabel()
            self.remote_edit = _FakeLabel()
            self.branch_edit = _FakeLabel()

        def exec(self) -> QDialog.DialogCode:
            return QDialog.DialogCode.Accepted

        def values(self):
            return (
                repo_manager.RepoSource(
                    "https://mirror.test/addon.git",
                    None,
                    "next",
                ),
                10,
                preset.name,
            )

    monkeypatch.setattr(repo_manager_tab_module, "_RepoSourceDialog", FakeDialog)
    updates: list[tuple[str, str]] = []
    monkeypatch.setattr(
        tab,
        "_start_update",
        lambda repo_id, message: updates.append((repo_id, message)),
    )
    tab.configured_table.selectRow(1)

    tab._edit_selected()

    repo = repo_manager.read_configured_repos(config)[1]
    assert repo.priority == 10
    assert repo.remote == "https://mirror.test/addon.git"
    assert repo.branch == "next"
    assert updates == []


def test_edit_active_additional_repo_starts_update(
    qtbot,
    monkeypatch,
    tmp_path: Path,
) -> None:
    _app = QApplication.instance() or QApplication([])
    config = tmp_path / "ruyi" / "config.toml"
    preset = repo_manager.PRESET_REPOS[0]
    repo_manager.add_repo(config, preset, preset.sources[0], 10)
    repo = repo_manager.read_configured_repos(config)[1]
    repo_manager.set_enabled(config, repo, True)
    tab = RepoManagementTab(config_path=config)
    qtbot.addWidget(tab)

    class FakeDialog:
        def __init__(self, *_args, **_kwargs) -> None:
            self.help_label = _FakeLabel()
            self.remote_edit = _FakeLabel()
            self.branch_edit = _FakeLabel()

        def exec(self) -> QDialog.DialogCode:
            return QDialog.DialogCode.Accepted

        def values(self):
            return (
                repo_manager.RepoSource(
                    "https://mirror.test/addon.git",
                    None,
                    "next",
                ),
                20,
                preset.name,
            )

    updates: list[tuple[str, str]] = []
    monkeypatch.setattr(repo_manager_tab_module, "_RepoSourceDialog", FakeDialog)
    monkeypatch.setattr(
        tab,
        "_start_update",
        lambda repo_id, message: updates.append((repo_id, message)),
    )
    tab.configured_table.selectRow(1)

    tab._edit_selected()

    assert updates == [("ruyi-addons-loongson", "Updated ruyi-addons-loongson.")]


def test_edit_active_default_repo_starts_update(
    qtbot,
    monkeypatch,
    tmp_path: Path,
) -> None:
    _app = QApplication.instance() or QApplication([])
    config = tmp_path / "ruyi" / "config.toml"
    config.parent.mkdir()
    config.write_text('[repo]\nremote = "https://example.test/default.git"\n')
    tab = RepoManagementTab(config_path=config)
    qtbot.addWidget(tab)

    class FakeDialog:
        def __init__(self, *_args, **_kwargs) -> None:
            self.help_label = _FakeLabel()
            self.remote_edit = _FakeLabel()
            self.branch_edit = _FakeLabel()

        def exec(self) -> QDialog.DialogCode:
            return QDialog.DialogCode.Accepted

        def values(self):
            return (
                repo_manager.RepoSource(
                    "https://mirror.test/default.git",
                    None,
                    "next",
                ),
                0,
                "",
            )

    updates: list[tuple[str, str]] = []
    monkeypatch.setattr(repo_manager_tab_module, "_RepoSourceDialog", FakeDialog)
    monkeypatch.setattr(
        tab,
        "_start_update",
        lambda repo_id, message: updates.append((repo_id, message)),
    )
    tab.configured_table.selectRow(0)

    tab._edit_selected()

    assert updates == [("ruyisdk", "Updated ruyisdk.")]


def test_update_child_imports_ruyi_for_local_only_repo(tmp_path: Path) -> None:
    config = tmp_path / "config" / "ruyi" / "config.toml"
    default_repo = tmp_path / "default-repo"
    local_repo = tmp_path / "local-repo"
    default_repo.mkdir()
    local_repo.mkdir()
    pygit2.init_repository(default_repo, bare=False)
    pygit2.init_repository(local_repo, bare=False)
    config.parent.mkdir(parents=True)
    config.write_text(f'[repo]\nlocal = "{default_repo}"\n')
    preset = repo_manager.RepoPreset(
        "local-test",
        "Local test",
        (repo_manager.RepoSource(local=os.fspath(local_repo)),),
    )
    repo_manager.add_repo(config, preset, preset.sources[0], 10)
    repo = repo_manager.read_configured_repos(config)[1]
    repo_manager.set_enabled(config, repo, True)

    env = os.environ.copy()
    for name in ("CACHE", "DATA", "STATE"):
        env[f"XDG_{name}_HOME"] = os.fspath(tmp_path / name.lower())
    env["RUYI_TELEMETRY_OPTOUT"] = "1"
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "oh_my_ruyi.repo_update_child",
            os.fspath(config),
            "local-test",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "syncing repo 'local-test'" in completed.stderr


def test_news_child_reads_and_marks_unread_news(tmp_path: Path) -> None:
    config = tmp_path / "config" / "ruyi" / "config.toml"
    local_repo = tmp_path / "local-repo"
    local_repo.mkdir(parents=True)
    pygit2.init_repository(local_repo, bare=False)
    (local_repo / "config.toml").write_text(
        """ruyi-repo = "v1"

[[mirrors]]
id = "ruyi-dist"
urls = ["https://example.test/dist/"]
"""
    )
    news_dir = local_repo / "news"
    news_dir.mkdir()
    (news_dir / "2026-07-19-test.en_US.md").write_text(
        """---
title: Test news
---

This is unread news.
"""
    )
    config.parent.mkdir(parents=True)
    config.write_text(f'[repo]\nlocal = "{local_repo}"\n')

    env = os.environ.copy()
    env["XDG_CONFIG_HOME"] = os.fspath(config.parent.parent)
    env["XDG_STATE_HOME"] = os.fspath(tmp_path / "state")
    env["RUYI_TELEMETRY_OPTOUT"] = "1"
    read = subprocess.run(
        [
            sys.executable,
            "-m",
            "oh_my_ruyi.repo_news_child",
            os.fspath(config),
            "read",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert read.returncode == 0, read.stderr
    assert "This is unread news." in read.stdout
    assert (
        "2026-07-19-test" in (tmp_path / "state" / "ruyi" / "news.read.txt").read_text()
    )


def test_update_cancellation_terminates_the_process_group(
    qtbot,
    monkeypatch,
    tmp_path: Path,
) -> None:
    _app = QApplication.instance() or QApplication([])
    tab = RepoManagementTab(config_path=tmp_path / "ruyi" / "config.toml")
    qtbot.addWidget(tab)
    process = _FakeProcess()
    signals: list[tuple[int, signal.Signals]] = []
    monkeypatch.setattr(os, "killpg", lambda pid, sig: signals.append((pid, sig)))
    tab._process = process  # type: ignore[assignment]

    tab._cancel_process()
    assert tab._cancel_requested
    assert signals == [(4242, signal.SIGTERM)]
    assert tab._kill_timer.isActive()

    tab._force_kill_process()
    assert signals[-1] == (4242, signal.SIGKILL)
    assert process.killed
    tab._kill_timer.stop()
    tab._process = None


def test_crashed_signal_waits_for_cancelled_process_to_finish(
    qtbot, tmp_path: Path
) -> None:
    _app = QApplication.instance() or QApplication([])
    tab = RepoManagementTab(config_path=tmp_path / "ruyi" / "config.toml")
    qtbot.addWidget(tab)
    process = _FakeProcess()
    tab._process = process  # type: ignore[assignment]
    tab._cancel_requested = True

    tab._on_process_error(process, QProcess.ProcessError.Crashed)  # type: ignore[arg-type]

    assert tab._process is process
    assert tab._cancel_requested
    tab._process = None
