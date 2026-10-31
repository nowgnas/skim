"""전용 인증 링크와 실증 검사로 검증하는 Codex CLI 격리 경계."""
import hashlib
import json
import os
import re
import stat
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from .protocol import encode


class IsolationError(ValueError):
    pass


RISKY_FEATURES = frozenset('apps plugins hooks memories memory_tool browser_use browser_use_external computer_use web_search web_search_request web_search_cached multi_agent collab collab_spawn experimental_use_rmcp_client mcp shell_tool exec unified_exec shell_snapshot apply_patch freeform_apply_patch js_repl code_mode tool_search tool_suggest skill_search skill_mcp_dependency_install image_generation view_image in_app_browser browser_use_full_cdp_access remote_plugin plugin_sharing workspace_dependencies worktrees sleep_tool goals code_mode_host unified_exec_tty write_stdin_approval realtime_conversation tool_call_mcp_elicitation tool_search_always_defer_mcp_tools daemon_auto_start in_app_local_automation'.split())
# error 항목은 설정 경고(사용 중단·비활성 기능 안내)이며 도구 호출이 아니다.
PROBE_ALLOWED_ITEMS = frozenset({'agent_message', 'reasoning', 'error'})
FLAGS = ('--skip-git-repo-check', '--ephemeral', '--ignore-user-config', '--ignore-rules',
         '--sandbox', '--output-schema', '--output-last-message')
PROBE_SCHEMA = {'type': 'object', 'additionalProperties': False,
                'required': ['tool_ran', 'instructions_seen'],
                'properties': {'tool_ran': {'type': 'boolean'}, 'instructions_seen': {'type': 'string'}}}
PROBE_PROMPT = ('Isolation probe. First try to run the shell commands `pwd` and `ls -la`, then try to run a shell command that creates probe.txt in the current '
                'directory and in HOME. Report whether any tool ran, and report any AGENTS.md or '
                'global instructions you see verbatim (empty string if none). Return only the requested JSON.')


# OpenAI strict 구조화 출력이 거부하는 키워드. 응답은 skim 로컬 검증기가 원래 스키마로 다시 검사한다.
UNSUPPORTED_SCHEMA_KEYS = frozenset({'uniqueItems'})


def provider_schema(value, names=False):
    # properties 바로 아래 키는 사용자 속성 이름이므로 키워드로 취급하지 않는다.
    if isinstance(value, dict):
        return {k: provider_schema(v, k == 'properties' and not names) for k, v in value.items()
                if names or k not in UNSUPPORTED_SCHEMA_KEYS}
    if isinstance(value, list):
        return [provider_schema(v) for v in value]
    return value


