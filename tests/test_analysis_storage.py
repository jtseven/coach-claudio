"""Exercise storage transactions in disposable homes, including overlapping reads."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class AnalysisStorage(unittest.TestCase):
    def test_concurrent_load_merges_preserve_both_updates(self):
        with tempfile.TemporaryDirectory() as tmp:
            code = '''
import contextlib, io, json, multiprocessing
from types import SimpleNamespace
import analyze
ctx = multiprocessing.get_context("fork")
barrier = ctx.Barrier(2)
def worker(activity_id):
    original = analyze.read_load_log
    def simultaneous_read():
        snapshot = original()
        barrier.wait(timeout=10)
        return snapshot
    analyze.read_load_log = simultaneous_read
    activity = dict(id=activity_id, sport_type="Run", start_local="2026-10-01", relative_effort=10)
    args = SimpleNamespace(activities=json.dumps([activity]), save=True, until="2026-10-02", days=42, json=True)
    with contextlib.redirect_stdout(io.StringIO()):
        analyze.cmd_load(args)
workers = [ctx.Process(target=worker, args=(i,)) for i in (1, 2)]
for worker in workers: worker.start()
for worker in workers:
    worker.join(timeout=15)
    if worker.is_alive():
        worker.kill()
        worker.join()
        raise RuntimeError("writer did not finish")
    assert worker.exitcode == 0, worker.exitcode
print(json.dumps(analyze.read_load_log()))
'''
            env = {**os.environ, 'COACH_HOME': tmp, 'HOME': tmp,
                   'PYTHONDONTWRITEBYTECODE': '1',
                   'PYTHONPATH': str(ROOT/'skill/scripts')}
            env.pop('COACH_DRY_RUN', None)
            p = subprocess.run(['python3', '-c', code], env=env,
                               capture_output=True, text=True, timeout=40)
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertEqual(set(json.loads(p.stdout)), {'1', '2'})
            self.assertEqual(list((Path(tmp)/'athlete/state').glob('load-*.tmp')), [])

    def test_save_does_not_replace_corrupt_load_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'athlete/state/load_log.json'
            path.parent.mkdir(parents=True)
            path.write_text('{broken')
            env = {**os.environ, 'COACH_HOME': tmp, 'HOME': tmp,
                   'PYTHONDONTWRITEBYTECODE': '1'}
            env.pop('COACH_DRY_RUN', None)
            p = subprocess.run(['python3', str(ROOT/'skill/scripts/analyze.py'),
                                'load', '--activities', json.dumps([dict(id=1, sport_type='Run',
                                    start_local='2026-10-01', relative_effort=5)]), '--save'],
                               env=env, capture_output=True, text=True)
            self.assertNotEqual(p.returncode, 0)
            self.assertEqual(path.read_text(), '{broken')


if __name__ == '__main__':
    unittest.main()
