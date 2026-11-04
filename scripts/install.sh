#!/usr/bin/env bash
# curl -fsSL .../scripts/install.sh | bash
# 본문 수신이 완료된 뒤 main을 호출한다.
find_python() {
  local candidate
  if [[ -n "${SKIM_PYTHON:-}" ]]; then
    "$SKIM_PYTHON" -I -c 'import sys; sys.exit(sys.version_info < (3,11))' >/dev/null 2>&1 || return 1
    printf '%s\n' "$SKIM_PYTHON"
    return
  fi
  for candidate in python3 python3.{20..11}; do
    if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -I -c 'import sys; sys.exit(sys.version_info < (3,11))' >/dev/null 2>&1; then
      command -v "$candidate"
      return
    fi
  done
  return 1
}
main() {
set -euo pipefail
local yes=0 arg os py tool answer tag codex_bin
local missing=()
for arg in "$@"; do
  case "$arg" in
    --yes) yes=1 ;;
    *) echo "사용법: bash install.sh [--yes]" >&2; return 2 ;;
  esac
done
os="$(uname -s)"
case "$os" in
  Darwin|Linux) ;;
  *) echo 'macOS와 Linux에서만 설치할 수 있습니다.' >&2; return 1 ;;
esac
py="$(find_python)" || missing+=(python3)
command -v gh >/dev/null || missing+=(gh)
codex_bin="${SKIM_CODEX_BIN:-$HOME/.local/share/skim-tools/node_modules/.bin/codex}"
if ! [[ -x "$codex_bin" ]]; then
  codex_bin="${SKIM_CODEX_BIN:-codex}"
fi
if ((${#missing[@]})); then
  echo "필요한 도구가 없습니다: ${missing[*]}"
  if [[ "$os" == Linux ]]; then
    echo 'Linux 프로그램 설치를 지원합니다. 필요한 도구를 수동 설치한 뒤 다시 실행하세요.'
    echo 'Python 3.11 이상: 배포판 저장소에 python3.11 또는 python3.12 이상 패키지가 있는지 확인하세요.'
    echo 'apt 사용 시 sudo apt-get update 후 apt-cache policy python3.11 python3.12로 확인하세요.'
    echo 'dnf 사용 시 dnf list --available python3.11 python3.12로 확인하세요.'
    echo '패키지가 없다면 https://www.python.org/downloads/ 안내로 설치하고 SKIM_PYTHON을 지정하세요.'
    echo 'gh 최신 버전: https://cli.github.com/ (auth token 지원 필요)'
    echo 'Codex 안정 버전 0.165.0 이상: https://developers.openai.com/codex/cli/'
    echo 'Node.js는 Codex 공식 요구 버전을 사용하세요. 배포판 기본 nodejs를 자동 설치하지 않습니다.'
    return 1
  fi
  if ! command -v brew >/dev/null; then
    echo 'Homebrew를 먼저 설치하세요: https://brew.sh/'
    return 1
  fi
  echo '설치할 명령:'
  for tool in "${missing[@]}"; do
    case "$tool" in
      python3) echo '  brew install python@3.11' ;;
      gh) echo '  brew install gh' ;;
    esac
  done
  if (( ! yes )); then
    answer=''
    if [[ -t 0 ]]; then
      read -r -p '위 도구를 설치할까요? [y/N] ' answer || true
    elif { exec 3<>/dev/tty; } 2>/dev/null; then
      printf '위 도구를 설치할까요? [y/N] ' >&3
      read -r answer <&3 || true
      exec 3>&-
    else
      echo '터미널이 없어 설치하지 않습니다. 수동 설치하거나 --yes를 사용하세요.' >&2
      return 1
    fi
    case "$answer" in
      y|Y) ;;
      *) echo '설치하지 않았습니다. 위 명령을 수동 실행한 뒤 다시 설치하세요.'; return 1 ;;
    esac
  fi
  for tool in "${missing[@]}"; do
    case "$tool" in
      python3) brew install python@3.11 ;;
      gh) brew install gh ;;
    esac
  done
