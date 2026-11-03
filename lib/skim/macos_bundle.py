"""Prepare a user LaunchAgent and a Dock launcher without activating them."""
import argparse
import fcntl
import os
from pathlib import Path
import plistlib
import re
import shlex
import shutil
import stat
import socket
import subprocess
import sys
import tempfile
import time

SOL_LOCAL = 0
LOCAL_PEERPID = 2


APP_NAME = 'skim PR.app'


def real_path(value):
    return os.path.realpath(os.path.expanduser(value)) if isinstance(value, str) else value


def normalized_environment(environment):
    if not isinstance(environment, dict):
        return environment
    return {name: (value if name == 'SKIM_CODEX_MODEL' else
                   os.pathsep.join(real_path(p) for p in value.split(os.pathsep))
                   if name == 'PATH' and isinstance(value, str) else real_path(value))
            for name, value in environment.items()}


def environment_settings():
    environment = {'HOME': str(Path.home()), 'PATH': os.pathsep.join(
        os.path.normpath(p) for p in os.environ.get('PATH', '/usr/bin:/bin:/usr/sbin:/sbin').split(os.pathsep)
        if os.path.isabs(p))}
    for name in ('SKIM_HOME', 'CODEX_HOME', 'SKIM_CODEX_MODEL', 'SKIM_PYTHON', 'SESSIO_HOME', 'GH_CONFIG_DIR', 'SKIM_CODEX_BIN'):
        if os.environ.get(name):
            value = os.environ[name]
            environment[name] = value if name == 'SKIM_CODEX_MODEL' else os.path.abspath(os.path.expanduser(value))
    return normalized_environment(environment)


def trusted(path, directory=False, private=False):
    info = path.lstat()
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    if info.st_uid != os.getuid() or not kind(info.st_mode) or info.st_mode & 0o022 or (private and stat.S_IMODE(info.st_mode) != 0o600):
        raise ValueError(f'소유자·권한·종류가 안전하지 않습니다: {path}')


def prepare_log(data_dir):
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    log = data_dir / 'launchagent.stderr.log'
    if log.exists() or log.is_symlink():
        trusted(log, private=True)
    fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    os.close(fd)
    return log


def validate_existing(app, agent, root, config, check_environment=True):
    expected = {'root': real_path(str(root)), 'data_dir': str(config.data_dir.resolve()),
                'environment': environment_settings()}
    if app.exists() or app.is_symlink():
        trusted(app, directory=True)
        marker = app / 'Contents/Resources/skim-managed.plist'
        if not marker.exists():
            raise ValueError(f'관리 마커 없는 기존 앱은 덮어쓰거나 제거하지 않습니다: {app}')
        for parent in (app / 'Contents', app / 'Contents/Resources'):
            trusted(parent, directory=True)
        trusted(marker)
        with marker.open('rb') as stream:
            saved = plistlib.load(stream)
        if any(real_path(saved.get(k)) != expected[k] for k in ('root', 'data_dir')) or (check_environment and normalized_environment(saved.get('environment')) != expected['environment']):
            raise ValueError('기존 앱의 마커 또는 환경이 현재 설치와 다릅니다.')
    if agent.exists() or agent.is_symlink():
        trusted(agent, private=True)
        with agent.open('rb') as stream:
            saved = plistlib.load(stream)
        arguments = saved.get('ProgramArguments')
        if isinstance(arguments, list) and arguments:
            arguments = [real_path(arguments[0]), *arguments[1:]]
        if saved.get('Label') != 'com.skim.web' or arguments != [real_path(str(root / 'bin/skim')), 'web', '--no-browser']:
            raise ValueError('기존 LaunchAgent는 다른 설치에 속합니다.')
        if check_environment and (real_path(saved.get('StandardOutPath')) != real_path('/dev/null') or real_path(saved.get('StandardErrorPath')) != real_path(str(config.data_dir / 'launchagent.stderr.log'))):
            raise ValueError('기존 LaunchAgent의 로그 설정이 현재 설치와 다릅니다.')
        if check_environment and normalized_environment(saved.get('EnvironmentVariables')) != expected['environment']:
            raise ValueError('기존 LaunchAgent와 현재 환경이 다릅니다. 같은 환경에서 실행하세요.')


