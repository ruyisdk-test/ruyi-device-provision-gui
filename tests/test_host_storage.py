from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from ruyi_device_provision_gui import host_storage


def test_disk_mount_detection_checks_children(monkeypatch) -> None:
    monkeypatch.setattr(host_storage, "_disk_child_paths", lambda path: [f"{path}1"])
    monkeypatch.setattr(
        host_storage,
        "is_path_mounted_blkdev",
        lambda path: path == "/dev/sda1",
    )

    assert host_storage.is_disk_or_child_mounted("/dev/sda")


def test_linux_disks_sort_unmounted_before_mounted(monkeypatch) -> None:
    class FakePath:
        def __init__(self, name: str) -> None:
            self.name = name

        def __truediv__(self, _name):
            return self

    fake_paths = [FakePath("sdc"), FakePath("sda"), FakePath("sdb"), FakePath("sdd")]
    monkeypatch.setattr(host_storage.platform, "system", lambda: "Linux")
    monkeypatch.setattr(host_storage.pathlib.Path, "iterdir", lambda _path: fake_paths)
    monkeypatch.setattr(host_storage.pathlib.Path, "is_block_device", lambda _path: True)
    monkeypatch.setattr(host_storage, "_skip_block_device_name", lambda _name: False)
    monkeypatch.setattr(host_storage, "_sysfs_disk_size", lambda _dev: "32.0 GiB")
    monkeypatch.setattr(host_storage, "_read_sysfs_text", lambda _path: "Test Disk")
    monkeypatch.setattr(
        host_storage,
        "is_disk_or_child_mounted",
        lambda path: path in {"/dev/sda", "/dev/sdc"},
    )

    disks = host_storage.list_disks()

    assert [disk.path for disk in disks] == [
        "/dev/sdb",
        "/dev/sdd",
        "/dev/sda",
        "/dev/sdc",
    ]
    assert [disk.mounted for disk in disks] == [False, False, True, True]
    assert "mounted" in disks[2].display_name


def test_darwin_disks_sort_unmounted_before_mounted(monkeypatch) -> None:
    def fake_diskutil(*args: str):
        if args == ("list", "-plist"):
            return {"WholeDisks": ["disk2", "disk1"]}
        if args == ("info", "-plist", "disk1"):
            return {
                "TotalSize": 1024,
                "MediaName": "Mounted",
                "VirtualOrPhysical": "Physical",
            }
        if args == ("info", "-plist", "disk2"):
            return {
                "TotalSize": 2048,
                "MediaName": "Unmounted",
                "VirtualOrPhysical": "Physical",
            }
        return None

    monkeypatch.setattr(host_storage.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(host_storage, "_darwin_diskutil_plist", fake_diskutil)
    monkeypatch.setattr(
        host_storage,
        "_darwin_disk_or_child_mounted",
        lambda path: path == "/dev/rdisk1",
    )

    disks = host_storage.list_disks()

    assert [disk.path for disk in disks] == ["/dev/rdisk2", "/dev/rdisk1"]
    assert [disk.mounted for disk in disks] == [False, True]


def test_darwin_diskutil_rejects_invalid_plist(monkeypatch) -> None:
    monkeypatch.setattr(
        host_storage.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout=b"not a plist"),
    )

    assert host_storage._darwin_diskutil_plist("list", "-plist") is None


def test_storage_platform_hints(monkeypatch) -> None:
    monkeypatch.setattr(host_storage.platform, "system", lambda: "Windows")
    assert "WSL2" in host_storage.storage_platform_hint()

    monkeypatch.setattr(host_storage.platform, "system", lambda: "Darwin")
    assert "/dev/rdiskN" in host_storage.storage_platform_hint()

    monkeypatch.setattr(host_storage.platform, "system", lambda: "Linux")
    monkeypatch.setattr(host_storage, "_is_wsl2", lambda: True)
    assert "usbipd" in host_storage.storage_platform_hint()


def test_read_sysfs_text_handles_missing_path(tmp_path: Path) -> None:
    assert host_storage._read_sysfs_text(tmp_path / "missing") is None
