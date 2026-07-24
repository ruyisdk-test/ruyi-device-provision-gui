"""Download, discover, and activate standalone ruyi package manager binaries."""

from __future__ import annotations

import argparse
import errno
import json
import os
import platform
import pty
import queue
import re
import select
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import urllib.request
import uuid
from urllib.parse import unquote, urlsplit
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Callable, Literal

from ruyi.utils.xdg_basedir import XDGBaseDir

from .i18n import locale_environment

PRIMARY_RELEASES_URL = "https://api.ruyisdk.cn/releases/latest-pm"
FALLBACK_RELEASES_URL = (
    "https://ruyisdk.org/data/api/api_ruyisdk_cn/releases_latest_pm.json"
)
DEFAULT_ACTIVATION_LINK = Path("/usr/local/bin/ruyi")
DEFAULT_SYSTEM_CONFIG = Path("/usr/share/ruyi/config.toml")
_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")
_CUSTOM_BINARY_RE = re.compile(
    r"^ruyi-(?P<version>"
    r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r")\.(?P<arch>[A-Za-z0-9_]+(?:-[A-Za-z0-9_]+)*)$"
)
_ARCH_PLATFORM_KEYS = {
    ("linux", "amd64"): "linux/x86_64",
    ("linux", "x86_64"): "linux/x86_64",
    ("linux", "aarch64"): "linux/aarch64",
    ("linux", "arm64"): "linux/aarch64",
    ("linux", "riscv64"): "linux/riscv64",
    ("darwin", "arm64"): "linux/macos-arm64",
}
_ELF_ARCHITECTURES = {
    3: "x86",
    8: "mips",
    20: "powerpc",
    21: "powerpc64",
    22: "s390x",
    40: "arm",
    62: "x86_64",
    183: "aarch64",
    258: "loongarch64",
}
_ARCHITECTURE_ALIASES = {
    "aarch64": "aarch64",
    "amd64": "x86_64",
    "arm": "arm",
    "arm64": "aarch64",
    "armhf": "arm",
    "armv7": "arm",
    "armv7l": "arm",
    "i386": "x86",
    "i486": "x86",
    "i586": "x86",
    "i686": "x86",
    "loongarch64": "loongarch64",
    "mips": "mips",
    "powerpc": "powerpc",
    "powerpc64": "powerpc64",
    "ppc": "powerpc",
    "ppc64": "powerpc64",
    "riscv32": "riscv32",
    "riscv64": "riscv64",
    "s390x": "s390x",
    "x64": "x86_64",
    "x86": "x86",
    "x86-64": "x86_64",
    "x86_64": "x86_64",
}


class VersionManagerError(RuntimeError):
    """Base error for package manager version operations."""


class UnsupportedPlatformError(VersionManagerError):
    """Raised when the release API has no binary for the current host."""


class UnmanagedActivationError(VersionManagerError):
    """Raised before replacing an activation path not owned by Oh My Ruyi."""


class ActiveVersionError(VersionManagerError):
    """Raised when attempting to delete the currently activated version."""


class DownloadCancelledError(VersionManagerError):
    """Raised when a package manager binary download is cancelled."""


class TelemetryCommandError(VersionManagerError):
    """Raised when ruyi telemetry fails after producing terminal output."""

    def __init__(self, message: str, output: str) -> None:
        super().__init__(message)
        self.output = output


@dataclass(frozen=True, slots=True)
class RuyiRelease:
    version: str
    channel: str
    release_date: str
    download_urls: tuple[str, ...]
    architecture: str = ""


@dataclass(frozen=True, slots=True)
class ReleaseCatalog:
    releases: tuple[RuyiRelease, ...]
    source_url: str


@dataclass(frozen=True, slots=True)
class InstalledVersion:
    version: str
    path: Path
    size: int
    architecture: str
    channel: str


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


@dataclass(frozen=True, slots=True)
class PathState:
    command: Path | None
    resolved_command: Path | None
    active_target: Path | None
    correct: bool


