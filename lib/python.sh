# setup.sh와 bin/skim이 source한다. 최소 버전은 lib/skim/__init__.py의 MIN_PYTHON과 같아야 한다.
SKIM_MIN_PYTHON=3.11
# 버전을 붙인 실행 파일을 이 minor부터 내려가며 찾는다.
SKIM_MAX_PYTHON_MINOR=20

skim_python_ok() {
  "$1" -c "import sys; sys.exit(sys.version_info < (${SKIM_MIN_PYTHON/./,}))" >/dev/null 2>&1
}

# 조건을 만족하는 첫 인터프리터 경로를 출력한다. SKIM_PYTHON을 지정하면 그 값만 검사한다.
skim_find_python() {
  local name path minor
  if [[ -n "${SKIM_PYTHON:-}" ]]; then
    if skim_python_ok "$SKIM_PYTHON"; then
      printf '%s\n' "$SKIM_PYTHON"
      return 0
    fi
    echo "SKIM_PYTHON($SKIM_PYTHON)이 Python $SKIM_MIN_PYTHON 이상이 아닙니다." >&2
    return 1
  fi
  minor=$SKIM_MAX_PYTHON_MINOR
  local candidates=(python3)
  while (( minor >= ${SKIM_MIN_PYTHON#*.} )); do
    candidates+=("python3.$minor")
    minor=$((minor - 1))
  done
  for name in "${candidates[@]}"; do
    path="$(command -v "$name" 2>/dev/null)" || continue
    if skim_python_ok "$path"; then
      printf '%s\n' "$path"
      return 0
    fi
  done
  echo "Python $SKIM_MIN_PYTHON 이상을 찾지 못했습니다. 설치하거나 SKIM_PYTHON으로 실행 파일을 지정하세요." >&2
  return 1
}
