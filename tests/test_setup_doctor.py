"""임시 홈과 fake 실행기로 설치·진단을 검증한다."""
import contextlib
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'lib'))
from skim.config import Config
from skim.doctor import run
from skim.setup import main


class SetupDoctorTests(unittest.TestCase):
    def setUp(self):
        # 실행 환경의 실제 데이터 디렉터리 대신 테스트 임시 홈만 검사한다.
        env = patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop('SKIM_HOME', None)
        self.temp = tempfile.TemporaryDirectory(prefix='skim space ')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)

    def test_doctor(self):
        config = Config(self.home, self.home / 'absent')
        runner = Mock(return_value=subprocess.CompletedProcess([], 0, 'SECRET', 'TOKEN'))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(run(config, runner, lambda n: '/fake'), 1)
        version = '.'.join(map(str, sys.version_info[:3]))
        python_lines = [line for line in out.getvalue().splitlines() if ': python — ' in line]
        self.assertEqual(python_lines, [
            f'정상: python — {version} ({sys.executable}), 최소 3.11'])
        self.assertIn('SSO', out.getvalue())
        self.assertNotIn('SECRET', out.getvalue())
        self.assertNotIn('TOKEN', out.getvalue())
        self.assertIn('차단', out.getvalue())
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(run(config, runner, lambda n: '/fake', Mock(unknown_enabled=[], model='unknown')), 0)
            self.home.chmod(0o755)
            self.assertEqual(run(config, runner, lambda n: '/fake', Mock(unknown_enabled=[], model='unknown')), 1)
            self.assertEqual(self.home.stat().st_mode & 0o777, 0o755)
            runner.reset_mock()
            self.assertEqual(run(config, runner, lambda n: None), 1)
            runner.assert_not_called()

    def install(self):
        with patch('pathlib.Path.home', return_value=self.home), contextlib.redirect_stdout(io.StringIO()):
            return main(['--root', str(ROOT)])

    def test_wrapper_and_conflict(self):
        result = subprocess.run([str(ROOT / 'setup.sh')], env=dict(os.environ, HOME=str(self.home), SKIM_PYTHON=sys.executable), capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        link = self.home / '.local/bin/skim'
        self.assertEqual(link.resolve(), ROOT / 'bin/skim')
        link.unlink()
        link.write_text('keep')
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.install(), 1)
        self.assertEqual(link.read_text(), 'keep')

    def test_doctor_checks_skim_home_root_without_changing_permissions(self):
        config = Config(self.home / 'config', self.home / 'data')
        config.config_dir.mkdir(mode=0o700)
        config.data_dir.mkdir(mode=0o700)
        self.home.chmod(0o755)
        with patch.dict(os.environ, SKIM_HOME=str(self.home)), contextlib.redirect_stdout(io.StringIO()):
            runner = Mock(return_value=subprocess.CompletedProcess([], 0))
            self.assertEqual(run(config, runner, lambda n: '/fake', Mock(unknown_enabled=[], model='unknown')), 1)
        self.assertEqual(self.home.stat().st_mode & 0o777, 0o755)

    def test_doctor_skips_disappearing_files(self):
        missing = self.home / 'removed-wal'
        original = Path.lstat

        def lstat(path):
            if path == missing:
                raise FileNotFoundError(path)
            return original(path)

        with patch.object(Path, 'rglob', return_value=iter([missing])), patch.object(Path, 'lstat', lstat), contextlib.redirect_stdout(io.StringIO()):
            runner = Mock(return_value=subprocess.CompletedProcess([], 0))
            self.assertEqual(run(Config(self.home, self.home / 'absent'), runner,
                                 lambda n: '/fake', Mock(unknown_enabled=[], model='unknown')), 0)

    def test_doctor_timeout_and_auth_failure(self):
        config = Config(self.home, self.home / 'absent')
        for runner in (Mock(side_effect=subprocess.TimeoutExpired('fake', 15)),
                       Mock(return_value=subprocess.CompletedProcess([], 1, 'SECRET', 'TOKEN'))):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(run(config, runner, lambda n: '/fake'), 1)
            self.assertNotIn('TOKEN', out.getvalue())
