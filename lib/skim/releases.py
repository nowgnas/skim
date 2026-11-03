"""정식 릴리스 메타데이터와 배포 파일의 신뢰 경계."""
from dataclasses import dataclass
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit
from urllib import request
import hashlib
import fcntl
import gzip
import io
import json
import os
import re
import secrets
import stat
import subprocess
import tarfile
import tempfile

from .collect import CollectionError


ASSETS = ('skim-macos.tar.gz', 'SHA256SUMS', 'manifest.json', 'install.sh')
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_EXPANDED_BYTES = 80 * 1024 * 1024
MAX_FILES = 5000
DOWNLOAD_HOSTS = frozenset(('github.com', 'objects.githubusercontent.com', 'release-assets.githubusercontent.com'))


class ReleaseUnavailable(Exception):
    """릴리스 조회에 실패했다. 비밀값이 포함될 수 있는 원문은 노출하지 않는다."""


class SafeRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_download_url(newurl)
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None:
            for name in list(redirected.headers):
                if name.lower() in ('authorization', 'cookie', 'proxy-authorization'):
                    redirected.remove_header(name)
        return redirected


def download_private_asset(release, name, limit, runner=subprocess.run, opener=None):
    if name not in release.asset_ids or type(limit) is not int or not 0 < limit <= MAX_ARCHIVE_BYTES:
        raise ValueError('잘못된 릴리스 자산입니다.')
    try:
        result = runner(['gh', 'auth', 'token', '--hostname', 'github.com'], capture_output=True, text=True, timeout=15)
        token = result.stdout.strip() if result.returncode == 0 and isinstance(result.stdout, str) else ''
        if not token or any(character.isspace() for character in token):
            raise ValueError('인증을 확인하세요.')
        url = f'https://api.github.com/repos/nowgnas/skim/releases/assets/{release.asset_ids[name]}'
        req = request.Request(url, headers={'Accept': 'application/octet-stream', 'Authorization': 'Bearer ' + token,
                                            'User-Agent': 'skim-updater'})
        with (opener or request.build_opener(SafeRedirect())).open(req, timeout=15) as response:
            content = response.read(limit + 1)
        if len(content) > limit:
            raise ValueError('응답 크기가 너무 큽니다.')
        return content
    except (OSError, ValueError, subprocess.TimeoutExpired):
        raise ReleaseUnavailable('릴리스 파일을 받지 못했습니다. gh 로그인·저장소 접근 권한과 네트워크를 확인하세요.') from None


def fetch_latest_authenticated(github):
    try:
        candidates = []
        for page in range(1, 11):
            rows = github.get(f'repos/nowgnas/skim/releases?per_page=100&page={page}')
            if not isinstance(rows, list):
                raise ValueError('릴리스 응답 형식이 잘못되었습니다.')
            for row in rows:
                if not isinstance(row, dict):
                    raise ValueError('릴리스 응답 형식이 잘못되었습니다.')
                if row.get('draft') is True or row.get('prerelease') is True:
                    continue
                candidates.append(parse_release(row))
            if len(rows) < 100:
                return max(candidates, key=lambda release: release.version, default=None)
        raise ValueError('릴리스 조회 한도를 초과했습니다.')
    except (CollectionError, OSError, ValueError, TypeError, KeyError):
        raise ReleaseUnavailable('정식 릴리스를 조회하지 못했습니다. gh 로그인·저장소 접근 권한과 네트워크를 확인하세요.') from None


@dataclass(frozen=True)
class Release:
    tag: str
    version: tuple[int, int, int]
    assets: dict[str, str]
    asset_ids: dict[str, int]


def check_download_url(url):
    parsed = urlsplit(url)
    if (parsed.scheme != 'https' or parsed.hostname not in DOWNLOAD_HOSTS or parsed.username is not None
            or parsed.password is not None or parsed.port not in (None, 443)):
        raise ValueError('신뢰할 수 없는 다운로드 주소입니다.')


def version_tuple(tag):
    match = re.fullmatch(r'v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)', tag if isinstance(tag, str) else '')
    if not match or len(tag) > 64:
        raise ValueError('지원하지 않는 릴리스 버전입니다.')
    return tuple(int(part) for part in match.groups())


