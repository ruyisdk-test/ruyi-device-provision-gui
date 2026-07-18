from __future__ import annotations

import io
import os
from pathlib import Path

import pytest

from oh_my_ruyi import version_manager


def _elf_header(machine: int, *, elf_class: int = 2) -> bytes:
    header = bytearray(64)
    header[:7] = b"\x7fELF" + bytes((elf_class, 1, 1))
    header[18:20] = machine.to_bytes(2, "little")
    return bytes(header)


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


def test_custom_release_url_requires_semver_and_arch_suffix() -> None:
    release = version_manager.release_from_url(
        "https://downloads.example/ruyi-0.53.0-beta.1-amd64"
    )

    assert release.version == "0.53.0-beta.1"
    assert release.architecture == "amd64"
    assert release.channel == "custom"

    for invalid in [
        "https://downloads.example/ruyi-0.53-amd64",
        "https://downloads.example/ruyi-0.53.0.amd64",
        "file:///tmp/ruyi-0.53.0-amd64",
    ]:
        with pytest.raises(version_manager.VersionManagerError):
            version_manager.release_from_url(invalid)


@pytest.mark.parametrize(
    ("architecture", "machine", "expected"),
    [
        ("amd64", "x86_64", True),
        ("x86_64", "AMD64", True),
        ("macos-arm64", "aarch64", True),
        ("riscv64", "x86_64", False),
        ("unknown-arch", "x86_64", False),
    ],
)
def test_architecture_compatibility_normalizes_common_aliases(
    architecture: str,
    machine: str,
    expected: bool,
) -> None:
    assert (
        version_manager.architecture_is_compatible(
            architecture,
            machine=machine,
        )
        is expected
    )


def test_version_sort_key_orders_stable_and_numeric_versions() -> None:
    versions = ["0.9.0", "0.10.0-alpha.1", "0.10.0"]

    assert sorted(versions, key=version_manager.version_sort_key, reverse=True) == [
        "0.10.0",
        "0.10.0-alpha.1",
        "0.9.0",
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
    (tmp_path / "ruyi-0.49.0").write_bytes(_elf_header(62))
    (tmp_path / "ruyi-0.52.0-alpha.20260714").write_bytes(_elf_header(243))
    (tmp_path / "unrelated").write_bytes(b"ignored")

    installed = version_manager.list_installed_versions(tmp_path)

    assert [item.version for item in installed] == [
        "0.52.0-alpha.20260714",
        "0.49.0",
    ]
    assert installed[0].size == 64
    assert installed[0].architecture == "riscv64"
    assert installed[0].channel == "testing"
    assert installed[1].architecture == "x86_64"
    assert installed[1].channel == "stable"


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        ("0.50.0", "stable"),
        ("0.50.0+build.1", "stable"),
        ("0.51.0-alpha.1", "testing"),
        ("0.51.0-beta.2", "testing"),
        ("0.51.0-rc.3", "testing"),
    ],
)
def test_version_channel_is_inferred_from_prerelease_suffix(
    version: str,
    expected: str,
) -> None:
    assert version_manager.version_channel(version) == expected


def test_binary_architecture_does_not_use_filename(tmp_path: Path) -> None:
    binary = tmp_path / "ruyi-0.50.0-amd64"
    binary.write_bytes(_elf_header(243))

    assert version_manager.binary_architecture(binary) == "riscv64"

    binary.write_bytes(b"not an executable header")
    assert version_manager.binary_architecture(binary) == "unknown"


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


def test_delete_version_refuses_active_binary(tmp_path: Path) -> None:
    directory = tmp_path / "versions"
    directory.mkdir()
    binary = directory / "ruyi-0.50.0"
    binary.write_bytes(b"ruyi")
    link = tmp_path / "bin" / "ruyi"
    link.parent.mkdir()
    link.symlink_to(binary)

    with pytest.raises(version_manager.ActiveVersionError):
        version_manager.delete_version(binary, directory, link=link)

    assert binary.exists()


def test_delete_version_removes_inactive_binary(tmp_path: Path) -> None:
    directory = tmp_path / "versions"
    directory.mkdir()
    binary = directory / "ruyi-0.49.0"
    binary.write_bytes(b"ruyi")

    deleted = version_manager.delete_version(
        binary,
        directory,
        link=tmp_path / "bin" / "ruyi",
    )

    assert deleted.version == "0.49.0"
    assert not binary.exists()


def test_deactivate_removes_only_managed_link(tmp_path: Path) -> None:
    directory = tmp_path / "versions"
    directory.mkdir()
    binary = directory / "ruyi-0.50.0"
    binary.write_bytes(b"ruyi")
    link = tmp_path / "bin" / "ruyi"
    link.parent.mkdir()
    link.symlink_to(binary)

    state = version_manager.deactivate_version(directory, link=link)

    assert not state.exists
    assert not os.path.lexists(link)
    assert binary.exists()

    link.symlink_to(tmp_path / "other-ruyi")
    with pytest.raises(version_manager.UnmanagedActivationError):
        version_manager.deactivate_version(directory, link=link)


def test_path_state_detects_managed_and_shadowed_commands(tmp_path: Path) -> None:
    directory = tmp_path / "versions"
    directory.mkdir()
    binary = directory / "ruyi-0.50.0"
    binary.write_bytes(b"ruyi")
    binary.chmod(0o755)
    managed_bin = tmp_path / "managed-bin"
    managed_bin.mkdir()
    link = managed_bin / "ruyi"
    link.symlink_to(binary)
    shadow_bin = tmp_path / "shadow-bin"
    shadow_bin.mkdir()
    shadow = shadow_bin / "ruyi"
    shadow.write_text("#!/bin/sh\n")
    shadow.chmod(0o755)

    correct = version_manager.read_path_state(
        directory,
        link=link,
        path=os.fspath(managed_bin),
    )
    shadowed = version_manager.read_path_state(
        directory,
        link=link,
        path=os.pathsep.join([os.fspath(shadow_bin), os.fspath(managed_bin)]),
    )
    missing = version_manager.read_path_state(
        directory,
        link=link,
        path="",
    )

    assert correct.correct
    assert correct.command == link
    assert not shadowed.correct
    assert shadowed.command == shadow
    assert missing.command is None


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
