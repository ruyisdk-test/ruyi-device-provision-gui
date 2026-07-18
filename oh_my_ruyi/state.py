"""Mutable state shared across the main window's steps."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ruyi.config import GlobalConfig
from ruyi.ruyipkg.composite_repo import CompositeRepo
from ruyi.ruyipkg.pkg_manifest import PartitionMapDecl

from .qt_logger import LogEmitter
from .ruyi_facade import (
    ComboChoice,
    DeviceChoice,
    PreparedProvision,
    VariantChoice,
)


@dataclass(slots=True)
class WizardState:
    """Mutable scratchpad accumulated as the user steps through the flow."""

    config: GlobalConfig
    emitter: LogEmitter

    # Populated by RepoInitWorker on the Welcome step.
    mr: Optional[CompositeRepo] = None

    # Choices made on the selection steps.
    device: Optional[DeviceChoice] = None
    variant: Optional[VariantChoice] = None
    combo: Optional[ComboChoice] = None

    # The package atoms of the chosen combo (possibly customized).
    pkg_atoms: list[str] = field(default_factory=list)

    # Built by run_download; consumed by Storage/Review/Flash.
    prepared: Optional[PreparedProvision] = None

    # Filled by the Storage step.
    host_blkdev_map: PartitionMapDecl = field(default_factory=dict)
    host_blkdev_fingerprints: dict[str, str] = field(default_factory=dict)

    # Outcome of the flashing step.
    flash_ret: Optional[int] = None

    # Cached message displayed on the Done step (may be empty).
    postinst_msg: Optional[str] = None
