"""래퍼의 인터프리터 선택과 최소 버전 상수의 일치를 검증한다."""
from pathlib import Path
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
        return subprocess.run([bash, '-c', f'source "{ROOT}/lib/python.sh" && skim_find_python'],
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

    def test_wrapper_exits_2_without_python(self):
        self.fake('python3', 1)
        # 시스템 python이 끼어들지 않도록 bin/skim이 쓰는 명령만 PATH에 둔다.
        for tool in ('dirname', 'readlink'):
            (self.bin / tool).symlink_to(shutil.which(tool))
        env = {'PATH': str(self.bin), 'HOME': str(self.bin)}
        result = subprocess.run([shutil.which('bash'), str(ROOT / 'bin/skim'), '--help'],
                                env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn('이상을 찾지 못했습니다', result.stderr)


if __name__ == '__main__':
    unittest.main()
