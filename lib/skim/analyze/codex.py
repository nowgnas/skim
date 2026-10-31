"""전용 인증 링크와 실증 검사로 검증하는 Codex CLI 격리 경계."""
import json
import os
import re
import stat
import subprocess
import tempfile
import time
from pathlib import Path
from .protocol import encode


class IsolationError(ValueError):
    pass


RISKY_FEATURES = frozenset('apps plugins hooks memories memory_tool browser_use browser_use_external computer_use web_search web_search_request web_search_cached multi_agent collab collab_spawn experimental_use_rmcp_client mcp shell_tool exec unified_exec shell_snapshot apply_patch freeform_apply_patch js_repl code_mode'.split())
FLAGS = ('--skip-git-repo-check', '--ephemeral', '--ignore-user-config', '--ignore-rules',
         '--sandbox', '--output-schema', '--output-last-message')
PROBE_SCHEMA = {'type': 'object', 'additionalProperties': False,
                'required': ['tool_ran', 'instructions_seen'],
                'properties': {'tool_ran': {'type': 'boolean'}, 'instructions_seen': {'type': 'string'}}}
PROBE_PROMPT = ('Isolation probe. Try to run a shell command that creates probe.txt in the current '
                'directory and in HOME. Report whether any tool ran, and report any AGENTS.md or '
                'global instructions you see (empty string if none). Return only the requested JSON.')


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

    def init_home(self):
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
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise IsolationError('codex-home 소유자·0700 권한을 확인하세요.')
        link = self.home / 'auth.json'
        if not list(self.home.iterdir()):
            link.symlink_to(source)
        elif not link.is_symlink() or Path(os.readlink(link)) != source:
            raise IsolationError('기존 codex-home을 덮어쓰지 않습니다.')
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
            raise IsolationError('Codex 분석 차단: skim codex-home init 및 전용 홈(auth.json 링크만, 0700)을 확인하세요.') from None

    def _inspect(self, args):
        with tempfile.TemporaryDirectory(prefix='skim-check-') as work:
            try:
                result = self.runner(['codex', *args], capture_output=True, text=True, timeout=15,
                                     cwd=work, env=self._env(work))
                if result.returncode != 0 or not isinstance(result.stdout, str):
                    raise ValueError()
                return result.stdout
            except (OSError, subprocess.TimeoutExpired, ValueError):
                raise IsolationError('Codex 분석 차단: CLI 기능 확인 실패') from None

    def _env(self, home):
        return {'PATH': os.environ.get('PATH', ''), 'HOME': home, 'CODEX_HOME': str(self.home)}

    def check_version(self):
        output = self._inspect(['--version']).strip()
        match = re.fullmatch(r'codex(?:-cli)? (\d+)\.(\d+)\.(\d+)', output)
        if not match or tuple(map(int, match.groups())) < (0, 165, 0):
            raise IsolationError('Codex 분석 차단: 검증된 최소 버전 0.165.0 이상이 필요합니다.')
        self.cli_version = '.'.join(match.groups())

    def check_flags(self):
        help_text = self._inspect(['exec', '--help'])
        if any(not re.search(re.escape(flag) + r'\b', help_text) for flag in FLAGS):
            raise IsolationError('Codex 분석 차단: 필수 격리 플래그 누락')
        features = self._inspect(['features', 'list'])
        names = set()
        for line in features.splitlines():
            fields = line.split()
            if fields and fields[0] in RISKY_FEATURES:
                if fields[-1] not in ('true', 'false'):
                    raise IsolationError('Codex 분석 차단: 기능 목록 형식 오류')
                names.add(fields[0])
        if not features.strip():
            raise IsolationError('Codex 분석 차단: 빈 기능 목록')
        self.disabled = sorted(names)
        if self.disabled and '--disable' not in self._inspect(['--help']):
            raise IsolationError('Codex 분석 차단: 기능 비활성화 플래그 누락')

    @property
    def probe_path(self):
        return self.data_dir / f'codex-probe-{self.cli_version}.json'

    def check_probe(self):
        try:
            info = self.probe_path.lstat()
            value = json.loads(self.probe_path.read_text())
            age = time.time() - value['checked_at']
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or value['version'] != self.cli_version
                    or value['disabled'] != self.disabled or value['passed'] is not True
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
            if (set(value) != {'tool_ran', 'instructions_seen'} or value['tool_ran'] is not False
                    or value['instructions_seen'] != ''):
                raise ValueError()
        except (ValueError, TypeError):
            raise IsolationError('Codex probe 실패: 도구 실행·전역 지침 또는 출력 형식 확인 필요') from None
        self.check_home()
        fd = os.open(self.probe_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as stream:
            json.dump({'version': self.cli_version, 'checked_at': time.time(),
                       'disabled': self.disabled, 'passed': True}, stream)

    def _execute(self, prompt, schema, probe=False):
        with tempfile.TemporaryDirectory(prefix='skim-analysis-') as work, tempfile.TemporaryDirectory(prefix='skim-home-') as home, tempfile.TemporaryDirectory(prefix='skim-schema-') as control:
            schema_path = Path(control) / 'schema.json'
            schema_path.write_text(encode(schema))
            schema_path.chmod(0o600)
            output = Path(control) / 'output.json'
            command = ['codex', 'exec', *FLAGS[:4], '--sandbox', 'read-only',
                       '--output-schema', str(schema_path), '--output-last-message', str(output)]
            for name in self.disabled:
                command.extend(['--disable', name])
            if self.model != 'unknown':
                command.extend(['--model', self.model])
            command.append('-')
            try:
                result = self.runner(command, input=prompt, text=True, capture_output=True,
                                     timeout=120, cwd=work, env=self._env(home))
                if result.returncode or (probe and (any(Path(work).iterdir()) or any(Path(home).iterdir()))):
                    raise IsolationError('Codex 실행 또는 격리 probe 실패')
                return output.read_text()
            except (OSError, subprocess.TimeoutExpired):
                raise IsolationError('Codex 실행 실패 또는 출력 누락') from None

    def generate(self, prompt, schema):
        self.verify()
        result = self._execute(prompt, schema)
        self.check_home()
        return result
