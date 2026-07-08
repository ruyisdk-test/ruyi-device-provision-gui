"""Thin facade over ruyi's internal provisioning APIs.

This module is intentionally free of any Qt imports so that it can be unit
tested in isolation and called from any thread. The wizard pages and the
:class:`~ruyi_device_provision_gui.workers.Worker` QObjects are its only
clients.

The flow mirrors :func:`ruyi.device.provision.do_provision_interactive` but
factors out every interactive step so that the GUI can drive them in any
order and resume after long-running operations.
"""

from __future__ import annotations

import itertools
import os
import pathlib
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Any, Iterable

from ruyi.config import GlobalConfig
from ruyi.log import RuyiLogger
from ruyi.ruyipkg.atom import Atom, ExprAtom, SlugAtom
from ruyi.ruyipkg.composite_repo import CompositeRepo
from ruyi.ruyipkg.entity_provider import BaseEntity
from ruyi.ruyipkg.host import get_native_host
from ruyi.ruyipkg.install import do_install_atoms
from ruyi.ruyipkg.pkg_manifest import PartitionKind, PartitionMapDecl

from ruyi.device.provision import (
    PackageProvisionStrategy,
    ProvisionStrategyProvider,
    get_pkg_provision_strategy,
    get_part_desc,
    make_pkg_part_map,
)


@dataclass(slots=True)
class DeviceChoice:
    """A device offered by the provisioner config."""

    entity: BaseEntity
    id: str
    display_name: str


@dataclass(slots=True)
class VariantChoice:
    """A variant of a device."""

    entity: BaseEntity
    id: str
    display_name: str


@dataclass(slots=True)
class ComboChoice:
    """An image combo supported by a device variant."""

    entity: BaseEntity
    id: str
    display_name: str


@dataclass(slots=True)
class PackageVersionOption:
    """One selectable concrete package version."""

    atom: str
    display_name: str


@dataclass(slots=True)
class PackageVersionSelection:
    """Version choices for one package atom in the TUI customization step."""

    original_atom: str
    package_name: str
    options: list[PackageVersionOption]
    locked_reason: str | None = None


@dataclass(slots=True)
class PreparedProvision:
    """Everything needed to flash a device after packages are installed.

    Built by :func:`prepare_provision` once the user's combo choice is known
    and the packages have been downloaded/installed. The flash worker
    consumes this.
    """

    strategies: list[tuple[str, PackageProvisionStrategy]]
    pkg_part_maps: dict[str, PartitionMapDecl]
    all_parts: list[PartitionKind]
    requested_host_blkdevs: list[PartitionKind]
    needed_cmds: set[str] = field(default_factory=set)


@dataclass(slots=True)
class BlockDeviceChoice:
    """A whole-disk block device offered as a dd target."""

    path: str
    display_name: str
    mounted: bool = False


def load_global_config(
    gm: Any,
    logger: RuyiLogger,
) -> GlobalConfig:
    """Load :class:`GlobalConfig` the same way ``ruyi.__main__`` does."""
    return GlobalConfig.load_from_config(gm, logger)


def ensure_repo(config: GlobalConfig) -> CompositeRepo:
    """Ensure the ruyi metadata repo is cloned and return it.

    This clones/updates the configured git repo on first run and may take a
    while. Always call from a background thread.
    """
    mr = config.repo
    mr.ensure_git_repo()
    return mr


def sync_repo(config: GlobalConfig, mr: CompositeRepo) -> CompositeRepo:
    """Run the same repository sync step as ``ruyi update`` and reload metadata."""
    mr.sync_all()
    return CompositeRepo(config.repo_entries, config)


def list_devices(mr: CompositeRepo) -> list[DeviceChoice]:
    """Enumerate all devices the wizard knows about, sorted by display name."""
    entities = list(mr.entity_store.iter_entities("device"))
    entities.sort(key=lambda x: x.display_name or x.id)
    return [
        DeviceChoice(
            entity=e,
            id=e.id,
            display_name=e.display_name or e.id,
        )
        for e in entities
    ]