TelemetryMode = Literal["consent", "local", "optout"]


@dataclass(frozen=True, slots=True)
class TelemetrySetupResult:
    mode: TelemetryMode
    status: str
    output: str = ""


def managed_data_dir(home: Path | None = None) -> Path:
    """Return Oh My Ruyi's per-user data directory."""
    if home is None:
        return XDGBaseDir("oh-my-ruyi").app_data
    home = Path(home)
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / "oh-my-ruyi"
    return home / ".local" / "share" / "oh-my-ruyi"


def versions_dir(home: Path | None = None) -> Path:
    """Return the private per-user directory holding downloaded binaries."""
    return managed_data_dir(home) / "versions"


def telemetry_installation_path(home: Path | None = None) -> Path:
    if home is None:
        return XDGBaseDir("ruyi").app_state / "telemetry" / "installation.json"
    home = Path(home)
    if sys.platform == "darwin":
        return (
            home
            / "Library"
            / "Application Support"
            / "ruyi"
            / "telemetry"
            / "installation.json"
        )
    return home / ".local" / "state" / "ruyi" / "telemetry" / "installation.json"


def is_ruyi_externally_managed(
    config_path: Path = DEFAULT_SYSTEM_CONFIG,
) -> bool:
    """Return whether ruyi delegates version control to a system package manager."""
    try:
        with Path(config_path).open("rb") as config_file:
            config = tomllib.load(config_file)
    except (OSError, tomllib.TOMLDecodeError):
        return False
    installation = config.get("installation")
    return (
        isinstance(installation, dict)
        and installation.get("externally_managed") is True
    )


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


def normalize_architecture(architecture: str) -> str | None:
    """Return a canonical CPU architecture name for common aliases."""
    architecture = architecture.strip().lower()
    for prefix in ("darwin-", "linux-", "macos-", "darwin/", "linux/", "macos/"):
        if architecture.startswith(prefix):
            architecture = architecture.removeprefix(prefix)
            break
    return _ARCHITECTURE_ALIASES.get(architecture)


def host_architecture(*, machine: str | None = None) -> str:
    machine = machine or platform.machine()
    return normalize_architecture(machine) or machine.strip().lower()


def architecture_is_compatible(
    architecture: str,
    *,
    machine: str | None = None,
) -> bool:
    """Return whether a known architecture matches the current host CPU."""
    normalized = normalize_architecture(architecture)
    host = normalize_architecture(machine or platform.machine())
    return normalized is not None and host is not None and normalized == host


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
        releases.append(
            RuyiRelease(
                version,
                channel_name,
                release_date,
                tuple(urls),
                platform_key.rsplit("/", 1)[-1],
            )
        )

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


