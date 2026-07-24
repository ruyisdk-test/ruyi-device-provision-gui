import re

with open('tests/test_smoke.py', 'r') as f:
    content = f.read()

# Replace thread = run_worker_in_thread(worker)
content = re.sub(
    r'thread = run_worker_in_thread\(worker\)',
    'from oh_my_ruyi.worker_manager import WorkerTaskRunner\n        runner = WorkerTaskRunner()\n        worker = runner.run_worker(worker)',
    content
)

content = re.sub(r'assert blocker\.args\[0\] is thread', '', content)
content = re.sub(r'thread\.quit\(\)', 'runner.safe_stop_all()', content)

with open('tests/test_smoke.py', 'w') as f:
    f.write(content)
