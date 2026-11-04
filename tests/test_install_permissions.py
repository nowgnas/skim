"""설치와 launcher의 상위 경로 권한을 가짜 계정 정보로 검사한다."""
import os
from pathlib import Path
import shlex
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
from skim import bootstrap, macos_bundle


class InstallPermissionTests(unittest.TestCase):
    def launcher_code(self, root):
        source = bootstrap.launcher_source(root, 'test')
        line = source.split('exec "$candidate" -I -B -c ', 1)[1].rsplit(' "$@"', 1)[0]
        return shlex.split(line)[0].split('\nprivate(root)\n')[0]

    def test_installer_and_generated_launcher_agree(self):
        # 안전한 임시 경로 안에서 권한과 /tmp 심볼릭 링크 구조를 재현한다.
        with tempfile.TemporaryDirectory(prefix='skim-test-', dir=Path(__file__).resolve().parents[1]) as temporary:
            base = Path(temporary)
            ancestor = base / 'ancestor'
            ancestor.mkdir()
            home = ancestor / 'home'
            home.mkdir()
            alias = base / 'alias'
            alias.symlink_to(ancestor, target_is_directory=True)
            cases = [('정상', 0o755, home, None),
                     ('전체 쓰기', 0o777, home, '다른 위치의 HOME'),
                     ('sticky', 0o1777, home, '다른 위치의 HOME'),
                     ('공유 그룹', 0o775, home, 'chmod g-w'),
                     ('심볼릭 링크', 0o1777, alias / 'home', '다른 위치의 HOME')]
            for label, mode, selected_home, hint in cases:
                with self.subTest(label=label), \
                        patch.dict(os.environ, {'HOME': str(selected_home), 'SKIM_HOME': str(base / 'data')}), \
                        patch('grp.getgrgid', return_value=SimpleNamespace(gr_name='shared', gr_mem=[])), \
                        patch('skim.releases.fetch_latest_authenticated', return_value=None) as fetch:
                    ancestor.chmod(mode)
                    root = selected_home / '.local/share/skim-program'
                    expected = bootstrap.unsafe_ancestor(selected_home.resolve() / 'program')
                    with self.assertRaises(ValueError) as installed:
                        bootstrap.install(selected_home)
                    namespace = {}
                    code = self.launcher_code(selected_home.resolve() / 'program')
                    if hint is None:
                        self.assertIsNone(expected)
                        self.assertIn('정식 릴리스가 없습니다', str(installed.exception))
                        fetch.assert_called_once()
                        exec(code, namespace)
                    else:
                        self.assertIn(hint, expected)
                        self.assertIn(str(ancestor.resolve()), expected)
                        self.assertEqual(str(installed.exception), expected)
                        fetch.assert_not_called()
                        with self.assertRaises(SystemExit) as launched:
                            exec(code, namespace)
                        self.assertEqual(str(launched.exception), expected)
                    self.assertEqual(namespace['root'], selected_home.resolve() / 'program')
                    self.assertFalse((home / '.local').exists())
                    self.assertFalse(root.exists())
                    self.assertFalse((selected_home / '.local/bin/skim').exists())

    def test_unsafe_launcher_ancestor_rejected_before_creation(self):
        with tempfile.TemporaryDirectory(prefix='skim-test-', dir=Path(__file__).resolve().parents[1]) as temporary:
            home = Path(temporary)
            local = home / '.local'
            local.mkdir()
            outside = home / 'shared'
            outside.mkdir(mode=0o1777)
            outside.chmod(0o1777)
            (local / 'bin').symlink_to(outside, target_is_directory=True)
            root = local / 'share/skim-program'
            self.assertIsNone(bootstrap.unsafe_ancestor(local / 'program'))
            with patch('skim.releases.fetch_latest_authenticated') as fetch:
                with self.assertRaisesRegex(ValueError, '다른 위치의 HOME'):
                    bootstrap.install(home)
                fetch.assert_not_called()
            self.assertFalse((local / 'share').exists())
            self.assertEqual(list(outside.iterdir()), [])

    def test_generated_launcher_rejects_replaced_ancestor(self):
        with tempfile.TemporaryDirectory(prefix='skim-test-', dir=Path(__file__).resolve().parents[1]) as temporary:
            home = Path(temporary)
            ancestor = home / 'ancestor'
            ancestor.mkdir()
            root = ancestor / 'program'
            code = self.launcher_code(root)
            with patch.dict(os.environ, {'SKIM_HOME': str(home / 'data')}):
                exec(code, {})
                moved = home / 'moved'
                ancestor.rename(moved)
                ancestor.symlink_to(moved, target_is_directory=True)
                expected = bootstrap.unsafe_ancestor(root)
                self.assertIn(str(ancestor), expected)
                with self.assertRaises(SystemExit) as launched:
                    exec(code, {})
                self.assertEqual(str(launched.exception), expected)
                ancestor.unlink()
                with self.assertRaisesRegex(SystemExit, '상위 경로'):
                    exec(code, {})

    def test_missing_or_non_directory_ancestor_is_unsafe(self):
        with tempfile.TemporaryDirectory(prefix='skim-test-', dir=Path(__file__).resolve().parents[1]) as temporary:
            ancestor = Path(temporary) / 'ancestor'
            root = ancestor / 'child/program'
            self.assertIn(str(root.parent), bootstrap.unsafe_ancestor(root))
            ancestor.write_text('file')
            self.assertIn(str(root.parent), bootstrap.unsafe_ancestor(root))

    def test_group_write_requires_exclusive_user_group(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)
            path.chmod(0o775)
            gid = path.stat().st_gid
            user = SimpleNamespace(pw_name='owner', pw_gid=gid)
            cases = [
                ('owner', [], [user], True),
                ('owner', ['owner'], [user], True),
                ('shared', [], [user], False),
                ('owner', ['other'], [user], False),
                ('owner', [], [user, SimpleNamespace(pw_name='other', pw_gid=gid)], False),
            ]
            for name, members, users, allowed in cases:
                with self.subTest(name=name, members=members, users=users), \
                        patch('pwd.getpwuid', return_value=user), \
                        patch('pwd.getpwall', return_value=users), \
                        patch('grp.getgrgid', return_value=SimpleNamespace(gr_name=name, gr_mem=members)):
                    if allowed:
                        macos_bundle.trusted(path, directory=True)
                        bootstrap.safe_parents(path, path)
                    else:
                        with self.assertRaisesRegex(ValueError, 'chmod g-w'):
                            bootstrap.safe_parents(path, path)
            with patch('grp.getgrgid', side_effect=KeyError):
                with self.assertRaisesRegex(ValueError, 'chmod g-w'):
                    macos_bundle.trusted(path, directory=True)

    def test_launcher_parent_permissions_before_import(self):
        # 공유 임시 경로를 예외 처리하지 않고 안전한 저장소 아래에서 검사한다.
        with tempfile.TemporaryDirectory(prefix='skim-test-', dir=Path(__file__).resolve().parents[1]) as temporary:
            home = Path(temporary)
            root = home / 'program'
            source = bootstrap.launcher_source(root, 'test')
            line = source.split('exec "$candidate" -I -B -c ', 1)[1].rsplit(' "$@"', 1)[0]
            code = shlex.split(line)[0]
            # payload 접근 직전까지만 실행하여 실제 설치나 분석을 하지 않는다.
            code = code.split('\nprivate(root)\n')[0]
            gid = home.stat().st_gid
            user = SimpleNamespace(pw_name='owner', pw_gid=gid)
            with patch.dict(os.environ, {'HOME': str(home), 'SKIM_HOME': str(home / 'data')}), \
                    patch('pwd.getpwuid', return_value=user), \
                    patch('pwd.getpwall', return_value=[user]), \
                    patch('grp.getgrgid', return_value=SimpleNamespace(gr_name='owner', gr_mem=[])):
                home.chmod(0o775)
                exec(code, {})
                with patch('grp.getgrgid', return_value=SimpleNamespace(gr_name='shared', gr_mem=[])):
                    with self.assertRaisesRegex(SystemExit, 'chmod g-w'):
                        exec(code, {})
                for mode in (0o777, 0o1777):
                    home.chmod(mode)
                    with self.assertRaisesRegex(SystemExit, '상위 경로'):
                        exec(code, {})

    def test_tmp_has_no_launcher_exception(self):
        source = bootstrap.launcher_source(Path('/tmp/skim-test/program'), 'test')
        line = source.split('exec "$candidate" -I -B -c ', 1)[1].rsplit(' "$@"', 1)[0]
        code = shlex.split(line)[0].split('\nprivate(root)\n')[0]
        # /tmp의 실제 모드나 링크 여부에 의존하지 않는 모의 경로 검사.
        original = Path.lstat
        def lstat(path):
            if path == Path('/tmp/skim-test'):
                return SimpleNamespace(st_mode=0o40700, st_gid=os.getgid())
            if path == Path('/tmp'):
                return SimpleNamespace(st_mode=0o41777, st_gid=os.getgid())
            return original(path)
        with patch.object(Path, 'lstat', lstat):
            with self.assertRaisesRegex(SystemExit, '상위 경로'):
                exec(code, {})
