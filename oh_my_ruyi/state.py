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

    def reset_from_category(self) -> None:
        """Reset all selections from category selection downwards."""
        self.device = None
        self.reset_from_device()

    def reset_from_device(self) -> None:
        """Reset all selections from device selection downwards."""
        self.variant = None
        self.reset_from_variant()

    def reset_from_variant(self) -> None:
        """Reset all selections from variant selection downwards."""
        self.combo = None
        self.reset_from_combo()

    def reset_from_combo(self) -> None:
        """Reset all selections from combo selection downwards."""
        self.pkg_atoms.clear()
        self.prepared = None
        self.host_blkdev_map.clear()
        self.host_blkdev_fingerprints.clear()
        self.flash_ret = None
        self.postinst_msg = None
