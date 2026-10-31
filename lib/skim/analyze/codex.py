"""Codex CLI 경계. 검증하지 않은 격리 계약은 실행하지 않는다."""
import os
import subprocess
import tempfile
from pathlib import Path
from .protocol import encode


class IsolationError(ValueError):
    pass


class CodexAdapter:
    provider = 'codex'
    model = 'unknown'
    cli_version = 'unknown'

    def __init__(self, runner=subprocess.run):
        self.runner = runner

    def verify(self):
        # read-only는 홈 읽기·도구 실행 금지가 아니다. 도움말 플래그 존재만으로
        # hooks/MCP/메모리/전역 설정 격리를 검증했다고 간주하지 않는다.
        # 검증된 버전별 계약이 추가되기 전에는 모델 호출을 무조건 차단한다.
        raise IsolationError('Codex 분석 차단: 설치 버전의 전역 설정·메모리·플러그인·hooks·MCP 로딩 및 모든 도구 실행 금지 계약을 검증하지 못했습니다 (skim doctor).')

    def generate(self, prompt, schema):
        self.verify()
        # verify가 향후 검증된 계약을 지원해도 입력은 빈 작업 디렉터리에서만 전달한다.
        with tempfile.TemporaryDirectory(prefix='skim-analysis-') as work, tempfile.TemporaryDirectory(prefix='skim-schema-') as control:
            schema_path = Path(control) / 'schema.json'
            schema_path.write_text(encode(schema))
            schema_path.chmod(0o600)
            output = Path(control) / 'output.json'
            command = ['codex', 'exec', '--skip-git-repo-check', '--ephemeral',
                       '--ignore-user-config', '--ignore-rules', '--sandbox', 'read-only',
                       '--output-schema', str(schema_path), '--output-last-message', str(output), '-']
            result = self.runner(command, input=prompt, text=True, capture_output=True,
                                 timeout=120, cwd=work, env={'PATH': os.environ.get('PATH', '')})
            if result.returncode:
                raise ValueError('Codex CLI 분석 실패')
            return output.read_text()
