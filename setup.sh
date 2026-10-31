#!/usr/bin/env bash
# 사용자 PATH 링크를 설치한다.
set -euo pipefail
ROOT="$(cd -P "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="${SKIM_PYTHON:-$(command -v python3 || true)}"
if [[ -z "$PY" ]] || ! "$PY" -c 'import sys; sys.exit(sys.version_info < (3,11))'; then
  echo 'Python 3.11 이상이 필요합니다.' >&2
  exit 2
fi
export PYTHONPATH="$ROOT/lib${PYTHONPATH:+:$PYTHONPATH}"
exec "$PY" -m skim.setup --root "$ROOT" "$@"
