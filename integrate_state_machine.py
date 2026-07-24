import re

with open('oh_my_ruyi/main_window.py', 'r') as f:
    content = f.read()

# Add imports for ProvisionStateMachine and ActionButtonsViewModel
content = re.sub(
    r'from \.state import WizardState',
    'from .state import WizardState\nfrom .state_machine import ProvisionStateMachine\nfrom .view_models import ActionButtonsViewModel',
    content
)

# In __init__, add machine and buttons_vm initialization
content = re.sub(
    r'self\.state = WizardState\(config, self\._emitter\)\n        self\._current_step = self\.STEP_WELCOME',
    'self.state = WizardState(config, self._emitter)\n        self._machine = ProvisionStateMachine(self.state, self._on_machine_step_changed)\n        self._buttons_vm = ActionButtonsViewModel(self._machine, self._is_busy)',
    content
)

# Remove the STEP_* constants from ProvisionMainWindow
content = re.sub(r'    STEP_WELCOME = 0\n    STEP_DEVICE = 1\n    STEP_VARIANT = 2\n    STEP_COMBO = 3\n    STEP_VERSIONS = 4\n    STEP_PACKAGES = 5\n    STEP_DOWNLOAD = 6\n    STEP_STORAGE = 7\n    STEP_REVIEW = 8\n    STEP_FLASH = 9\n    STEP_DONE = 10\n', '', content)

# Replace self.STEP_* with self._machine.STEP_*
content = re.sub(r'self\.STEP_', 'self._machine.STEP_', content)

# Replace self._current_step with self._machine.current_step
# But handle assignments to self._current_step differently? Wait, there are no assignments except `self._set_step` and initialization.
content = re.sub(r'self\._current_step', 'self._machine.current_step', content)

# We need to replace the `_set_step` method. Wait, `_set_step` was:
#     def _set_step(self, step: int) -> None:
#         if step < 0 or step >= self._steps.count():
#             return
#         if step < self._current_step:
#             self._invalidate_downstream(step)
#         self._current_step = step
#         self._activate_current_step()
#         self._steps.setCurrentRow(step)
#         self._stack.setCurrentIndex(step)
#         self._refresh_step_items()
#         self._refresh_summary()
#         self._refresh_buttons()
#         QTimer.singleShot(0, self._focus_current_step)

content = re.sub(
    r'    def _set_step\(self, step: int\) -> None:.*?def _refresh_step_items\(self\) -> None:',
    '''    def _set_step(self, step: int) -> None:
        self._machine.set_step(step)

    def _on_machine_step_changed(self, step: int) -> None:
        self._activate_current_step()
        self._steps.setCurrentRow(step)
        self._stack.setCurrentIndex(step)
        self._refresh_step_items()
        self._refresh_summary()
        self._refresh_buttons()
        QTimer.singleShot(0, self._focus_current_step)

    def _refresh_step_items(self) -> None:''',
    content,
    flags=re.DOTALL
)

# We also have to delete `_invalidate_downstream`
content = re.sub(
    r'    def _invalidate_downstream\(self, dest_step: int\) -> None:.*?def _on_step_clicked\(self, row: int\) -> None:',
    '    def _on_step_clicked(self, row: int) -> None:',
    content,
    flags=re.DOTALL
)

# In `_on_step_clicked`, replace `_can_open_step` and `_current_step` etc.
# Wait, `self._is_completed_flash_history_step` needs to use `self._machine.STEP_FLASH`
# And `self._can_open_step` is replaced by `self._machine.can_open_step`
content = re.sub(r'self\._can_open_step\(', 'self._machine.can_open_step(', content)
content = re.sub(r'self\._invalidate_downstream\(', 'self._machine.invalidate_downstream(', content)

# Remove `_can_open_step` method completely
content = re.sub(
    r'    def _can_open_step\(self, step: int\) -> bool:.*?def _next_step\(self\) -> None:',
    '    def _next_step(self) -> None:',
    content,
    flags=re.DOTALL
)

# Replace `_next_step` and `_previous_step`
content = re.sub(
    r'    def _next_step\(self\) -> None:.*?def _refresh_buttons\(self\) -> None:',
    '''    def _next_step(self) -> None:
        self._machine.next_step()

    def _previous_step(self) -> None:
        self._machine.previous_step()

    def _refresh_buttons(self) -> None:''',
    content,
    flags=re.DOTALL
)

# In `_refresh_buttons`
content = re.sub(
    r'    def _refresh_buttons\(self\) -> None:.*?def _refresh_summary\(self\) -> None:',
    '''    def _refresh_buttons(self) -> None:
        state = self._buttons_vm.get_state()
        self._back_btn.setEnabled(state.back_enabled)
        self._next_btn.setEnabled(state.next_enabled)
        self._next_btn.setText(state.next_text)
        self._next_btn.setDefault(state.next_is_default)
        self._cancel_btn.setEnabled(state.cancel_enabled)
        self._cancel_btn.setText(state.cancel_text)

    def _refresh_summary(self) -> None:''',
    content,
    flags=re.DOTALL
)

# Replace self._download_ok with self._machine.download_ok
content = re.sub(r'self\._download_ok', 'self._machine.download_ok', content)
# Replace self._download_recoverable with self._machine.download_recoverable
content = re.sub(r'self\._download_recoverable', 'self._machine.download_recoverable', content)
# Replace self._flash_recoverable with self._machine.flash_recoverable
content = re.sub(r'self\._flash_recoverable', 'self._machine.flash_recoverable', content)
# Replace self._versions_visited with self._machine.versions_visited
content = re.sub(r'self\._versions_visited', 'self._machine.versions_visited', content)


with open('oh_my_ruyi/main_window.py', 'w') as f:
    f.write(content)
