#!/usr/bin/env python3
"""실제 모델 회귀 평가가 끝나지 않은 버전의 출시를 차단한다."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
from skim.analyze.protocol import PROMPT_VERSION


def main():
    # 실제 평가 자료를 검토한 뒤에만 통과 경로를 구현한다. 버전 변경도 자동 승인하지 않는다.
    print(f'출시 차단: {PROMPT_VERSION} 실제 모델 회귀 평가 미검증. '
          'docs/release-checklist.md의 평가 근거를 준비해야 합니다.', file=sys.stderr)
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
