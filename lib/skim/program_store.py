"""사용자 데이터와 분리된 관리형 프로그램 디렉터리."""
import json
import fcntl
import hashlib
import os
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
import re
import secrets
import stat
import tempfile

from . import maintenance
from .releases import MAX_ARCHIVE_BYTES, MAX_FILES, UpdateJournal, stage_release, version_tuple


def default_root():
    """관리 설치 프로그램 경로. 사용자 데이터(~/.local/share/skim, SKIM_HOME)와 분리한다."""
    return Path.home() / '.local/share/skim-program'


def private_directory(path):
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError('프로그램 디렉터리의 소유자 또는 권한을 확인하세요.')


def _payload_contents(root):
    private_directory(root)
    entries = {}
    total = 0
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            path = Path(directory) / name
            info = path.lstat()
            if info.st_uid != os.getuid() or len(entries) >= MAX_FILES:
                raise ValueError('기존 배포 파일을 확인하세요.')
            mode = stat.S_IMODE(info.st_mode)
            relative = str(path.relative_to(root))
            if stat.S_ISDIR(info.st_mode) and mode == 0o700:
                entries[relative] = ('directory', mode)
            elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and mode in (0o600, 0o700):
                total += info.st_size
                if total > MAX_ARCHIVE_BYTES:
                    raise ValueError('기존 배포 파일이 너무 큽니다.')
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                with os.fdopen(fd, 'rb') as stream:
                    entries[relative] = ('file', mode, hashlib.file_digest(stream, 'sha256').hexdigest())
            else:
                raise ValueError('기존 배포 파일이 안전하지 않습니다.')
    return entries


