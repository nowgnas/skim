# skim

GitHub PR을 모아 변경 목적과 중요도를 보여주는 로컬 검토 도우미입니다.

![데모 데이터로 본 skim 대시보드](docs/images/dashboard.png)

## 주요 기능

- 등록 저장소의 열린 PR과 개인·팀 리뷰 요청을 한곳에서 확인합니다.
- 허용한 저장소만 Codex로 분석합니다. 변경 목적과 파일·변경 단위별 중요도를 보여줍니다.
- 규칙 후보와 미분석 부분을 먼저 보여줍니다. 목록 필터와 diff 상세 보기를 제공합니다.
- 로컬 웹에서 5분 주기 수집과 수동 갱신을 지원합니다.
- 본인이 작성한 PR은 조건이 맞으면 로컬 세션 조회 앱인 Sessio의 세션으로 연결합니다.

## 요구 사항

- Python 3.11 이상. 별도 Python 패키지는 필요하지 않습니다.
- Git과 GitHub CLI(`gh`). GitHub.com 로그인과 대상 저장소 읽기 권한이 필요합니다.
- 분석 시 Codex CLI 안정 버전 0.165.0 이상과 로그인이 필요합니다. 필수 격리 플래그 검사도 통과해야 합니다.
- 분석 전 `skim codex-home init`과 `skim doctor --probe`를 실행해야 합니다.

설치 후 사용할 터미널에서 로그인하세요.

```bash
gh auth login
codex login
```

각 CLI의 로그인 절차가 완료되어야 합니다. 조직 저장소는 SSO 승인과 팀 접근 권한도 확인하세요.
데모는 GitHub·Codex 로그인 없이 볼 수 있습니다.

## 설치

Mac 터미널에서 실행합니다.

```bash
git clone https://github.com/nowgnas/skim.git
cd skim
./setup.sh
export PATH="$HOME/.local/bin:$PATH"
```

`설치 완료: ~/.local/bin/skim (PATH에 ~/.local/bin 추가)`가 출력됩니다.
설치 스크립트는 이 저장소의 `bin/skim`을 가리키는 심볼릭 링크를 만듭니다.
다운로드나 관리자 권한은 필요하지 않습니다. 저장소 폴더를 유지하세요.
PATH 설정은 현재 터미널에 적용됩니다. 계속 사용하려면 같은 `export` 줄을 `~/.zshrc`에 추가하세요.

```bash
skim --help
```

`repo`, `analysis`, `collect`, `analyze`, `web`, `doctor`, `codex-home` 명령 목록이 표시됩니다.

## 사용법

아래 `owner/repo`는 실제 GitHub 저장소 이름으로 바꾸세요.

### 1. 저장소 등록

```bash
skim repo add owner/repo
skim repo list
```

등록 완료 메시지와 추적 목록이 표시됩니다. 등록만으로 분석 전송을 허용하지 않습니다.

### 2. 분석 전송 허용

코드를 모델 서비스로 보내도 되는 저장소만 명시적으로 허용하세요.

```bash
skim analysis allow owner/repo
```

코드 전송 안내와 `저장소 분석 정책을 변경했습니다.`가 출력됩니다.
허용을 취소하려면 다음을 실행합니다.

```bash
skim analysis deny owner/repo
```

정책 변경 메시지가 표시되고 이후 해당 저장소의 분석 전송이 차단됩니다.

### 3. PR 수집

```bash
skim collect
```

`PR N개를 수집했습니다.`가 출력됩니다. 열린 PR과 리뷰 요청의 스냅샷을 로컬에 저장합니다.

### 4. 모델 지정과 분석 준비

`~/.config/skim/config.json`의 기존 JSON 객체에 `"codex_model"` 항목을 추가하는 것을 권장합니다.
`SKIM_HOME`을 쓰면 `$SKIM_HOME/config/config.json`을 편집합니다.
다음 모델명은 예시입니다. 계정에서 사용할 수 있는 모델을 지정하세요.

```json
{
  "codex_model": "gpt-5.4"
}
```

설정 후 분석 준비를 실행합니다.

```bash
skim codex-home init
skim doctor --probe
```

인증 링크 생성 메시지와 항목별 검사 결과가 표시됩니다. 모두 통과하면 분석할 수 있습니다.
`--probe`는 저장소 코드 없이 실제 모델을 호출합니다. 성공 기록은 7일간 유효합니다.
모델을 바꾸면 다시 검사하세요.

현재 터미널에서만 모델을 바꾸려면 `export SKIM_CODEX_MODEL=gpt-5.4`를 사용할 수 있습니다.
환경 변수는 파일 설정보다 우선합니다. **이 override를 쓰면 `analyze`나 `web`을 실행하는 각 터미널에 같은 값을 설정해야 합니다.**
새 터미널에서 변수가 빠지면 파일 설정 또는 미지정 모델로 돌아가 probe 기록이 일치하지 않아 분석이 `blocked`될 수 있습니다.
설정한 모델은 doctor 출력에서 확인하고, 모델을 바꾼 터미널에서 `skim doctor --probe`를 다시 실행하세요.

### 5. 분석 실행

```bash
skim analyze
```

허용된 저장소의 최신 열린 PR 스냅샷을 분석합니다. 기본적으로 draft는 건너뜁니다.
`스냅샷 N: completed` 또는 일부 제외·누락이 있는 `partial` 등의 상태가 표시됩니다.
`failed`, `blocked`, `deferred`가 있으면 종료 코드 1을 반환합니다. `blocked`이면 `skim doctor --probe`로 점검하세요.
대상이 없으면 `분석 가능한 허용 스냅샷이 없습니다.`가 출력됩니다.

