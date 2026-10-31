"""사용자 PATH 링크를 설치한다. 자동 시작 등록은 MVP 범위 밖이다."""
import argparse
from pathlib import Path
import sys


def main(argv=None):
    parser = argparse.ArgumentParser(description='skim 설치 (Python 3.11+, 사용자 범위)')
    parser.add_argument('--root', required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        home = Path.home()
        root = args.root.resolve()
        binary = root / 'bin/skim'
        link = home / '.local/bin/skim'
        link.parent.mkdir(parents=True, exist_ok=True)
        if link.is_symlink():
            if link.resolve() != binary:
                raise ValueError('기존 skim 링크가 다른 설치를 가리킵니다.')
        elif link.exists():
            raise ValueError('기존 ~/.local/bin/skim 파일을 덮어쓰지 않습니다.')
        else:
            link.symlink_to(binary)
        print('설치 완료: ~/.local/bin/skim (PATH에 ~/.local/bin 추가)')
        return 0
    except (OSError, ValueError) as exc:
        print(f'설치 실패: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
