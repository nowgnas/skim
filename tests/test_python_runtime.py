"""래퍼의 인터프리터 선택과 최소 버전 상수의 일치를 검증한다."""
from pathlib import Path
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'lib'))

from skim import MIN_PYTHON

CURRENT = f'python3.{sys.version_info.minor}'


class PythonRuntimeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.bin = Path(temp.name)

    def fake(self, name, exit_code):
        """버전 검사에 exit_code로 답하는 가짜 인터프리터."""
        path = self.bin / name
        path.write_text(f'#!/bin/sh\nexit {exit_code}\n')
        path.chmod(0o755)
        return path

    def real(self, name):
        path = self.bin / name
        path.symlink_to(sys.executable)
        return path

    def find(self, **env):
        # PATH를 임시 디렉터리로 한정해 개발자 환경의 python에 영향받지 않게 한다.
        bash = shutil.which('bash')
        env = {'PATH': str(self.bin), **env}
        return subprocess.run([bash, '-c', f'set -euo pipefail; source "{ROOT}/lib/python.sh"; skim_find_python'],
                              env=env, capture_output=True, text=True)

    def test_min_version_matches_shell(self):
        text = (ROOT / 'lib/python.sh').read_text()
        shell = re.search(r'^SKIM_MIN_PYTHON=(\d+)\.(\d+)$', text, re.M)
        self.assertEqual(tuple(map(int, shell.groups())), MIN_PYTHON)

    def test_prefers_python3_when_supported(self):
        expected = self.real('python3')
        self.real(CURRENT)
        result = self.find()
        self.assertEqual(result.stdout.strip(), str(expected), result.stderr)

    def test_skips_old_python3_for_versioned_binary(self):
        self.fake('python3', 1)
        expected = self.real(CURRENT)
        result = self.find()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), str(expected))

    def test_ignores_versions_below_minimum(self):
        self.fake('python3', 1)
        self.fake(f'python3.{MIN_PYTHON[1] - 1}', 0)
        result = self.find()
        self.assertEqual(result.returncode, 1)
        self.assertIn('SKIM_PYTHON', result.stderr)

    def test_explicit_skim_python_is_not_overridden(self):
        self.real(CURRENT)
        old = self.fake('old-python', 1)
        result = self.find(SKIM_PYTHON=str(old))
        self.assertEqual(result.returncode, 1)
        self.assertIn(str(old), result.stderr)
        result = self.find(SKIM_PYTHON=sys.executable)
        self.assertEqual(result.stdout.strip(), sys.executable)

    def wrappers(self, **env):
        for tool in ('dirname', 'readlink'):
            path = self.bin / tool
            if not path.exists():
                path.symlink_to(shutil.which(tool))
        for script in ('bin/skim', 'setup.sh'):
            with self.subTest(script=script):
                yield subprocess.run(
                    [shutil.which('bash'), str(ROOT / script), '--help'],
                    env={'PATH': str(self.bin), 'HOME': str(self.bin), **env},
                    capture_output=True, text=True)

    def test_wrappers_exit_2_without_python(self):
        for result in self.wrappers():
            self.assertEqual(result.returncode, 2)
            self.assertIn('이상을 찾지 못했습니다', result.stderr)
            self.assertIn('python3: 없음', result.stderr)

    def test_failed_discovery_reports_candidates_and_versions(self):
        old = self.bin / 'python3'
        old.write_text('#!/bin/sh\necho "Python 3.10.0"\nexit 1\n')
        old.chmod(0o755)
        for result in self.wrappers():
            self.assertEqual(result.returncode, 2)
            self.assertIn('이상을 찾지 못했습니다', result.stderr)
            self.assertIn('python3 (', result.stderr)
            self.assertIn('Python 3.10.0', result.stderr)
            self.assertIn('python3.11: 없음', result.stderr)

    def test_explicit_missing_or_non_executable_python(self):
        missing = self.bin / 'missing-python'
        blocked = self.bin / 'blocked-python'
        blocked.write_text('#!/bin/sh\nexit 0\n')
        blocked.chmod(0o644)
        for path, message in ((missing, '존재하지 않습니다'),
                              (blocked, '실행할 수 없습니다')):
            result = self.find(SKIM_PYTHON=str(path))
            self.assertEqual(result.returncode, 1)
            self.assertIn(message, result.stderr)
            self.assertIn(str(path), result.stderr)
            for result in self.wrappers(SKIM_PYTHON=str(path)):
                self.assertEqual(result.returncode, 2)
                self.assertIn(message, result.stderr)
                self.assertIn(str(path), result.stderr)

    def test_wrappers_do_not_import_skim_from_cwd(self):
        package = self.bin / 'skim'
        package.mkdir()
        (package / '__init__.py').write_text('raise RuntimeError("cwd hijack")\n')
        env = dict(os.environ, SKIM_PYTHON=sys.executable, HOME=str(self.bin))
        env.pop('PYTHONPATH', None)
        for script in ('bin/skim', 'setup.sh'):
            with self.subTest(script=script):
                result = subprocess.run([str(ROOT / script), '--help'], cwd=self.bin,
                                        env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('usage:', result.stdout)
                self.assertNotIn('cwd hijack', result.stderr)


if __name__ == '__main__':
    unittest.main()
