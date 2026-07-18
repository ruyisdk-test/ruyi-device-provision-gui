from __future__ import annotations

import io
import os
from pathlib import Path

import pytest

from oh_my_ruyi import version_manager


def _release_payload() -> dict:
    return {
        "channels": {
            "testing": {
                "version": "0.52.0-alpha.20260714",
                "release_date": "2026-07-14T10:54:29Z",
                "download_urls": {
                    "linux/x86_64": [
                        "https://example.test/ruyi-0.52.0-alpha.20260714.amd64"
                    ]
                },
            },
            "stable": {
                "version": "0.50.0",
                "release_date": "2026-06-23T13:06:10Z",
                "download_urls": {
                    "linux/x86_64": ["https://example.test/ruyi-0.50.0.amd64"]
                },
            },
        }
    }


def test_fetch_release_catalog_falls_back_to_static_mirror() -> None:
    calls: list[str] = []

    def read_json(url: str, _timeout: float) -> object:
        calls.append(url)
        if url == version_manager.PRIMARY_RELEASES_URL:
            raise OSError("primary unavailable")
        return _release_payload()

    catalog = version_manager.fetch_release_catalog(
        platform_key="linux/x86_64",
        read_json=read_json,
    )

    assert calls == [
        version_manager.PRIMARY_RELEASES_URL,
        version_manager.FALLBACK_RELEASES_URL,
    ]
    assert catalog.source_url == version_manager.FALLBACK_RELEASES_URL
    assert [release.version for release in catalog.releases] == [
        "0.52.0-alpha.20260714",
        "0.50.0",
    ]


def test_download_release_uses_mirror_and_drops_arch_suffix(tmp_path: Path) -> None:
    release = version_manager.RuyiRelease(
        "0.50.0",
        "stable",
        "2026-06-23T13:06:10Z",
        ("https://primary.test/ruyi.amd64", "https://mirror.test/ruyi.amd64"),
    )
    calls: list[str] = []

    def open_download(url: str, _timeout: float):
        calls.append(url)
        if "primary" in url:
            raise OSError("download failed")
        return io.BytesIO(b"standalone ruyi")

    path = version_manager.download_release(
        release,
        tmp_path,
        open_download=open_download,
    )

    assert calls == list(release.download_urls)
    assert path == tmp_path / "ruyi-0.50.0"
    assert path.read_bytes() == b"standalone ruyi"
    assert os.access(path, os.X_OK)
    assert not list(tmp_path.glob("*.download"))


def test_installed_versions_are_discovered_without_state_file(tmp_path: Path) -> None:
    (tmp_path / "ruyi-0.49.0").write_bytes(b"old")
    (tmp_path / "ruyi-0.52.0-alpha.20260714").write_bytes(b"new")
    (tmp_path / "unrelated").write_bytes(b"ignored")

    installed = version_manager.list_installed_versions(tmp_path)

    assert [item.version for item in installed] == [
        "0.52.0-alpha.20260714",
        "0.49.0",
    ]


def test_activation_state_is_derived_from_symlink(tmp_path: Path) -> None:
    directory = tmp_path / "versions"
    directory.mkdir()
    binary = directory / "ruyi-0.50.0"
    binary.write_bytes(b"ruyi")
    link = tmp_path / "bin" / "ruyi"
    link.parent.mkdir()
    link.symlink_to(binary)

    state = version_manager.read_activation_state(link, directory)

    assert state.managed
    assert state.version == "0.50.0"
    assert state.target == binary


def test_activation_requires_confirmation_for_unmanaged_file(tmp_path: Path) -> None:
    directory = tmp_path / "versions"
    directory.mkdir()
    binary = directory / "ruyi-0.50.0"
    binary.write_bytes(b"new")
    link = tmp_path / "bin" / "ruyi"
    link.parent.mkdir()
    link.write_bytes(b"old")

    with pytest.raises(version_manager.UnmanagedActivationError):
        version_manager.activate_version(binary, directory, link=link)

    assert link.read_bytes() == b"old"


def test_activation_backs_up_unmanaged_file_and_replaces_managed_link(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "versions"
    directory.mkdir()
    first = directory / "ruyi-0.50.0"
    second = directory / "ruyi-0.52.0-alpha.20260714"
    first.write_bytes(b"stable")
    second.write_bytes(b"testing")
    link = tmp_path / "bin" / "ruyi"
    link.parent.mkdir()
    link.write_bytes(b"unmanaged")

    first_result = version_manager.activate_version(
        first,
        directory,
        link=link,
        backup_unmanaged=True,
    )

    assert first_result.backup_path == link.with_name("ruyi.bak")
    assert first_result.backup_path.read_bytes() == b"unmanaged"
    assert link.resolve() == first

    second_result = version_manager.activate_version(second, directory, link=link)

    assert second_result.backup_path is None
    assert link.resolve() == second
    assert second_result.state.version == "0.52.0-alpha.20260714"


def test_existing_backup_is_not_overwritten(tmp_path: Path) -> None:
    link = tmp_path / "ruyi"
    link.write_bytes(b"current")
    (tmp_path / "ruyi.bak").write_bytes(b"previous backup")

    assert version_manager.next_backup_path(link) == tmp_path / "ruyi.bak.1"


def test_telemetry_setup_applies_choice_then_runs_status(tmp_path: Path) -> None:
    binary = tmp_path / "ruyi-0.50.0"
    binary.write_bytes(b"ruyi")
    calls: list[tuple[Path, tuple[str, ...], float]] = []

    def run_interactive(path: Path, answers: tuple[str, ...], timeout: float) -> str:
        calls.append((path, answers, timeout))
        return "first-run output\r\n\x1b[32mlocal\x1b[0m\r\n"

    result = version_manager.run_telemetry_setup(
        binary,
        "local",
        timeout=12,
        run_interactive=run_interactive,
    )

    assert calls == [(binary, ("n", "n"), 12)]
    assert result.mode == "local"
    assert result.status == "local"


def test_telemetry_setup_surfaces_command_failure(tmp_path: Path) -> None:
    binary = tmp_path / "ruyi-0.50.0"
    binary.write_bytes(b"ruyi")

    def run_interactive(
        _path: Path,
        _answers: tuple[str, ...],
        _timeout: float,
    ) -> str:
        raise version_manager.VersionManagerError("permission denied")

    with pytest.raises(version_manager.VersionManagerError, match="permission denied"):
        version_manager.run_telemetry_setup(
            binary,
            "optout",
            run_interactive=run_interactive,
        )
