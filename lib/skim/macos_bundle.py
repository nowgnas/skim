"""Prepare a user LaunchAgent and a Dock launcher without activating them."""
import argparse
import fcntl
import os
from pathlib import Path
import plistlib
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time

SOL_LOCAL = 0
LOCAL_PEERPID = 2


def main(argv=None, quiet=False):
    parser = argparse.ArgumentParser(description='skim macOS 실행 파일 준비')
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    if sys.platform != 'darwin':
        print('macOS에서 실행하세요.', file=sys.stderr)
        return 1
    binary = (args.root.resolve() / 'bin/skim')
    output = args.output.resolve()
    agent = output / 'com.skim.web.plist'
    app = output / 'Skim.app'
    if not binary.is_file() or agent.exists() or agent.is_symlink() or app.exists() or app.is_symlink():
        print('실행 파일을 확인하세요. 기존 준비 파일은 덮어쓰지 않습니다.', file=sys.stderr)
        return 1
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = os.environ.get('PATH', '/usr/bin:/bin:/usr/sbin:/sbin')
    environment = {'HOME': str(Path.home()), 'PATH': os.pathsep.join(os.path.abspath(p or '.') for p in path.split(os.pathsep))}
    for name in ('SKIM_HOME', 'CODEX_HOME', 'SKIM_CODEX_MODEL', 'SKIM_PYTHON', 'SESSIO_HOME', 'GH_CONFIG_DIR'):
        if os.environ.get(name):
            value = os.environ[name]
            environment[name] = value if name == 'SKIM_CODEX_MODEL' else os.path.abspath(os.path.expanduser(value))
    data_dir = str((Path(environment['SKIM_HOME']) / 'data' if 'SKIM_HOME' in environment else Path.home() / '.local/share/skim').resolve())
    plist = {'Label': 'com.skim.web', 'ProgramArguments': [str(binary), 'web', '--no-browser'],
             'EnvironmentVariables': environment, 'RunAtLoad': True,
             'KeepAlive': {'SuccessfulExit': False}, 'ThrottleInterval': 10,
             'StandardOutPath': '/dev/null', 'StandardErrorPath': '/dev/null'}
    command = ' '.join(shlex.quote(part) for part in ['/usr/bin/env'] +
                       [name + '=' + value for name, value in environment.items()] + [str(binary), 'open'])
    literal = command.replace('\\', '\\\\').replace('"', '\\"')
    source = ('on run\ntry\ndo shell script "' + literal + '"\n'
              'on error\ndisplay dialog "skim 서버가 실행 중인지 확인해 주세요." buttons {"확인"} default button 1\n'
              'end try\nend run')
    try:
        subprocess.run(['/usr/bin/osacompile', '-o', str(app), '-e', source], check=True, capture_output=True)
        marker = app / 'Contents/Resources/skim-managed.plist'
        with marker.open('wb') as stream:
            plistlib.dump({'root': str(args.root.resolve()), 'data_dir': data_dir}, stream)
        fd = os.open(agent, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            plistlib.dump(plist, stream)
    except (OSError, subprocess.CalledProcessError):
        print('macOS 실행 파일을 준비하지 못했습니다. 준비 폴더를 확인하세요.', file=sys.stderr)
        return 1
    if not quiet:
        print('준비 완료:', output)
        print('자동 시작은 아직 등록되지 않았습니다.')
    return 0


def install_app(config):
    """Install or open the user's macOS app without replacing unrelated files."""
    if sys.platform != 'darwin':
        print('skim app은 macOS에서 사용할 수 있습니다.', file=sys.stderr)
        return 1
    root = Path(__file__).resolve().parents[2]
    home = Path.home()
    app = home / 'Applications/Skim.app'
    agent = home / 'Library/LaunchAgents/com.skim.web.plist'
    marker = app / 'Contents/Resources/skim-managed.plist'
    label = f'gui/{os.getuid()}/com.skim.web'
    try:
        if app.is_symlink() or agent.is_symlink():
            raise ValueError('기존 앱 또는 설정이 심볼릭 링크입니다. 직접 확인하세요.')
        if app.exists():
            with marker.open('rb') as stream:
                app_settings = plistlib.load(stream)
                if app_settings.get('root') != str(root):
                    raise ValueError('기존 Skim.app은 다른 설치에 속합니다.')
                if app_settings.get('data_dir') != str(config.data_dir.resolve()):
                    raise ValueError('기존 앱과 현재 SKIM_HOME이 다릅니다. 같은 환경에서 실행하세요.')
        if agent.exists():
            with agent.open('rb') as stream:
                saved = plistlib.load(stream)
            if saved.get('Label') != 'com.skim.web' or saved.get('ProgramArguments') != [str(root / 'bin/skim'), 'web', '--no-browser']:
                raise ValueError('기존 LaunchAgent는 다른 설치에 속합니다.')
            saved_env = saved.get('EnvironmentVariables', {})
            saved_data = (Path(saved_env['SKIM_HOME']) / 'data' if saved_env.get('SKIM_HOME') else Path(saved_env.get('HOME', str(home))) / '.local/share/skim').resolve()
            if saved_data != config.data_dir.resolve():
                raise ValueError('자동 시작 설정과 현재 SKIM_HOME이 다릅니다. 같은 환경에서 실행하세요.')
        status = subprocess.run(['/bin/launchctl', 'print', label], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode
        if status == 0:
            if not agent.exists():
                raise ValueError('등록된 LaunchAgent 설정 파일을 확인하세요.')
        else:
            lock_path = config.data_dir / 'web.lock'
            fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            with os.fdopen(fd, 'r+') as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise ValueError('기존 skim 서버가 실행 중입니다. 진행 중인 작업을 확인하고 종료한 뒤 skim app을 다시 실행하세요.') from None
                finally:
                    fcntl.flock(lock, fcntl.LOCK_UN)
        if not app.exists() or not agent.exists():
            with tempfile.TemporaryDirectory(prefix='skim-app-') as temporary:
                result = main(['--root', str(root), '--output', temporary], quiet=True)
                if result:
                    return result
                app.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                agent.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                if not app.exists():
                    with tempfile.TemporaryDirectory(prefix='.skim-app-', dir=app.parent) as staging:
                        staged_app = Path(staging) / 'Skim.app'
                        shutil.copytree(Path(temporary) / 'Skim.app', staged_app)
                        os.rename(staged_app, app)
                if not agent.exists():
                    fd = os.open(agent, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(fd, 'wb') as output, (Path(temporary) / agent.name).open('rb') as source:
                        shutil.copyfileobj(source, output)
        if status != 0:
            result = subprocess.run(['/bin/launchctl', 'bootstrap', f'gui/{os.getuid()}', str(agent)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if result.returncode:
                raise ValueError('자동 시작을 등록하지 못했습니다. macOS 로그인 세션과 LaunchAgent 설정을 확인하세요.')
        started = subprocess.run(['/bin/launchctl', 'kickstart', '-p', label], capture_output=True, text=True)
        if started.returncode or not started.stdout.strip().isdigit():
            raise ValueError('자동 시작 서버를 시작하지 못했습니다. 상태를 확인한 뒤 다시 실행하세요.')
        server_pid = started.stdout.strip()
        from .web import open_running
        ready = False
        for _ in range(50):
            try:
                with socket.socket(socket.AF_UNIX) as client:
                    client.settimeout(0.1)
                    client.connect(str(config.data_dir / 'web.sock'))
                    if client.getsockopt(SOL_LOCAL, LOCAL_PEERPID) != int(server_pid):
                        raise ValueError('다른 skim 서버가 실행 중입니다. 진행 중인 작업을 확인하고 종료한 뒤 다시 실행하세요.')
                    ready = True
                    break
            except OSError:
                pass
            time.sleep(0.1)
        if ready:
            status_check = subprocess.run(['/bin/launchctl', 'print', label], capture_output=True, text=True)
            if status_check.returncode or not re.search(r'\bpid\s*=\s*' + re.escape(server_pid) + r'\b', status_check.stdout):
                raise ValueError('자동 시작 서버가 실행 중이 아닙니다. 기존 수동 서버를 종료한 뒤 다시 실행하세요.')
            print('Skim.app과 로그인 시 자동 시작이 준비되었습니다. Applications에서 Dock에 추가할 수 있습니다.')
            return open_running(config)
        raise ValueError('서버가 아직 시작되지 않았습니다. 로그인 세션을 확인한 뒤 skim app을 다시 실행하세요.')
    except (OSError, ValueError, plistlib.InvalidFileException) as exc:
        print(f'skim app: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
