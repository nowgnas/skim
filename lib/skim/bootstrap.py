"""정식 릴리스 최초 설치. 기존 설치와 사용자 데이터는 변경하지 않는다."""
import argparse
import inspect
import os
from pathlib import Path
import plistlib
import shlex
import shutil
import sys
import tempfile
from types import SimpleNamespace

from . import macos_bundle, releases
from .collect import GitHub
from .program_store import ProgramStore


def data_paths(home):
    custom = os.environ.get('SKIM_HOME')
    if custom:
        base = Path(custom).expanduser().resolve()
        return (base,)
    return (home / '.config/skim', home / '.local/share/skim')


def separate(root, home):
    root = root.resolve()
    for path in data_paths(home):
        path = path.resolve()
        if root == path or root in path.parents or path in root.parents:
            raise ValueError('프로그램 경로와 사용자 데이터 경로를 분리하세요.')


def safe_parents(path, home):
    """기존 디렉터리는 권한을 바꾸지 않고 소유자와 링크를 검사한다."""
    missing = []
    cursor = path
    while not cursor.exists() and not cursor.is_symlink():
        missing.append(cursor)
        cursor = cursor.parent
    for parent in (cursor, *cursor.parents):
        if parent == home.parent:
            break
        macos_bundle.trusted(parent, directory=True)
    for directory in reversed(missing):
        directory.mkdir(mode=0o700)


def launcher_source(root, installation_id):
    # -I는 외부 PYTHONPATH와 현재 디렉터리의 모듈 주입을 막는다.
    code = f'''import os, sys, stat, json, grp, pwd, shlex
from pathlib import Path
sys.dont_write_bytecode = True
root = Path({str(root)!r})
custom = os.environ.get('SKIM_HOME')
data = (Path(custom).expanduser().resolve(),) if custom else (Path.home() / '.config/skim', Path.home() / '.local/share/skim')
for program in (root, Path.home() / '.local/bin/skim'):
    for path in data:
        path = path.resolve()
        if program == path or program in path.parents or path in program.parents:
            raise SystemExit('프로그램 경로와 사용자 데이터 경로를 분리하세요.')
{inspect.getsource(macos_bundle.private_user_group)}
def private(path):
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise SystemExit('프로그램 디렉터리의 소유자 또는 권한을 확인하세요.')
for parent in root.parents:
    info = parent.lstat()
    unsafe_group = info.st_mode & 0o020 and not private_user_group(info.st_gid)
    if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o002 or unsafe_group:
        hint = '. 그룹 쓰기 권한은 chmod g-w ' + shlex.quote(str(parent)) + ' 명령으로 제거하세요.' if unsafe_group else ''
        raise SystemExit('프로그램 상위 경로가 안전하지 않습니다: ' + str(parent) + hint)
private(root)
private(root / 'versions')
fd = os.open(root / 'install.json', os.O_RDONLY | os.O_NOFOLLOW)
with os.fdopen(fd, 'rb') as stream:
    info = os.fstat(stream.fileno())
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 4096):
        raise SystemExit('설치 정보가 안전하지 않습니다.')
    if json.load(stream) != {{'installation_id': {installation_id!r}}}:
        raise SystemExit('설치 소유 정보가 다릅니다.')
info = (root / 'current').lstat()
if not stat.S_ISLNK(info.st_mode) or info.st_uid != os.getuid():
    raise SystemExit('현재 버전 링크가 안전하지 않습니다.')
target = os.readlink(root / 'current')
import re
if not re.fullmatch(r'versions/v[0-9]+[.][0-9]+[.][0-9]+', target):
    raise SystemExit('현재 버전 경로가 안전하지 않습니다.')
payload = root / target
private(payload)
# payload에서 코드를 불러오기 전에 모든 하위 파일의 소유자, 링크, 권한을 검사한다.
for directory, dirs, files in os.walk(payload, followlinks=False):
    for name in dirs + files:
        path = Path(directory) / name
        info = path.lstat()
        if info.st_uid != os.getuid():
            raise SystemExit('배포 파일 소유자가 다릅니다.')
        if stat.S_ISDIR(info.st_mode):
            private(path)
        elif (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) not in (0o600, 0o700)):
            raise SystemExit('배포 파일의 링크 또는 권한이 안전하지 않습니다.')
sys.path.insert(0, str(payload / 'lib'))
from skim.program_store import ProgramStore
from skim.bootstrap import separate
store = ProgramStore.open(root)
if store.installation_id != {installation_id!r}:
    raise SystemExit('설치 소유 정보가 다릅니다.')
separate(root, Path.home())
tag = store.current()
if tag is None:
    raise SystemExit('설치된 버전이 없습니다.')
if str(payload) != str(root / 'versions' / tag):
    raise SystemExit('설치 버전이 변경되었습니다. 다시 실행하세요.')
os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
codex = Path.home() / '.local/share/skim-tools/node_modules/.bin/codex'
if codex.is_file() and os.access(codex, os.X_OK):
    os.environ.setdefault('SKIM_CODEX_BIN', str(codex))
from skim.cli import main
raise SystemExit(main())
'''
    # 특정 venv나 공백을 포함한 Python 경로를 shebang에 고정하지 않는다.
    return """#!/bin/bash
set -eu
if [[ -n "${SKIM_PYTHON:-}" ]]; then
  candidates=("$SKIM_PYTHON")
else
  candidates=(python3 python3.{20..11})
fi
for candidate in "${candidates[@]}"; do
  if "$candidate" -I -c 'import sys; sys.exit(sys.version_info < (3,11))' >/dev/null 2>&1; then
    exec "$candidate" -I -B -c """ + shlex.quote(code) + """ "$@"
  fi
done
echo 'Python 3.11 이상을 설치하거나 SKIM_PYTHON을 지정하세요.' >&2
exit 1
"""



