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
