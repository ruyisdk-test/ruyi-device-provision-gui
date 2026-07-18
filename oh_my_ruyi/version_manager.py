"""Download, discover, and activate standalone ruyi package manager binaries."""

from __future__ import annotations

import argparse
import errno
import json
import os
import platform
import pty
import re
import select
import shutil
import subprocess
import tempfile
import time
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Callable, Literal

PRIMARY_RELEASES_URL = "https://api.ruyisdk.cn/releases/latest-pm"
FALLBACK_RELEASES_URL = (
    "https://ruyisdk.org/data/api/api_ruyisdk_cn/releases_latest_pm.json"
)
DEFAULT_ACTIVATION_LINK = Path("/usr/local/bin/ruyi")
_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")
_ARCH_PLATFORM_KEYS = {
    ("linux", "amd64"): "linux/x86_64",
    ("linux", "x86_64"): "linux/x86_64",
    ("linux", "aarch64"): "linux/aarch64",
    ("linux", "arm64"): "linux/aarch64",
    ("linux", "riscv64"): "linux/riscv64",
    ("darwin", "arm64"): "linux/macos-arm64",
}


class VersionManagerError(RuntimeError):
    """Base error for package manager version operations."""


class UnsupportedPlatformError(VersionManagerError):
    """Raised when the release API has no binary for the current host."""


class UnmanagedActivationError(VersionManagerError):
    """Raised before replacing an activation path not owned by Oh My Ruyi."""


@dataclass(frozen=True, slots=True)
class RuyiRelease:
    version: str
    channel: str
    release_date: str
    download_urls: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ReleaseCatalog:
    releases: tuple[RuyiRelease, ...]
    source_url: str


@dataclass(frozen=True, slots=True)
class InstalledVersion:
    version: str
    path: Path


@dataclass(frozen=True, slots=True)
class ActivationState:
    path: Path
    exists: bool
    is_symlink: bool
    managed: bool
    target: Path | None
    version: str | None


@dataclass(frozen=True, slots=True)
class ActivationResult:
    state: ActivationState
    backup_path: Path | None


TelemetryMode = Literal["consent", "local", "optout"]


@dataclass(frozen=True, slots=True)
class TelemetrySetupResult:
    mode: TelemetryMode
    status: str


def versions_dir(home: Path | None = None) -> Path:
    """Return the private per-user directory holding downloaded binaries."""
    home = Path.home() if home is None else Path(home)
    return home / ".local" / "share" / "oh-my-ruyi" / "versions"


def telemetry_installation_path(home: Path | None = None) -> Path:
    home = Path.home() if home is None else Path(home)
    return home / ".local" / "state" / "ruyi" / "telemetry" / "installation.json"


def host_platform_key(
    *,
    system: str | None = None,
    machine: str | None = None,
) -> str:
    system = (system or platform.system()).lower()
    machine = (machine or platform.machine()).lower()
    try:
        return _ARCH_PLATFORM_KEYS[(system, machine)]
    except KeyError as exc:
        raise UnsupportedPlatformError(
            f"ruyi binaries are not available for {system}/{machine}"
        ) from exc


def parse_release_catalog(
    payload: object, platform_key: str
) -> tuple[RuyiRelease, ...]:
    if not isinstance(payload, dict) or not isinstance(payload.get("channels"), dict):
        raise VersionManagerError("release response has no channels object")

    releases: list[RuyiRelease] = []
    channels = payload["channels"]
    for channel_name, channel_data in channels.items():
        if not isinstance(channel_name, str) or not isinstance(channel_data, dict):
            continue
        version = channel_data.get("version")
        release_date = channel_data.get("release_date", "")
        download_urls = channel_data.get("download_urls")
        if not isinstance(version, str) or not _VERSION_RE.fullmatch(version):
            raise VersionManagerError(f"invalid version in channel '{channel_name}'")
        if not isinstance(release_date, str) or not isinstance(download_urls, dict):
            raise VersionManagerError(
                f"invalid release data in channel '{channel_name}'"
            )
        urls = download_urls.get(platform_key)
        if (
            not isinstance(urls, list)
            or not urls
            or not all(
                isinstance(url, str) and url.startswith(("https://", "http://"))
                for url in urls
            )
        ):
            continue
        releases.append(RuyiRelease(version, channel_name, release_date, tuple(urls)))

    if not releases:
        raise UnsupportedPlatformError(
            f"release response has no downloads for {platform_key}"
        )
    return tuple(releases)


