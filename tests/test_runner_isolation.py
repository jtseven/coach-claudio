"""The smoke runner must not touch checkout bytecode, host HOME, or network tools."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]


class RunnerIsolationTests(unittest.TestCase):
    def test_runner_uses_disposable_sources_home_and_no_implicit_uv(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            hosthome = root / 'host-home'
            hosthome.mkdir()
            bindir = root / 'bin'
            bindir.mkdir()
            log = root / 'calls'
            spy = '#!/bin/bash\nprintf "%s|%s|%s\\n" "$HOME" "$0" "$*" >> "$SPY_LOG"\nexit 1\n'
            for name in ('python3', 'uv'):
                p = bindir / name
                p.write_text(spy)
                p.chmod(0o755)
            env = dict(os.environ, HOME=str(hosthome), PATH=str(bindir)+':/usr/bin:/bin',
                       TMPDIR=str(root), SPY_LOG=str(log))
            for key in list(env):
                if key.startswith(('COACH_', 'RUNNING_COACH_', 'TELEGRAM_', 'HEALTH_')):
                    env.pop(key)
            subprocess.run(['bash', str(REPO / 'tests/run.sh')], env=env,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
            calls = log.read_text().splitlines()
            self.assertTrue(calls)
            self.assertFalse(any(line.split('|')[0] == str(hosthome) for line in calls),
                             'smoke commands must use an isolated HOME')
            self.assertFalse(any(str(REPO / 'skill/scripts') in line for line in calls),
                             'smoke commands must operate on copied source, not the checkout')
            self.assertFalse(any('|'+str(bindir / 'uv')+'|' in line for line in calls),
                             'default offline smoke suite must not invoke a dependency resolver')


if __name__ == '__main__':
    unittest.main()