def list_entity_types(mr: CompositeRepo) -> list[str]:
    """Return entity types available in the current metadata repository."""
    return sorted(mr.entity_store.get_entity_types())


def list_package_version_selections(
    config: GlobalConfig,
    mr: CompositeRepo,
    pkg_atoms: Iterable[str],
) -> list[PackageVersionSelection]:
    """Return GUI data for the TUI's package version customization step."""
    selections: list[PackageVersionSelection] = []
    for atom_str in pkg_atoms:
        atom = Atom.parse(atom_str)
        if isinstance(atom, SlugAtom):
            selections.append(
                PackageVersionSelection(
                    original_atom=atom_str,
                    package_name=atom_str,
                    options=[PackageVersionOption(atom_str, atom_str)],
                    locked_reason="version cannot be overridden for slug atom",
                )
            )
            continue

        if isinstance(atom, ExprAtom):
            selections.append(
                PackageVersionSelection(
                    original_atom=atom_str,
                    package_name=atom_str,
                    options=[PackageVersionOption(atom_str, atom_str)],
                    locked_reason="already has version constraints",
                )
            )
            continue

        category = atom.category
        package_name = atom.name
        pkg_fullname = f"{category}/{package_name}" if category else package_name
        try:
            versions = list(mr.iter_pkg_vers(package_name, category))
        except KeyError:
            selections.append(
                PackageVersionSelection(
                    original_atom=atom_str,
                    package_name=pkg_fullname,
                    options=[PackageVersionOption(atom_str, atom_str)],
                    locked_reason="package not found in repository",
                )
            )
            continue

        versions = [pm for pm in versions if not pm.is_prerelease or config.include_prereleases]
        versions.sort(key=lambda pm: pm.semver, reverse=True)
        if not versions:
            selections.append(
                PackageVersionSelection(
                    original_atom=atom_str,
                    package_name=pkg_fullname,
                    options=[PackageVersionOption(atom_str, atom_str)],
                    locked_reason="no matching versions found",
                )
            )
            continue

        options: list[PackageVersionOption] = []
        for pm in versions:
            remarks: list[str] = []
            if pm.is_prerelease:
                remarks.append("prerelease")
            if pm.service_level.has_known_issues:
                remarks.append("has known issues")
            if pm.upstream_version:
                remarks.append(f"upstream: {pm.upstream_version}")
            remark_str = f" ({', '.join(remarks)})" if remarks else ""
            if category:
                new_atom = f"{category}/{package_name}(=={pm.ver})"
            else:
                new_atom = f"{package_name}(=={pm.ver})"
            options.append(PackageVersionOption(new_atom, f"{pm.semver}{remark_str}"))

        selections.append(
            PackageVersionSelection(
                original_atom=atom_str,
                package_name=pkg_fullname,
                options=options,
                locked_reason=None if len(options) > 1 else "only one version available",
            )
        )
    return selections


def is_package_version_customization_possible(
    config: GlobalConfig,
    mr: CompositeRepo,
    pkg_atoms: Iterable[str],
) -> bool:
    """Mirror the TUI predicate for whether customization is useful."""
    for atom_str in pkg_atoms:
        atom = Atom.parse(atom_str)
        try:
            if len(list(atom.iter_in_repo(mr, config.include_prereleases))) > 1:
                return True
        except KeyError:
            continue
    return False


def list_variants(mr: CompositeRepo, dev: BaseEntity) -> list[VariantChoice]:
    """Enumerate variants of the given device, sorted by ``variant_name``."""
    variants = list(
        mr.entity_store.traverse_related_entities(
            dev,
            entity_types=["device-variant"],
        )
    )
    variants.sort(key=lambda x: x.data.get("variant_name", x.id))

    def display_name(v: BaseEntity) -> str:
        if n := v.display_name:
            return n
        return f"{dev.display_name} ({v.data.get('variant_name', v.id)})"

    return [
        VariantChoice(entity=v, id=v.id, display_name=display_name(v))
        for v in variants
    ]


