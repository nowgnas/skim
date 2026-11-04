"""정식 설치본의 업데이트 적용. 검증, 작업 대기, 백업, 전환, 준비 확인, 복구를 기존 모듈로 잇는다."""
from pathlib import Path
import os
import sqlite3
import stat
import subprocess
import sys
import time

from . import __version__, maintenance, releases, update_backup
from .collect import GitHub
from .config import Config
from .program_store import ProgramStore

PROGRAM_DIR = '.local/share/skim-program'
LAUNCHER = '.local/bin/skim'
WAIT_SECONDS = 60


class UpdateFailed(Exception):
    pass


def message(exc):
    # 하위 모듈의 안내 문구도 사용자 출력 규칙에 맞춘다.
    return str(exc).replace('·', ', ').replace('—', '-')


def installed_store(home=None, module=__file__):
    """실행 중인 코드가 정식 설치본의 버전 경로에 있을 때만 저장소와 태그를 돌려준다."""
    root = Path(home or Path.home()).resolve() / PROGRAM_DIR
    path = Path(module).resolve()
    if len(path.parents) < 4 or path.parents[3] != root / 'versions':
        return None
    tag = path.parents[2].name
    releases.version_tuple(tag)
    return ProgramStore.open(root), tag


def wait_quiet(data_dir, update_id=None, step=None, *, seconds=WAIT_SECONDS, sleep=time.sleep):
    """실행 중인 작업을 중단하지 않고 공유 잠금이 모두 풀릴 때까지 기다린다."""
    for attempt in range(seconds + 1):
        try:
            with maintenance.guard(data_dir, exclusive=True, update_id=update_id):
                if step:
                    step()
                return
        except maintenance.UpdateInProgress:
            if attempt == seconds:
                raise
            if attempt == 0:
                print('실행 중인 skim 서버나 수집, 분석 작업이 끝나기를 기다립니다.', flush=True)
            sleep(1)


