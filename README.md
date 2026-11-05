# Skim

**GitHub PR을 읽기 전에 변경 목적과 먼저 살펴볼 파일을 파악하는 로컬 리뷰 도구**

Skim은 연결한 저장소의 열린 PR과 리뷰 요청을 모으고, 분석을 허용한 PR의 변경 의도와 파일별 중요도·선정 이유를 보여줍니다. 파일별 diff를 읽으면서 같은 PR에 나타난 함수의 정의와 사용 위치 후보도 살펴볼 수 있습니다.

- **여러 저장소를 한곳에서**: 호스트와 소속 팀으로 필터링하고 리뷰 요청을 확인합니다.
- **검토 순서 정하기**: PR의 목적, 중요 파일, 판단 근거와 미분석 범위를 함께 봅니다.
- **로컬에서 사용**: 브라우저와 로컬 서버를 사용하며 GitHub에는 읽기 전용으로 접근합니다.
- **분석은 선택 사항**: 저장소 연결과 코드 수집만으로 모델 분석이 허용되지 않습니다.

분석 결과와 함수 위치는 검토를 돕는 후보입니다. 실제 코드와 변경 맥락을 확인하고 리뷰어가 최종 판단해야 합니다.

![가상 PR 데이터로 본 Skim 대시보드](docs/images/dashboard.png)

![가상 PR 데이터로 본 파일별 변경 화면](docs/images/detail.png)

## 요구 사항

- macOS 또는 Linux
- Python 3.11 이상, GitHub CLI (`gh`) 및 GitHub 저장소 읽기 권한
- PR 분석을 사용할 경우: 로그인된 Codex CLI 안정 버전 0.165.0 이상과 필요한 격리 옵션

추가 Python 패키지는 필요하지 않습니다. 회사 GitHub Enterprise의 PR도 호스트별 `gh` 인증과 접근 권한을 설정해 조회할 수 있습니다. 분석을 켜면 PR 코드와 설명이 Codex를 통해 모델 서비스로 전송될 수 있으므로 조직 정책을 먼저 확인하세요.

## 빠른 시작

### 정식 릴리스 설치

GitHub에 로그인한 뒤 설치 스크립트를 내려받아 실행합니다.

```bash
gh auth login --hostname github.com
gh api -H "Accept: application/vnd.github.raw+json" repos/nowgnas/skim/contents/scripts/install.sh > skim-install.sh
bash skim-install.sh
export PATH="$HOME/.local/bin:$PATH"
skim
```

