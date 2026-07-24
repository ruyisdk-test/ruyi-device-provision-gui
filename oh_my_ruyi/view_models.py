from __future__ import annotations

from typing import TYPE_CHECKING, Callable
from dataclasses import dataclass

if TYPE_CHECKING:
    from .state_machine import ProvisionStateMachine


@dataclass(slots=True)
class ActionButtonsState:
    """State for the bottom action buttons."""

    back_enabled: bool = False
    next_enabled: bool = False
    next_text: str = ""  # Uses translation string or empty for default
    next_is_default: bool = False
    cancel_enabled: bool = True
    cancel_text: str = ""  # Uses translation string or empty for default


class ActionButtonsViewModel:
    """ViewModel mapping the ProvisionStateMachine to ActionButtonsState."""

    def __init__(self, machine: ProvisionStateMachine, is_busy_cb: Callable[[], bool]):
        self.machine = machine
        self.is_busy_cb = is_busy_cb

    def get_state(self) -> ActionButtonsState:
        state = ActionButtonsState()
        step = self.machine.current_step
        machine = self.machine
        is_busy = self.is_busy_cb()

        # Default button texts
        from .i18n import _

        if step == machine.STEP_DONE:
            state.back_enabled = machine.state.flash_ret != 0
            state.next_enabled = True
            state.next_text = _("Finish")
            state.next_is_default = True
            state.cancel_enabled = False
            return state

        if step == machine.STEP_FLASH:
            state.back_enabled = machine.flash_recoverable and not is_busy
            state.next_enabled = False
            state.cancel_enabled = True
            state.cancel_text = _("Interrupt") if is_busy else _("Cancel")
            return state

        # Normal steps
        state.back_enabled = not is_busy and step > machine.STEP_WELCOME
        state.cancel_enabled = True
        state.cancel_text = _("Cancel")

        if step == machine.STEP_WELCOME:
            state.next_enabled = not is_busy and machine.state.mr is not None
        elif step == machine.STEP_DEVICE:
            state.next_enabled = not is_busy and machine.state.device is not None
        elif step == machine.STEP_VARIANT:
            state.next_enabled = not is_busy and machine.state.variant is not None
        elif step == machine.STEP_COMBO:
            state.next_enabled = not is_busy and machine.state.combo is not None
        elif step == machine.STEP_VERSIONS:
            state.next_enabled = not is_busy
        elif step == machine.STEP_PACKAGES:
            state.next_enabled = not is_busy
        elif step == machine.STEP_DOWNLOAD:
            state.next_enabled = not is_busy and machine.download_ok
            if machine.download_recoverable and not is_busy and not machine.download_ok:
                state.next_text = _("Retry")
                state.next_enabled = True
        elif step == machine.STEP_STORAGE:
            state.next_enabled = not is_busy and bool(machine.state.host_blkdev_map)
        elif step == machine.STEP_REVIEW:
            state.next_enabled = not is_busy and bool(machine.state.host_blkdev_map)
            state.next_text = _("Flash")

        if not state.next_text:
            state.next_text = _("Next")

        # Default behavior: Next button is default if it's enabled and we're not busy
        state.next_is_default = state.next_enabled

        return state
