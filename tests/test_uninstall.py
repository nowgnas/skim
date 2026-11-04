"""임시 홈과 모의 macOS 명령만 사용한다."""
import fcntl
import io
import os
from pathlib import Path
import sys
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
from skim import cli, macos_bundle as bundle, uninstall
from skim.program_store import ProgramStore


class UninstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='skim-uninstall-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        for context in (patch.object(Path, 'home', return_value=self.home),
                        patch.dict(os.environ, {'PATH': '/usr/bin:/bin'}, clear=True),
                        patch.object(uninstall.sys, 'platform', 'linux')):
            context.start()
            self.addCleanup(context.stop)
        self.config_dir = self.home / '.config/skim'
        self.data_dir = self.home / '.local/share/skim'
        for path in (self.config_dir, self.data_dir / 'codex-home'):
            path.mkdir(parents=True, mode=0o700)
        (self.config_dir / 'config.json').write_text('{}')
        (self.data_dir / 'skim.db').write_text('demo')
        self.store = self.home / '.local/share/skim-program'
        self.store.mkdir(mode=0o700)
        ProgramStore.initialize(self.store)
        self.link = self.home / '.local/bin/skim'
        self.link.parent.mkdir(parents=True)
        self.link.symlink_to(uninstall.SOURCE_ROOT / 'bin/skim')

    def run_uninstall(self, answers=(), **kwargs):
        answers = list(answers)
        asked = []
        def ask(prompt):
            asked.append(prompt)
            return answers.pop(0)
        kwargs.setdefault('interactive', True)
        with redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()) as err:
            status = uninstall.run(ask=ask, **kwargs)
        return status, out.getvalue(), err.getvalue(), asked

    def assert_data_kept(self):
        self.assertEqual((self.data_dir / 'skim.db').read_text(), 'demo')
        self.assertTrue((self.config_dir / 'config.json').exists())
        self.assertTrue((self.data_dir / 'codex-home').is_dir())

    def test_lists_paths_and_rejection_deletes_nothing(self):
        status, out, _, asked = self.run_uninstall(['n'])
        self.assertEqual(status, 1)
        removing, keeping = out.split('보존할 항목:')
        self.assertIn(str(self.store), removing)
        self.assertIn(str(self.link), removing)
        self.assertIn(str(self.data_dir), keeping)
        self.assertIn(str(self.config_dir), keeping)
        self.assertIn('취소했습니다', out)
        self.assertEqual(len(asked), 1)
        self.assertTrue(self.store.exists())
        self.assertTrue(self.link.is_symlink())
        self.assert_data_kept()

    def test_noninteractive_requires_yes(self):
        status, _, err, asked = self.run_uninstall(interactive=False)
        self.assertEqual(status, 1)
        self.assertIn('--yes', err)
        self.assertEqual(asked, [])
        self.assertTrue(self.store.exists())
        self.assertTrue(self.link.is_symlink())

    def test_yes_removes_program_and_preserves_data_by_default(self):
        # 홈의 dotfiles 저장소는 skim checkout이 아니므로 삭제를 막지 않는다.
        (self.home / '.git').mkdir()
        status, out, _, asked = self.run_uninstall(yes=True, interactive=False)
        self.assertEqual(status, 0)
        self.assertEqual(asked, [])
        self.assertFalse(self.store.exists())
        self.assertFalse(os.path.lexists(self.link))
        self.assertTrue((uninstall.SOURCE_ROOT / 'bin/skim').exists())
        self.assert_data_kept()
        self.assertIn('사용자 데이터는 보존했습니다', out)

    def test_interactive_data_question_is_separate(self):
        status, _, _, asked = self.run_uninstall(['y', 'n'])
        self.assertEqual(status, 0)
        self.assertEqual(len(asked), 2)
        self.assertFalse(self.store.exists())
        self.assert_data_kept()
        self.store.mkdir(mode=0o700)
        ProgramStore.initialize(self.store)
        status, _, _, asked = self.run_uninstall(['y', 'y'])
        self.assertEqual(status, 0)
        self.assertFalse(self.data_dir.exists())
        self.assertFalse(self.config_dir.exists())

    def test_purge_data_removes_user_data(self):
        status, out, _, _ = self.run_uninstall(yes=True, purge_data=True, interactive=False)
        self.assertEqual(status, 0)
        self.assertIn(str(self.data_dir), out.split('보존할 항목:')[0])
        self.assertFalse(self.data_dir.exists())
        self.assertFalse(self.config_dir.exists())
        self.assertFalse(self.store.exists())

    def test_foreign_files_are_preserved(self):
        self.link.unlink()
        other = self.home / 'other/bin/skim'
        other.parent.mkdir(parents=True)
        other.write_text('other')
        self.link.symlink_to(other)
        (self.store / 'install.json').unlink()
        (self.store / 'install.json').write_text('{"name": "someone else"}')
        os.chmod(self.store / 'install.json', 0o600)
        status, out, _, _ = self.run_uninstall(yes=True, purge_data=True, interactive=False)
        self.assertEqual(status, 0)
        self.assertIn('다른 설치를 가리킵니다', out)
        self.assertIn('skim 설치 정보를 확인하지 못했습니다', out)
        self.assertTrue(self.link.is_symlink())
        self.assertEqual(other.read_text(), 'other')
        self.assertTrue((self.store / 'install.json').exists())
        self.link.unlink()
        self.link.write_text('#!/bin/sh\n')
        status, out, _, _ = self.run_uninstall(yes=True, interactive=False)
        self.assertIn('skim이 만든 링크가 아닙니다', out)
        self.assertEqual(self.link.read_text(), '#!/bin/sh\n')

    def test_checkout_is_never_removed(self):
        checkout = self.home / 'checkout'
        for path in (checkout / '.git', checkout / 'bin', checkout / 'lib/skim'):
            path.mkdir(parents=True)
        (checkout / 'bin/skim').write_text('keep')
        (self.store / '.git').mkdir(mode=0o700)
        with patch.dict(os.environ, {'SKIM_HOME': str(checkout)}):
            (checkout / 'data').mkdir(mode=0o700)
            (checkout / 'data/skim.db').write_text('demo')
            status, out, _, _ = self.run_uninstall(yes=True, purge_data=True, interactive=False)
        self.assertEqual(status, 0)
        self.assertIn('개발 checkout', out)
        self.assertTrue((checkout / 'data/skim.db').exists())
        self.assertTrue((checkout / 'bin/skim').exists())
        self.assertTrue(self.store.exists())
        self.assertTrue((uninstall.SOURCE_ROOT / 'lib/skim/uninstall.py').exists())

    def test_running_work_warns_and_blocks_data_purge(self):
        fd = os.open(self.data_dir / 'web.lock', os.O_CREAT | os.O_RDWR, 0o600)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        status, _, err, _ = self.run_uninstall(yes=True, purge_data=True, interactive=False)
        self.assertEqual(status, 1)
        self.assertIn('서버가 실행 중', err)
        self.assertFalse(self.store.exists())
        self.assert_data_kept()

    def test_cli_does_not_create_data_directories(self):
        for path in (self.data_dir, self.config_dir):
            for child in sorted(path.rglob('*'), reverse=True):
                child.rmdir() if child.is_dir() else child.unlink()
            path.rmdir()
        with patch.object(sys, 'stdin', io.StringIO()), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(['uninstall']), 1)
        self.assertFalse(self.data_dir.exists())
        self.assertFalse(self.config_dir.exists())


class MacUninstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='skim-uninstall-mac-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        for context in (patch.object(bundle.sys, 'platform', 'darwin'),
                        patch.object(Path, 'home', return_value=self.home),
                        patch.dict(os.environ, {'PATH': '/usr/bin:/bin'}, clear=True),
                        patch.object(bundle.shutil, 'which', return_value=None)):
            context.start()
            self.addCleanup(context.stop)
        self.commands = []
        def run(command, **kwargs):
            self.commands.append(command)
            if command[0] == '/usr/bin/osacompile':
                (Path(command[2]) / 'Contents/Resources').mkdir(parents=True)
            return SimpleNamespace(returncode=0, stdout='')
        patch.object(bundle.subprocess, 'run', side_effect=run).start()
        self.addCleanup(patch.stopall)
        self.app = self.home / 'Applications' / bundle.APP_NAME
        self.agent = self.home / 'Library/LaunchAgents/com.skim.web.plist'
        self.data = self.home / '.local/share/skim'

    def install_app(self):
        output = self.home / 'prepared'
        self.assertEqual(bundle.main(['--root', str(uninstall.SOURCE_ROOT), '--output', str(output)], quiet=True), 0)
        self.app.parent.mkdir(exist_ok=True)
        self.agent.parent.mkdir(parents=True, exist_ok=True)
        (output / bundle.APP_NAME).rename(self.app)
        (output / self.agent.name).rename(self.agent)

    def test_managed_app_removed_with_existing_uninstall(self):
        self.install_app()
        with redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()):
            self.assertEqual(uninstall.run(yes=True, interactive=False), 0)
        self.assertIn(str(self.app), out.getvalue().split('보존할 항목:')[0])
        self.assertFalse(self.app.exists())
        self.assertFalse(self.agent.exists())
        self.assertTrue(any(c[1] == 'bootout' for c in self.commands if len(c) > 1))
        self.assertTrue(self.data.is_dir())

    def test_unmanaged_app_is_preserved(self):
        (self.app / 'Contents/Resources').mkdir(parents=True)
        with redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()):
            self.assertEqual(uninstall.run(yes=True, interactive=False), 0)
        self.assertIn('마커 없는 기존 앱', out.getvalue())
        self.assertTrue(self.app.exists())
        self.assertEqual(self.commands, [])


if __name__ == '__main__':
    unittest.main()