def _read_json_url(url: str, timeout: float) -> object:
    request = urllib.request.Request(url, headers={"User-Agent": "oh-my-ruyi"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def fetch_release_catalog(
    *,
    platform_key: str | None = None,
    timeout: float = 10,
    read_json: Callable[[str, float], object] = _read_json_url,
) -> ReleaseCatalog:
    """Fetch latest release channels, falling back to the static mirror."""
    platform_key = platform_key or host_platform_key()
    errors: list[str] = []
    for url in (PRIMARY_RELEASES_URL, FALLBACK_RELEASES_URL):
        try:
            payload = read_json(url, timeout)
            return ReleaseCatalog(parse_release_catalog(payload, platform_key), url)
        except Exception as exc:  # noqa: BLE001 - try the documented fallback
            errors.append(f"{url}: {type(exc).__name__}: {exc}")
    raise VersionManagerError("failed to fetch ruyi releases: " + "; ".join(errors))


def binary_path(version: str, directory: Path) -> Path:
    if not _VERSION_RE.fullmatch(version):
        raise VersionManagerError(f"invalid ruyi version '{version}'")
    return Path(directory) / f"ruyi-{version}"


def list_installed_versions(directory: Path) -> tuple[InstalledVersion, ...]:
    directory = Path(directory)
    if not directory.is_dir():
        return ()
    versions = [
        InstalledVersion(path.name.removeprefix("ruyi-"), path)
        for path in directory.iterdir()
        if path.is_file()
        and not path.is_symlink()
        and path.name.startswith("ruyi-")
        and _VERSION_RE.fullmatch(path.name.removeprefix("ruyi-"))
    ]
    versions.sort(key=lambda item: _natural_version_key(item.version), reverse=True)
    return tuple(versions)


def _natural_version_key(version: str) -> tuple[tuple[int, int | str], ...]:
    return tuple(
        (0, int(part)) if part.isdigit() else (1, part.lower())
        for part in re.split(r"(\d+)", version)
        if part
    )


def _open_download(url: str, timeout: float) -> BinaryIO:
    request = urllib.request.Request(url, headers={"User-Agent": "oh-my-ruyi"})
    return urllib.request.urlopen(request, timeout=timeout)  # type: ignore[return-value]


def download_release(
    release: RuyiRelease,
    directory: Path,
    *,
    timeout: float = 30,
    open_download: Callable[[str, float], BinaryIO] = _open_download,
) -> Path:
    """Download a release atomically, trying each URL supplied by the API."""
    directory = Path(directory)
    destination = binary_path(release.version, directory)
    if destination.is_file() and destination.stat().st_size > 0:
        destination.chmod(0o755)
        return destination

    directory.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []
    for url in release.download_urls:
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                prefix=f".{destination.name}.",
                suffix=".download",
                dir=directory,
                delete=False,
            ) as output:
                temporary = Path(output.name)
                with open_download(url, timeout) as response:
                    shutil.copyfileobj(response, output)
                output.flush()
                os.fsync(output.fileno())
            if temporary.stat().st_size == 0:
                raise VersionManagerError("downloaded file is empty")
            temporary.chmod(0o755)
            os.replace(temporary, destination)
            return destination
        except Exception as exc:  # noqa: BLE001 - try the next release mirror
            errors.append(f"{url}: {type(exc).__name__}: {exc}")
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    raise VersionManagerError(
        f"failed to download ruyi {release.version}: " + "; ".join(errors)
    )


def read_activation_state(link: Path, directory: Path) -> ActivationState:
    link = Path(link)
    directory = Path(directory).resolve(strict=False)
    exists = os.path.lexists(link)
    if not link.is_symlink():
        return ActivationState(link, exists, False, False, None, None)

    raw_target = Path(os.readlink(link))
    target = raw_target if raw_target.is_absolute() else link.parent / raw_target
    target = target.resolve(strict=False)
    managed = target.parent == directory and target.name.startswith("ruyi-")
    version = target.name.removeprefix("ruyi-") if managed else None
    return ActivationState(link, True, True, managed, target, version)


def next_backup_path(link: Path) -> Path:
    link = Path(link)
    candidate = link.with_name(f"{link.name}.bak")
    suffix = 1
    while os.path.lexists(candidate):
        candidate = link.with_name(f"{link.name}.bak.{suffix}")
        suffix += 1
    return candidate


def activate_version(
    binary: Path,
    directory: Path,
    *,
    link: Path = DEFAULT_ACTIVATION_LINK,
    backup_unmanaged: bool = False,
) -> ActivationResult:
    """Atomically point ``link`` at one downloaded binary.

    This function performs no privilege escalation. Callers may invoke it in a
    privileged helper after receiving the user's explicit confirmation.
    """
    directory = Path(directory).resolve(strict=False)
    binary = Path(binary).resolve(strict=False)
    link = Path(link)
    if binary.parent != directory or not binary.is_file() or binary.is_symlink():
        raise VersionManagerError("activation target is not a managed ruyi binary")

    state = read_activation_state(link, directory)
    if state.exists and not state.managed and not backup_unmanaged:
        raise UnmanagedActivationError(
            f"'{link}' exists and is not managed by Oh My Ruyi"
        )
    if state.exists and not state.is_symlink and link.is_dir():
        raise VersionManagerError(f"refusing to replace directory '{link}'")

    link.parent.mkdir(parents=True, exist_ok=True)
    temporary = link.with_name(f".{link.name}.oh-my-ruyi-{uuid.uuid4().hex}.tmp")
    backup_path: Path | None = None
    moved_existing = False
    try:
        temporary.symlink_to(binary)
        if state.exists and not state.managed:
            backup_path = next_backup_path(link)
            os.replace(link, backup_path)
            moved_existing = True
        os.replace(temporary, link)
    except BaseException:
        temporary.unlink(missing_ok=True)
        if moved_existing and backup_path is not None and not os.path.lexists(link):
            os.replace(backup_path, link)
        raise

    return ActivationResult(read_activation_state(link, directory), backup_path)


def run_telemetry_setup(
    binary: Path,
    mode: TelemetryMode,
    *,
    timeout: float = 30,
    run_interactive: Callable[[Path, tuple[str, ...], float], str] | None = None,
) -> TelemetrySetupResult:
    """Run first-install ``telemetry status`` with the graphical choices."""
    binary = Path(binary)
    if not binary.is_file():
        raise VersionManagerError(f"ruyi binary does not exist: {binary}")
    answers = {
        "consent": ("y",),
        "local": ("n", "n"),
        "optout": ("n", "y"),
    }[mode]
    runner = run_interactive or _run_interactive_telemetry_status
    output = runner(binary, answers, timeout)
    status = _telemetry_status_from_output(output, mode)
    return TelemetrySetupResult(mode, status)


def _run_interactive_telemetry_status(
    binary: Path,
    answers: tuple[str, ...],
    timeout: float,
) -> str:
    """Give ruyi a TTY so its normal first-run OOBE executes."""
    master_fd, slave_fd = pty.openpty()
    process: subprocess.Popen[bytes] | None = None
    chunks: list[bytes] = []
    try:
        process = subprocess.Popen(
            [os.fspath(binary), "telemetry", "status"],
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            close_fds=True,
            start_new_session=True,
        )
        os.close(slave_fd)
        slave_fd = -1
        os.write(master_fd, ("\n".join(answers) + "\n").encode())
        deadline = time.monotonic() + timeout
        while True:
            if time.monotonic() >= deadline:
                process.kill()
                process.wait()
                raise VersionManagerError("ruyi telemetry status timed out")
            readable, _, _ = select.select([master_fd], [], [], 0.1)
            if readable:
                try:
                    chunk = os.read(master_fd, 4096)
                except OSError as exc:
                    if exc.errno == errno.EIO or process.poll() is not None:
                        break
                    raise VersionManagerError(
                        f"failed to read ruyi telemetry output: {exc}"
                    ) from exc
                if not chunk:
                    break
                chunks.append(chunk)
            if process.poll() is not None and not readable:
                break
        return_code = process.wait()
        output = b"".join(chunks).decode(errors="replace")
        if return_code != 0:
            raise VersionManagerError(
                output.strip()
                or f"ruyi telemetry status exited with code {return_code}"
            )
        return output
    finally:
        os.close(master_fd)
        if slave_fd >= 0:
            os.close(slave_fd)
        if process is not None and process.poll() is None:
            process.kill()
            process.wait()


def _telemetry_status_from_output(output: str, mode: TelemetryMode) -> str:
    plain = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", output).replace("\r", "")
    statuses = [
        line.strip()
        for line in plain.splitlines()
        if line.strip() in {"on", "local", "off"}
    ]
    if statuses:
        return statuses[-1]
    return {"consent": "on", "local": "local", "optout": "off"}[mode]


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("binary", type=Path)
    parser.add_argument("directory", type=Path)
    parser.add_argument("link", type=Path)
    parser.add_argument("--backup-unmanaged", action="store_true")
    args = parser.parse_args(argv)
    result = activate_version(
        args.binary,
        args.directory,
        link=args.link,
        backup_unmanaged=args.backup_unmanaged,
    )
    print(
        json.dumps(
            {
                "target": os.fspath(result.state.target),
                "version": result.state.version,
                "backup_path": (
                    os.fspath(result.backup_path) if result.backup_path else None
                ),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
