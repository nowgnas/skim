"""업데이트 적용 전 SQLite의 일관된 사본을 보관한다."""
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
from urllib.parse import quote

from . import maintenance
from .program_store import private_directory
from .releases import UpdateJournal


def _private_file(path):
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o600):
        raise ValueError('업데이트 백업 파일의 소유자 또는 권한을 확인하세요.')


def _readonly(path):
    return sqlite3.connect('file:' + quote(str(path), safe='/') + '?mode=ro', uri=True)


def _check_database(path):
    _private_file(path)
    with closing(_readonly(path)) as connection:
        if connection.execute('PRAGMA quick_check').fetchone() != ('ok',):
            raise ValueError('업데이트 백업이 손상되었습니다.')


def _recover_backup_link(path):
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600):
        raise ValueError('업데이트 백업 파일의 소유자 또는 권한을 확인하세요.')
    if info.st_nlink == 1:
        return
    leftovers = []
    for candidate in path.parent.glob('.backup-*'):
        entry = candidate.lstat()
        if stat.S_ISREG(entry.st_mode) and entry.st_uid == os.getuid() and entry.st_dev == info.st_dev and entry.st_ino == info.st_ino:
            leftovers.append(candidate)
    if len(leftovers) != info.st_nlink - 1:
        raise ValueError('업데이트 백업의 추가 링크를 확인하세요.')
    for candidate in leftovers:
        candidate.unlink()
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def backup_database(data_dir, update_id):
    data_dir = Path(data_dir)
    journal = UpdateJournal(data_dir)
    with maintenance.guard(data_dir, exclusive=True, update_id=update_id):
        record = journal.read()
        if record['state'] not in ('quiesced', 'backup'):
            raise ValueError('백업할 업데이트 단계가 아닙니다.')
        backups = data_dir / 'update-backups'
        backups.mkdir(mode=0o700, exist_ok=True)
        private_directory(backups)
        target = backups / (update_id + '.sqlite')
        absent = backups / (update_id + '.empty')
        if (target.exists() or target.is_symlink()) and (absent.exists() or absent.is_symlink()):
            raise ValueError('업데이트 백업 상태가 일치하지 않습니다.')
        if target.exists() or target.is_symlink():
            _recover_backup_link(target)
            _check_database(target)
            result = target
        elif absent.exists() or absent.is_symlink():
            _private_file(absent)
            result = None
        elif record['state'] == 'backup':
            raise ValueError('업데이트 백업을 찾을 수 없습니다.')
        else:
            source = data_dir / 'skim.db'
            if source.exists() or source.is_symlink():
                _private_file(source)
                fd, name = tempfile.mkstemp(prefix='.backup-', dir=backups)
                os.close(fd)
                try:
                    with closing(_readonly(source)) as original, closing(sqlite3.connect(name)) as copy:
                        original.backup(copy)
                        copy.commit()
                    temporary = Path(name)
                    _check_database(temporary)
                    fd = os.open(temporary, os.O_RDONLY | os.O_NOFOLLOW)
                    try:
                        os.fsync(fd)
                    finally:
                        os.close(fd)
                    os.link(temporary, target, follow_symlinks=False)
                    result = target
                finally:
                    if os.path.exists(name):
                        os.unlink(name)
            else:
                fd = os.open(absent, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
                result = None
            fd = os.open(backups, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        if record['state'] == 'quiesced':
            journal.advance(update_id, 'backup')
        return result


def restore_database(data_dir, update_id):
    data_dir = Path(data_dir)
    journal = UpdateJournal(data_dir)
    with maintenance.guard(data_dir, exclusive=True, update_id=update_id):
        record = journal.read()
        if record['state'] != 'rolling_back':
            raise ValueError('데이터를 복구할 업데이트 단계가 아닙니다.')
        backups = data_dir / 'update-backups'
        private_directory(backups)
        source = backups / (update_id + '.sqlite')
        absent = backups / (update_id + '.empty')
        if (source.exists() or source.is_symlink()) == (absent.exists() or absent.is_symlink()):
            raise ValueError('업데이트 백업 상태가 일치하지 않습니다.')
        destination = data_dir / 'skim.db'
        sidecars = (data_dir / 'skim.db-wal', data_dir / 'skim.db-shm')
        for path in (destination, *sidecars):
            if path.exists() or path.is_symlink():
                _private_file(path)
        if absent.exists() or absent.is_symlink():
            _private_file(absent)
            for path in (*sidecars, destination):
                path.unlink(missing_ok=True)
        else:
            _recover_backup_link(source)
            _check_database(source)
            fd, name = tempfile.mkstemp(prefix='.restore-', dir=data_dir)
            os.close(fd)
            try:
                with closing(_readonly(source)) as backup, closing(sqlite3.connect(name)) as restored:
                    backup.backup(restored)
                    restored.commit()
                    restored.execute('PRAGMA journal_mode=DELETE')
                temporary = Path(name)
                _check_database(temporary)
                fd = os.open(temporary, os.O_RDONLY | os.O_NOFOLLOW)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
                for path in sidecars:
                    path.unlink(missing_ok=True)
                os.replace(temporary, destination)
            finally:
                if os.path.exists(name):
                    os.unlink(name)
        fd = os.open(data_dir, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        restored_marker = backups / (update_id + '.restored')
        if restored_marker.exists() or restored_marker.is_symlink():
            _private_file(restored_marker)
        else:
            fd = os.open(restored_marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            fd = os.open(backups, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
