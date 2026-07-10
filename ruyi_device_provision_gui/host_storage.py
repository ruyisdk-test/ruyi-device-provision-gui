"""Host disk discovery and mount-state checks.

This module owns the platform-specific storage logic used by dd-based
provisioning strategies. It deliberately has no Qt dependencies.
"""

from __future__ import annotations

import os
import pathlib
import platform
import plistlib
import subprocess
from dataclasses import dataclass
from typing import Any, Iterable

DEFAULT_DEVICE_ROOT = "/dev"


@dataclass(slots=True)
class BlockDeviceChoice:
    """A whole-disk block device offered as a dd target."""

    path: str
    display_name: str
    mounted: bool = False


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
    if platform.system() == "Darwin":
        return _darwin_disk_or_child_mounted(path)
    paths = {path, *_disk_child_paths(path)}
    return any(is_path_mounted_blkdev(candidate) for candidate in paths)


def list_disks() -> list[BlockDeviceChoice]:
    """Return whole-disk block devices for selecting a dd target."""
    system = platform.system()
    if system == "Darwin":
        return _darwin_list_disks()
    if system == "Linux":
        return _linux_list_disks()
    return []


def storage_platform_hint() -> str:
    """Return concise platform guidance for the storage selector."""
    system = platform.system()
    if system == "Darwin":
        return "Select a whole disk such as /dev/rdiskN. Mounted disks require confirmation."
    if system == "Linux":
        if _is_wsl2():
            return "Running under WSL2. Attach USB storage with usbipd before selecting /dev/sdX or similar."
        return "Select a whole disk such as /dev/sdX or /dev/nvme0n1. Mounted disks require confirmation."
    if system == "Windows":
        return "Native Windows storage flashing is not supported. Run this GUI inside WSL2 and attach USB devices with usbipd."
    return f"Storage flashing is not supported on {system}."


def _is_wsl2() -> bool:
    try:
        text = pathlib.Path("/proc/sys/kernel/osrelease").read_text(encoding="utf-8").lower()
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
            parts.append("mounted")
        choices.append(
            BlockDeviceChoice(
                path=dev_path,
                display_name=" - ".join(parts),
                mounted=mounted,
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
            parts.append("mounted")
        choices.append(
            BlockDeviceChoice(
                path=path,
                display_name=" - ".join(parts),
                mounted=mounted,
            )
        )
    return _sort_disk_choices(choices)


def _darwin_disk_or_child_mounted(path: str) -> bool:
    disk = pathlib.Path(path).name.removeprefix("r")
    info = _darwin_diskutil_plist("info", "-plist", disk)
    if info is not None and info.get("MountPoint"):
        return True
    payload = _darwin_diskutil_plist("list", "-plist", disk)
    if payload is None:
        return False
    for item in payload.get("AllDisksAndPartitions", []):
        if not isinstance(item, dict) or item.get("DeviceIdentifier") != disk:
            continue
        for part in item.get("Partitions", []):
            if not isinstance(part, dict):
                continue
            part_id = part.get("DeviceIdentifier")
            if not isinstance(part_id, str):
                continue
            part_info = _darwin_diskutil_plist("info", "-plist", part_id)
            if part_info is not None and part_info.get("MountPoint"):
                return True
    return False


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


def _disk_child_paths(path: str) -> list[str]:
    name = pathlib.Path(path).name
    sys_disk = pathlib.Path("/sys/block") / name
    if not sys_disk.is_dir():
        return []
    children: list[str] = []
    try:
        entries = sys_disk.iterdir()
    except OSError:
        return []
    for entry in entries:
        if (entry / "partition").exists():
            children.append(f"{DEFAULT_DEVICE_ROOT}/{entry.name}")
    return children


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