def list_combos(mr: CompositeRepo, variant: BaseEntity) -> list[ComboChoice]:
    """Enumerate image combos supported by the given variant."""
    combos = list(
        mr.entity_store.traverse_related_entities(
            variant,
            forward_refs=False,
            reverse_refs=True,
            entity_types=["image-combo"],
        )
    )
    combos.sort(key=lambda x: x.display_name or x.id)
    return [
        ComboChoice(
            entity=c,
            id=c.id,
            display_name=c.display_name or c.id,
        )
        for c in combos
    ]


def combo_package_atoms(combo: BaseEntity) -> list[str]:
    """Return the list of package atoms for the given image combo."""
    atoms = combo.data.get("package_atoms", [])
    return list(atoms) if atoms else []


def combo_postinst_msgid(combo: BaseEntity) -> str | None:
    eid = combo.data.get("postinst_msgid")
    return eid if isinstance(eid, str) else None


def get_postinst_msg(
    mr: CompositeRepo,
    combo: BaseEntity,
    lang_code: str,
) -> str | None:
    """Return the rendered post-install message for the combo, if any."""
    msgid = combo_postinst_msgid(combo)
    if msgid is None:
        return None
    return mr.messages.get_message_template(msgid, lang_code)


def run_download(
    config: GlobalConfig,
    mr: CompositeRepo,
    pkg_atoms: Iterable[str],
    *,
    fetch_only: bool = False,
    reinstall: bool = False,
) -> int:
    """Download and install the given package atoms.

    Thin wrapper around :func:`ruyi.ruyipkg.install.do_install_atoms`. Blocks
    until all packages are fetched and unpacked. Always call from a worker
    thread.
    """
    return do_install_atoms(
        config,
        mr,
        set(pkg_atoms),
        canonicalized_host=get_native_host(),
        fetch_only=fetch_only,
        reinstall=reinstall,
    )


def prepare_provision(
    config: GlobalConfig,
    mr: CompositeRepo,
    pkg_atoms: Iterable[str],
) -> PreparedProvision:
    """Build the :class:`PreparedProvision` for the given package atoms.

    Pulls together the per-package provision strategies, sorts them by
    priority (matching the CLI), and computes the set of host block device
    partitions the user will be asked to provide paths for.
    """
    atoms = list(pkg_atoms)
    strat_provider = ProvisionStrategyProvider(mr)
    strategies = [
        (pkg, get_pkg_provision_strategy(strat_provider, mr, pkg)) for pkg in atoms
    ]
    strategies.sort(key=lambda x: x[1].priority, reverse=True)

    pkg_part_maps = {pkg: make_pkg_part_map(config, mr, pkg) for pkg in atoms}

    all_parts: list[PartitionKind] = []
    seen_parts: set[PartitionKind] = set()
    for pm in pkg_part_maps.values():
        for part in pm.keys():
            if part not in seen_parts:
                seen_parts.add(part)
                all_parts.append(part)

    requested: list[PartitionKind] = []
    seen_req: set[PartitionKind] = set()
    for _, strat in strategies:
        for part in strat.need_host_blkdevs(all_parts):
            if part not in seen_req:
                seen_req.add(part)
                requested.append(part)

    needed_cmds: set[str] = set(
        itertools.chain(*(strat.need_cmd for _, strat in strategies))
    )

    return PreparedProvision(
        strategies=strategies,
        pkg_part_maps=pkg_part_maps,
        all_parts=all_parts,
        requested_host_blkdevs=requested,
        needed_cmds=needed_cmds,
    )


def compute_pretend_steps(
    prepared: PreparedProvision,
    host_blkdev_map: PartitionMapDecl,
) -> list[str]:
    """Return the human-readable list of flashing steps the wizard will perform."""
    steps: list[str] = []
    for pkg, strat in prepared.strategies:
        steps.extend(strat.pretend(prepared.pkg_part_maps[pkg], host_blkdev_map))
    return steps


def missing_cmds(prepared: PreparedProvision) -> list[str]:
    """Return the subset of needed commands not currently on ``$PATH``."""
    return sorted(c for c in prepared.needed_cmds if shutil.which(c) is None)


