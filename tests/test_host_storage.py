from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import os

from oh_my_ruyi import host_storage


def test_disk_mount_detection_checks_children(monkeypatch) -> None:
    monkeypatch.setattr(
        host_storage,
        "_linux_related_block_device_ids",
        lambda _path: {101, 202},
    )
    monkeypatch.setattr(host_storage, "_mounted_block_device_ids", lambda: {202})
    monkeypatch.setattr(
        host_storage,
        "is_path_mounted_blkdev",
        lambda _path: False,
    )

    assert host_storage.is_disk_or_child_mounted("/dev/sda")


def test_linux_mount_detection_fails_closed_when_topology_is_unknown(
    monkeypatch,
) -> None:
    monkeypatch.setattr(host_storage.platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        host_storage,
        "_linux_related_block_device_ids",
        lambda _path: None,
    )

    assert host_storage.is_disk_or_child_mounted("/dev/sda")


def test_linux_mount_detection_follows_holder_devices(
    monkeypatch, tmp_path: Path
) -> None:
    disk = tmp_path / "sda"
    partition = disk / "sda1"
    holder = partition / "holders" / "dm-0"
    holder.mkdir(parents=True)
    (disk / "dev").write_text("8:0")
    (partition / "dev").write_text("8:1")
    (partition / "partition").write_text("")
    (holder / "dev").write_text("253:0")
    monkeypatch.setattr(
        host_storage, "_path_block_device_id", lambda _path: os.makedev(8, 0)
    )
    monkeypatch.setattr(host_storage, "_linux_sysfs_node", lambda _device_id: disk)
    monkeypatch.setattr(
        host_storage,
        "_mounted_block_device_ids",
        lambda: {os.makedev(253, 0)},
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
    monkeypatch.setattr(
        host_storage.pathlib.Path, "is_block_device", lambda _path: True
    )
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


def test_darwin_apfs_volume_marks_physical_store_mounted(monkeypatch) -> None:
    def fake_diskutil(*args: str):
        if args == ("list", "-plist", "disk0"):
            return {
                "AllDisksAndPartitions": [
                    {
                        "DeviceIdentifier": "disk0",
                        "Partitions": [
                            {
                                "DeviceIdentifier": "disk0s2",
                                "Content": "Apple_APFS",
                            }
                        ],
                    }
                ]
            }
        if args == ("apfs", "list", "-plist"):
            return {
                "Containers": [
                    {
                        "PhysicalStores": [{"DeviceIdentifier": "disk0s2"}],
                        "Volumes": [
                            {
                                "DeviceIdentifier": "disk3s1",
                                "MountPoint": "/",
                            }
                        ],
                    }
                ]
            }
        return {"DeviceIdentifier": args[-1], "MountPoint": None}

    monkeypatch.setattr(host_storage, "_darwin_disk_identifier", lambda _path: "disk0")
    monkeypatch.setattr(host_storage, "_darwin_diskutil_plist", fake_diskutil)

    assert host_storage._darwin_disk_or_child_mounted("/dev/rdisk0")


def test_darwin_file_named_disk_is_not_treated_as_device(
    monkeypatch, tmp_path: Path
) -> None:
    image = tmp_path / "disk.img"
    image.touch()
    monkeypatch.setattr(host_storage.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(host_storage, "is_path_mounted_blkdev", lambda _path: False)

    assert not host_storage.is_native_disk_path(str(image))
    assert not host_storage.is_disk_or_child_mounted(str(image))
    assert host_storage.device_fingerprint(str(image)).startswith("file:")


def test_darwin_fingerprint_requires_stable_media_id() -> None:
    assert (
        host_storage._darwin_fingerprint_from_info(
            {
                "DeviceIdentifier": "disk4",
                "Size": 64_000_000_000,
                "MediaName": "Generic STORAGE DEVICE Media",
            }
        )
        is None
    )
    fingerprint = host_storage._darwin_fingerprint_from_info(
        {
            "DeviceIdentifier": "disk4",
            "MediaUUID": "stable-media-id",
            "Size": 64_000_000_000,
            "MediaName": "Generic STORAGE DEVICE Media",
        }
    )
    assert fingerprint is not None
    assert "stable-media-id" in fingerprint
    assert "64000000000" in fingerprint


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


def test_btrfs_groups_expand_mounted_member(monkeypatch, tmp_path: Path) -> None:
    filesystem = tmp_path / "filesystem" / "devices"
    first = filesystem / "1"
    second = filesystem / "2"
    first.mkdir(parents=True)
    second.mkdir()
    (first / "dev").write_text("8:1")
    (second / "dev").write_text("8:17")

    groups = host_storage._btrfs_device_groups(tmp_path)

    assert groups == [{os.makedev(8, 1), os.makedev(8, 17)}]
