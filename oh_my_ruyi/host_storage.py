"""Host disk discovery and mount-state checks.

This module owns the platform-specific storage logic used by dd-based
provisioning strategies. It deliberately has no Qt dependencies.
"""

from __future__ import annotations

import os
import pathlib
import platform
import plistlib
import re
import stat
import subprocess
from dataclasses import dataclass
from typing import Any, Iterable

from .i18n import tr

DEFAULT_DEVICE_ROOT = "/dev"


@dataclass(slots=True)
class BlockDeviceChoice:
    """A whole-disk block device offered as a dd target."""

    path: str
    display_name: str
    mounted: bool = False
    fingerprint: str | None = None


def is_path_mounted_blkdev(path: str) -> bool:
    """Return whether ``path`` is a currently mounted block device."""
    if not os.path.exists(path):
        return False

    from ruyi.utils import mounts

    for mount in mounts.parse_mounts():
        if not mount.source_is_blkdev:
            continue
        try:
            if mount.source_path.samefile(path):
                return True
        except (OSError, ValueError):
            continue
    return False


def is_disk_or_child_mounted(path: str) -> bool:
    """Return whether ``path`` or a child partition below it is mounted."""
    system = platform.system()
    if system == "Darwin":
        if _darwin_disk_identifier(path) is None:
            return is_path_mounted_blkdev(path)
        return _darwin_disk_or_child_mounted(path)
    if system != "Linux":
        return is_path_mounted_blkdev(path)
    related = _linux_related_block_device_ids(path)
    if related is None:
        return True
    return bool(related & _mounted_block_device_ids())


def device_fingerprint(path: str) -> str | None:
    """Return an identity used to detect target replacement before flashing."""
    try:
        path_stat = os.stat(path)
    except OSError:
        return None

    if platform.system() == "Darwin":
        disk = _darwin_disk_identifier(path)
        if disk is not None:
            info = _darwin_diskutil_plist("info", "-plist", disk)
            if info is None:
                return None
            return _darwin_fingerprint_from_info(info)

    if stat.S_ISBLK(path_stat.st_mode) or stat.S_ISCHR(path_stat.st_mode):
        parts = [
            platform.system().lower(),
            str(os.major(path_stat.st_rdev)),
            str(os.minor(path_stat.st_rdev)),
        ]
        if platform.system() == "Linux":
            sysfs_node = _linux_sysfs_node(path_stat.st_rdev)
            if sysfs_node is None:
                return None
            identity_node = (
                sysfs_node.parent if (sysfs_node / "partition").exists() else sysfs_node
            )
            parts.extend(
                [
                    str(sysfs_node),
                    str(identity_node),
                    _read_first_sysfs_text(
                        identity_node / "wwid",
                        identity_node / "device" / "wwid",
                        identity_node / "dm" / "uuid",
                        identity_node / "md" / "uuid",
                    ),
                    _read_first_sysfs_text(
                        identity_node / "device" / "serial",
                        identity_node / "serial",
                    ),
                    _read_sysfs_text(identity_node / "diskseq") or "",
                    _read_sysfs_text(sysfs_node / "size") or "",
                    _read_sysfs_text(sysfs_node / "start") or "",
                ]
            )
        return "block:" + ":".join(parts)

    return (
        f"file:{path_stat.st_dev}:{path_stat.st_ino}:"
        f"{path_stat.st_size}:{path_stat.st_mtime_ns}"
    )


def list_disks() -> list[BlockDeviceChoice]:
    """Return whole-disk block devices for selecting a dd target."""
    system = platform.system()
    if system == "Darwin":
        return _darwin_list_disks()
    if system == "Linux":
        return _linux_list_disks()
    return []


def is_native_disk_path(path: str) -> bool:
    """Return whether ``path`` names a platform block/raw disk device."""
    if platform.system() == "Darwin":
        return _darwin_disk_identifier(path) is not None
    if platform.system() == "Linux":
        return _path_block_device_id(path) is not None
    return False


def validation_is_slow() -> bool:
    """Return whether full target validation should run outside the UI thread."""
    return platform.system() == "Darwin"


def storage_platform_hint() -> str:
    """Return concise platform guidance for the storage selector."""
    system = platform.system()
    if system == "Darwin":
        return tr(
            "Select a whole disk such as /dev/rdiskN. Mounted disks require confirmation."
        )
    if system == "Linux":
        if _is_wsl2():
            return tr(
                "Running under WSL2. Attach USB storage with usbipd before selecting /dev/sdX or similar."
            )
        return tr(
            "Select a whole disk such as /dev/sdX or /dev/nvme0n1. Mounted disks require confirmation."
        )
    if system == "Windows":
        return tr(
            "Native Windows storage flashing is not supported. Run this GUI inside WSL2 and attach USB devices with usbipd."
        )
    return tr("Storage flashing is not supported on {system}.", system=system)


def _is_wsl2() -> bool:
    try:
        text = (
            pathlib.Path("/proc/sys/kernel/osrelease")
            .read_text(encoding="utf-8")
            .lower()
        )
    except OSError:
        return False
    return "microsoft" in text or "wsl" in text


