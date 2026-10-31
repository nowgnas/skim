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

    @classmethod
    def load(cls):
        home = os.environ.get("SKIM_HOME")
        if home:
            root = Path(home).expanduser().resolve()
            config_dir, data_dir = root / "config", root / "data"
            private_dir(root)
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
        for name in ("cache", "logs"):
            private_dir(data_dir / name)
        return cls(config_dir, data_dir)

    @property
    def db_path(self):
        return self.data_dir / "skim.db"
