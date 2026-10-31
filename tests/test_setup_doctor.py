"""임시 홈과 fake 실행기로 설치·진단을 검증한다."""
import contextlib
import io
import os
from pathlib import Path
import plistlib
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
        self.temp = tempfile.TemporaryDirectory(prefix='skim space ')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)

    def test_doctor(self):
        config = Config(self.home, self.home / 'absent')
        runner = Mock(return_value=subprocess.CompletedProcess([], 0, 'SECRET', 'TOKEN'))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(run(config, runner, lambda n: '/fake'), 1)
        self.assertIn('SSO', out.getvalue())
        self.assertNotIn('SECRET', out.getvalue())
        self.assertIn('차단', out.getvalue())
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(run(config, runner, lambda n: '/fake', Mock()), 0)
            self.home.chmod(0o755)
            self.assertEqual(run(config, runner, lambda n: '/fake', Mock()), 1)
            self.assertEqual(self.home.stat().st_mode & 0o777, 0o755)
            runner.reset_mock()
            self.assertEqual(run(config, runner, lambda n: None), 1)
            runner.assert_not_called()

    def install(self, system, *options):
        with patch('pathlib.Path.home', return_value=self.home), patch('skim.setup.platform.system', return_value=system), patch('skim.setup.shutil.which', return_value='/fake'), patch('skim.setup.subprocess.run') as runner, contextlib.redirect_stdout(io.StringIO()):
            code = main(['--root', str(ROOT), *options])
        return code, runner

    def test_linux(self):
        for _ in range(2):
            code, runner = self.install('Linux', '--web')
            self.assertEqual(code, 0)
            self.assertIn(unittest.mock.call(['systemctl', '--user', 'enable', '--now', 'skim-web.service'], check=True), runner.call_args_list)
        service = self.home / '.config/systemd/user/skim-web.service'
        self.assertIn('--no-browser', service.read_text())
        self.assertEqual(service.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.install('Linux', '--remove-web')[0], 0)
        self.assertFalse(service.exists())

    def test_mac(self):
        self.assertEqual(self.install('Darwin', '--web')[0], 0)
        service = self.home / 'Library/LaunchAgents/dev.skim.web.plist'
        value = plistlib.loads(service.read_bytes())
        self.assertEqual(value['ProgramArguments'], [str(ROOT / 'bin/skim'), 'web', '--no-browser'])
        self.assertIn('SKIM_PYTHON', value['EnvironmentVariables'])
        self.assertEqual(self.install('Darwin', '--remove-web')[0], 0)
        self.assertFalse(service.exists())

    def test_wrapper_and_conflict(self):
        result = subprocess.run([str(ROOT / 'setup.sh')], env=dict(os.environ, HOME=str(self.home)), capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        link = self.home / '.local/bin/skim'
        self.assertEqual(link.resolve(), ROOT / 'bin/skim')
        link.unlink()
        link.write_text('keep')
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.install('Linux')[0], 1)
        self.assertEqual(link.read_text(), 'keep')

    def test_service_failure_is_reported(self):
        with patch('pathlib.Path.home', return_value=self.home), patch('skim.setup.platform.system', return_value='Linux'), patch('skim.setup.shutil.which', return_value='/fake'), patch('skim.setup.subprocess.run', side_effect=subprocess.CalledProcessError(1, 'fake')), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(['--root', str(ROOT), '--web']), 1)

    def test_doctor_timeout_and_auth_failure(self):
        config = Config(self.home, self.home / 'absent')
        for runner in (Mock(side_effect=subprocess.TimeoutExpired('fake', 15)),
                       Mock(return_value=subprocess.CompletedProcess([], 1, 'SECRET', 'TOKEN'))):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(run(config, runner, lambda n: '/fake'), 1)
            self.assertNotIn('TOKEN', out.getvalue())