def uninstall_app(config):
    if sys.platform != 'darwin':
        print('skim app은 macOS에서 사용할 수 있습니다.', file=sys.stderr)
        return 1
    app = Path.home() / 'Applications' / APP_NAME
    agent = Path.home() / 'Library/LaunchAgents/com.skim.web.plist'
    try:
        validate_existing(app, agent, Path(__file__).resolve().parents[2], config, check_environment=False)
        print('경고: 자동 시작 서버와 진행 중인 작업이 중단됩니다. 데이터는 보존합니다.', file=sys.stderr)
        label = f'gui/{os.getuid()}/com.skim.web'
        result = subprocess.run(['/bin/launchctl', 'bootout', label], capture_output=True)
        if result.returncode and subprocess.run(['/bin/launchctl', 'print', label], capture_output=True).returncode == 0:
            raise ValueError('서버를 중단하지 못했습니다. 앱과 설정은 보존합니다.')
        agent.unlink(missing_ok=True)
        if app.exists():
            shutil.rmtree(app)
        return 0
    except (OSError, ValueError, plistlib.InvalidFileException) as exc:
        print(f'skim app: {exc}', file=sys.stderr)
        return 1


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
    app = output / APP_NAME
    if not binary.is_file() or agent.exists() or agent.is_symlink() or app.exists() or app.is_symlink():
        print('실행 파일을 확인하세요. 기존 준비 파일은 덮어쓰지 않습니다.', file=sys.stderr)
        return 1
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    environment = environment_settings()
    data_dir = str((Path(environment['SKIM_HOME']) / 'data' if 'SKIM_HOME' in environment else Path.home() / '.local/share/skim').resolve())
    plist = {'Label': 'com.skim.web', 'ProgramArguments': [str(binary), 'web', '--no-browser'],
             'EnvironmentVariables': environment, 'RunAtLoad': True,
             'KeepAlive': {'SuccessfulExit': False}, 'ThrottleInterval': 10,
             'StandardOutPath': '/dev/null', 'StandardErrorPath': str(Path(data_dir) / 'launchagent.stderr.log')}
    command = ' '.join(shlex.quote(part) for part in ['/usr/bin/env'] +
                       [name + '=' + value for name, value in environment.items()] + [str(binary), 'open'])
    literal = command.replace('\\', '\\\\').replace('"', '\\"')
    source = ('on run\ntry\ndo shell script "' + literal + '"\n'
              'on error\ndisplay dialog "skim 서버가 실행 중인지 확인해 주세요." buttons {"확인"} default button 1\n'
              'end try\nend run')
    try:
        prepare_log(data_dir)
        subprocess.run(['/usr/bin/osacompile', '-o', str(app), '-e', source], check=True, capture_output=True)
        marker = app / 'Contents/Resources/skim-managed.plist'
        with marker.open('wb') as stream:
            plistlib.dump({'root': str(args.root.resolve()), 'data_dir': data_dir, 'environment': environment}, stream)
        if sys.platform == 'darwin' and shutil.which('/usr/bin/codesign'):
            if subprocess.run(['/usr/bin/codesign', '--verify', str(app)], capture_output=True).returncode:
                subprocess.run(['/usr/bin/codesign', '-f', '-s', '-', str(app)], check=True, capture_output=True)
                subprocess.run(['/usr/bin/codesign', '--verify', str(app)], check=True, capture_output=True)
        fd = os.open(agent, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            plistlib.dump(plist, stream)
    except (OSError, ValueError, subprocess.CalledProcessError):
        print('macOS 실행 파일을 준비하지 못했습니다. 준비 폴더를 확인하세요.', file=sys.stderr)
        return 1
    if not quiet:
        print('준비 완료:', output)
        print('자동 시작은 아직 등록되지 않았습니다.')
    return 0


def install_app(config, *, restart=False):
    """Install or open the user's macOS app without replacing unrelated files."""
    if sys.platform != 'darwin':
        print('skim app은 macOS에서 사용할 수 있습니다.', file=sys.stderr)
        return 1
    root = Path(__file__).resolve().parents[2]
    home = Path.home()
    app = home / 'Applications' / APP_NAME
    agent = home / 'Library/LaunchAgents/com.skim.web.plist'
    label = f'gui/{os.getuid()}/com.skim.web'
    try:
        validate_existing(app, agent, root, config)
        prepare_log(config.data_dir.resolve())
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
                        staged_app = Path(staging) / APP_NAME
                        shutil.copytree(Path(temporary) / APP_NAME, staged_app)
                        os.rename(staged_app, app)
                if not agent.exists():
                    fd = os.open(agent, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(fd, 'wb') as output, (Path(temporary) / agent.name).open('rb') as source:
                        shutil.copyfileobj(source, output)
        if status != 0:
            result = subprocess.run(['/bin/launchctl', 'bootstrap', f'gui/{os.getuid()}', str(agent)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if result.returncode:
                raise ValueError('자동 시작을 등록하지 못했습니다. macOS 로그인 세션과 LaunchAgent 설정을 확인하세요.')
        command = ['/bin/launchctl', 'kickstart']
        if restart and status == 0:
            print('서버를 재시작합니다. 진행 중인 수집·분석 작업이 중단될 수 있습니다.', file=sys.stderr, flush=True)
            command.append('-k')
        started = subprocess.run([*command, '-p', label], capture_output=True, text=True)
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
            print('skim PR.app과 로그인 시 자동 시작이 준비되었습니다. Applications에서 Dock에 추가할 수 있습니다.')
            return open_running(config)
        raise ValueError('서버가 아직 시작되지 않았습니다. 로그인 세션을 확인한 뒤 skim app을 다시 실행하세요.')
    except (OSError, ValueError, plistlib.InvalidFileException) as exc:
        print(f'skim app: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