def compare_versions(current, latest, existing_hash, latest_hash):
    target = version_tuple(latest)
    if not isinstance(latest_hash, str) or not re.fullmatch(r'[a-f0-9]{64}', latest_hash):
        raise ValueError('배포 파일 해시가 잘못되었습니다.')
    if current is None:
        return 'install'
    previous = version_tuple(current)
    if not isinstance(existing_hash, str) or not re.fullmatch(r'[a-f0-9]{64}', existing_hash):
        raise ValueError('기존 배포 파일 해시가 잘못되었습니다.')
    if target < previous or (target == previous and existing_hash != latest_hash):
        raise ValueError('버전 역행 또는 동일 버전의 파일 변경을 허용하지 않습니다.')
    return 'current' if target == previous else 'update'


class UpdateJournal:
    """프로세스 중단 뒤에도 대상과 적용 단계를 보존한다."""
    STATES = ('prepared', 'quiesced', 'backup', 'switched', 'ready', 'committed')
    ROLLBACK_STATES = ('rolling_back', 'rolled_back')

    def __init__(self, directory):
        self.directory = Path(directory)
        info = self.directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError('업데이트 상태 디렉터리 권한이 안전하지 않습니다.')
        self.path = self.directory / 'update-journal.json'
        self.lock_path = self.directory / 'update-journal.lock'

    @contextmanager
    def locked(self):
        fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
                raise ValueError('업데이트 잠금 파일 권한이 안전하지 않습니다.')
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def _read(self):
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return None
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 16384:
                raise ValueError('업데이트 상태 파일 권한이 안전하지 않습니다.')
            try:
                data = json.load(stream)
            except (ValueError, UnicodeError) as exc:
                raise ValueError('업데이트 상태 파일이 손상되었습니다.') from exc
        if (not isinstance(data, dict) or set(data) != {'id', 'installation_id', 'tag', 'sha256', 'previous', 'state'}
                or not isinstance(data['id'], str) or not re.fullmatch(r'[0-9a-f]{32}', data['id'])
                or not isinstance(data['installation_id'], str) or not re.fullmatch(r'[0-9a-f]{32}', data['installation_id'])
                or data['state'] not in self.STATES + self.ROLLBACK_STATES or not isinstance(data['sha256'], str)
                or not re.fullmatch(r'[0-9a-f]{64}', data['sha256'])):
            raise ValueError('업데이트 상태 형식이 잘못되었습니다.')
        version_tuple(data['tag'])
        self._validate_previous(data['previous'])
        return data

    @staticmethod
    def _validate_previous(previous):
        if previous is None:
            return
        if (not isinstance(previous, dict) or set(previous) != {'tag', 'sha256'}
                or not isinstance(previous['sha256'], str) or not re.fullmatch(r'[0-9a-f]{64}', previous['sha256'])):
            raise ValueError('이전 버전 정보가 잘못되었습니다.')
        version_tuple(previous['tag'])

    def _write(self, data):
        fd, name = tempfile.mkstemp(prefix='.update-journal-', dir=self.directory)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                json.dump(data, stream, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path)
            directory_fd = os.open(self.directory, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def read(self):
        with self.locked():
            return self._read()

    def start(self, installation_id, tag, sha256, previous=None):
        if not isinstance(installation_id, str) or not re.fullmatch(r'[0-9a-f]{32}', installation_id):
            raise ValueError('설치 식별자가 잘못되었습니다.')
        version_tuple(tag)
        self._validate_previous(previous)
        if not isinstance(sha256, str) or not re.fullmatch(r'[0-9a-f]{64}', sha256):
            raise ValueError('업데이트 해시가 잘못되었습니다.')
        if previous is not None and compare_versions(previous['tag'], tag, previous['sha256'], sha256) != 'update':
            raise ValueError('새로운 정식 버전이 아닙니다.')
        with self.locked():
            current = self._read()
            if (current and current['state'] == 'committed' and current['installation_id'] == installation_id
                    and current['tag'] == tag and current['sha256'] == sha256 and current['previous'] == previous):
                return current
            if current and current['state'] not in ('committed', 'rolled_back'):
                if (current['installation_id'] == installation_id and current['tag'] == tag
                        and current['sha256'] == sha256 and current['previous'] == previous):
                    return current
                raise ValueError('다른 업데이트가 진행 중입니다.')
            record = {'id': secrets.token_hex(16), 'installation_id': installation_id, 'tag': tag,
                      'sha256': sha256, 'previous': previous, 'state': 'prepared'}
            self._write(record)
            return record

    def advance(self, update_id, next_state):
        with self.locked():
            current = self._read()
            if not current or current['id'] != update_id or next_state not in self.STATES:
                raise ValueError('업데이트 상태가 일치하지 않습니다.')
            index = self.STATES.index(current['state'])
            if next_state == current['state']:
                return current
            if index + 1 >= len(self.STATES) or self.STATES[index + 1] != next_state:
                raise ValueError('잘못된 업데이트 상태 전이입니다.')
            current['state'] = next_state
            self._write(current)
            return current

    def begin_rollback(self, update_id):
        with self.locked():
            current = self._read()
            if not current or current['id'] != update_id:
                raise ValueError('업데이트 상태가 일치하지 않습니다.')
            if current['state'] == 'rolling_back':
                return current
            if current['state'] not in ('backup', 'switched', 'ready'):
                raise ValueError('이 단계에서는 자동 복구할 수 없습니다.')
            current['state'] = 'rolling_back'
            self._write(current)
            return current

    def finish_rollback(self, update_id):
        with self.locked():
            current = self._read()
            if not current or current['id'] != update_id or current['state'] not in ('rolling_back', 'rolled_back'):
                raise ValueError('복구할 업데이트 상태가 아닙니다.')
            if current['state'] == 'rolling_back':
                current['state'] = 'rolled_back'
                self._write(current)
            return current


def verify_payload(release, sums, payloads):
    expected_names = set(ASSETS) - {'SHA256SUMS'}
    if set(payloads) != expected_names or len(sums) > 16 * 1024:
        raise ValueError('릴리스 파일이 부족합니다.')
    try:
        rows = sums.decode('ascii').splitlines()
        expected = {}
        for row in rows:
            match = re.fullmatch(r'([a-f0-9]{64})  ([A-Za-z0-9._-]+)', row)
            if not match or match[2] in expected:
                raise ValueError('체크섬 형식이 잘못되었습니다.')
            expected[match[2]] = match[1]
        if set(expected) != expected_names or any(hashlib.sha256(payloads[name]).hexdigest() != expected[name] for name in expected_names):
            raise ValueError('배포 파일의 체크섬이 일치하지 않습니다.')
        manifest = json.loads(payloads['manifest.json'])
        if not isinstance(manifest, dict) or manifest.get('version') != release.tag or manifest.get('archive') != 'skim-macos.tar.gz':
            raise ValueError('배포 파일의 버전이 일치하지 않습니다.')
    except (UnicodeError, json.JSONDecodeError, TypeError) as exc:
        raise ValueError('배포 파일 형식이 잘못되었습니다.') from exc


def stage_release(release, destination, download=download_private_asset):
    destination = Path(destination)
    if destination.is_symlink() or not destination.is_dir() or any(destination.iterdir()):
        raise ValueError('준비할 위치가 비어 있지 않습니다.')
    limits = {'skim-macos.tar.gz': MAX_ARCHIVE_BYTES, 'manifest.json': 64 * 1024,
              'install.sh': 1024 * 1024, 'SHA256SUMS': 16 * 1024}
    payloads = {name: download(release, name, limits[name]) for name in ASSETS}
    if any(not isinstance(value, bytes) or len(value) > limits[name] for name, value in payloads.items()):
        raise ValueError('다운로드한 배포 파일 형식이 잘못되었습니다.')
    verify_payload(release, payloads['SHA256SUMS'], {name: payloads[name] for name in ASSETS if name != 'SHA256SUMS'})
    with tempfile.TemporaryDirectory(prefix='.staging-', dir=destination) as temporary:
        root = Path(temporary)
        extract_archive(payloads['skim-macos.tar.gz'], root)
        manifest_path = root / 'skim' / 'manifest.json'
        if not manifest_path.is_file() or manifest_path.read_bytes() != payloads['manifest.json']:
            raise ValueError('압축 파일의 버전 정보가 일치하지 않습니다.')
        required = ('bin/skim', 'lib/python.sh', 'lib/skim/__init__.py', 'lib/skim/cli.py', 'lib/skim/web.py')
        program = root / 'skim'
        if any(not (program / name).is_file() for name in required) or not os.access(program / 'bin/skim', os.X_OK):
            raise ValueError('실행에 필요한 프로그램 파일이 없습니다.')
        os.rename(root / 'skim', destination / 'skim')
    return destination / 'skim', hashlib.sha256(payloads['skim-macos.tar.gz']).hexdigest()


def parse_release(data):
    if not isinstance(data, dict) or data.get('draft') is not False or data.get('prerelease') is not False:
        raise ValueError('정식 릴리스가 아닙니다.')
    tag = data.get('tag_name')
    version = version_tuple(tag)
    rows = data.get('assets')
    if not isinstance(rows, list):
        raise ValueError('릴리스 자산이 없습니다.')
    assets, asset_ids = {}, {}
    for row in rows:
        if not isinstance(row, dict) or row.get('name') not in ASSETS:
            continue
        name, url = row['name'], row.get('browser_download_url')
        asset_id = row.get('id')
        if name in assets or not isinstance(url, str) or type(asset_id) is not int or asset_id <= 0:
            raise ValueError('릴리스 자산이 잘못되었습니다.')
        parsed = urlsplit(url)
        if (parsed.scheme != 'https' or parsed.netloc != 'github.com' or parsed.query or parsed.fragment
                or parsed.path != f'/nowgnas/skim/releases/download/{tag}/{name}'):
            raise ValueError('신뢰할 수 없는 릴리스 주소입니다.')
        assets[name] = url
        asset_ids[name] = asset_id
    if set(assets) != set(ASSETS):
        raise ValueError('정식 릴리스 자산이 부족합니다.')
    return Release(tag, version, assets, asset_ids)


def extract_archive(content, destination):
    destination = Path(destination)
    if len(content) > MAX_ARCHIVE_BYTES or destination.is_symlink() or not destination.is_dir() or any(destination.iterdir()):
        raise ValueError('안전하지 않은 압축 파일 또는 대상입니다.')
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(content), mode='rb') as compressed:
            expanded = compressed.read(MAX_EXPANDED_BYTES + 1)
        if len(expanded) > MAX_EXPANDED_BYTES:
            raise ValueError('압축 해제 크기가 너무 큽니다.')
        with tarfile.open(fileobj=io.BytesIO(expanded), mode='r:') as archive:
            seen = set()
            total = 0
            for member in archive:
                if len(seen) >= MAX_FILES:
                    raise ValueError('배포 파일이 너무 많습니다.')
                name = member.name
                path = PurePosixPath(name)
                if (not name or '\\' in name or path.is_absolute() or '..' in path.parts
                        or not path.parts or path.parts[0] != 'skim' or str(path) in seen
                        or not (member.isdir() or member.isfile())):
                    raise ValueError('안전하지 않은 배포 파일 경로입니다.')
                seen.add(str(path))
                total += member.size
                if total > MAX_ARCHIVE_BYTES:
                    raise ValueError('압축 해제 크기가 너무 큽니다.')
                target = destination.joinpath(*PurePosixPath(member.name).parts)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True, mode=0o700)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    stream = archive.extractfile(member)
                    if stream is None:
                        raise ValueError('배포 파일을 읽지 못했습니다.')
                    with target.open('xb') as output:
                        while chunk := stream.read(1024 * 1024):
                            output.write(chunk)
                        output.flush()
                        os.fsync(output.fileno())
                    target.chmod(0o700 if member.mode & 0o111 else 0o600)
    except (tarfile.TarError, EOFError, OSError) as exc:
        raise ValueError('배포 파일을 해제하지 못했습니다.') from exc