class ProgramStore:
    def __init__(self, root, installation_id):
        self.root = Path(root)
        self.installation_id = installation_id

    @classmethod
    def initialize(cls, root):
        root = Path(root)
        private_directory(root)
        if any(root.iterdir()):
            raise ValueError('빈 디렉터리에만 새 설치를 만들 수 있습니다.')
        parent = root.parent.lstat()
        if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid() or parent.st_mode & 0o022:
            raise ValueError('설치 상위 디렉터리의 소유자 또는 권한을 확인하세요.')
        installation_id = secrets.token_hex(16)
        with tempfile.TemporaryDirectory(prefix='.skim-install-', dir=root.parent) as temporary:
            staging = Path(temporary) / 'program'
            staging.mkdir(mode=0o700)
            (staging / 'versions').mkdir(mode=0o700)
            fd = os.open(staging / 'install.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                json.dump({'installation_id': installation_id}, stream)
                stream.flush()
                os.fsync(stream.fileno())
            directory_fd = os.open(staging, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            os.rename(staging, root)
            parent_fd = os.open(root.parent, os.O_RDONLY)
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
        return cls(root, installation_id)

    @classmethod
    def open(cls, root):
        root = Path(root)
        private_directory(root)
        private_directory(root / 'versions')
        fd = os.open(root / 'install.json', os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 4096):
                raise ValueError('설치 정보 파일의 소유자 또는 권한을 확인하세요.')
            try:
                data = json.load(stream)
            except (ValueError, UnicodeError) as exc:
                raise ValueError('설치 정보가 손상되었습니다.') from exc
        if (not isinstance(data, dict) or set(data) != {'installation_id'}
                or not isinstance(data['installation_id'], str)
                or not re.fullmatch(r'[0-9a-f]{32}', data['installation_id'])):
            raise ValueError('설치 정보 형식이 잘못되었습니다.')
        return cls(root, data['installation_id'])

    def current(self):
        private_directory(self.root)
        private_directory(self.root / 'versions')
        link = self.root / 'current'
        try:
            info = link.lstat()
        except FileNotFoundError:
            return None
        if not stat.S_ISLNK(info.st_mode) or info.st_uid != os.getuid():
            raise ValueError('현재 버전 링크를 확인하세요.')
        target = PurePosixPath(os.readlink(link))
        if target.is_absolute() or len(target.parts) != 2 or target.parts[0] != 'versions':
            raise ValueError('현재 버전 링크가 설치 경로를 벗어납니다.')
        version_tuple(target.parts[1])
        private_directory(self.root / 'versions' / target.parts[1])
        return target.parts[1]

    def stage(self, release, download):
        version_tuple(release.tag)
        versions = self.root / 'versions'
        private_directory(self.root)
        private_directory(versions)
        target = versions / release.tag
        with tempfile.TemporaryDirectory(prefix='.stage-', dir=versions) as temporary:
            staged, sha256 = stage_release(release, Path(temporary), download)
            metadata = staged / '.skim-payload.json'
            fd = os.open(metadata, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                json.dump({'tag': release.tag, 'sha256': sha256}, stream, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            staged_fd = os.open(staged, os.O_RDONLY)
            try:
                os.fsync(staged_fd)
            finally:
                os.close(staged_fd)
            staged_contents = _payload_contents(staged)
            if target.exists() or target.is_symlink():
                if _payload_contents(target) != staged_contents:
                    raise ValueError('같은 버전의 기존 파일이 다릅니다. 기존 파일은 보존합니다.')
                return sha256
            os.rename(staged, target)
            versions_fd = os.open(versions, os.O_RDONLY)
            try:
                os.fsync(versions_fd)
            finally:
                os.close(versions_fd)
        return sha256

    @contextmanager
    def _bound_data(self, data_dir, update_id, states):
        private_directory(self.root)
        data_dir = Path(data_dir)
        private_directory(data_dir)
        info = data_dir.stat()
        binding = {'path': str(data_dir.resolve()), 'device': info.st_dev, 'inode': info.st_ino}
        fd = os.open(self.root / '.update.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            lock_info = os.fstat(fd)
            if (not stat.S_ISREG(lock_info.st_mode) or lock_info.st_uid != os.getuid()
                    or lock_info.st_nlink != 1 or stat.S_IMODE(lock_info.st_mode) != 0o600):
                raise ValueError('설치 잠금 파일이 안전하지 않습니다.')
            fcntl.flock(fd, fcntl.LOCK_EX)
            record = UpdateJournal(data_dir).read()
            if (not record or record['id'] != update_id or record['installation_id'] != self.installation_id
                    or record['state'] not in states):
                raise ValueError('설치와 업데이트 상태가 일치하지 않습니다.')
            path = self.root / '.data-binding.json'
            try:
                binding_fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            except FileNotFoundError:
                binding_fd, name = tempfile.mkstemp(prefix='.binding-', dir=self.root)
                try:
                    with os.fdopen(binding_fd, 'w', encoding='utf-8') as stream:
                        json.dump(binding, stream, sort_keys=True)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(name, path)
                finally:
                    if os.path.exists(name):
                        os.unlink(name)
                directory_fd = os.open(self.root, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            else:
                with os.fdopen(binding_fd, 'rb') as stream:
                    saved_info = os.fstat(stream.fileno())
                    if (not stat.S_ISREG(saved_info.st_mode) or saved_info.st_uid != os.getuid()
                            or saved_info.st_nlink != 1 or stat.S_IMODE(saved_info.st_mode) != 0o600
                            or saved_info.st_size > 4096 or json.load(stream) != binding):
                        raise ValueError('설치에 연결된 데이터 경로가 다르거나 안전하지 않습니다.')
            yield
        finally:
            os.close(fd)

    def activate(self, data_dir, update_id):
        """유지보수 장벽 안에서만 검증된 버전 링크를 교체한다."""
        with self._bound_data(data_dir, update_id, ('backup', 'switched')):
            self._activate(data_dir, update_id)

    def cancel(self, data_dir, update_id):
        """전환 전 상태에서만 기존 프로그램을 유지하고 장벽을 해제한다."""
        with self._bound_data(data_dir, update_id, ('prepared', 'quiesced', 'rolled_back')):
            current = UpdateJournal(data_dir).read()
            with maintenance.guard(data_dir, exclusive=True,
                                   update_id=None if current['state'] == 'rolled_back' else update_id):
                journal = UpdateJournal(data_dir)
                with journal.locked():
                    record = journal._read()
                    if (not record or record['id'] != update_id
                            or record['installation_id'] != self.installation_id
                            or record['state'] not in ('prepared', 'quiesced', 'rolled_back')):
                        raise ValueError('취소할 업데이트 상태가 아닙니다.')
                    previous = record['previous']['tag'] if record['previous'] else None
                    if self.current() != previous:
                        raise ValueError('프로그램이 이미 전환되어 취소할 수 없습니다.')
                    if record['state'] != 'rolled_back':
                        record['state'] = 'rolled_back'
                        journal._write(record)

    def _activate(self, data_dir, update_id):
        journal = UpdateJournal(data_dir)
        with maintenance.guard(data_dir, exclusive=True, update_id=update_id):
            record = journal.read()
            if (record['installation_id'] != self.installation_id or record['state'] not in ('backup', 'switched')):
                raise ValueError('업데이트 준비 상태가 일치하지 않습니다.')
            target = self.root / 'versions' / record['tag']
            private_directory(target)
            fd = os.open(target / '.skim-payload.json', os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, 'rb') as stream:
                info = os.fstat(stream.fileno())
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1
                        or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 4096):
                    raise ValueError('준비된 버전 정보가 안전하지 않습니다.')
                metadata = json.load(stream)
            if metadata != {'tag': record['tag'], 'sha256': record['sha256']}:
                raise ValueError('준비된 버전이 업데이트 요청과 다릅니다.')
            current = self.current()
            if current != record['tag']:
                previous = record['previous']['tag'] if record['previous'] else None
                if record['state'] != 'backup' or current != previous:
                    raise ValueError('현재 버전이 업데이트 요청과 다릅니다.')
                temporary = self.root / ('.current-' + secrets.token_hex(16))
                try:
                    os.symlink('versions/' + record['tag'], temporary)
                    os.replace(temporary, self.root / 'current')
                    root_fd = os.open(self.root, os.O_RDONLY)
                    try:
                        os.fsync(root_fd)
                    finally:
                        os.close(root_fd)
                finally:
                    if temporary.is_symlink():
                        temporary.unlink()
            if record['state'] == 'backup':
                journal.advance(update_id, 'switched')

    def rollback(self, data_dir, update_id):
        """복원된 DB를 확인한 뒤 이전 프로그램으로 되돌린다."""
        with self._bound_data(data_dir, update_id, ('rolling_back',)):
            self._rollback(data_dir, update_id)

    def _rollback(self, data_dir, update_id):
        journal = UpdateJournal(data_dir)
        with maintenance.guard(data_dir, exclusive=True, update_id=update_id):
            record = journal.read()
            if record['installation_id'] != self.installation_id or record['state'] != 'rolling_back':
                raise ValueError('복구할 설치 또는 단계가 일치하지 않습니다.')
            marker = Path(data_dir) / 'update-backups' / (update_id + '.restored')
            info = marker.lstat()
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1
                    or stat.S_IMODE(info.st_mode) != 0o600):
                raise ValueError('데이터 복구 상태를 확인하세요.')
            previous = record['previous']
            prior_tag = previous['tag'] if previous else None
            if previous:
                prior = self.root / 'versions' / prior_tag
                private_directory(prior)
                fd = os.open(prior / '.skim-payload.json', os.O_RDONLY | os.O_NOFOLLOW)
                with os.fdopen(fd, 'rb') as stream:
                    info = os.fstat(stream.fileno())
                    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1
                            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 4096
                            or json.load(stream) != previous):
                        raise ValueError('이전 버전 정보를 확인하세요.')
            current = self.current()
            if current not in (prior_tag, record['tag']):
                raise ValueError('현재 버전이 복구 대상과 다릅니다.')
            if current != prior_tag:
                if prior_tag is None:
                    (self.root / 'current').unlink()
                else:
                    temporary = self.root / ('.current-' + secrets.token_hex(16))
                    try:
                        os.symlink('versions/' + prior_tag, temporary)
                        os.replace(temporary, self.root / 'current')
                    finally:
                        if temporary.is_symlink():
                            temporary.unlink()
                root_fd = os.open(self.root, os.O_RDONLY)
                try:
                    os.fsync(root_fd)
                finally:
                    os.close(root_fd)
            journal.finish_rollback(update_id)