def _sort_disk_choices(choices: Iterable[BlockDeviceChoice]) -> list[BlockDeviceChoice]:
    unmounted: list[BlockDeviceChoice] = []
    mounted: list[BlockDeviceChoice] = []
    for choice in choices:
        (mounted if choice.mounted else unmounted).append(choice)
    unmounted.sort(key=_block_device_sort_key)
    mounted.sort(key=_block_device_sort_key)
    return [*unmounted, *mounted]


def _linux_list_disks() -> list[BlockDeviceChoice]:
    choices: list[BlockDeviceChoice] = []
    sys_block = pathlib.Path("/sys/block")
    try:
        entries = list(sys_block.iterdir())
    except OSError:
        return []

    for dev in entries:
        name = dev.name
        if _skip_block_device_name(name):
            continue
        dev_path = f"{DEFAULT_DEVICE_ROOT}/{name}"
        if not pathlib.Path(dev_path).is_block_device():
            continue
        parts = [dev_path]
        if size := _sysfs_disk_size(dev):
            parts.append(size)
        if model := _read_sysfs_text(dev / "device" / "model"):
            parts.append(model)
        mounted = is_disk_or_child_mounted(dev_path)
        if mounted:
            parts.append(tr("mounted"))
        choices.append(
            BlockDeviceChoice(
                path=dev_path,
                display_name=" - ".join(parts),
                mounted=mounted,
                fingerprint=device_fingerprint(dev_path),
            )
        )
    return _sort_disk_choices(choices)


def _block_device_sort_key(choice: BlockDeviceChoice) -> tuple[str, str]:
    return (pathlib.Path(choice.path).name, choice.display_name)


def _skip_block_device_name(name: str) -> bool:
    prefixes = ("loop", "ram", "zram", "dm-", "md")
    return name.startswith(prefixes)


def _darwin_list_disks() -> list[BlockDeviceChoice]:
    payload = _darwin_diskutil_plist("list", "-plist")
    if payload is None:
        return []
    choices: list[BlockDeviceChoice] = []
    for disk in payload.get("WholeDisks", []):
        if not isinstance(disk, str) or not disk:
            continue
        info = _darwin_diskutil_plist("info", "-plist", disk)
        if info is None or info.get("VirtualOrPhysical") == "Virtual":
            continue
        path = f"{DEFAULT_DEVICE_ROOT}/r{disk}"
        parts = [path]
        if size := info.get("TotalSize"):
            try:
                parts.append(_format_bytes(int(size)))
            except (TypeError, ValueError):
                pass
        if name := str(info.get("MediaName") or info.get("DeviceNode") or "").strip():
            parts.append(name)
        mounted = _darwin_disk_or_child_mounted(path)
        if mounted:
            parts.append(tr("mounted"))
        choices.append(
            BlockDeviceChoice(
                path=path,
                display_name=" - ".join(parts),
                mounted=mounted,
                fingerprint=_darwin_fingerprint_from_info(info),
            )
        )
    return _sort_disk_choices(choices)


def _darwin_fingerprint_from_info(info: dict[str, Any]) -> str | None:
    stable_ids = tuple(
        str(info.get(key) or "")
        for key in (
            "MediaUUID",
            "DiskUUID",
            "VolumeUUID",
            "APFSContainerUUID",
            "APFSVolumeUUID",
        )
    )
    if not any(stable_ids):
        return None
    values = (
        info.get("DeviceIdentifier"),
        *stable_ids,
        info.get("TotalSize") or info.get("Size"),
        info.get("MediaName"),
    )
    return "darwin:" + ":".join(str(value or "") for value in values)


def _darwin_disk_or_child_mounted(path: str) -> bool:
    disk = _darwin_disk_identifier(path)
    if disk is None:
        return is_path_mounted_blkdev(path)
    payload = _darwin_diskutil_plist("list", "-plist", disk)
    if payload is None:
        return True
    identifiers = _darwin_device_identifiers(payload)
    identifiers.add(disk)
    apfs_payload = _darwin_diskutil_plist("apfs", "list", "-plist")
    if apfs_payload is None:
        if _darwin_payload_mentions_apfs(payload):
            return True
    else:
        for container in apfs_payload.get("Containers", []):
            if not isinstance(container, dict):
                continue
            physical_stores = _darwin_device_identifiers(
                container.get("PhysicalStores", [])
            )
            if physical_stores & identifiers:
                if _darwin_payload_has_mountpoint(container):
                    return True
                identifiers.update(_darwin_device_identifiers(container))
    topology_unknown = False
    for identifier in identifiers:
        info = _darwin_diskutil_plist("info", "-plist", identifier)
        if info is None:
            topology_unknown = True
        elif info.get("MountPoint"):
            return True
    return topology_unknown


def _darwin_disk_identifier(path: str) -> str | None:
    try:
        path_stat = os.stat(path)
    except OSError:
        return None
    if not (stat.S_ISBLK(path_stat.st_mode) or stat.S_ISCHR(path_stat.st_mode)):
        return None
    name = pathlib.Path(path).name
    if re.fullmatch(r"r?disk\d+(?:s\d+)*", name) is None:
        return None
    return name.removeprefix("r")