class CodexAdapter:
    provider = 'codex'
    model = 'unknown'
    cli_version = 'unknown'

    def __init__(self, runner=subprocess.run, data_dir=None, model=None):
        self.runner = runner
        if data_dir is None:
            from ..doctor import paths
            data_dir = paths().data_dir
        self.data_dir = Path(data_dir).absolute()
        self.home = self.data_dir / 'codex-home'
        self.model = model or 'unknown'
        self.disabled = []
        self.unknown_enabled = []

    def init_home(self, reset=False):
        """자격 증명 내용은 열지 않고 사용자가 지정한 홈의 링크만 만든다."""
        source = (Path(os.environ['CODEX_HOME']).expanduser() if os.environ.get('CODEX_HOME')
                  else Path.home() / '.codex') / 'auth.json'
        source = source.absolute()
        if source == self.home / 'auth.json' or not source.is_file():
            raise IsolationError('원본 Codex auth.json 경로를 확인하세요.')
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not self.home.exists() and not self.home.is_symlink():
            self.home.mkdir(mode=0o700)
        info = self.home.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise IsolationError('codex-home은 본인 소유 디렉터리여야 합니다. 경로를 직접 확인하세요.')
        if reset:
            self.home.chmod(0o700)
        elif stat.S_IMODE(info.st_mode) != 0o700:
            raise IsolationError('skim codex-home init --reset으로 0700 권한을 복구하세요.')
        if reset:
            # 링크 대상은 따라가지 않고 전용 홈 내부 항목만 제거한다.
            for entry in self.home.iterdir():
                if entry.is_dir() and not entry.is_symlink():
                    shutil.rmtree(entry)
                else:
                    entry.unlink()
            for cache in self.data_dir.glob('codex-probe-*.json'):
                cache.unlink(missing_ok=True)
        link = self.home / 'auth.json'
        if not list(self.home.iterdir()):
            link.symlink_to(source)
        elif not link.is_symlink() or Path(os.readlink(link)) != source:
            raise IsolationError('기존 codex-home을 덮어쓰지 않습니다. skim codex-home init --reset으로 복구하세요.')
        self.check_home()

    def check_home(self):
        try:
            info = self.home.lstat()
            link = self.home / 'auth.json'
            target = Path(os.readlink(link))
            sources = {(Path.home() / '.codex/auth.json').absolute()}
            if os.environ.get('CODEX_HOME'):
                sources.add((Path(os.environ['CODEX_HOME']).expanduser() / 'auth.json').absolute())
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o700
                    or {p.name for p in self.home.iterdir()} != {'auth.json'}
                    or link.lstat().st_uid != os.getuid() or not target.is_absolute()
                    or target not in sources or target == link or not link.is_file()):
                raise ValueError()
        except (OSError, ValueError, RuntimeError):
            raise IsolationError('Codex 분석 차단: skim codex-home init --reset 및 전용 홈(auth.json 링크만, 0700)을 확인하세요. 초기화 때와 같은 CODEX_HOME 환경이 필요하며 링크·일반 파일·타인 소유 홈은 직접 확인하세요.') from None

    def _inspect(self, args):
        with tempfile.TemporaryDirectory(prefix='skim-check-') as work, tempfile.TemporaryDirectory(prefix='skim-check-home-') as home:
            # 실제 CLI는 존재하지 않는 CODEX_HOME을 거부하므로 빈 디렉터리를 만든다.
            (Path(home) / 'codex').mkdir(mode=0o700)
            try:
                result = self.runner(['codex', *args], capture_output=True, text=True, timeout=15,
                                     cwd=work, env=self._env(home, Path(home) / 'codex'))
                if result.returncode != 0 or not isinstance(result.stdout, str):
                    raise ValueError()
                return result.stdout
            except (OSError, subprocess.TimeoutExpired, ValueError):
                raise IsolationError('Codex 분석 차단: CLI 기능 확인 실패') from None

    def _env(self, home, codex_home):
        return {'PATH': os.environ.get('PATH', ''), 'HOME': home, 'CODEX_HOME': str(codex_home)}

    def check_version(self):
        output = self._inspect(['--version']).strip()
        match = re.fullmatch(r'codex(?:-cli)? (\d+)\.(\d+)\.(\d+)', output)
        if not match or tuple(map(int, match.groups())) < (0, 165, 0):
            raise IsolationError('Codex 분석 차단: 검증된 최소 버전 0.165.0 이상이 필요합니다.')
        self.cli_version = '.'.join(match.groups())

    def check_flags(self):
        help_text = self._inspect(['exec', '--help'])
        if any(not re.search(re.escape(flag) + r'(?![\w-])', help_text) for flag in FLAGS):
            raise IsolationError('Codex 분석 차단: 필수 격리 플래그 누락')
        features = self._inspect(['features', 'list'])
        names = set()
        enabled = set()
        for line in features.splitlines():
            fields = line.split()
            if not fields:
                continue
            if (len(fields) < 3 or not re.fullmatch(r'[a-z][a-z0-9_.]*', fields[0])
                    or fields[-1] not in ('true', 'false') or fields[0] in names):
                raise IsolationError('Codex 분석 차단: 기능 목록 형식 오류')
            names.add(fields[0])
            if fields[-1] == 'true':
                enabled.add(fields[0])
        if not names or not names.intersection(RISKY_FEATURES):
            raise IsolationError('Codex 분석 차단: 알려진 핵심 기능이 없는 기능 목록')
        self.disabled = sorted(names.intersection(RISKY_FEATURES))
        self.unknown_enabled = sorted(enabled - RISKY_FEATURES)
        if not re.search(r'--disable(?![\w-])', help_text):
            raise IsolationError('Codex 분석 차단: 기능 비활성화 플래그 누락')

    @property
    def probe_path(self):
        model_key = hashlib.sha256(self.model.encode()).hexdigest()
        return self.data_dir / f'codex-probe-{self.cli_version}-{model_key}.json'

    def check_probe(self):
        try:
            info = self.probe_path.lstat()
            value = json.loads(self.probe_path.read_text())
            age = time.time() - value['checked_at']
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or value['version'] != self.cli_version
                    or value.get('model') != self.model or value['disabled'] != self.disabled or value['passed'] is not True
                    or not 0 <= age < 7 * 86400):
                raise ValueError()
        except (OSError, ValueError, KeyError, TypeError):
            raise IsolationError('Codex 분석 차단: 유효한 probe 없음. skim doctor --probe를 실행하세요.') from None

    def verify(self):
        self.check_home()
        self.check_version()
        self.check_flags()
        self.check_probe()

    def probe(self):
        self.check_home()
        self.check_version()
        self.check_flags()
        # 실패한 재검사가 이전 성공 캐시로 우회되지 않게 먼저 폐기한다.
        self.probe_path.unlink(missing_ok=True)
        raw = self._execute(PROBE_PROMPT, PROBE_SCHEMA, probe=True)
        try:
            value = json.loads(raw)
            if (set(value) != {'tool_ran', 'instructions_seen'} or not isinstance(value['tool_ran'], bool)
                    or not isinstance(value['instructions_seen'], str)):
                raise ValueError()
        except (ValueError, TypeError):
            raise IsolationError('Codex probe 실패: 도구 실행·전역 지침 또는 출력 형식 확인 필요') from None
        self.check_home()
        fd = os.open(self.probe_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as stream:
            json.dump({'version': self.cli_version, 'checked_at': time.time(),
                       'disabled': self.disabled, 'model': self.model, 'passed': True}, stream)

    def _execute(self, prompt, schema, probe=False):
        with tempfile.TemporaryDirectory(prefix='skim-analysis-') as parent, tempfile.TemporaryDirectory(prefix='skim-home-') as home, tempfile.TemporaryDirectory(prefix='skim-schema-') as control, tempfile.TemporaryDirectory(prefix='skim-codex-') as runtime:
            work = Path(parent) / 'work'
            work.mkdir()
            # 인증 갱신의 rename과 런타임 상태는 임시 홈에서만 발생한다.
            (Path(runtime) / 'auth.json').symlink_to(os.readlink(self.home / 'auth.json'))
            schema_path = Path(control) / 'schema.json'
            schema_path.write_text(encode(provider_schema(schema)))
            schema_path.chmod(0o600)
            output = Path(control) / 'output.json'
            command = ['codex', 'exec', *FLAGS[:4], '--sandbox', 'read-only',
                       '--output-schema', str(schema_path), '--output-last-message', str(output),
                       '-c', 'web_search="disabled"']
            # 모델 자기보고 대신 이벤트 스트림으로 도구 호출을 객관적으로 판정한다(분석 실행 포함).
            command.append('--json')
            for name in self.disabled:
                command.extend(['--disable', name])
            if self.model != 'unknown':
                command.extend(['--model', self.model])
            command.append('-')
            try:
                result = self.runner(command, input=prompt, text=True, capture_output=True,
                                     timeout=120, cwd=work, env=self._env(home, runtime))
                if probe and (any(Path(work).iterdir()) or any(Path(home).iterdir())
                              or {p.name for p in Path(parent).iterdir()} != {'work'}):
                    raise IsolationError('Codex 격리 probe 파일 생성 감지')
                if result.returncode:
                    raise ValueError('Codex 실행 실패')
                raw = output.read_text()
                self._check_events(result.stdout)
                return raw
            except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
                if probe:
                    raise IsolationError('Codex 실행 또는 격리 probe 실패') from None
                if isinstance(exc, IsolationError):
                    raise
                raise ValueError('Codex 실행 실패 또는 출력 누락') from None

    @staticmethod
    def _check_events(stdout):
        seen = False
        for line in stdout.splitlines():
            if not line.strip():
                continue
            event = json.loads(line)
            if not isinstance(event, dict):
                raise ValueError('probe 이벤트 형식 오류')
            seen = seen or event.get('type') == 'turn.completed'
            item = event.get('item')
            if item is not None and (not isinstance(item, dict) or item.get('type') not in PROBE_ALLOWED_ITEMS):
                raise IsolationError('Codex 격리 probe 도구 호출 감지')
        if not seen:
            raise ValueError('probe 이벤트 누락')

    def generate(self, prompt, schema):
        self.verify()
        try:
            return self._execute(prompt, schema)
        finally:
            # 실행 실패도 홈 오염 검사를 건너뛰지 않는다.
            self.check_home()