def release_from_url(url: str) -> RuyiRelease:
    """Parse a transient custom release from a strict standalone-binary URL."""
    parsed = urlsplit(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise VersionManagerError("download URL must use http or https")
    filename = unquote(Path(parsed.path).name)
    match = _CUSTOM_BINARY_RE.fullmatch(filename)
    if match is None:
        raise VersionManagerError(
            "URL filename must match ruyi-<semver version>.<arch>"
        )
    return RuyiRelease(
        match.group("version"),
        "custom",
        "",
        (url.strip(),),
        match.group("arch"),
    )


def list_installed_versions(directory: Path) -> tuple[InstalledVersion, ...]:
    directory = Path(directory)
    if not directory.is_dir():
        return ()
    versions = [
        inspect_installed_version(path)
        for path in directory.iterdir()
        if path.is_file()
        and not path.is_symlink()
        and path.name.startswith("ruyi-")
        and _VERSION_RE.fullmatch(path.name.removeprefix("ruyi-"))
    ]
    versions.sort(key=lambda item: _natural_version_key(item.version), reverse=True)
    return tuple(versions)


def inspect_installed_version(path: Path) -> InstalledVersion:
    path = Path(path)
    version = path.name.removeprefix("ruyi-")
    return InstalledVersion(
        version,
        path,
        path.stat().st_size,
        binary_architecture(path),
        version_channel(version),
    )


def binary_architecture(path: Path) -> str:
    """Read the executable header and return its architecture."""
    try:
        with Path(path).open("rb") as binary:
            header = binary.read(64)
    except OSError:
        return "unknown"

    if len(header) >= 20 and header.startswith(b"\x7fELF"):
        byte_order_val = {1: "little", 2: "big"}.get(header[5])
        if byte_order_val is None:
            return "unknown"
        from typing import cast, Literal

        byte_order = cast(Literal["little", "big"], byte_order_val)
        machine = int.from_bytes(header[18:20], byte_order)
        if machine == 243:
            return "riscv64" if header[4] == 2 else "riscv32"
        return _ELF_ARCHITECTURES.get(machine, "unknown")

    mach_byte_order_val = {
        b"\xfe\xed\xfa\xce": "big",
        b"\xce\xfa\xed\xfe": "little",
        b"\xfe\xed\xfa\xcf": "big",
        b"\xcf\xfa\xed\xfe": "little",
    }.get(header[:4])
    if mach_byte_order_val is not None and len(header) >= 8:
        from typing import cast, Literal

        mach_byte_order = cast(Literal["little", "big"], mach_byte_order_val)
        cpu_type = int.from_bytes(header[4:8], mach_byte_order)
        return {
            7: "x86",
            0x01000007: "x86_64",
            12: "arm",
            0x0100000C: "arm64",
        }.get(cpu_type, "unknown")
    return "unknown"


def version_channel(version: str) -> str:
    """Infer the release channel from a semantic version prerelease suffix."""
    match = re.fullmatch(
        r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)"
        r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
        r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?",
        version,
    )
    if match is None:
        return "unknown"
    return "testing" if match.group(1) else "stable"


def _natural_version_key(version: str) -> tuple[tuple[int, int | str], ...]:
    return tuple(
        (0, int(part)) if part.isdigit() else (1, part.lower())
        for part in re.split(r"(\d+)", version)
        if part
    )


def version_sort_key(version: str) -> tuple:
    """Return a stable descending-sort key for semver-like ruyi versions."""
    match = re.fullmatch(
        r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
        r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
        r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?",
        version,
    )
    if match is None:
        return (0, _natural_version_key(version))
    prerelease = match.group(4)
    prerelease_key = (
        tuple(
            (0, int(part)) if part.isdigit() else (1, part.lower())
            for part in prerelease.split(".")
        )
        if prerelease
        else ()
    )
    return (
        1,
        int(match.group(1)),
        int(match.group(2)),
        int(match.group(3)),
        1 if prerelease is None else 0,
        prerelease_key,
    )


def _open_download(url: str, timeout: float) -> BinaryIO:
    request = urllib.request.Request(url, headers={"User-Agent": "oh-my-ruyi"})
    return urllib.request.urlopen(request, timeout=timeout)  # type: ignore[return-value]


def _open_download_interruptibly(
    url: str,
    timeout: float,
    open_download: Callable[[str, float], BinaryIO],
    cancelled: Callable[[], bool] | None,
) -> BinaryIO:
    if cancelled is None:
        return open_download(url, timeout)

    result: queue.Queue[tuple[BinaryIO | None, BaseException | None]] = queue.Queue(
        maxsize=1
    )

    def connect() -> None:
        try:
            response = open_download(url, timeout)
        except BaseException as exc:  # noqa: BLE001 - forwarded to the worker
            result.put((None, exc))
            return
        if cancelled():
            try:
                response.close()
            except Exception:  # noqa: BLE001 - cancellation is already complete
                pass
            result.put((None, DownloadCancelledError("download cancelled")))
            return
        result.put((response, None))

    threading.Thread(target=connect, daemon=True).start()
    while True:
        if cancelled():
            raise DownloadCancelledError("download cancelled")
        try:
            response, error = result.get(timeout=0.05)
        except queue.Empty:
            continue
        if error is not None:
            raise error
        assert response is not None
        if cancelled():
            try:
                response.close()
            except Exception:  # noqa: BLE001 - cancellation is already complete
                pass
            raise DownloadCancelledError("download cancelled")
        return response


