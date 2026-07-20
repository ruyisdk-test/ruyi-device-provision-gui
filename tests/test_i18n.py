from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from importlib import resources
from pathlib import Path

from oh_my_ruyi.i18n import preferred_locales, resolve_locale


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _locale_probe(locale: str, root: Path) -> dict[str, object]:
    code = textwrap.dedent(
        """
        import json
        import os
        from pathlib import Path

        from PySide6.QtCore import QProcessEnvironment
        from PySide6.QtWidgets import QApplication, QDialogButtonBox

        from oh_my_ruyi.i18n import (
            active_locale,
            apply_qprocess_locale,
            initialize,
            install_qt_translations,
            locale_environment,
            localize_config,
            tr,
        )

        initialize()
        app = QApplication([])
        install_qt_translations(app)

        from ruyi.config import GlobalConfig
        from ruyi.utils.global_mode import EnvGlobalModeProvider

        from oh_my_ruyi.main_window import ProvisionMainWindow, _VersionDownloadDialog
        from oh_my_ruyi.qt_logger import LogEmitter, QtRuyiLogger
        from oh_my_ruyi.repo_manager_tab import _RepoUpdateDialog
        from oh_my_ruyi.version_manager import RuyiRelease

        root = Path(os.environ["I18N_TEST_ROOT"])
        gm = EnvGlobalModeProvider({}, [])
        emitter = LogEmitter()
        logger = QtRuyiLogger(gm, emitter)
        config = localize_config(GlobalConfig(gm, logger))
        logs = []
        emitter.log_emitted.connect(lambda level, text: logs.append([level, text]))
        logger.I("hello")

        window = ProvisionMainWindow(
            config,
            logger,
            emitter,
            auto_start=False,
            versions_directory=root / "versions",
            activation_link=root / "bin" / "ruyi",
            telemetry_installation=root / "telemetry" / "installation.json",
            system_ruyi_config=root / "etc" / "ruyi" / "config.toml",
            repo_config_path=root / "config" / "ruyi" / "config.toml",
        )

        standard_buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        download_dialog = _VersionDownloadDialog(
            RuyiRelease(
                "1.2.3",
                "stable",
                "2026-01-01",
                ("https://example.test/ruyi-1.2.3.amd64",),
                "x86_64",
            )
        )
        download_dialog.update_progress(1024, 2048)

        update_dialog = _RepoUpdateDialog("repo-id")
        cancellations = []
        update_dialog.cancel_requested.connect(lambda: cancellations.append(True))
        update_dialog.cancel_button.click()

        process_environment = QProcessEnvironment()
        apply_qprocess_locale(process_environment)
        result = {
            "active_locale": active_locale(),
            "config_lang": config.lang_code,
            "locale_environment": locale_environment(),
            "qprocess_language": process_environment.value("LANGUAGE"),
            "tabs": [
                window._tabs.tabText(index)
                for index in range(window._tabs.count())
            ],
            "bundled_ruyi": window._about_tab.bundled_version.toPlainText(),
            "qt_cancel": standard_buttons.button(
                QDialogButtonBox.StandardButton.Cancel
            ).text(),
            "logger": logs,
            "progress": download_dialog._progress.format(),
            "cancel_requested": cancellations,
            "formatted_template": tr("Version for {package}", package="foo"),
            "matched_template": tr("Version for foo"),
        }
        window.close()
        print(json.dumps(result))
        """
    )
    environment = os.environ.copy()
    for name in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
        environment.pop(name, None)
    environment.update(
        {
            "I18N_TEST_ROOT": os.fspath(root),
            "LANGUAGE": locale,
            "QT_QPA_PLATFORM": "offscreen",
            "RUYI_TELEMETRY_OPTOUT": "1",
            "XDG_CACHE_HOME": os.fspath(root / "cache"),
            "XDG_CONFIG_HOME": os.fspath(root / "config"),
            "XDG_DATA_HOME": os.fspath(root / "data"),
            "XDG_STATE_HOME": os.fspath(root / "state"),
        }
    )
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return json.loads(completed.stdout.strip().splitlines()[-1])


def test_preferred_locales_match_gettext_precedence() -> None:
    assert preferred_locales(
        {
            "LANGUAGE": "zh_CN.UTF-8:en_US",
            "LC_ALL": "zh_TW.UTF-8",
            "LANG": "C.UTF-8",
        }
    ) == ("zh_CN", "en_US", "C")
    assert preferred_locales({"LC_MESSAGES": "zh_CN.UTF-8"}) == ("zh_CN", "C")


def test_locale_requires_both_translation_sets() -> None:
    assert resolve_locale({"LANGUAGE": "zh_TW.UTF-8:zh_CN.UTF-8"}) == "zh_CN"
    assert resolve_locale({"LANGUAGE": "zh_TW.UTF-8"}) is None


def test_zh_cn_localizes_gui_qt_ruyi_and_dynamic_text(tmp_path: Path) -> None:
    result = _locale_probe("zh_CN.UTF-8", tmp_path)

    assert result["active_locale"] == "zh_CN"
    assert result["config_lang"] == "zh_CN"
    assert result["locale_environment"] == {"LANGUAGE": "zh_CN"}
    assert result["qprocess_language"] == "zh_CN"
    assert result["tabs"] == ["版本管理", "仓库管理", "设备配置", "关于"]
    assert "\n\n在 " in result["bundled_ruyi"]
    assert "上运行。" in result["bundled_ruyi"]
    assert result["qt_cancel"] == "取消"
    assert result["logger"] == [["I", "信息：hello"]]
    assert result["progress"] == "%p%（1.0 KiB / 2.0 KiB）"
    assert result["cancel_requested"] == [True]
    assert result["formatted_template"] == "foo 的版本"
    assert result["matched_template"] == "foo 的版本"


def test_unsupported_locale_keeps_every_layer_in_english(tmp_path: Path) -> None:
    result = _locale_probe("zh_TW.UTF-8", tmp_path)

    assert result["active_locale"] is None
    assert result["config_lang"] == "en_US"
    assert result["locale_environment"] == {"LANGUAGE": "C"}
    assert result["qprocess_language"] == "C"
    assert result["tabs"] == [
        "Version Management",
        "Repo Management",
        "Device Provision",
        "About",
    ]
    assert "\n\nRunning on " in result["bundled_ruyi"]
    assert result["qt_cancel"] == "Cancel"
    assert result["logger"] == [["I", "info: hello"]]
    assert result["progress"] == "%p% (1.0 KiB / 2.0 KiB)"
    assert result["cancel_requested"] == [True]
    assert result["formatted_template"] == "Version for foo"
    assert result["matched_template"] == "Version for foo"


def test_translation_catalog_is_a_packaged_resource() -> None:
    catalog_path = resources.files("oh_my_ruyi") / "locales" / "zh_CN.json"
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))

    assert catalog["About"] == "关于"
    assert catalog["Version for {package}"] == "{package} 的版本"