def check_ready(home, tag, runner=subprocess.run):
    """영구 launcher가 새 버전 경로를 실제로 실행하는지 확인한다."""
    launcher = Path(home) / LAUNCHER
    info = launcher.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        raise UpdateFailed('skim launcher를 확인하세요.')
    try:
        result = runner([str(launcher), '--version'], stdin=subprocess.DEVNULL,
                        capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        raise UpdateFailed('새 버전을 실행하지 못했습니다.') from None
    if result.returncode != 0 or result.stdout.strip() != 'skim ' + tag[1:]:
        raise UpdateFailed('새 버전 실행 확인에 실패했습니다.')


def restore(store, data_dir, update_id):
    """전환 전이면 취소하고, 백업 이후면 데이터와 이전 버전을 함께 되돌린다."""
    journal = releases.UpdateJournal(data_dir)
    if journal.read()['state'] in ('prepared', 'quiesced', 'rolled_back'):
        store.cancel(data_dir, update_id)
        return
    journal.begin_rollback(update_id)
    update_backup.restore_database(data_dir, update_id)
    store.rollback(data_dir, update_id)


def recover(store, data_dir):
    """중단된 업데이트를 마지막 기록 단계에 맞춰 완료하거나 되돌린다."""
    journal = releases.UpdateJournal(data_dir)
    record = journal.read()
    if record is None or record['state'] in ('committed', 'rolled_back'):
        return None
    if record['installation_id'] != store.installation_id:
        raise UpdateFailed('다른 설치의 업데이트 기록이 있습니다. 데이터 경로를 확인하세요.')
    if record['state'] == 'ready':
        journal.advance(record['id'], 'committed')
        return f"중단된 {record['tag']} 업데이트는 준비 확인을 마친 상태여서 완료로 기록했습니다."
    restore(store, data_dir, record['id'])
    return f"중단된 {record['tag']} 업데이트를 되돌려 이전 버전과 데이터를 복구했습니다."


def apply(release, store, running, data_dir, home, *, download=releases.download_private_asset,
          runner=subprocess.run, sleep=time.sleep, seconds=WAIT_SECONDS):
    previous = store.payload(running)
    if not isinstance(previous, dict) or previous.get('tag') != running:
        raise UpdateFailed('현재 설치 버전 정보를 확인하세요.')
    print('릴리스 파일을 내려받아 검증합니다.', flush=True)
    sha256 = store.stage(release, download)
    # 작업 중이면 업데이트 기록을 만들지 않아 다른 진입점을 막지 않는다.
    wait_quiet(data_dir, seconds=seconds, sleep=sleep)
    journal = releases.UpdateJournal(data_dir)
    update_id = journal.start(store.installation_id, release.tag, sha256, previous)['id']
    try:
        wait_quiet(data_dir, update_id, lambda: journal.advance(update_id, 'quiesced'), seconds=seconds, sleep=sleep)
        print('데이터를 백업합니다.', flush=True)
        update_backup.backup_database(data_dir, update_id)
        store.activate(data_dir, update_id)
        check_ready(home, release.tag, runner)
        journal.advance(update_id, 'ready')
    except BaseException as exc:
        try:
            restore(store, data_dir, update_id)
        except (OSError, ValueError, sqlite3.Error, maintenance.UpdateInProgress):
            raise UpdateFailed(f'업데이트에 실패했고 자동 복구도 마치지 못했습니다: {message(exc)} '
                               'skim update를 다시 실행하면 복구를 이어서 시도합니다.') from None
        if not isinstance(exc, Exception):
            raise
        raise UpdateFailed(f'업데이트에 실패해 이전 버전 {running}과 데이터를 복구했습니다: {message(exc)}') from None
    journal.advance(update_id, 'committed')


def run(check=False, assume_yes=False, *, home=None, module=__file__, ask=input,
        download=releases.download_private_asset, runner=subprocess.run, sleep=time.sleep, seconds=WAIT_SECONDS):
    running = 'v' + __version__
    try:
        installed = None if check else installed_store(home, module)
        if installed:
            store, tag = installed
            data_dir = Config.load().data_dir
            recovered = recover(store, data_dir)
            if recovered:
                print(recovered)
            if tag != running or store.current() != running:
                print('설치된 버전과 실행 중인 버전이 다릅니다. skim update를 다시 실행하세요.', file=sys.stderr)
                return 1
        latest = releases.fetch_latest_authenticated(GitHub())
        if latest is None:
            print('게시된 정식 릴리스가 없습니다.')
            return 0
        if latest.version <= releases.version_tuple(running):
            print(f'최신 버전입니다: {running}')
            return 0
        print(f'사용 가능한 정식 릴리스: {latest.tag} (현재 {running})')
        if check:
            return 0
        if not installed:
            print('정식 설치본이 아니어서 업데이트를 적용하지 않습니다. 개발 checkout은 git으로 직접 갱신하고, '
                  '정식 설치는 README의 설치 절차를 따르세요.', file=sys.stderr)
            return 1
        if not assume_yes:
            try:
                answer = ask(f'{running}에서 {latest.tag}로 업데이트할까요? [y/N] ')
            except EOFError:
                print('확인 입력을 받을 수 없습니다. 확인 없이 진행하려면 --yes를 사용하세요.', file=sys.stderr)
                return 1
            if answer.strip().lower() not in ('y', 'yes'):
                print('업데이트를 취소했습니다.')
                return 0
        apply(latest, store, running, data_dir, Path(home or Path.home()).resolve(),
              download=download, runner=runner, sleep=sleep, seconds=seconds)
    except maintenance.UpdateInProgress as exc:
        print(f'업데이트 대기 실패: {message(exc)} 실행 중인 skim 서버와 작업을 종료한 뒤 다시 실행하세요.', file=sys.stderr)
        return 1
    except (UpdateFailed, releases.ReleaseUnavailable, OSError, ValueError, sqlite3.Error) as exc:
        print(f'오류: {message(exc)}', file=sys.stderr)
        return 1
    print(f'{latest.tag} 업데이트를 마쳤습니다. skim을 다시 실행하면 새 버전이 사용됩니다.')
    return 0
