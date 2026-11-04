"""로컬 PR 검토 도우미."""
import sys

__version__ = "1.0.0"
# lib/python.sh의 SKIM_MIN_PYTHON과 같아야 한다.
MIN_PYTHON = (3, 11)

# 래퍼를 거치지 않는 python -m 실행도 막는다. 하위 모듈보다 먼저 실행되므로 이 파일은 구버전 문법만 쓴다.
if sys.version_info < MIN_PYTHON:
    raise SystemExit('Python %d.%d 이상이 필요합니다. 현재: %s (%s)'
                     % (MIN_PYTHON + (sys.version.split()[0], sys.executable)))
