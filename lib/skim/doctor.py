"""외부 도구와 로컬 권한을 점검하며 자격 증명 출력은 노출하지 않는다."""
import os
from pathlib import Path
import shutil
import stat
import subprocess

from .analyze.codex import CodexAdapter, IsolationError


def run(config, runner=None, which=None, adapter=None, probe=False):
    runner = runner or subprocess.run
    which = which or shutil.which
    failed = False

    def report(ok, name, detail):
        nonlocal failed
        failed |= not ok
        print(f'{"정상" if ok else "실패"}: {name} — {detail}')

    if not which('gh'):
        report(False, 'gh', 'PATH에서 실행 파일을 찾지 못했습니다.')
    else:
        try:
            result = runner(['gh', 'auth', 'status', '--hostname', 'github.com'],
                            capture_output=True, text=True, timeout=15)
            report(result.returncode == 0, 'gh', '인증 확인' if result.returncode == 0 else 'gh auth login으로 인증을 확인하세요.')
        except (OSError, subprocess.TimeoutExpired):
            report(False, 'gh', '실행 실패 또는 시간 초과')
    adapter = adapter or CodexAdapter(runner=runner, data_dir=config.data_dir)
    ready = True
    for label, method in (('codex-home', adapter.check_home), ('version', adapter.check_version),
                          ('flags', adapter.check_flags),
                          ('probe', adapter.probe if probe else adapter.check_probe)):
        if label != 'codex-home' and (not ready or not which('codex')):
            report(False, label, 'Codex 분석 차단: 선행 검사 실패 또는 CLI 없음')
            ready = False
            continue
        try:
            method()
            report(True, label, '격리 검사 통과')
            if label == 'flags' and getattr(adapter, 'unknown_enabled', []):
                print('경고: 차단 목록 밖의 활성 Codex 기능: ' + ', '.join(adapter.unknown_enabled))
        except (IsolationError, OSError, ValueError, subprocess.TimeoutExpired):
            ready = False
            details = {
                'codex-home': '초기화 때와 같은 CODEX_HOME 환경인지 확인하세요. 본인 소유 디렉터리는 skim codex-home init --reset으로 복구하세요. 링크·일반 파일·타인 소유 경로는 직접 확인해야 합니다.',
                'version': 'Codex 안정 버전 0.165.0 이상과 버전 출력 형식을 확인하세요.',
                'flags': 'exec --help의 필수 격리 플래그와 features list 형식을 확인하세요.',
                'probe': 'skim doctor --probe로 격리를 재검증하세요. 도구 실행·지침·파일 생성 시 차단됩니다.',
            }
            report(False, label, 'Codex 분석 차단: ' + details[label])
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
            if path == config.data_dir / 'codex-home/auth.json':
                try:
                    adapter.check_home()
                    continue
                except (IsolationError, OSError, ValueError):
                    pass
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