fi
py="$(find_python)" || { echo 'Python 3.11 이상을 설치하고 SKIM_PYTHON을 지정하세요.' >&2; return 1; }
gh help auth | "$py" -I -c 'import re, sys; sys.exit(not re.search(r"(?m)^\s+token:?\s", sys.stdin.read()))' || { echo 'gh를 최신 버전으로 갱신하세요. auth token 지원이 필요합니다.' >&2; return 1; }
gh auth status --hostname github.com >/dev/null 2>&1 || {
  echo '다운로드 전에 gh auth login --hostname github.com을 실행하고 저장소 읽기 권한을 확인하세요.' >&2
  return 1
}
# 토큰 값은 읽거나 출력하지 않는다.
"$py" -I - "$codex_bin" <<'CHECK'
import re
import subprocess
import sys
try:
    version = subprocess.run([sys.argv[1], '--version'], capture_output=True, text=True, timeout=15, check=True).stdout.strip()
    match = re.fullmatch(r'codex(?:-cli)? (\d+)\.(\d+)\.(\d+)', version)
    if not match or tuple(map(int, match.groups())) < (0, 165, 0):
        raise ValueError()
    help_text = subprocess.run([sys.argv[1], 'exec', '--help'], capture_output=True, text=True, timeout=15, check=True).stdout
    flags = ('--skip-git-repo-check', '--ephemeral', '--ignore-user-config', '--ignore-rules',
             '--sandbox', '--output-schema', '--output-last-message', '--disable')
    if any(not re.search(re.escape(flag) + r'(?![\w-])', help_text) for flag in flags):
        raise ValueError()
except (OSError, ValueError, subprocess.SubprocessError):
    print('경고: PR 분석에는 필수 격리 플래그가 있는 Codex 안정 버전 0.165.0 이상이 필요합니다. 설치 또는 갱신하세요: https://developers.openai.com/codex/cli/', file=sys.stderr)
CHECK
# Latest 표시와 무관하게 정식 semver 최대 태그를 선택한다.
temporary="$(mktemp -d)"
trap 'rm -rf -- "$temporary"' EXIT
gh api --paginate 'repos/nowgnas/skim/releases?per_page=100' > "$temporary/releases.json"
tag="$("$py" -I - "$temporary/releases.json" <<'TAG'
import json, re, sys
# gh --paginate 출력의 연속 JSON 배열을 읽는다.
text = open(sys.argv[1]).read()
decoder = json.JSONDecoder()
versions = []
while text.strip():
    rows, end = decoder.raw_decode(text.lstrip())
    text = text.lstrip()[end:]
    for row in rows:
        tag = row.get('tag_name', '')
        match = re.fullmatch(r'v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)', tag)
        if match and not row.get('draft') and not row.get('prerelease'):
            versions.append((tuple(map(int, match.groups())), tag))
if not versions:
    raise SystemExit('정식 릴리스가 없습니다. 릴리스 게시 후 다시 설치하세요.')
print(max(versions)[1])
TAG
)"
gh release download "$tag" --repo nowgnas/skim --pattern install.sh --pattern SHA256SUMS --dir "$temporary" || {
  echo '정식 릴리스 파일을 받지 못했습니다. 로그인, 권한과 네트워크를 확인하세요.' >&2
  return 1
}
"$py" -I - "$temporary" <<'PY'
import hashlib
from pathlib import Path
import re
import sys
root = Path(sys.argv[1])
installer = root / 'install.sh'
if installer.stat().st_size > 1024 * 1024 or (root / 'SHA256SUMS').stat().st_size > 16384:
    raise SystemExit('설치 자산 크기가 너무 큽니다.')
rows = (root / 'SHA256SUMS').read_text().splitlines()
matches = [re.fullmatch(r'([a-f0-9]{64})  install[.]sh', row) for row in rows]
hashes = [match[1] for match in matches if match]
if len(hashes) != 1 or hashlib.sha256(installer.read_bytes()).hexdigest() != hashes[0]:
    raise SystemExit('설치 자산 SHA256SUMS 검증에 실패했습니다.')
PY
"$py" -I "$temporary/install.sh"
}
main "$@"