def needs_fastboot_confirmation(prepared: PreparedProvision) -> bool:
    return "fastboot" in prepared.needed_cmds


def check_fastboot_devices() -> tuple[bool, str]:
    """Run ``fastboot devices`` and report whether at least one device is present."""
    try:
        proc = subprocess.run(
            ["fastboot", "devices"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except FileNotFoundError:
        return False, "fastboot command was not found."
    except subprocess.TimeoutExpired:
        return False, "fastboot devices timed out."

    output = (proc.stdout + proc.stderr).strip()
    if proc.returncode != 0:
        return False, output or f"fastboot devices exited with code {proc.returncode}."

    devices = [line for line in proc.stdout.splitlines() if line.strip()]
    return bool(devices), output or "No fastboot devices found."


def run_flash(
    config: GlobalConfig,
    prepared: PreparedProvision,
    host_blkdev_map: PartitionMapDecl,
) -> int:
    """Execute every flashing strategy in priority order.

    Returns the first non-zero return code (in which case subsequent
    strategies are skipped, matching the CLI), or 0 if all succeeded. Must
    run on a worker thread because each ``flash_fn`` shells out to e.g.
    ``sudo dd`` / ``fastboot``.
    """
    log = config.logger
    for pkg, strat in prepared.strategies:
        log.D(f"flashing {pkg} with strategy {strat}")
        ret = strat.flash(prepared.pkg_part_maps[pkg], host_blkdev_map)
        if ret != 0:
            log.F("flashing failed, check your device right now")
            return ret
    return 0


def part_description(part: PartitionKind) -> str:
    """Human description of a partition kind, e.g. for label text."""
    return get_part_desc(part)


def is_path_mounted_blkdev(path: str) -> bool:
    """Return True if ``path`` is a block device that is currently mounted.

    Used to refuse paths that would be unsafe to write to.
    """
    if not os.path.exists(path):
        return False
    # Imported lazily so the GUI doesn't pay for it at import time.
    from ruyi.utils import mounts

    all_mounts = mounts.parse_mounts()
    for m in all_mounts:
        if not m.source_is_blkdev:
            continue
        try:
            if m.source_path.samefile(path):
                return True
        except (OSError, ValueError):
            continue
    return False


def is_disk_or_child_mounted(path: str) -> bool:
    """Return True if ``path`` or any sysfs child below it is mounted."""
    paths = {path, *_disk_child_paths(path)}
    return any(is_path_mounted_blkdev(p) for p in paths)


def list_disks() -> list[BlockDeviceChoice]:
    """Return whole-disk block devices for selecting a dd target."""
    unmounted: list[BlockDeviceChoice] = []
    mounted: list[BlockDeviceChoice] = []
    sys_block = pathlib.Path("/sys/block")
    try:
        entries = list(sys_block.iterdir())
    except OSError:
        return []

    for dev in entries:
        name = dev.name
        if _skip_block_device_name(name):
            continue
        dev_path = f"/dev/{name}"
        if not pathlib.Path(dev_path).is_block_device():
            continue
        parts = [dev_path]
        if size := _sysfs_disk_size(dev):
            parts.append(size)
        if model := _read_sysfs_text(dev / "device" / "model"):
            parts.append(model)
        is_mounted = is_disk_or_child_mounted(dev_path)
        if is_mounted:
            parts.append("mounted")
        choice = BlockDeviceChoice(
            path=dev_path,
            display_name=" - ".join(parts),
            mounted=is_mounted,
        )
        (mounted if is_mounted else unmounted).append(choice)
    unmounted.sort(key=_block_device_sort_key)
    mounted.sort(key=_block_device_sort_key)
    return [*unmounted, *mounted]


def _block_device_sort_key(choice: BlockDeviceChoice) -> tuple[str, str]:
    return (pathlib.Path(choice.path).name, choice.display_name)


def _skip_block_device_name(name: str) -> bool:
    prefixes = ("loop", "ram", "zram", "dm-", "md")
    return name.startswith(prefixes)


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
            children.append(f"/dev/{entry.name}")
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
