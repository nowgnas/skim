"""사용자 전용 경로와 JSON 설정."""
import json
import os
from dataclasses import dataclass
from pathlib import Path


def private_dir(path):
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return path


def private_file(path):
    # 생성 시점부터 다른 사용자에게 공개하지 않는다.
    fd = os.open(path, os.O_CREAT | os.O_WRONLY, 0o600)
    os.close(fd)
    path.chmod(0o600)


@dataclass(frozen=True)
class Config:
    config_dir: Path
    data_dir: Path
    poll_interval_seconds: int = 300
    retention_days: int = 30
    sessio_index_path: Path | None = None
    sessio_web_url: str = 'http://127.0.0.1:8765/'

    codex_model: str | None = None

    def resolved_codex_model(self):
        # doctor도 파일 권한을 바꾸지 않고 같은 설정과 환경 우선순위를 사용한다.
        path = self.config_dir / 'config.json'
        settings = json.loads(path.read_text()) if path.exists() else {}
        value = os.environ.get('SKIM_CODEX_MODEL', settings.get('codex_model', self.codex_model))
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise ValueError('codex_model 설정은 비어 있지 않은 문자열이어야 합니다')
        return value.strip() if value else None

    @classmethod
    def load(cls):
        home = os.environ.get("SKIM_HOME")
        if home:
            root = Path(home).expanduser().resolve()
            config_dir, data_dir = root / "config", root / "data"
        else:
            config_dir = Path.home() / ".config/skim"
            data_dir = Path.home() / ".local/share/skim"
        private_dir(config_dir)
        private_dir(data_dir)
        path = config_dir / "config.json"
        if not path.exists():
            private_file(path)
            path.write_text(json.dumps({"poll_interval_seconds": 300,
                                        "retention_days": 30}, indent=2) + "\n")
        private_file(path)
        settings = json.loads(path.read_text())
        if not isinstance(settings, dict):
            raise ValueError("설정은 JSON 객체여야 합니다")
        values = {}
        for name, default in (("poll_interval_seconds", 300), ("retention_days", 30)):
            value = settings.get(name, default)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} 설정은 양의 정수여야 합니다")
            values[name] = value
        for name in ("cache", "logs"):
            private_dir(data_dir / name)
        index = settings.get('sessio_index_path')
        if index is not None and (not isinstance(index, str) or not index):
            raise ValueError('sessio_index_path 설정은 비어 있지 않은 경로여야 합니다')
        values['sessio_index_path'] = (Path(index).expanduser() if index else
                                      Path(os.environ.get('SESSIO_HOME', '~/.sessio')).expanduser() / 'index/sessions.jsonl')
        url = settings.get('sessio_web_url', 'http://127.0.0.1:8765/')
        from .sessio import valid_base_url
        if not valid_base_url(url):
            raise ValueError('sessio_web_url 설정은 로컬 Sessio HTTP 주소여야 합니다')
        values['sessio_web_url'] = url
        config = cls(config_dir, data_dir, **values)
        return cls(config_dir, data_dir, **values, codex_model=config.resolved_codex_model())

    @property
    def db_path(self):
        return self.data_dir / "skim.db"
