"""Public configuration defaults must never enable external meal sync."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import tomllib
import unittest

ROOT = Path(__file__).resolve().parents[1]


class NutritionConfigurationTests(unittest.TestCase):
    def test_salt_omission_defaults_false_and_cli_requires_literal_true(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            athlete = root / 'athlete'
            athlete.mkdir()
            env = {'HOME': temp, 'COACH_HOME': temp, 'TMPDIR': temp,
                   'XDG_CONFIG_HOME': str(root / 'empty-config'),
                   'PYTHONPYCACHEPREFIX': str(root / 'bytecode')}
            out = subprocess.check_output([
                'python3', '-c',
                'import json,coachconf as c; print(json.dumps(c.get("nutrition.yazio_omit_salt")))'
            ], cwd=ROOT/'skill/scripts', env=env, text=True)
            self.assertIs(json.loads(out), False)
            template = tomllib.loads((ROOT/'skill/templates/config.toml').read_text())
            self.assertIs(template['nutrition']['yazio_omit_salt'], False)
            for value, expected in (('true', ['salt_g']), ('false', []), ('"true"', []), ('1', [])):
                with self.subTest(value=value):
                    (athlete / 'config.toml').write_text('[nutrition]\nyazio_omit_salt=' + value + '\n')
                    result = subprocess.run([
                        'python3', str(ROOT/'skill/scripts/nutrition_sync.py'), 'status'
                    ], env=env, capture_output=True, text=True, timeout=10)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(json.loads(result.stdout)['omitted_nutrients'], expected)
                    self.assertFalse((athlete / 'state').exists())

    def test_external_sync_has_explicit_false_default_and_template(self):
        with tempfile.TemporaryDirectory() as temp:
            env = dict(os.environ, COACH_HOME=temp, HOME=temp,
                       PYTHONPYCACHEPREFIX=str(Path(temp)/'bytecode'))
            out = subprocess.check_output([
                'python3', '-c',
                'import json,coachconf as c; print(json.dumps(c.get("integrations.nutrition_yazio")))'
            ], cwd=ROOT/'skill/scripts', env=env, text=True)
            self.assertIs(json.loads(out), False)
        template = tomllib.loads((ROOT/'skill/templates/config.toml').read_text())
        self.assertIs(template['integrations']['nutrition_yazio'], False)


if __name__ == '__main__':
    unittest.main()
