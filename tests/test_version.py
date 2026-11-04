"""버전 단일 출처와 --version 출력을 검증한다."""
import contextlib
import io
import os
from pathlib import Path
import re
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'lib'))
import skim
from skim.cli import main


class VersionTests(unittest.TestCase):
    def test_version_is_1_0_0(self):
        self.assertEqual(skim.__version__, '1.0.0')

    def test_cli_version_flag(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as raised:
            main(['--version'])
        self.assertEqual(raised.exception.code, 0)
        self.assertEqual(out.getvalue().strip(), f'skim {skim.__version__}')

    def test_entry_points_print_version(self):
        env = dict(os.environ, PYTHONPATH=str(ROOT / 'lib'))
        env.pop('SKIM_PYTHON', None)
        commands = ([str(ROOT / 'bin' / 'skim'), '--version'],
                    [sys.executable, '-m', 'skim', '--version'])
        for command in commands:
            with self.subTest(command=command[0]):
                result = subprocess.run(command, capture_output=True, text=True, env=env, timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, f'skim {skim.__version__}\n')

    def test_version_defined_only_in_package_init(self):
        literal = re.compile(r'(?<![\d.])' + re.escape(skim.__version__) + r'(?![\d.])')
        sources = [ROOT / 'bin' / 'skim', ROOT / 'setup.sh', *(ROOT / 'lib').rglob('*.py'), *(ROOT / 'lib').glob('*.sh')]
        found = [str(path.relative_to(ROOT)) for path in sources
                 if path != ROOT / 'lib' / 'skim' / '__init__.py' and literal.search(path.read_text(encoding='utf-8'))]
        self.assertEqual(found, [])

    def test_changelog_has_current_version(self):
        changelog = (ROOT / 'CHANGELOG.md').read_text(encoding='utf-8')
        self.assertIn('## [Unreleased]', changelog)
        self.assertIn(f'## [{skim.__version__}] - 2026-11-04', changelog)
        self.assertIn(f'[{skim.__version__}]: https://github.com/nowgnas/skim/releases/tag/v{skim.__version__}', changelog)


if __name__ == '__main__':
    unittest.main()
