"""Thin facade over ruyi's internal provisioning APIs.

This module is intentionally free of any Qt imports so that it can be unit
tested in isolation and called from any thread. The wizard pages and the
:class:`~oh_my_ruyi.workers.Worker` QObjects are its only
clients.

The flow mirrors :func:`ruyi.device.provision.do_provision_interactive` but
factors out every interactive step so that the GUI can drive them in any
order and resume after long-running operations.
"""

from __future__ import annotations

import itertools
import shutil
import subprocess
import threading
from contextlib import contextmanager
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
from ruyi.ruyipkg.repo import DEFAULT_REPO_ID

from ruyi.device.provision import (
    PackageProvisionStrategy,
    ProvisionStrategyProvider,
    get_pkg_provision_strategy,
    get_part_desc,
    make_pkg_part_map,
)

from .host_storage import (
    BlockDeviceChoice as BlockDeviceChoice,
    is_disk_or_child_mounted as is_disk_or_child_mounted,
    is_path_mounted_blkdev as is_path_mounted_blkdev,
    list_disks as list_disks,
    storage_platform_hint as storage_platform_hint,
)
from .i18n import _


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


def load_global_config(
    gm: Any,
    logger: RuyiLogger,
) -> GlobalConfig:
    """Load :class:`GlobalConfig` the same way ``ruyi.__main__`` does."""
    return GlobalConfig.load_from_config(gm, logger)


PROVISION_REPO_ID = DEFAULT_REPO_ID
_GIT_PROGRESS_LOCK = threading.RLock()


@contextmanager
def _route_git_progress(logger: RuyiLogger | None):
    """Route ruyi's internally-created Git progress console through ``logger``."""
    if logger is None:
        yield
        return
    from rich.progress import Progress
    from ruyi.ruyipkg import repo as repo_module
    from ruyi.utils import git as git_utils

    with _GIT_PROGRESS_LOCK:
        original_git_indicator = git_utils.RemoteGitProgressIndicator
        original_repo_indicator = repo_module.RemoteGitProgressIndicator

        class LoggerGitProgressIndicator(git_utils.RemoteGitProgressIndicator):  # type: ignore[misc]
            def __init__(self) -> None:
                super().__init__()
                self.p = Progress(
                    console=getattr(logger, "log_console", None),
                    transient=False,
                    redirect_stdout=False,
                    redirect_stderr=False,
                )

        git_utils.RemoteGitProgressIndicator = LoggerGitProgressIndicator
        repo_module.RemoteGitProgressIndicator = LoggerGitProgressIndicator
        try:
            yield
        finally:
            git_utils.RemoteGitProgressIndicator = original_git_indicator
            repo_module.RemoteGitProgressIndicator = original_repo_indicator


def use_provision_repo(config: GlobalConfig) -> CompositeRepo:
    """Make ``config.repo`` refer only to the official RuyiSDK repository."""
    entries = [
        entry
        for entry in config.repo_entries
        if entry.id == PROVISION_REPO_ID and entry.active
    ]
    if not entries:
        raise RuntimeError(
            f"active metadata repository '{PROVISION_REPO_ID}' is not configured"
        )

    mr = CompositeRepo(entries, config)
    config.__dict__["repo"] = mr
    return mr


def ensure_repo(config: GlobalConfig) -> CompositeRepo:
    """Ensure the ruyi metadata repo is cloned and return it.

    This clones/updates the configured git repo on first run and may take a
    while. Always call from a background thread.
    """
    mr = use_provision_repo(config)
    with _route_git_progress(getattr(config, "logger", None)):
        mr.ensure_git_repo()
    return mr


def sync_repo(config: GlobalConfig, mr: CompositeRepo) -> CompositeRepo:
    """Sync only the official RuyiSDK repository and reload its metadata."""
    with _route_git_progress(getattr(config, "logger", None)):
        mr.sync_one(PROVISION_REPO_ID)
    return use_provision_repo(config)


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
                    locked_reason=_("version cannot be overridden for slug atom"),
                )
            )
            continue

        if isinstance(atom, ExprAtom):
            selections.append(
                PackageVersionSelection(
                    original_atom=atom_str,
                    package_name=atom_str,
                    options=[PackageVersionOption(atom_str, atom_str)],
                    locked_reason=_("already has version constraints"),
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
                    locked_reason=_("package not found in repository"),
                )
            )
            continue

        versions = [
            pm for pm in versions if not pm.is_prerelease or config.include_prereleases
        ]
        versions.sort(key=lambda pm: pm.semver, reverse=True)
        if not versions:
            selections.append(
                PackageVersionSelection(
                    original_atom=atom_str,
                    package_name=pkg_fullname,
                    options=[PackageVersionOption(atom_str, atom_str)],
                    locked_reason=_("no matching versions found"),
                )
            )
            continue

        options: list[PackageVersionOption] = []
        for pm in versions:
            remarks: list[str] = []
            if pm.is_prerelease:
                remarks.append(_("prerelease"))
            if pm.service_level.has_known_issues:
                remarks.append(_("has known issues"))
            if pm.upstream_version:
                remarks.append(_("upstream: {version}", version=pm.upstream_version))
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
                locked_reason=None
                if len(options) > 1
                else _("only one version available"),
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
        VariantChoice(entity=v, id=v.id, display_name=display_name(v)) for v in variants
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
    for _pkg, strat in strategies:
        for part in strat.need_host_blkdevs(all_parts):
            if part not in seen_req:
                seen_req.add(part)
                requested.append(part)

    needed_cmds: set[str] = set(
        itertools.chain(*(strat.need_cmd for _pkg, strat in strategies))
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

    output = "\n".join(part for part in (proc.stdout, proc.stderr) if part).strip()
    if proc.returncode != 0:
        return False, output or f"fastboot devices exited with code {proc.returncode}."

    return bool(output), output or "No fastboot devices found."


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
            log.F(_("flashing failed, check your device right now"))
            return ret
    return 0


def part_description(part: PartitionKind) -> str:
    """Human description of a partition kind, e.g. for label text."""
    return get_part_desc(part)
