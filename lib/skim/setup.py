"""사용자 전용 설치. 시스템 범위 설정이나 패키지 설치는 하지 않는다."""
import argparse
import os
from pathlib import Path
import platform
import plistlib
import shutil
import subprocess
import sys


LABEL = 'dev.skim.web'
UNIT = 'skim-web.service'


def write(path, content):
    """서비스 파일을 사용자 전용 권한으로 원자 교체한다."""
    temporary = path.with_name(path.name + '.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(content)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description='skim 설치 (Python 3.11+, 사용자 범위)')
    parser.add_argument('--root', required=True, type=Path)
    options = parser.add_mutually_exclusive_group()
    options.add_argument('--web', action='store_true', help='로그인 웹 자동 시작')
    options.add_argument('--remove-web', action='store_true', help='웹 자동 시작 제거')
    args = parser.parse_args(argv)
    try:
        home = Path.home()
        root = args.root.resolve()
        binary = root / 'bin/skim'
        system = platform.system()
        if (args.web or args.remove_web) and system not in ('Darwin', 'Linux'):
            raise ValueError('자동 시작은 macOS와 Linux에서 지원합니다.')
        link = home / '.local/bin/skim'
        link.parent.mkdir(parents=True, exist_ok=True)
        if link.is_symlink():
            if link.resolve() != binary:
                raise ValueError('기존 skim 링크가 다른 설치를 가리킵니다.')
        elif link.exists():
            raise ValueError('기존 ~/.local/bin/skim 파일을 덮어쓰지 않습니다.')
        else:
            link.symlink_to(binary)
        if args.web or args.remove_web:
            manager = 'launchctl' if system == 'Darwin' else 'systemctl'
            if not shutil.which(manager):
                raise ValueError(f'{manager}가 PATH에 없습니다.')
            service = (home / 'Library/LaunchAgents' / (LABEL + '.plist') if system == 'Darwin'
                       else home / '.config/systemd/user' / UNIT)
            if args.remove_web:
                if system == 'Darwin':
                    # 미등록 상태의 bootout 실패는 멱등 제거에서 허용한다.
                    subprocess.run(['launchctl', 'bootout', f'gui/{os.getuid()}/{LABEL}'],
                                   check=False, capture_output=True)
                elif service.exists():
                    subprocess.run(['systemctl', '--user', 'disable', '--now', UNIT], check=True)
                service.unlink(missing_ok=True)
                if system == 'Linux':
                    subprocess.run(['systemctl', '--user', 'daemon-reload'], check=True)
            else:
                service.parent.mkdir(parents=True, exist_ok=True)
                environment = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'),
                               'SKIM_PYTHON': str(Path(sys.executable).resolve())}
                if os.environ.get('SKIM_HOME'):
                    environment['SKIM_HOME'] = str(Path(os.environ['SKIM_HOME']).expanduser().resolve())
                if system == 'Darwin':
                    content = plistlib.dumps({'Label': LABEL,
                        'ProgramArguments': [str(binary), 'web', '--no-browser'],
                        'EnvironmentVariables': environment, 'RunAtLoad': True,
                        'KeepAlive': {'SuccessfulExit': False}})
                    write(service, content)
                    subprocess.run(['launchctl', 'bootout', f'gui/{os.getuid()}/{LABEL}'],
                                   check=False, capture_output=True)
                    subprocess.run(['launchctl', 'bootstrap', f'gui/{os.getuid()}', str(service)], check=True)
                else:
                    # systemd의 인수 파싱·지정자·환경 확장을 명시적으로 이스케이프한다.
                    def quote(value):
                        if any(c in value for c in '\n\r\0'):
                            raise ValueError('서비스 경로·환경에 줄바꿈을 사용할 수 없습니다.')
                        return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%') + '"'
                    command = quote(str(binary)).replace('$', '$$')
                    lines = ['[Unit]', 'Description=skim 로컬 PR 대시보드', '[Service]',
                             'Type=simple', f'ExecStart={command} web --no-browser',
                             'Restart=on-failure', 'RestartSec=5', 'UMask=0077']
                    lines += ['Environment=' + quote(k + '=' + v) for k, v in environment.items()]
                    lines += ['[Install]', 'WantedBy=default.target', '']
                    write(service, '\n'.join(lines).encode())
                    subprocess.run(['systemctl', '--user', 'daemon-reload'], check=True)
                    subprocess.run(['systemctl', '--user', 'enable', '--now', UNIT], check=True)
                    subprocess.run(['systemctl', '--user', 'restart', UNIT], check=True)
        print('설치 완료: ~/.local/bin/skim (PATH에 ~/.local/bin 추가)')
        return 0
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f'설치 실패: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
