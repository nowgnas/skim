#!/usr/bin/env bash
# 사용자 PATH 링크를 설치한다.
set -euo pipefail
ROOT="$(cd -P "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$ROOT/lib/python.sh"
PY="$(skim_find_python)" || exit 2
export PYTHONPATH="$ROOT/lib${PYTHONPATH:+:$PYTHONPATH}"
exec "$PY" -m skim.setup --root "$ROOT" "$@"
