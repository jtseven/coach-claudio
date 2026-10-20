"""Offline delivery integration tests; all state and executables are temporary."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'skill/scripts'


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='delivery-', dir=os.environ['TMPDIR'])
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        (self.home / 'athlete/state').mkdir(parents=True)
        self.state = self.home / 'athlete/state'
        (self.home / 'athlete/config.toml').write_text('''[athlete]
name="Test"
language="English"
timezone="UTC"
[goal]
race="Test"
date="2030-01-01"
[physiology]
hrmax=180
easy_ceiling=140
[integrations]
telegram=true
''')
        for name in ('profile.md', 'plan.md'):
            (self.home / 'athlete' / name).write_text('Synthetic fixture only')
        self.fake = self.home / 'model'
        self.fake.write_text('''#!/usr/bin/env python3
import json, os
from pathlib import Path
h=Path(os.environ['COACH_HOME'])
with (h/'calls').open('a') as f: f.write(json.dumps(__import__('sys').argv[1:])+'\\n')
print((h/'activities').read_text() if '--model' in __import__('sys').argv else 'A reaction')
''')
        self.fake.chmod(0o755)
        self.curl = self.home / 'curl'
        self.curl.write_text('''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
h=Path(os.environ['COACH_HOME'])
a=sys.argv[1:]
with (h/'sends').open('a') as f: f.write(json.dumps(a)+'\\n')
reject = (h/'fail-send').exists() or ((h/'reject-call').exists() and
         len((h/'sends').read_text().splitlines()) == int((h/'reject-call').read_text()))
body=json.dumps({'ok': not reject, 'result': {'message_id': 42}})
if '-o' in a:
    Path(a[a.index('-o')+1]).write_text(body)
    print('200')
else:
    print(body)
''')
        self.curl.chmod(0o755)
        (self.home / '.local/bin').mkdir(parents=True)
        (self.home / '.local/bin/curl').symlink_to(self.curl)
        self.env = {'HOME': str(self.home), 'COACH_HOME': str(self.home),
                    'PATH': '/usr/local/bin:/usr/bin:/bin', 'TMPDIR': str(self.home),
                    'CLAUDE_BIN': str(self.fake), 'COACH_CURL_BIN': str(self.curl),
                    'TELEGRAM_ENV': str(self.home / 'absent'),
                    'TELEGRAM_CHAT_ID': '123', 'TELEGRAM_BOT_TOKEN': 'fake'}

    def run_script(self, script, *args):
        return subprocess.run(['bash', str(SCRIPTS / script), *args], env=self.env,
                              text=True, capture_output=True, timeout=20)

    def acts(self, *ids):
        from datetime import datetime
        (self.home / 'activities').write_text(json.dumps([
            {'id': i, 'sport_type': 'Run', 'start_local': datetime.now().isoformat()}
            for i in ids]))

    def calls(self, name):
        p = self.home / name
        return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []

    def test_first_poll_seeds_then_reacts_to_every_new_activity(self):
        self.acts(1)
        self.assertEqual(self.run_script('post_run_watch.sh').returncode, 0)
        self.assertEqual(self.calls('sends'), [])
        self.acts(3, 2, 1)
        result = self.run_script('post_run_watch.sh')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.calls('sends')), 2)
        self.assertEqual(self.run_script('post_run_watch.sh').returncode, 0)
        self.assertEqual(len(self.calls('sends')), 2)

    def test_api_rejection_keeps_pending_and_retries_saved_message(self):
        self.acts(1)
        self.run_script('post_run_watch.sh')
        self.acts(2, 1)
        (self.home / 'fail-send').touch()
        result = self.run_script('post_run_watch.sh')
        self.assertNotEqual(result.returncode, 0)
        generated = [x for x in self.calls('calls') if '--model' not in x]
        self.assertEqual(len(generated), 1)
        (self.home / 'fail-send').unlink()
        # Retry must survive both a new process and the activity falling off the list.
        self.acts(1)
        result = self.run_script('post_run_watch.sh')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.calls('sends')), 2)
        self.assertEqual(len([x for x in self.calls('calls') if '--model' not in x]), 1)

    def test_model_coverage_is_staged_until_send_confirmation(self):
        self.fake.write_text('''#!/usr/bin/env python3
import os, subprocess, sys
subprocess.run([sys.executable, os.environ['COACH_TEST_LOGGER'], 'add',
                '--mode', 'postrun', '--ids', '77', '--note', 'A reaction'], check=True,
               stdout=sys.stderr)
print('A reaction')
''')
        self.env['COACH_TEST_LOGGER'] = str(SCRIPTS / 'coachlog.py')
        (self.home / 'fail-send').touch()
        result = self.run_script('check_in.sh', 'postrun', '77')
        self.assertNotEqual(result.returncode, 0)
        logfile = self.state / 'checkin_log.jsonl'
        self.assertFalse(logfile.exists() and logfile.read_text().strip(), 'undelivered message covered activity')
        (self.home / 'fail-send').unlink()
        result = self.run_script('check_in.sh', 'postrun', '77')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(logfile.read_text())['activity_ids'], [77])
        self.run_script('check_in.sh', 'postrun', '77')
        self.assertEqual(len(logfile.read_text().splitlines()), 1)

    def test_migration_does_not_replay_seen_or_covered_history(self):
        (self.state / 'postrun_seen.txt').write_text('1\n')
        (self.state / 'checkin_log.jsonl').write_text(json.dumps({
            'mode': 'evening', 'activity_ids': [2], 'note': 'Already discussed'}) + '\n')
        self.acts(3, 2, 1)
        result = self.run_script('post_run_watch.sh')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.calls('sends')), 1)

    def test_skip_is_terminal_without_claiming_delivery_or_coverage(self):
        (self.state / 'postrun_seen.txt').write_text('1\n')
        self.acts(2, 1)
        self.fake.write_text(self.fake.read_text().replace("else 'A reaction'", "else 'SKIP'"))
        result = self.run_script('post_run_watch.sh')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls('sends'), [])
        import sqlite3
        with sqlite3.connect(self.state / 'delivery.sqlite3') as db:
            self.assertEqual(db.execute("SELECT status FROM activity WHERE id='2'").fetchone()[0], 'skipped')
        self.run_script('post_run_watch.sh')
        self.assertEqual(len([x for x in self.calls('calls') if '--model' not in x]), 1)
        self.assertFalse((self.state / 'checkin_log.jsonl').exists())

    def test_no_send_creates_no_state_or_outbox(self):
        before = sorted(p.relative_to(self.home) for p in self.state.rglob('*'))
        result = self.run_script('check_in.sh', 'morning', '--no-send')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(sorted(p.relative_to(self.home) for p in self.state.rglob('*')), before)
        self.assertEqual(self.calls('sends'), [])
        self.assertEqual(list((self.home / 'outbox').iterdir()), [])

    def test_alert_rejects_api_error_and_has_bounded_transport(self):
        (self.home / 'fail-send').touch()
        result = subprocess.run(['bash', '-c', 'source "$1"; alert "Synthetic alert"',
                                 'test', str(SCRIPTS / 'lib.sh')], env=self.env,
                                capture_output=True, text=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        args = self.calls('sends')[0]
        self.assertIn('--connect-timeout', args)
        self.assertIn('--max-time', args)

    def test_poll_recovers_scheduled_outbox_without_resending_confirmed_chunk(self):
        self.fake.write_text(self.fake.read_text().replace("else 'A reaction'", "else ('a'*4000 + 'tail')"))
        (self.home / 'reject-call').write_text('2')
        result = self.run_script('check_in.sh', 'morning')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(self.calls('sends')), 2)
        (self.home / 'reject-call').unlink()
        (self.home / 'activities').write_text('not json')
        result = self.run_script('post_run_watch.sh')
        self.assertEqual(result.returncode, 0, result.stderr)
        sends = self.calls('sends')
        self.assertEqual(len(sends), 3)
        self.assertIn('text=tail', sends[-1])
        self.assertEqual(len([x for x in self.calls('calls') if '--model' not in x]), 1)

    def test_direct_postrun_already_covered_does_not_generate_or_send(self):
        (self.state / 'checkin_log.jsonl').write_text(json.dumps({
            'mode': 'evening', 'activity_ids': [77], 'note': 'Already discussed'}) + '\n')
        result = self.run_script('check_in.sh', 'postrun', '77')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls('calls'), [])
        self.assertEqual(self.calls('sends'), [])

    def test_scheduled_generation_failure_alert_is_bounded_and_best_effort(self):
        self.fake.write_text(self.fake.read_text() + '''
print('synthetic generation diagnostic', file=__import__('sys').stderr)
raise SystemExit(9)
''')
        (self.home / 'fail-send').touch()
        result = self.run_script('check_in.sh', 'morning')
        self.assertEqual(result.returncode, 9, result.stderr)
        sends = self.calls('sends')
        self.assertEqual(len(sends), 1)
        self.assertIn('--connect-timeout', sends[0])
        self.assertIn('--max-time', sends[0])
        self.assertTrue(any('morning' in arg and 'failed' in arg.lower() for arg in sends[0]))
        outputs = list((self.home / 'outbox').glob('*.txt'))
        self.assertEqual(len(outputs), 1)
        self.assertIn('synthetic generation diagnostic', outputs[0].read_text())

    def test_generation_timeout_saves_captured_output_before_propagation(self):
        code = '''
import subprocess
from unittest.mock import patch
import delivery
original_run = subprocess.run
def run(cmd, **kwargs):
    if cmd[0] == delivery.os.environ['CLAUDE_BIN']:
        raise subprocess.TimeoutExpired(cmd, 900, output=b'partial generated text',
                                        stderr=b'timeout diagnostic')
    return original_run(cmd, **kwargs)
with patch.object(delivery.subprocess, 'run', side_effect=run):
    try:
        delivery.checkin('evening', '', '', False, 'synthetic prompt')
    except subprocess.TimeoutExpired:
        print('timeout propagated')
    else:
        raise AssertionError('timeout was swallowed')
'''
        env = dict(self.env, PYTHONPATH=str(SCRIPTS))
        result = subprocess.run([sys.executable, '-c', code], env=env,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('timeout propagated', result.stdout)
        outputs = list((self.home / 'outbox').glob('*.txt'))
        self.assertEqual(len(outputs), 1)
        self.assertIn('partial generated text', outputs[0].read_text())
        self.assertIn('timeout diagnostic', outputs[0].read_text())
        self.assertEqual(len(self.calls('sends')), 1)

    def test_generation_failure_is_saved_and_pending_activity_retries(self):
        self.fake.write_text(self.fake.read_text() + '''
if '--model' not in __import__('sys').argv and (h/'fail-generation').exists():
    raise SystemExit(9)
''')
        (self.state / 'postrun_seen.txt').write_text('1\n')
        self.acts(2, 1)
        (self.home / 'fail-generation').touch()
        result = self.run_script('post_run_watch.sh')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.calls('sends'), [])
        outputs = list((self.home / 'outbox').glob('*.txt'))
        self.assertEqual(len(outputs), 1)
        self.assertIn('A reaction', outputs[0].read_text())
        (self.home / 'fail-generation').unlink()
        result = self.run_script('post_run_watch.sh')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.calls('sends')), 1)

    def test_detection_alerts_once_after_four_failures_and_on_recovery(self):
        (self.home / 'activities').write_text('not json')
        for _ in range(5):
            self.run_script('post_run_watch.sh')
        self.assertEqual(len(self.calls('sends')), 1)
        self.acts(1)
        self.run_script('post_run_watch.sh')
        self.assertEqual(len(self.calls('sends')), 2)
        self.run_script('post_run_watch.sh')
        self.assertEqual(len(self.calls('sends')), 2)

    def test_recovery_commits_coverage_after_confirmed_send_even_with_torn_log_tail(self):
        self.fake.write_text('''#!/usr/bin/env python3
import os, subprocess, sys
subprocess.run([sys.executable, os.environ['COACH_TEST_LOGGER'], 'add',
                '--mode', 'postrun', '--ids', '77', '--note', 'A reaction'], check=True,
               stdout=sys.stderr)
print('A reaction')
''')
        self.env['COACH_TEST_LOGGER'] = str(SCRIPTS / 'coachlog.py')
        (self.home / 'fail-send').touch()
        self.run_script('check_in.sh', 'postrun', '77')
        # Simulate durable API confirmation followed by a crash mid-log append.
        import sqlite3
        with sqlite3.connect(self.state / 'delivery.sqlite3') as db:
            db.execute("UPDATE job SET next_chunk=1 WHERE id='postrun:77'")
        (self.state / 'checkin_log.jsonl').write_text('{"delivery_id":"postrun:77:0"')
        (self.home / 'fail-send').unlink()
        result = self.run_script('check_in.sh', 'postrun', '77')
        self.assertEqual(result.returncode, 0, result.stderr)
        covered = subprocess.run([sys.executable, str(SCRIPTS / 'coachlog.py'), 'covered', '77'],
                                 env=self.env, capture_output=True, text=True)
        self.assertEqual(covered.returncode, 0, covered.stdout)
        self.assertEqual(len(self.calls('sends')), 1, 'confirmed chunk was resent')
        self.run_script('check_in.sh', 'postrun', '77')
        valid = []
        for line in (self.state / 'checkin_log.jsonl').read_text().splitlines():
            try:
                valid.append(json.loads(line))
            except ValueError:
                pass
        self.assertEqual(len(valid), 1)

    def test_explicit_retry_recovers_outbox_without_running_model(self):
        (self.home / 'fail-send').touch()
        self.run_script('check_in.sh', 'evening')
        (self.home / 'fail-send').unlink()
        result = subprocess.run([sys.executable, str(SCRIPTS / 'delivery.py'), 'retry'],
                                env=self.env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.calls('sends')), 2)
        self.assertEqual(len(self.calls('calls')), 1)

    def test_disabled_telegram_preserves_scheduled_outbox_on_watch_and_retry(self):
        import sqlite3
        (self.home / 'fail-send').touch()
        result = self.run_script('check_in.sh', 'morning')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(self.calls('sends')), 1)
        (self.home / 'fail-send').unlink()
        config = self.home / 'athlete/config.toml'
        config.write_text(config.read_text().replace('telegram=true', 'telegram=false'))
        self.acts(1)
        for command in ('watch', 'retry'):
            with self.subTest(command=command):
                result = subprocess.run([sys.executable, str(SCRIPTS / 'delivery.py'), command],
                                        env=self.env, capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(len(self.calls('sends')), 1)
                with sqlite3.connect(self.state / 'delivery.sqlite3') as db:
                    self.assertEqual(db.execute('SELECT status, next_chunk FROM job').fetchone(),
                                     ('pending', 0))
        config.write_text(config.read_text().replace('telegram=false', 'telegram=true'))
        result = subprocess.run([sys.executable, str(SCRIPTS / 'delivery.py'), 'retry'],
                                env=self.env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.calls('sends')), 2)
        self.assertEqual(len([x for x in self.calls('calls') if '--model' not in x]), 1)

    def test_live_poll_lock_is_not_discarded_based_on_age(self):
        import fcntl
        lockpath = self.state / 'postrun.poll.lock'
        with lockpath.open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            os.utime(lockpath, (1, 1))
            self.acts(1)
            result = self.run_script('post_run_watch.sh')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(self.calls('calls'), [])
            self.assertTrue(lockpath.exists())
        # Kernel release, not deletion or an age heuristic, permits the next poll.
        result = self.run_script('post_run_watch.sh')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.calls('calls')), 1)

    def test_commute_stale_and_future_activities_do_not_trigger(self):
        from datetime import datetime, timedelta
        (self.state / 'postrun_seen.txt').write_text('1\n')
        now = datetime.now()
        (self.home / 'activities').write_text(json.dumps([
            {'id': 2, 'sport_type': 'EBikeRide', 'start_local': now.isoformat()},
            {'id': 3, 'sport_type': 'Run', 'start_local': (now-timedelta(days=1)).isoformat()},
            {'id': 4, 'sport_type': 'Run', 'start_local': (now+timedelta(days=1)).isoformat()},
            {'id': 5, 'sport_type': 'Run', 'start_local': 'invalid'},
        ]))
        result = self.run_script('post_run_watch.sh')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls('sends'), [])
        self.assertEqual(len(self.calls('calls')), 1)


if __name__ == '__main__':
    unittest.main()
