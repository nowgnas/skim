"""skim 프로그램 제거. 사용자 데이터는 별도로 동의한 경우에만 지운다."""
from dataclasses import dataclass
import fcntl
import os
from pathlib import Path
import plistlib
import shutil
import sqlite3
import stat
import sys

from . import bootstrap, macos_bundle
from .config import Config
from .program_store import ProgramStore, default_root

SOURCE_ROOT = Path(__file__).resolve().parents[2]


@dataclass
class Item:
    label: str
    path: Path
    kind: str = ''
    detail: str = ''
    identity: tuple = ()


@dataclass
class Plan:
    remove: list
    keep: list
    data: list


def data_paths():
    """Config.load와 같은 경로를 디렉터리를 만들지 않고 계산한다."""
    home = os.environ.get('SKIM_HOME')
    if home:
        root = Path(home).expanduser().resolve()
        return root / 'config', root / 'data'
    return Path.home() / '.config/skim', Path.home() / '.local/share/skim'


def git_checkout(path, skim_only=False):
    # 홈의 dotfiles 저장소처럼 관련 없는 상위 저장소는 skim_only로 건너뛴다.
    for candidate in (path, *path.parents):
        if os.path.lexists(candidate / '.git') and (
                not skim_only or candidate == path
                or ((candidate / 'bin/skim').exists() and (candidate / 'lib/skim').is_dir())):
            return candidate
    return None


def protected(path):
    """지우면 안 되는 경로면 이유를, 아니면 None을 돌려준다."""
    path = Path(os.path.abspath(path))
    home = Path.home().resolve()
    if path.resolve() == home or home.is_relative_to(path.resolve()):
        return '홈 디렉터리 또는 그 상위 경로는 삭제하지 않습니다'
    if git_checkout(path, skim_only=True):
        return '개발 checkout 안의 경로는 삭제하지 않습니다'
    if path.is_dir() and not path.is_symlink():
        for _, dirs, files in os.walk(path):
            if '.git' in dirs or '.git' in files:
                return 'git 저장소를 포함한 경로는 삭제하지 않습니다'
    return None


def overlaps(first, second):
    first, second = Path(os.path.abspath(first)), Path(os.path.abspath(second))
    return first.is_relative_to(second) or second.is_relative_to(first)