### 6. 웹 대시보드 열기

```bash
skim web
```

로컬 접속 URL이 출력되고 브라우저가 열립니다. PR 목록·필터·상세와 분석 결과를 확인하세요.
실행 중에는 주기적으로 수집·분석합니다. 터미널에서 Ctrl-C로 종료합니다.

### 7. 상태 점검

```bash
skim doctor
```

GitHub 인증, Codex 홈·버전·격리 플래그·probe 기록, 모델과 로컬 권한을 표시합니다.
실패 항목이 있으면 종료 코드 1을 반환합니다. 일반 점검은 모델을 호출하지 않습니다.

## 데모로 보기

저장소 폴더에서 실행합니다. 설치를 마치고 PATH를 적용한 상태여야 합니다.

```bash
"${SKIM_PYTHON:-python3}" scripts/demo_seed.py --home /tmp/skim-demo
SKIM_HOME=/tmp/skim-demo skim web --offline
```

가짜 PR 5개가 생성되고 브라우저에서 대시보드가 열립니다.
GitHub·모델 서비스에 접속하지 않습니다. 위 스크린샷도 데모 데이터 화면입니다.
이미 데모를 생성한 경로라면 첫 줄을 생략하고 웹만 실행하세요.

## 보안과 데이터

- GitHub API는 읽기 전용입니다. 리뷰 작성·승인·머지를 하지 않습니다.
- 조직 저장소를 포함해 분석을 허용하지 않은 저장소의 코드는 모델 서비스로 보내지 않습니다. 저장소·조직 정책으로 허용할 수 있으며 저장소 정책이 우선합니다. 조직 허용은 리뷰 요청으로 발견된 해당 조직 저장소에도 적용됩니다.
- 수집한 원문 diff는 로컬에 저장합니다. 허용된 분석 입력은 Codex의 모델 서비스로 전송됩니다. 민감 정보 가림은 완전하지 않습니다.
- 설정은 `~/.config/skim/config.json`, DB는 `~/.local/share/skim/skim.db`, diff 캐시는 같은 데이터 디렉터리의 `cache/`에 저장합니다.
- `SKIM_HOME`을 지정하면 설정은 `$SKIM_HOME/config/`, 데이터는 `$SKIM_HOME/data/`에 저장합니다. 전용 디렉터리는 0700, 설정·DB 파일은 0600입니다.
- 웹은 `127.0.0.1`에서만 열립니다. 출력된 접속 URL에는 일회용 토큰이 있으므로 공유하지 마세요.
- 중요도는 검토 순서의 참고 자료이며 보안 감사나 정확성 보증이 아닙니다. 현재 실제 모델 회귀 평가가 없어 출시가 차단된 상태입니다. [출시 체크리스트](docs/release-checklist.md)를 확인하세요.

## 문제 해결

| 증상 | 조치 |
| --- | --- |
| `skim: command not found` | 설치 절의 PATH 설정을 적용하세요. 저장소에서는 `bin/skim --help`로 확인할 수 있습니다. |
| Python 버전 오류 | Python 3.11 이상을 설치하세요. `SKIM_PYTHON` 환경 변수로 실행 파일 경로를 지정할 수 있습니다. |
| GitHub 인증·수집 실패 | `gh auth login` 후 `skim doctor`로 확인하세요. 조직 SSO 승인·비공개 저장소 권한·팀 멤버십도 필요합니다. |
| 분석 대상이 없음 | `skim repo list`에서 허용 상태를 확인하고 `skim collect`를 실행하세요. draft는 기본 제외됩니다. |
| Codex 홈 검사 실패 | `codex login`을 확인하세요. 초기화 때와 같은 `CODEX_HOME`을 사용해야 합니다. |
| Codex 사용 후 원본 CLI 로그인 실패 | 인증 refresh token이 교체되면 원본 Codex 로그인도 무효화될 수 있습니다. 원본 CLI에서 `codex login`을 다시 실행하세요. |
| 전용 Codex 홈에 다른 파일이 있음 | `skim codex-home init --reset`으로 전용 홈 내부를 초기화하세요. 원본 인증 링크 대상은 삭제하지 않습니다. 이후 probe를 다시 실행하세요. |
| probe 실패·만료 또는 모델 변경 | `skim doctor --probe`를 실행하세요. 실패하면 실제 분석이 차단됩니다. 수집과 웹은 사용할 수 있습니다. |
| 브라우저가 안 열리거나 “진입 토큰 만료 또는 사용됨” | 진입 토큰은 60초 동안 한 번만 사용할 수 있습니다. `skim web`을 다시 실행해 새 URL을 받으세요. 서버가 실행 중이면 기존 서버에서 새 URL을 발급합니다. |
| 데모 생성 시 비어 있는 홈을 요구함 | 이미 만든 데모는 웹만 실행하세요. 새로 만들려면 다른 `--home` 경로와 같은 `SKIM_HOME`을 사용하세요. |

정책 상속, 분석 예산·캐시, Codex 격리, Sessio 연결과 보안 한계는 [상세 문서](docs/02-details.md)에 정리했습니다.
웹 인증과 화면 동작은 [웹 문서](docs/04-web.md)를 참고하세요.
설계는 [설계 문서](docs/01-design.md)를 참고하세요.
