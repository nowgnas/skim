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
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as temporary:
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
