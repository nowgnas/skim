"""정식 배포물 생성. 출시 평가 실패 시 아무 파일도 게시하지 않는다."""
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import sys
import tarfile
import tempfile
import zipfile

from .releases import MAX_ARCHIVE_BYTES, MAX_FILES, version_tuple


def check_release(root):
    result = subprocess.run([sys.executable, str(root / 'scripts/check_release.py')], cwd=root,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
    return result.returncode == 0


def build(root, destination, tag, release_gate=check_release):
    root, destination = Path(root).resolve(), Path(destination)
    version_tuple(tag)
    if not release_gate(root):
        raise ValueError('출시 평가 게이트를 통과하지 못했습니다.')
    if destination.exists() or destination.is_symlink():
        raise ValueError('기존 배포 파일을 덮어쓰지 않습니다.')
    if not destination.parent.is_dir():
        raise ValueError('배포 파일의 상위 디렉터리가 없습니다.')
    try:
        dirty = subprocess.run(['git', 'status', '--porcelain', '--untracked-files=no'], cwd=root,
                               capture_output=True, timeout=30, check=True)
        if dirty.stdout:
            raise ValueError('변경된 추적 파일이 있어 배포할 수 없습니다.')
        archived = subprocess.run(['git', 'archive', '--format=tar', '--prefix=skim/', 'HEAD',
                                   'bin', 'lib', 'README.md'],
                                  cwd=root, capture_output=True, timeout=60, check=True).stdout
        if len(archived) > MAX_ARCHIVE_BYTES:
            raise ValueError('배포 파일이 너무 큽니다.')
        manifest = json.dumps({'version': tag, 'archive': 'skim-macos.tar.gz'}, sort_keys=True).encode() + b'\n'
        archive = io.BytesIO()
        installer = io.BytesIO()
        installer.write(b'#!/usr/bin/env python3\n')
        bootstrap_present = False
        with tarfile.open(fileobj=io.BytesIO(archived), mode='r:') as source, tarfile.open(fileobj=archive, mode='w:gz') as target, zipfile.ZipFile(installer, mode='a', compression=zipfile.ZIP_DEFLATED) as bootstrap:
            members = source.getmembers()
            if len(members) > MAX_FILES:
                raise ValueError('배포 파일이 너무 많습니다.')
            bootstrap.writestr('__main__.py', 'from skim.bootstrap import main\nraise SystemExit(main())\n')
            for member in members:
                path = PurePosixPath(member.name)
                if path.is_absolute() or '..' in path.parts or not path.parts or path.parts[0] != 'skim' or not (member.isdir() or member.isfile()):
                    raise ValueError('배포 파일에 안전하지 않은 경로가 있습니다.')
                data = source.extractfile(member) if member.isfile() else None
                if member.isfile() and member.name.startswith('skim/lib/skim/') and member.name.endswith('.py'):
                    contents = data.read()
                    bootstrap.writestr(member.name.removeprefix('skim/lib/'), contents)
                    bootstrap_present |= member.name == 'skim/lib/skim/bootstrap.py'
                    data = io.BytesIO(contents)
                target.addfile(member, data)
            metadata = tarfile.TarInfo('skim/manifest.json')
            metadata.size, metadata.mode, metadata.mtime = len(manifest), 0o644, 0
            target.addfile(metadata, io.BytesIO(manifest))
        if not bootstrap_present or installer.tell() > 1024 * 1024 or archive.tell() > MAX_ARCHIVE_BYTES:
            raise ValueError('릴리스 설치 파일 또는 배포 파일 크기를 확인하세요.')
        payloads = {'skim-macos.tar.gz': archive.getvalue(), 'manifest.json': manifest, 'install.sh': installer.getvalue()}
        sums = ''.join(hashlib.sha256(data).hexdigest() + '  ' + name + '\n' for name, data in payloads.items()).encode('ascii')
        with tempfile.TemporaryDirectory(prefix='.skim-release-', dir=destination.parent) as temp:
            package = Path(temp) / 'release'
            package.mkdir(mode=0o700)
            for name, data in {**payloads, 'SHA256SUMS': sums}.items():
                with (package / name).open('xb') as stream:
                    stream.write(data)
            os.rename(package, destination)
    except (OSError, subprocess.SubprocessError, tarfile.TarError) as exc:
        raise ValueError('배포 파일을 만들지 못했습니다.') from exc
