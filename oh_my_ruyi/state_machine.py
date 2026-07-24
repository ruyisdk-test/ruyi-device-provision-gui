from __future__ import annotations

from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from .state import WizardState


class ProvisionStateMachine:
    """Manages the step progression and downstream invalidation for the provisioning wizard."""

    STEP_WELCOME = 0
    STEP_DEVICE = 1
    STEP_VARIANT = 2
    STEP_COMBO = 3
    STEP_VERSIONS = 4
    STEP_PACKAGES = 5
    STEP_DOWNLOAD = 6
    STEP_STORAGE = 7
    STEP_REVIEW = 8
    STEP_FLASH = 9
    STEP_DONE = 10

    TOTAL_STEPS = 11

    def __init__(self, state: WizardState, on_step_changed: Callable[[int], None]):
        self.state = state
        self._current_step = self.STEP_WELCOME
        self.on_step_changed = on_step_changed

        # Flags used to determine if steps can be opened
        self.download_ok = False
        self.download_recoverable = False
        self.flash_recoverable = False
        self.versions_visited = False

    @property
    def current_step(self) -> int:
        return self._current_step

    def set_step(self, step: int) -> None:
        if step < 0 or step >= self.TOTAL_STEPS:
            return

        if step < self._current_step:
            self.invalidate_downstream(step)

        self._current_step = step
        self.on_step_changed(step)

    def invalidate_downstream(self, dest_step: int) -> None:
        """Invalidate dependent state when moving backwards in the flow."""
        if dest_step < self.STEP_FLASH:
            self.state.flash_ret = None
            self.flash_recoverable = False
        if dest_step < self.STEP_STORAGE:
            self.state.host_blkdev_map = {}
            self.state.host_blkdev_fingerprints = {}
        if dest_step < self.STEP_DOWNLOAD:
            self.state.prepared = None
            self.download_ok = False
            self.download_recoverable = False
        if dest_step < self.STEP_VERSIONS:
            # Re-derive pkg_atoms from the combo, discarding any version
            # customization the user may have done.
            if self.state.combo is not None:
                from .ruyi_facade import combo_package_atoms

                self.state.pkg_atoms = combo_package_atoms(self.state.combo.entity)
            self.versions_visited = False
        if dest_step < self.STEP_COMBO:
            self.state.combo = None
            self.state.pkg_atoms = []
            self.versions_visited = False
        if dest_step < self.STEP_VARIANT:
            self.state.variant = None
        if dest_step < self.STEP_DEVICE:
            self.state.device = None

    def can_open_step(self, step: int) -> bool:
        """Return True if the state permits opening this step."""
        if step == self.STEP_WELCOME:
            return True
        if step == self.STEP_DEVICE:
            return self.state.mr is not None
        if step == self.STEP_VARIANT:
            return self.state.device is not None
        if step == self.STEP_COMBO:
            return self.state.variant is not None
        if step == self.STEP_VERSIONS:
            # Only allow jumping here if the TUI would actually have offered
            # customization; otherwise the page is unpopulated and would be
            # blank/confusing.
            from .ruyi_facade import is_package_version_customization_possible

            return (
                self.state.combo is not None
                and bool(self.state.pkg_atoms)
                and self.state.mr is not None
                and is_package_version_customization_possible(
                    self.state.config,
                    self.state.mr,
                    self.state.pkg_atoms,
                )
            )
        if step == self.STEP_PACKAGES:
            return self.state.combo is not None
        if step == self.STEP_DOWNLOAD:
            return bool(self.state.pkg_atoms)
        if step == self.STEP_STORAGE:
            return (
                self.download_ok
                and self.state.prepared is not None
                and bool(self.state.prepared.requested_host_blkdevs)
            )
        if step == self.STEP_REVIEW:
            return self.download_ok and self.state.prepared is not None
        if step == self.STEP_FLASH:
            return self.state.flash_ret is not None
        if step == self.STEP_DONE:
            return self.state.flash_ret == 0 or (
                self.state.combo is not None and not self.state.pkg_atoms
            )
        return False

    def next_step(self) -> None:
        self.set_step(self._current_step + 1)

    def previous_step(self) -> None:
        self.set_step(self._current_step - 1)