def install(home=None, github=None, download=None):
    home = Path(home or Path.home()).resolve()
    root = home / '.local/share/skim-program'
    launcher = home / '.local/bin/skim'
    separate(root, home)
    separate(launcher, home)
    for path in (root, launcher):
        if path.exists() or path.is_symlink():
            raise ValueError(f'기존 설치 또는 파일을 보존합니다: {path}. 개발 checkout 링크는 자동 이전하지 않습니다.')
    custom = os.environ.get('SKIM_HOME')
    data = Path(custom).expanduser().resolve() / 'data' if custom else home / '.local/share/skim'
    macos_bundle.validate_existing(home / 'Applications' / macos_bundle.APP_NAME,
                                  home / 'Library/LaunchAgents/com.skim.web.plist',
                                  root, SimpleNamespace(data_dir=data), check_environment=False)
    latest = releases.fetch_latest_authenticated(github or GitHub())
    if latest is None:
        raise ValueError('정식 릴리스가 없습니다. 릴리스 게시 후 다시 설치하세요.')
    download = download or releases.download_private_asset
    # 검증 실패는 프로그램 경로조차 만들지 않는다.
    cache = {}
    def cached(release, name, limit):
        if name not in cache:
            cache[name] = download(release, name, limit)
        return cache[name]
    with tempfile.TemporaryDirectory(prefix='skim-bootstrap-') as temporary:
        releases.stage_release(latest, Path(temporary), cached)
    safe_parents(root.parent, home)
    safe_parents(launcher.parent, home)
    root.mkdir(mode=0o700)  # 배타적 생성으로 다른 설치와의 경합을 거부한다.
    identity = root.lstat()
    try:
        store = ProgramStore.initialize(root)
        identity = root.lstat()
        store.stage(latest, cached)
        os.symlink('versions/' + latest.tag, root / 'current')
        # 완성한 파일을 hard link로 배타적으로 게시한다. 기존 파일은 교체하지 않는다.
        fd, name = tempfile.mkstemp(prefix='.skim-launcher-', dir=launcher.parent)
        try:
            with os.fdopen(fd, 'w') as stream:
                stream.write(launcher_source(root, store.installation_id))
                stream.flush()
                os.fsync(stream.fileno())
                os.fchmod(stream.fileno(), 0o700)
            os.link(name, launcher)
        finally:
            os.unlink(name)
    except BaseException:
        # 이 실행이 만든 디렉터리만 정리한다. 기존 파일과 경합한 파일은 보존한다.
        current = root.lstat()
        if (current.st_dev, current.st_ino) == (identity.st_dev, identity.st_ino):
            shutil.rmtree(root)
        raise
    print(f'{latest.tag} 설치 완료: {launcher}')
    print('PATH 설정: export PATH="$HOME/.local/bin:$PATH"')
    print('skim을 실행하세요. 분석 전 codex login이 필요합니다.')
    if sys.platform == 'linux':
        print('Linux에서는 skim app 대신 터미널에서 skim을 실행하세요.')
    return store


def main(argv=None):
    parser = argparse.ArgumentParser(description='skim 정식 릴리스 최초 설치')
    parser.add_argument('--yes', action='store_true', help='도구 설치 확인은 저장소 install.sh에서 처리')
    parser.parse_args(argv)
    if sys.version_info < (3, 11):
        print('Python 3.11 이상이 필요합니다.', file=sys.stderr)
        return 1
    if sys.platform not in ('darwin', 'linux'):
        print('macOS와 Linux에서만 설치할 수 있습니다.', file=sys.stderr)
        return 1
    try:
        install()
    except (OSError, ValueError, releases.ReleaseUnavailable, plistlib.InvalidFileException) as exc:
        # 예외 원문에 인증 정보가 들어갈 수 있는 네트워크 오류는 releases에서 정제한다.
        message = str(exc).replace('\u00b7', ',').replace('\u2014', '-')
        print(f'skim 설치 실패: {message}', file=sys.stderr)
        print('기존 파일은 보존됩니다. 부분 설치가 있으면 경로를 확인한 뒤 수동으로 정리하세요.', file=sys.stderr)
        return 1
    return 0
