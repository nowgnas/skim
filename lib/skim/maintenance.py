"""업데이트 중 DB 접근을 막는 사용자 전용 유지보수 장벽."""
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import stat

from .releases import UpdateJournal


class UpdateInProgress(Exception):
    pass


@contextmanager
def guard(directory, *, exclusive=False, update_id=None):
    directory = Path(directory)
    journal = UpdateJournal(directory)
    fd = os.open(directory / 'maintenance.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise ValueError('유지보수 잠금 파일 권한이 안전하지 않습니다.')
        try:
            fcntl.flock(fd, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        except BlockingIOError:
            raise UpdateInProgress('업데이트 또는 다른 작업이 진행 중입니다. 잠시 후 다시 시도하세요.') from None
        current = journal.read()
        pending = current is not None and current['state'] not in ('committed', 'rolled_back')
        authorized = exclusive and pending and update_id == current['id']
        if (pending and not authorized) or (update_id is not None and not authorized):
            raise UpdateInProgress('완료되지 않은 업데이트가 있습니다. 복구 후 다시 시도하세요.')
        yield
    finally:
        os.close(fd)