def _darwin_payload_mentions_apfs(value: object) -> bool:
    if isinstance(value, str):
        return "apfs" in value.lower()
    if isinstance(value, dict):
        return any(_darwin_payload_mentions_apfs(child) for child in value.values())
    if isinstance(value, list):
        return any(_darwin_payload_mentions_apfs(child) for child in value)
    return False


def _darwin_payload_has_mountpoint(value: object) -> bool:
    if isinstance(value, dict):
        if value.get("MountPoint"):
            return True
        return any(_darwin_payload_has_mountpoint(child) for child in value.values())
    if isinstance(value, list):
        return any(_darwin_payload_has_mountpoint(child) for child in value)
    return False


def _darwin_device_identifiers(value: object) -> set[str]:
    identifiers: set[str] = set()
    if isinstance(value, dict):
        identifier = value.get("DeviceIdentifier")
        if isinstance(identifier, str):
            identifiers.add(identifier)
        for child in value.values():
            identifiers.update(_darwin_device_identifiers(child))
    elif isinstance(value, list):
        for child in value:
            identifiers.update(_darwin_device_identifiers(child))
    return identifiers


def _darwin_diskutil_plist(*args: str) -> dict[str, Any] | None:
    try:
        proc = subprocess.run(
            ["diskutil", *args],
            check=False,
            capture_output=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    try:
        payload = plistlib.loads(proc.stdout)
    except (plistlib.InvalidFileException, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _linux_related_block_device_ids(path: str) -> set[int] | None:
    device_id = _path_block_device_id(path)
    if device_id is None:
        return set()
    root = _linux_sysfs_node(device_id)
    if root is None:
        return None

    device_ids: set[int] = set()
    visited: set[pathlib.Path] = set()
    pending = [root]
    while pending:
        node = pending.pop()
        try:
            node = node.resolve(strict=True)
        except OSError:
            return None
        if node in visited:
            continue
        visited.add(node)
        raw_dev = _read_sysfs_text(node / "dev")
        if raw_dev is None:
            return None
        try:
            major, minor = (int(part) for part in raw_dev.split(":", 1))
        except ValueError:
            return None
        device_ids.add(os.makedev(major, minor))

        try:
            pending.extend(
                child for child in node.iterdir() if (child / "partition").exists()
            )
        except OSError:
            return None
        holders = node / "holders"
        if holders.is_dir():
            try:
                pending.extend(holders.iterdir())
            except OSError:
                return None
    return device_ids


def _path_block_device_id(path: str) -> int | None:
    try:
        path_stat = os.stat(path)
    except OSError:
        return None
    return path_stat.st_rdev if stat.S_ISBLK(path_stat.st_mode) else None


def _linux_sysfs_node(device_id: int) -> pathlib.Path | None:
    node = pathlib.Path(f"/sys/dev/block/{os.major(device_id)}:{os.minor(device_id)}")
    try:
        return node.resolve(strict=True)
    except OSError:
        return None


def _mounted_block_device_ids() -> set[int]:
    from ruyi.utils import mounts

    device_ids: set[int] = set()
    for mount in mounts.parse_mounts():
        if not mount.source_is_blkdev:
            continue
        try:
            device_ids.add(mount.source_path.stat().st_rdev)
        except OSError:
            continue
    for group in _btrfs_device_groups():
        if group & device_ids:
            device_ids.update(group)
    return device_ids


def _btrfs_device_groups(
    root: pathlib.Path = pathlib.Path("/sys/fs/btrfs"),
) -> list[set[int]]:
    groups: list[set[int]] = []
    try:
        filesystems = list(root.iterdir())
    except OSError:
        return groups
    for filesystem in filesystems:
        devices_root = filesystem / "devices"
        try:
            device_dirs = list(devices_root.iterdir())
        except OSError:
            continue
        device_ids: set[int] = set()
        for device_dir in device_dirs:
            raw_dev = _read_sysfs_text(device_dir / "dev")
            if raw_dev is None:
                continue
            try:
                major, minor = (int(part) for part in raw_dev.split(":", 1))
            except ValueError:
                continue
            device_ids.add(os.makedev(major, minor))
        if device_ids:
            groups.append(device_ids)
    return groups


def _read_first_sysfs_text(*paths: pathlib.Path) -> str:
    for path in paths:
        if value := _read_sysfs_text(path):
            return value
    return ""


def _sysfs_disk_size(dev: pathlib.Path) -> str | None:
    raw = _read_sysfs_text(dev / "size")
    if raw is None:
        return None
    try:
        size = int(raw) * 512
    except ValueError:
        return None
    return _format_bytes(size)


def _read_sysfs_text(path: pathlib.Path) -> str | None:
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None


def _format_bytes(size: int) -> str:
    units = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"]
    value = float(size)
    unit = units[0]
    for unit in units:
        if value < 1024 or unit == units[-1]:
            break
        value /= 1024
    return f"{value:.1f} {unit}" if unit != "B" else f"{size} B"
