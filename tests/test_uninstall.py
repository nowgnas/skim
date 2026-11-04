"""임시 홈과 모의 macOS 명령만 사용한다."""
import fcntl
import io
import os
from pathlib import Path
import shutil
import sys
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from skim import bootstrap, cli, macos_bundle as bundle, uninstall
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
        self.assertIn('skim이 만든 실행 파일로 확인되지 않습니다', out)
        self.assertEqual(self.link.read_text(), '#!/bin/sh\n')
        # 관리 설치가 확인되어도 내용이 다른 일반 파일은 보존한다.
        shutil.rmtree(self.store)
        self.store.mkdir(mode=0o700)
        ProgramStore.initialize(self.store)
        status, out, _, _ = self.run_uninstall(yes=True, interactive=False)
        self.assertIn('이 관리 설치가 만든 실행 파일이 아닙니다', out)
        self.assertEqual(self.link.read_text(), '#!/bin/sh\n')
        self.assertFalse(self.store.exists())

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


class ReleaseInstallUninstallTests(unittest.TestCase):
    """bootstrap.install로 만든 정식 설치를 임시 HOME에서 제거한다. 네트워크는 사용하지 않는다."""

    def setUp(self):
        from test_install_update import BootstrapTests
        self.temp = tempfile.TemporaryDirectory(prefix='skim-uninstall-release-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        for context in (patch.object(Path, 'home', return_value=self.home),
                        patch.dict(os.environ, {'PATH': '/usr/bin:/bin'}, clear=True),
                        patch.object(uninstall.sys, 'platform', 'linux')):
            context.start()
            self.addCleanup(context.stop)
        release, payloads = BootstrapTests().payloads()
        with patch('skim.releases.fetch_latest_authenticated', return_value=release), redirect_stdout(io.StringIO()):
            self.store = bootstrap.install(self.home, download=lambda r, n, limit: payloads[n])
        self.root = self.home / '.local/share/skim-program'
        self.launcher = self.home / '.local/bin/skim'
        self.data = self.home / '.local/share/skim'
        self.data.mkdir(parents=True, mode=0o700)
        (self.data / 'skim.db').write_text('demo')

    def run_uninstall(self, **kwargs):
        with redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()) as err:
            status = uninstall.run(yes=True, interactive=False, **kwargs)
        return status, out.getvalue(), err.getvalue()

    def test_release_install_is_removed_and_data_kept(self):
        self.assertFalse(self.launcher.is_symlink())
        status, out, err = self.run_uninstall()
        self.assertEqual(status, 0, err)
        removing = out.split('보존할 항목:')[0]
        self.assertIn(str(self.launcher), removing)
        self.assertIn(str(self.root), removing)
        self.assertFalse(os.path.lexists(self.launcher))
        self.assertFalse(self.root.exists())
        self.assertEqual((self.data / 'skim.db').read_text(), 'demo')

    def test_install_lock_blocks_uninstall_and_purge_removes_lock_with_store(self):
        from skim.releases import UpdateJournal
        with self.store.exclusive():
            status, out, err = self.run_uninstall(purge_data=True)
        self.assertEqual((status, out), (1, ''))
        self.assertIn('다른 skim 업데이트 또는 제거가 진행 중', err)
        self.assertTrue(self.launcher.exists())
        self.assertTrue((self.root / '.install.lock').exists())
        self.assertEqual((self.data / 'skim.db').read_text(), 'demo')
        # 완료된 업데이트 기록은 막지 않고, 잠금 파일은 쥔 채로 프로그램과 함께 지운다.
        journal = UpdateJournal(self.data)
        update_id = journal.start(self.store.installation_id, 'v1.0.1', 'b' * 64, self.store.payload('v1.0.0'))['id']
        for state in UpdateJournal.STATES[1:]:
            journal.advance(update_id, state)
        status, _, err = self.run_uninstall(purge_data=True)
        self.assertEqual(status, 0, err)
        self.assertFalse(self.root.exists())
        self.assertFalse(self.data.exists())
        self.assertFalse(os.path.lexists(self.launcher))

    def test_launcher_for_other_installation_is_preserved(self):
        content = self.launcher.read_text()
        self.launcher.unlink()
        self.launcher.write_text(bootstrap.launcher_source(self.root, '0' * 32))
        status, out, _ = self.run_uninstall()
        self.assertEqual(status, 0)
        self.assertIn('이 관리 설치가 만든 실행 파일이 아닙니다', out)
        self.assertNotEqual(self.launcher.read_text(), content)
        self.assertTrue(self.launcher.exists())

    def test_fifo_launcher_is_preserved_without_blocking(self):
        self.launcher.unlink()
        os.mkfifo(self.launcher)
        status, out, _ = self.run_uninstall()
        self.assertEqual(status, 0)
        self.assertIn('본인 소유 일반 파일이 아닙니다', out)
        self.assertTrue(self.launcher.is_fifo())

    def test_launcher_changed_before_removal_keeps_launcher_and_store(self):
        original = uninstall.build_plan
        def build_plan(*args):
            plan = original(*args)
            # 확인 뒤 삭제 직전에 다른 파일로 바뀐 경우.
            self.launcher.unlink()
            self.launcher.write_text('사용자 파일')
            return plan
        with patch.object(uninstall, 'build_plan', side_effect=build_plan):
            status, _, err = self.run_uninstall()
        self.assertEqual(status, 1)
        self.assertIn('삭제하지 못했습니다', err)
        self.assertEqual(self.launcher.read_text(), '사용자 파일')
        self.assertTrue((self.root / 'install.json').exists())

    def test_replaced_launcher_with_same_content_is_preserved(self):
        original = uninstall.build_plan
        def build_plan(*args):
            plan = original(*args)
            # 기존 파일이 남아 있는 동안 만들어 다른 inode를 보장한다.
            replacement = self.launcher.with_name('replacement')
            replacement.write_bytes(self.launcher.read_bytes())
            os.replace(replacement, self.launcher)
            return plan
        with patch.object(uninstall, 'build_plan', side_effect=build_plan):
            status, _, err = self.run_uninstall()
        self.assertEqual(status, 1)
        self.assertIn('실행 파일이 바뀌어', err)
        self.assertTrue(self.launcher.exists())
        self.assertTrue(self.root.exists())


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