def read_launcher(path):
    """본인 소유 일반 파일만 읽고 (장치, inode)와 내용을 돌려준다."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1 or info.st_size > 1 << 20:
            raise ValueError('본인 소유 일반 파일이 아닙니다')
        return (info.st_dev, info.st_ino), stream.read()


def launcher_matches(path, store):
    """bootstrap.install이 이 관리 설치용으로 만든 launcher인지 설치 ID와 내용으로 확인한다."""
    installation_id = ProgramStore.open(store).installation_id
    identity, content = read_launcher(path)
    # install은 해석한 홈 기준 경로를 launcher에 기록한다.
    roots = {str(store), str(store.parent.resolve() / store.name)}
    if not any(content == bootstrap.launcher_source(Path(root), installation_id).encode() for root in roots):
        raise ValueError('이 관리 설치가 만든 실행 파일이 아닙니다')
    return identity


def build_plan(config_dir, data_dir):
    plan = Plan([], [], [])
    checkout = git_checkout(SOURCE_ROOT, skim_only=True)
    if checkout:
        plan.keep.append(Item('개발 checkout', checkout, detail='삭제하지 않습니다'))

    if sys.platform == 'darwin':
        app = Path.home() / 'Applications' / macos_bundle.APP_NAME
        agent = Path.home() / 'Library/LaunchAgents/com.skim.web.plist'
        present = [Item(label, path, 'app') for label, path in (('macOS 앱', app), ('LaunchAgent', agent))
                   if os.path.lexists(path)]
        if present:
            try:
                macos_bundle.validate_existing(app, agent, SOURCE_ROOT, Config(config_dir, data_dir), check_environment=False)
                plan.remove.extend(present)
            except (OSError, ValueError, plistlib.InvalidFileException) as exc:
                plan.keep.extend(Item(item.label, item.path, detail=str(exc)) for item in present)

    store = default_root()
    store_owned = False
    if os.path.lexists(store):
        reason = protected(store)
        if not reason and any(overlaps(store, path) for path in (config_dir, data_dir)):
            reason = '사용자 데이터 경로와 겹쳐 삭제하지 않습니다'
        if not reason:
            try:
                ProgramStore.open(store)
            except (OSError, ValueError) as exc:
                reason = f'skim 설치 정보를 확인하지 못했습니다 ({exc})'
        if reason:
            plan.keep.append(Item('관리 설치 프로그램', store, detail=reason))
        else:
            store_owned = True

    link = Path.home() / '.local/bin/skim'
    if link.is_symlink():
        target = Path(os.path.realpath(link))
        ours = target == (SOURCE_ROOT / 'bin/skim').resolve() or (store_owned and target.is_relative_to(store.resolve()))
        if ours:
            plan.remove.append(Item('실행 링크', link, 'link', str(target)))
        else:
            plan.keep.append(Item('실행 링크', link, detail=f'다른 설치를 가리킵니다 ({target})'))
    elif os.path.lexists(link):
        reason = '관리 설치가 없어 소유를 확인할 수 없습니다'
        if store_owned:
            try:
                plan.remove.append(Item('실행 파일', link, 'launcher', str(store), launcher_matches(link, store)))
                reason = None
            except (OSError, ValueError) as exc:
                reason = str(exc)
        if reason:
            plan.keep.append(Item('실행 파일', link, detail=f'skim이 만든 실행 파일로 확인되지 않습니다 ({reason})'))
    # launcher 재검증에 설치 정보가 필요하므로 프로그램 경로는 launcher 다음에 지운다.
    if store_owned:
        plan.remove.append(Item('관리 설치 프로그램', store, 'store'))

    for label, path in (('설정', config_dir), ('데이터(DB, 캐시, Codex 전용 홈)', data_dir)):
        if not os.path.lexists(path):
            continue
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            plan.keep.append(Item(label, path, detail='본인 소유 디렉터리가 아니어서 삭제하지 않습니다'))
        elif reason := protected(path):
            plan.keep.append(Item(label, path, detail=reason))
        else:
            plan.data.append(Item(label, path, 'data'))

    tools = Path.home() / '.local/share/skim-tools'
    if os.path.lexists(tools):
        plan.keep.append(Item('Codex CLI 도구', tools, detail='별도로 설치한 도구입니다'))
    return plan


def held(path):
    if not path.is_file() or path.is_symlink():
        return False
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    finally:
        os.close(fd)
    return False


def running_work(data_dir):
    return [message for name, message in (('web.lock', 'skim 서버가 실행 중입니다.'),
                                          ('collection.lock', 'PR 수집이 진행 중입니다.'),
                                          ('analysis.lock', '분석이 진행 중입니다.'))
            if held(data_dir / name)]


def active_work(data_dir):
    if not data_dir.is_dir() or data_dir.is_symlink():
        return []
    try:
        warnings = running_work(data_dir)
    except OSError:
        warnings = ['작업 잠금 상태를 확인하지 못했습니다.']
    database = data_dir / 'skim.db'
    if database.is_file():
        try:
            conn = sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)
            try:
                count = conn.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('pending','running')").fetchone()[0]
            finally:
                conn.close()
            if count:
                warnings.append(f'대기 또는 실행 중으로 기록된 작업이 {count}개 있습니다.')
        except sqlite3.Error:
            pass
    if os.path.lexists(data_dir / 'update-journal.json'):
        warnings.append('업데이트 기록이 남아 있습니다. 진행 중인 업데이트가 없는지 확인하세요.')
    return warnings


def confirm(ask, prompt):
    try:
        return ask(prompt).strip().lower() in ('y', 'yes')
    except EOFError:
        return False


def remove_item(item):
    if item.kind == 'link':
        if not item.path.is_symlink() or os.path.realpath(item.path) != item.detail:
            raise ValueError(f'실행 링크가 바뀌어 삭제하지 않았습니다: {item.path}')
        item.path.unlink()
        return
    if item.kind == 'launcher':
        if launcher_matches(item.path, Path(item.detail)) != item.identity:
            raise ValueError(f'실행 파일이 바뀌어 삭제하지 않았습니다: {item.path}')
        item.path.unlink()
        return
    if reason := protected(item.path):
        raise ValueError(f'{reason}: {item.path}')
    if item.kind == 'store':
        ProgramStore.open(item.path)
    info = item.path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError(f'디렉터리가 바뀌어 삭제하지 않았습니다: {item.path}')
    shutil.rmtree(item.path)


def run(*, purge_data=False, yes=False, interactive=None, ask=input):
    interactive = sys.stdin.isatty() if interactive is None else interactive
    config_dir, data_dir = data_paths()
    plan = build_plan(config_dir, data_dir)
    if purge_data:
        plan.remove.extend(plan.data)

    print('삭제할 항목:')
    for item in plan.remove:
        print(f'  - {item.label}: {item.path}')
    if not plan.remove:
        print('  (없음)')
    print('보존할 항목:')
    for item in plan.keep:
        print(f'  - {item.label}: {item.path} ({item.detail})')
    if not purge_data:
        for item in plan.data:
            print(f'  - {item.label}: {item.path} (사용자 데이터는 기본으로 보존합니다)')
    if not plan.keep and (purge_data or not plan.data):
        print('  (없음)')

    warnings = active_work(data_dir)
    for warning in warnings:
        print(f'경고: {warning}', file=sys.stderr)
    if warnings:
        print('경고: 제거하면 자동 시작 서버와 진행 중인 수집, 분석 작업이 중단될 수 있습니다. 작업이 끝난 뒤 진행하세요.', file=sys.stderr)

    offer_data = interactive and not yes and not purge_data and bool(plan.data)
    if not plan.remove and not offer_data:
        print('삭제할 항목이 없습니다.')
        return 0
    if not yes:
        if not interactive:
            print('비대화형 실행에서는 확인할 수 없어 중단합니다. 삭제하려면 --yes를 함께 지정하세요.', file=sys.stderr)
            return 1
        if plan.remove and not confirm(ask, '위 항목을 삭제할까요? [y/N] '):
            print('취소했습니다. 아무것도 삭제하지 않았습니다.')
            return 1
    if offer_data and confirm(ask, '사용자 데이터(설정, DB, 캐시, Codex 전용 홈)도 삭제할까요? 되돌릴 수 없습니다. [y/N] '):
        plan.remove.extend(plan.data)
    if not plan.remove:
        print('삭제할 항목이 없습니다.')
        return 0

    status = 0
    launcher_failed = False
    if any(item.kind == 'app' for item in plan.remove):
        if macos_bundle.uninstall_app(Config(config_dir, data_dir)):
            print('macOS 앱을 제거하지 못해 나머지 항목도 보존했습니다.', file=sys.stderr)
            return 1
        for item in plan.remove:
            if item.kind == 'app':
                print(f'삭제했습니다: {item.path}')
    for item in plan.remove:
        if item.kind == 'app':
            continue
        if item.kind == 'data' and data_dir.is_dir() and not data_dir.is_symlink() and running_work(data_dir):
            print(f'실행 중인 skim 작업이 있어 삭제하지 않았습니다: {item.path}. 서버와 작업을 종료한 뒤 다시 실행하세요.', file=sys.stderr)
            status = 1
            continue
        if item.kind == 'store' and launcher_failed:
            print(f'실행 파일을 삭제하지 못해 관리 설치 프로그램도 보존했습니다: {item.path}', file=sys.stderr)
            continue
        try:
            remove_item(item)
            print(f'삭제했습니다: {item.path}')
        except (OSError, ValueError) as exc:
            print(f'삭제하지 못했습니다: {exc}', file=sys.stderr)
            status = 1
            launcher_failed = launcher_failed or item.kind == 'launcher'
    kept = [item for item in plan.data if item not in plan.remove]
    if kept:
        print('사용자 데이터는 보존했습니다. 필요 없으면 다음 경로를 직접 삭제하세요: ' + ', '.join(str(item.path) for item in kept))
    if any(item.label == '개발 checkout' for item in plan.keep):
        print('개발 checkout은 그대로 두었습니다. 필요 없으면 직접 삭제하세요.')
    return status