설치기와 업데이트는 공개 릴리스에도 `gh` 인증이 필요합니다. `PATH`를 유지하려면 사용 중인 셸 설정 파일에 `export`를 추가하세요. 설치 옵션과 운영체제별 안내는 [사용 가이드](docs/user-guide.md#설치와-시작)를 참고하세요.

### 소스에서 실행

최신 `main`을 사용하거나 개발에 참여할 때 사용합니다.

```bash
git clone https://github.com/nowgnas/skim.git
cd skim
./setup.sh
export PATH="$HOME/.local/bin:$PATH"
gh auth login
skim
```

개발 설치의 실행 파일은 checkout을 가리키므로 저장소 디렉터리를 유지해야 합니다. `main`의 기능과 [정식 릴리스](https://github.com/nowgnas/skim/releases)의 기능은 다를 수 있습니다.

## 사용 흐름

1. **저장소 연결**: 웹의 저장소 화면에서 호스트와 저장소를 선택합니다. 팀 필터도 사용할 수 있습니다.
2. **분석 허용**: 필요한 경우 호스트별로 분석 범위를 고릅니다. 기본값은 분석 안 함입니다.
3. **분석 준비**: `codex login` 후 상태 화면에서 분석 준비 초기화와 격리 검증을 실행합니다. 격리 검증은 실제 모델을 호출하므로 비용이 발생할 수 있습니다.
4. **PR 검토**: PR을 수집하고 변경 의도, 먼저 볼 파일, diff와 미분석 범위를 확인합니다.

터미널에서 준비하려면 `skim codex-home init`과 `skim doctor --probe`를 사용할 수 있습니다. 분석에 동의하지 않아도 PR 수집과 저장된 diff 조회는 가능합니다. 화면별 상세 사용법은 [사용 가이드](docs/user-guide.md#웹에서-사용하기)를 참고하세요.

### 데모

별도의 빈 경로에 가상 데이터를 만들면 GitHub·모델 서비스 연결 없이 화면을 볼 수 있습니다. 소스 checkout에서 실행하세요.

```bash
python3 scripts/demo_seed.py --home /tmp/skim-demo
SKIM_HOME=/tmp/skim-demo bin/skim web --offline
```

`/tmp/skim-demo`가 이미 사용 중이면 비어 있는 다른 경로를 지정하세요.

## 주요 명령

| 명령 | 설명 |
| --- | --- |
| `skim` | 로컬 웹 서버와 브라우저 실행 |
| `skim app` | 개발 설치에서 macOS 앱과 로그인 시 자동 시작 설정 |
| `skim app --restart` | 개발 설치의 백그라운드 서버 재시작 |
| `skim update --check` / `skim update` | 정식 설치본의 업데이트 확인 / 적용 |
| `skim collect` / `skim analyze` | PR 수집 / 허용된 PR 수동 분석 |
| `skim doctor` / `skim doctor --probe` | 환경 점검 / 실제 모델을 호출하는 격리 검증 |
| `skim data status` | 로컬 데이터 경로와 보관 정책 확인 (`main` 기준) |
| `skim uninstall` | 프로그램 제거. 사용자 데이터는 기본 보존 |

정식 설치본의 macOS 앱 연결은 아직 지원하지 않습니다. 전체 명령과 제거 방법은 [사용 가이드](docs/user-guide.md#cli-하위-명령)에 있습니다.

## 데이터와 비용

- 설정은 기본 `~/.config/skim/`, DB와 원본 diff는 `~/.local/share/skim/`에 저장합니다. `SKIM_HOME`으로 경로를 바꿀 수 있습니다.
- `main`에서는 종료된 PR 데이터를 기본 7일 보관하고 온라인 서버가 실행되는 동안 12시간마다 정리 여부를 확인합니다. 기존 설정값은 유지하며, 접근이 불가능하거나 종료 시각을 확인하지 못한 PR은 보존합니다.
- DB 백업은 자동 삭제하지 않습니다. 백업과 SQLite의 여유 공간, 운영체제 백업에는 이전 데이터가 남을 수 있습니다.
- 분석은 사용자의 Codex 계정과 요금제로 실행됩니다. 자동 분석의 일일 호출 제한은 금액 한도가 아니며 수동 분석에는 적용되지 않습니다.
- 웹 서버는 `127.0.0.1`에 바인딩합니다. 분석을 허용하지 않은 저장소의 코드는 모델 서비스로 전송하지 않지만, 수집된 원본 diff는 로컬에 저장됩니다.

자세한 보안 경계와 설정은 [사용 가이드](docs/user-guide.md#보안과-데이터)와 [상세 문서](docs/02-details.md)를 확인하세요.

## 개발과 기여

버그나 기능 제안은 GitHub 이슈로 알려 주세요. 변경을 제안할 때는 재현 방법과 기대 동작을 함께 적어 주면 확인하기 쉽습니다.

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q lib scripts
node --check lib/skim/web_static/jobs.js
```

테스트는 가짜 GitHub 응답과 격리된 로컬 데이터를 사용합니다. 실제 모델 평가 및 릴리스 기준은 [출시 체크리스트](docs/release-checklist.md)를 참고하세요.

## 문서

- [사용 가이드](docs/user-guide.md): 설치, 설정, 업데이트, 문제 해결
- [설계와 데이터 흐름](docs/01-design.md)
- [기능·데이터·보안 상세](docs/02-details.md)
- [웹 화면과 인증](docs/04-web.md)
- [변경 이력](CHANGELOG.md)

## 라이선스

[MIT License](LICENSE)