def download_release(
    release: RuyiRelease,
    directory: Path,
    *,
    timeout: float = 30,
    open_download: Callable[[str, float], BinaryIO] = _open_download,
    download_url: str | None = None,
    progress: Callable[[int, int], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
    response_changed: Callable[[BinaryIO | None], None] | None = None,
) -> Path:
    """Download a release atomically from a selected or fallback URL."""
    directory = Path(directory)
    destination = binary_path(release.version, directory)
    if download_url is not None and download_url not in release.download_urls:
        raise VersionManagerError("selected download URL is not part of this release")

    if destination.is_file() and destination.stat().st_size > 0:
        destination.chmod(0o755)
        if progress is not None:
            size = destination.stat().st_size
            progress(size, size)
        return destination

    directory.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []
    urls = (download_url,) if download_url is not None else release.download_urls
    for url in urls:
        temporary: Path | None = None
        try:
            if cancelled is not None and cancelled():
                raise DownloadCancelledError("download cancelled")
            with tempfile.NamedTemporaryFile(
                prefix=f".{destination.name}.",
                suffix=".download",
                dir=directory,
                delete=False,
            ) as output:
                temporary = Path(output.name)
                response = _open_download_interruptibly(
                    url,
                    timeout,
                    open_download,
                    cancelled,
                )
                with response:
                    if response_changed is not None:
                        response_changed(response)
                    try:
                        if cancelled is not None and cancelled():
                            raise DownloadCancelledError("download cancelled")
                        total = -1
                        headers = getattr(response, "headers", None)
                        if headers is not None:
                            content_length = headers.get("Content-Length")
                            if content_length is not None:
                                try:
                                    total = int(content_length)
                                except (TypeError, ValueError):
                                    total = -1
                        downloaded = 0
                        while True:
                            if cancelled is not None and cancelled():
                                raise DownloadCancelledError("download cancelled")
                            chunk = response.read(128 * 1024)
                            if not chunk:
                                break
                            output.write(chunk)
                            downloaded += len(chunk)
                            if progress is not None:
                                progress(downloaded, total)
                    finally:
                        if response_changed is not None:
                            response_changed(None)
                output.flush()
                os.fsync(output.fileno())
            if cancelled is not None and cancelled():
                raise DownloadCancelledError("download cancelled")
            if temporary.stat().st_size == 0:
                raise VersionManagerError("downloaded file is empty")
            temporary.chmod(0o755)
            os.replace(temporary, destination)
            return destination
        except DownloadCancelledError:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            raise
        except Exception as exc:  # noqa: BLE001 - try the next release mirror
            if cancelled is not None and cancelled():
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
                raise DownloadCancelledError("download cancelled") from exc
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
    version_name = target.name.removeprefix("ruyi-")
    managed = (
        target.parent == directory
        and target.name.startswith("ruyi-")
        and _VERSION_RE.fullmatch(version_name) is not None
    )
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
    binary_input = Path(binary)
    if binary_input.is_symlink():
        raise VersionManagerError("activation target is not a managed ruyi binary")
    binary = binary_input.resolve(strict=False)
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


def delete_version(
    binary: Path,
    directory: Path,
    *,
    link: Path = DEFAULT_ACTIVATION_LINK,
) -> InstalledVersion:
    """Delete one inactive managed binary from the user's version directory."""
    directory = Path(directory).resolve(strict=False)
    binary_input = Path(binary)
    if binary_input.is_symlink():
        raise VersionManagerError("delete target is not a managed ruyi binary")
    binary = binary_input.resolve(strict=False)
    if (
        binary.parent != directory
        or not binary.is_file()
        or binary.is_symlink()
        or not binary.name.startswith("ruyi-")
        or not _VERSION_RE.fullmatch(binary.name.removeprefix("ruyi-"))
    ):
        raise VersionManagerError("delete target is not a managed ruyi binary")
    state = read_activation_state(link, directory)
    if state.managed and state.target == binary:
        raise ActiveVersionError("deactivate this ruyi version before deleting it")
    installed = inspect_installed_version(binary)
    binary.unlink()
    return installed


def deactivate_version(
    directory: Path,
    *,
    link: Path = DEFAULT_ACTIVATION_LINK,
) -> ActivationState:
    """Remove only the activation symlink managed by Oh My Ruyi."""
    directory = Path(directory).resolve(strict=False)
    link = Path(link)
    state = read_activation_state(link, directory)
    if not state.exists:
        raise VersionManagerError(f"no active ruyi command at '{link}'")
    if not state.managed:
        raise UnmanagedActivationError(
            f"refusing to remove unmanaged ruyi command '{link}'"
        )
    link.unlink()
    return read_activation_state(link, directory)


def read_path_state(
    directory: Path,
    *,
    link: Path = DEFAULT_ACTIVATION_LINK,
    path: str | None = None,
    which: Callable[..., str | None] = shutil.which,
) -> PathState:
    """Inspect the first ``ruyi`` command resolved by the current PATH."""
    active = read_activation_state(link, directory)
    command_str = which("ruyi", path=path)
    if command_str is None:
        return PathState(None, None, active.target if active.managed else None, False)
    command = Path(command_str)
    resolved = command.resolve(strict=False)
    active_target = active.target if active.managed else None
    return PathState(
        command,
        resolved,
        active_target,
        active_target is not None and resolved == active_target,
    )


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
    return TelemetrySetupResult(mode, status, output)


def _run_interactive_telemetry_status(
    binary: Path,
    answers: tuple[str, ...],
    timeout: float,
) -> str:
    """Give ruyi a TTY so its normal first-run OOBE executes."""
    master_fd = -1
    slave_fd = -1
    process: subprocess.Popen[bytes] | None = None
    chunks: list[bytes] = []
    try:
        master_fd, slave_fd = pty.openpty()
        env = os.environ.copy()
        env.update(locale_environment())
        env.pop("NO_COLOR", None)
        env.update(
            {
                "COLORTERM": "truecolor",
                "COLUMNS": "120",
                "FORCE_COLOR": "1",
                "TERM": "xterm-256color",
                "TTY_COMPATIBLE": "1",
            }
        )
        process = subprocess.Popen(
            [os.fspath(binary), "telemetry", "status"],
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            close_fds=True,
            env=env,
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
                raise TelemetryCommandError(
                    "ruyi telemetry status timed out",
                    b"".join(chunks).decode(errors="replace"),
                )
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
            raise TelemetryCommandError(
                f"ruyi telemetry status exited with code {return_code}",
                output,
            )
        return output
    finally:
        if master_fd >= 0:
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
    subparsers = parser.add_subparsers(dest="action", required=True)
    activate_parser = subparsers.add_parser("activate")
    activate_parser.add_argument("binary", type=Path)
    activate_parser.add_argument("directory", type=Path)
    activate_parser.add_argument("link", type=Path)
    activate_parser.add_argument("--backup-unmanaged", action="store_true")
    deactivate_parser = subparsers.add_parser("deactivate")
    deactivate_parser.add_argument("directory", type=Path)
    deactivate_parser.add_argument("link", type=Path)
    args = parser.parse_args(argv)
    if args.action == "activate":
        result = activate_version(
            args.binary,
            args.directory,
            link=args.link,
            backup_unmanaged=args.backup_unmanaged,
        )
        payload = {
            "target": os.fspath(result.state.target) if result.state.target else None,
            "version": result.state.version,
            "backup_path": (
                os.fspath(result.backup_path) if result.backup_path else None
            ),
        }
    else:
        state = deactivate_version(args.directory, link=args.link)
        payload = {"target": None, "version": state.version, "backup_path": None}
    print(json.dumps(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
