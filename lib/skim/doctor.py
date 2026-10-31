"""외부 도구와 로컬 권한을 점검하며 자격 증명 출력은 노출하지 않는다."""
import os
from pathlib import Path
import shutil
import stat
import subprocess

from .analyze.codex import CodexAdapter, IsolationError


def run(config, runner=None, which=None, adapter=None):
    runner = runner or subprocess.run
    which = which or shutil.which
    failed = False

    def report(ok, name, detail):
        nonlocal failed
        failed |= not ok
        print(f'{"정상" if ok else "실패"}: {name} — {detail}')

    for name, args in (('gh', ['gh', 'auth', 'status', '--hostname', 'github.com']),
                       ('codex', ['codex', '--version'])):
        if not which(name):
            report(False, name, 'PATH에서 실행 파일을 찾지 못했습니다.')
            continue
        try:
            result = runner(args, capture_output=True, text=True, timeout=15)
            report(result.returncode == 0, name,
                   ('인증 확인' if name == 'gh' else '설치 확인') if result.returncode == 0
                   else ('gh auth login으로 인증을 확인하세요.' if name == 'gh' else '실행에 실패했습니다.'))
        except (OSError, subprocess.TimeoutExpired):
            report(False, name, '실행 실패 또는 시간 초과')
    try:
        (adapter or CodexAdapter()).verify()
        report(True, '분석 격리', '어댑터의 격리 계약 확인')
    except (IsolationError, OSError, ValueError):
        report(False, '분석 격리', '전역 설정·메모리·플러그인·hooks·MCP 및 도구 금지 계약 미검증. 실제 분석은 차단됩니다.')
    # Config.load 이전의 상태도 보존하도록 CLI는 경로만 전달한다.
    roots = [config.config_dir, config.data_dir]
    if os.environ.get('SKIM_HOME'):
        roots.insert(0, Path(os.environ['SKIM_HOME']).expanduser().resolve())
    for root in dict.fromkeys(roots):
        if not root.exists() and not root.is_symlink():
            report(True, '로컬 권한', f'{root}: 미생성 (최초 실행 시 0700/0600)')
            continue
        paths = [root, *root.rglob('*')] if root.is_dir() and not root.is_symlink() else [root]
        unsafe = False
        for path in paths:
            try:
                info = path.lstat()
            except FileNotFoundError:
                # 실행 중 소켓·WAL 등이 삭제되면 해당 항목만 건너뛴다.
                continue
            expected = 0o700 if stat.S_ISDIR(info.st_mode) else 0o600
            if (stat.S_ISLNK(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != expected):
                unsafe = True
        report(not unsafe, '로컬 권한', f'{root}: ' + ('소유자·심볼릭 링크·0700/0600 권한을 확인하세요.' if unsafe else '사용자 전용'))
    print('안내: 조직 SSO는 gh 자격 증명의 SSO 승인을 확인하세요. 비공개 저장소 읽기 권한과 팀 멤버십·리뷰 요청 접근 권한도 필요합니다.')
    print('안내: gh 인증 성공만으로 모든 조직·팀 접근을 보장하지 않습니다. PATH와 로그인 키체인 접근은 실행 터미널에서 확인하세요.')
    return int(failed)


def paths():
    """권한을 변경하거나 DB를 만들지 않고 기본 경로를 계산한다."""
    from .config import Config
    home = os.environ.get('SKIM_HOME')
    if home:
        root = Path(home).expanduser().resolve()
        return Config(root / 'config', root / 'data')
    return Config(Path.home() / '.config/skim', Path.home() / '.local/share/skim')
