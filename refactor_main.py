import re

with open('oh_my_ruyi/main_window.py', 'r') as f:
    content = f.read()

# Remove run_worker_in_thread, safe_stop_thread imports
content = re.sub(
    r'\s+run_worker_in_thread,\s+safe_stop_thread,',
    '',
    content
)

# Replace self._worker = None with self._runner instantiation
content = re.sub(
    r'self\._worker: _BaseWorker \| None = None\s+self\._thread: QThread \| None = None',
    'self._worker: _BaseWorker | None = None\n        from .worker_manager import WorkerTaskRunner\n        self._runner = WorkerTaskRunner(self)',
    content
)

content = re.sub(
    r'self\._pm_worker: _BaseWorker \| None = None\s+self\._pm_thread: QThread \| None = None',
    'self._pm_worker: _BaseWorker | None = None\n        self._pm_runner = WorkerTaskRunner(self)',
    content
)

# Replace thread = run_worker_in_thread
# For PM:
# self._pm_thread = run_worker_in_thread(self._pm_worker) -> self._pm_runner.run_worker(self._pm_worker)
content = re.sub(
    r'self\._pm_thread = run_worker_in_thread\(self\._pm_worker\)',
    'self._pm_runner.run_worker(self._pm_worker)',
    content
)

# For normal worker:
# self._thread = run_worker_in_thread(self._worker) -> self._runner.run_worker(self._worker)
content = re.sub(
    r'self\._thread = run_worker_in_thread\(self\._worker\)',
    'self._runner.run_worker(self._worker)',
    content
)

# Replace _cleanup_pm_thread and _cleanup_thread
content = re.sub(
    r'def _cleanup_pm_thread\(self\) -> None:\n        if self\._pm_thread is not None:\n            safe_stop_thread\(self\._pm_thread\)\n            self\._pm_thread\.deleteLater\(\)\n        self\._pm_thread = None\n        self\._pm_worker = None',
    'def _cleanup_pm_thread(self) -> None:\n        self._pm_runner.cancel_all()\n        self._pm_worker = None',
    content
)

content = re.sub(
    r'def _cleanup_thread\(self\) -> None:\n        if self\._thread is not None:\n            safe_stop_thread\(self\._thread\)\n            self\._thread\.deleteLater\(\)\n        self\._thread = None\n        self\._worker = None',
    'def _cleanup_thread(self) -> None:\n        self._runner.cancel_all()\n        self._worker = None',
    content
)

# Remove all self._pm_thread is None checks and replace with self._pm_worker is None
content = re.sub(r'self\._pm_thread is None', 'self._pm_worker is None', content)
content = re.sub(r'self\._pm_thread is not None', 'self._pm_worker is not None', content)
content = re.sub(r'self\._thread is None', 'self._worker is None', content)
content = re.sub(r'self\._thread is not None', 'self._worker is not None', content)


with open('oh_my_ruyi/main_window.py', 'w') as f:
    f.write(content)
