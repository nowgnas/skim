"""데모 임시 경로와 모의 macOS 명령만 사용한다."""
import io
import os
from pathlib import Path
import plistlib
import sys
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
from skim import macos_bundle as bundle
from skim import web


class BundleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='demo-한글 & "')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.root = self.home / '소스 & "'
        (self.root / 'bin').mkdir(parents=True)
        (self.root / 'bin/skim').touch()
        self.config = SimpleNamespace(data_dir=self.home / 'data')
        self.config.data_dir.mkdir()
        self.output = self.home / '준비'
        for context in (patch.object(bundle.sys, 'platform', 'darwin'),
                        patch.object(Path, 'home', return_value=self.home),
                        patch.dict(os.environ, {'SKIM_HOME': str(self.home), 'PATH': ':/usr/bin:relative:.:/bin/../bin'}, clear=True),
                        patch.object(bundle.shutil, 'which', return_value='/usr/bin/codesign')):
            context.start()
            self.addCleanup(context.stop)
        self.commands = []
        self.verify_fail = False
        def run(command, **kwargs):
            self.commands.append(command)
            if command[0] == '/usr/bin/osacompile':
                (Path(command[2]) / 'Contents/Resources').mkdir(parents=True)
            return SimpleNamespace(returncode=int(self.verify_fail and command[:2] == ['codesign', '--verify']), stdout='')
        self.run = patch.object(bundle.subprocess, 'run', side_effect=run).start()
        self.addCleanup(patch.stopall)

    def prepare(self):
        return bundle.main(['--root', str(self.root), '--output', str(self.output)], quiet=True)

    def settings(self):
        with (self.output / 'com.skim.web.plist').open('rb') as stream:
            return plistlib.load(stream)

    def test_prepare_path_permissions_special_characters_and_signature(self):
        self.assertEqual(self.prepare(), 0)
        saved = self.settings()
        self.assertEqual(saved['EnvironmentVariables']['PATH'], '/usr/bin:/bin')
        self.assertEqual(saved['ProgramArguments'][0], str(self.root / 'bin/skim'))
        self.assertEqual(saved['EnvironmentVariables']['SKIM_HOME'], str(self.home))
        self.assertEqual((self.output / 'com.skim.web.plist').stat().st_mode & 0o777, 0o600)
        log = Path(saved['StandardErrorPath'])
        self.assertEqual(log.stat().st_mode & 0o777, 0o600)
        self.assertEqual(saved['StandardOutPath'], '/dev/null')
        self.assertIn(['codesign', '--verify', str(self.output / bundle.APP_NAME)], self.commands)

    def test_signature_failure_resigns_and_rechecks(self):
        attempts = []
        original = self.run.side_effect
        def run(command, **kwargs):
            result = original(command, **kwargs)
            if command[:2] == ['codesign', '--verify']:
                attempts.append(command)
                result.returncode = int(len(attempts) == 1)
            return result
        self.run.side_effect = run
        self.assertEqual(self.prepare(), 0)
        self.assertEqual(len(attempts), 2)
        self.assertIn(['codesign', '-f', '-s', '-', str(self.output / bundle.APP_NAME)], self.commands)

    def test_no_codesign_skips_verification(self):
        with patch.object(bundle.shutil, 'which', return_value=None):
            self.assertEqual(self.prepare(), 0)
        self.assertFalse(any(c[0] == 'codesign' for c in self.commands))

    def test_reused_log_and_plist_must_keep_private_stderr(self):
        self.prepare()
        app, agent = self.output / bundle.APP_NAME, self.output / 'com.skim.web.plist'
        log = self.config.data_dir / 'launchagent.stderr.log'
        log.chmod(0o622)
        with self.assertRaises(ValueError):
            bundle.prepare_log(self.config.data_dir)
        log.chmod(0o600)
        saved = self.settings()
        saved['StandardErrorPath'] = '/dev/null'
        agent.write_bytes(plistlib.dumps(saved))
        with self.assertRaisesRegex(ValueError, '로그'):
            bundle.validate_existing(app, agent, self.root, self.config)

    def test_refuses_overwrite(self):
        self.assertEqual(self.prepare(), 0)
        saved = (self.output / 'com.skim.web.plist').read_bytes()
        with redirect_stderr(io.StringIO()):
            self.assertEqual(self.prepare(), 1)
        self.assertEqual((self.output / 'com.skim.web.plist').read_bytes(), saved)

    def test_non_macos(self):
        with patch.object(bundle.sys, 'platform', 'linux'), redirect_stderr(io.StringIO()):
            self.assertEqual(self.prepare(), 1)
            self.assertEqual(bundle.install_app(self.config), 1)
            self.assertEqual(bundle.uninstall_app(self.config), 1)
        self.assertEqual(self.commands, [])

    def test_existing_marker_environment_permissions_and_types(self):
        self.prepare()
        app, agent = self.output / bundle.APP_NAME, self.output / 'com.skim.web.plist'
        bundle.validate_existing(app, agent, self.root, self.config)
        with patch.dict(os.environ, CODEX_HOME='/different'):
            with self.assertRaisesRegex(ValueError, '환경'):
                bundle.validate_existing(app, agent, self.root, self.config)
        agent.chmod(0o644)
        with self.assertRaises(ValueError):
            bundle.validate_existing(app, agent, self.root, self.config)
        agent.chmod(0o600)
        with patch.object(bundle.os, 'getuid', return_value=os.getuid() + 1):
            with self.assertRaises(ValueError):
                bundle.validate_existing(app, agent, self.root, self.config)
        marker = app / 'Contents/Resources/skim-managed.plist'
        marker.unlink()
        with self.assertRaisesRegex(ValueError, '마커'):
            bundle.validate_existing(app, agent, self.root, self.config)
        marker.write_bytes(plistlib.dumps({'root': '/different'}))
        with self.assertRaisesRegex(ValueError, '마커'):
            bundle.validate_existing(app, agent, self.root, self.config)
        agent.unlink()
        agent.symlink_to(marker)
        with self.assertRaises(ValueError):
            bundle.trusted(agent, private=True)
        agent.unlink()
        agent.mkdir()
        with self.assertRaises(ValueError):
            bundle.trusted(agent)

    def test_partial_installation_environment_mismatch_is_rejected(self):
        self.prepare()
        app, agent = self.output / bundle.APP_NAME, self.output / 'com.skim.web.plist'
        missing = self.output / 'missing'
        with patch.dict(os.environ, CODEX_HOME='/demo/different'):
            for existing_app, existing_agent in ((app, missing), (missing, agent)):
                with self.assertRaisesRegex(ValueError, '환경'):
                    bundle.validate_existing(existing_app, existing_agent, self.root, self.config)

    def test_install_refuses_unmanaged_name_collision_before_launchctl(self):
        app = self.home / 'Applications' / bundle.APP_NAME
        app.mkdir(parents=True)
        with redirect_stderr(io.StringIO()) as output:
            self.assertEqual(bundle.install_app(self.config), 1)
        self.assertIn('마커 없는 기존 앱', output.getvalue())
        self.assertTrue(app.exists())
        self.assertEqual(self.commands, [])

    def test_uninstall_preserves_data_and_refuses_unmanaged_app(self):
        # 실제 소스 루트에 속한 가짜 번들을 사용자 임시 홈으로 설치한다.
        self.root = Path(bundle.__file__).resolve().parents[2]
        self.prepare()
        app = self.home / 'Applications' / bundle.APP_NAME
        agent = self.home / 'Library/LaunchAgents/com.skim.web.plist'
        app.parent.mkdir()
        agent.parent.mkdir(parents=True)
        (self.output / bundle.APP_NAME).rename(app)
        (self.output / agent.name).rename(agent)
        data = self.config.data_dir / 'demo.txt'
        data.write_text('demo')
        with redirect_stderr(io.StringIO()) as err:
            self.assertEqual(bundle.uninstall_app(self.config), 0)
        self.assertIn('진행 중인 작업', err.getvalue())
        self.assertFalse(app.exists())
        self.assertFalse(agent.exists())
        self.assertEqual(data.read_text(), 'demo')
        self.assertTrue(any(c[1] == 'bootout' for c in self.commands if len(c) > 1))
        app.mkdir()
        with redirect_stderr(io.StringIO()):
            self.assertEqual(bundle.uninstall_app(self.config), 1)
        self.assertTrue(app.exists())
