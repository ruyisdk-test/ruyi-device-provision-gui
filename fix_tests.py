import re

with open('tests/test_main_window_interactions.py', 'r') as f:
    content = f.read()

# Replace run_worker_in_thread mocking
content = re.sub(
    r'thread = object\(\)\n\s+monkeypatch\.setattr\(main_window, "run_worker_in_thread", lambda _worker: thread\)',
    'monkeypatch.setattr("oh_my_ruyi.worker_manager.WorkerTaskRunner.run_worker", lambda self, worker, *args, **kwargs: worker)',
    content
)

# Replace window._thread checks
content = re.sub(r'assert window\._thread is thread', 'assert window._worker is not None', content)
content = re.sub(r'window\._thread = None', '', content)
content = re.sub(r'assert window\._pm_thread is thread', 'assert window._pm_worker is not None', content)
content = re.sub(r'window\._pm_thread = None', '', content)
content = re.sub(r'assert window\._pm_thread is None', 'assert window._pm_worker is None', content)
content = re.sub(r'assert window\._thread is None', 'assert window._worker is None', content)


with open('tests/test_main_window_interactions.py', 'w') as f:
    f.write(content)
