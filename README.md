# skim

담당 레포에 올라온 PR에서 중요한 부분만 골라 보여주는 로컬 도구입니다.

설계: docs/01-design.md (작성 중)

## 실행

Python 3.11 이상과 표준 라이브러리만 사용합니다.

```bash
bin/skim repo add owner/repo --source responsible
bin/skim repo list
bin/skim collect
bin/skim analyze
bin/skim analyze --snapshot 1 --include-drafts
bin/skim repo list --candidates
bin/skim analysis allow --org owner
bin/skim analysis deny owner/repo
bin/skim analysis reset owner/repo
bin/skim repo remove owner/repo
python3 -m unittest discover -s tests
```

`--source`는 `owned`, `managed`, `responsible` 중 하나이며 기본값은
`responsible`입니다. 등록만으로 분석을 허용하지 않습니다. 저장소의 명시적
allow/deny가 조직 정책보다 우선하며, `reset`은 조직 정책을 상속합니다.
조직 정책도 `analysis reset --org owner`로 지울 수 있습니다.
저장소 삭제는 추적을 해제하고 저장소 유입 경로만 제거합니다. 리뷰 요청이 남은
PR·스냅샷·분석은 보존하며, 출처가 없어진 PR만 삭제합니다. 저장소 레코드와
분석 정책은 유지합니다. 리뷰 요청만으로 발견한 저장소는 추적 목록에 나타나지 않습니다.

설정 파일은 `~/.config/skim/config.json`, DB는
`~/.local/share/skim/skim.db`입니다. `SKIM_HOME`을 지정하면 각각
`$SKIM_HOME/config/config.json`, `$SKIM_HOME/data/skim.db`를 사용합니다.
설정 기본값은 수집 간격 300초, 보존 기간 30일입니다. 보존 기간에 따른 정리는 후속 구현입니다. 전용 config/data 디렉터리는 0700이며 SKIM_HOME 자체의 기존 권한은 바꾸지 않습니다. 설정·DB·백업 파일은 0600입니다.
DB 버전은 `PRAGMA user_version`으로 관리하고 기존 DB 변경 전에 백업합니다.
`SKIM_PYTHON`으로 래퍼가 사용할 Python 실행 파일을 지정할 수 있습니다.

`collect`는 기존 `gh` 로그인으로 GitHub.com REST API의 GET만 호출합니다.
등록 저장소의 열린 PR과 개인·팀 리뷰 요청을 중복 없이 수집합니다. 소유·관리
권한 저장소는 `repo list --candidates`로 확인한 뒤 `repo add`로 선택합니다.
후보와 리뷰 요청 저장소는 자동으로 전체 PR 추적이나 분석 전송을 허용하지 않습니다.
SHA를 수집 전후 확인하고 설명·파일 목록·diff 해시를 스냅샷에 저장합니다.
닫힘·머지 PR은 DB 상태를 갱신하고 활성 출처에서 제외하며, 해제된 리뷰 요청은
다른 출처가 없으면 삭제합니다. 고아 diff 캐시도 정리합니다.
요청 제한 시 재개 시각을 표시하고 기존 결과를 보존합니다.

`CollectionScheduler`는 웹 프로세스의 루프에서 `tick(collector)`을 호출하는
내장 도우미입니다. 주기 갱신과 `request()` 수동 갱신은 SQLite 작업 큐를 공유하고,
실패 시 지수 백오프와 Retry-After/요청 제한 재개 시각을 적용합니다.
`interval=config.poll_interval_seconds`를 전달하며 자체 데몬이나 스레드를 만들지 않습니다.
`analyze`는 분석을 허용한 저장소의 열린 최신 SHA 스냅샷만 처리합니다.
기본적으로 draft를 건너뛰며 수동 `--include-drafts`도 허용 정책과 예산을 따릅니다.
`--snapshot`은 로컬 스냅샷 ID입니다. 제외·누락·실패는 low로 취급하지 않습니다.
분석 결과와 단위별 제외 사유·규칙 후보·coverage는 SQLite에 저장합니다.
`logic_without_tests`는 “제공된 diff에서 관련 테스트 변경을 확인하지 못함”이라는
규칙 후보이며 기존 테스트 커버리지를 검사했다는 뜻이 아닙니다.
민감 정보 탐지는 완전하지 않습니다.

실제 Codex 분석은 다음 격리 검사를 모두 통과한 뒤 실행합니다.

```bash
skim codex-home init
skim doctor --probe
skim analysis allow owner/repo
skim analyze
```

`codex-home init`은 `<data_dir>/codex-home`(0700)에 기존
`$CODEX_HOME/auth.json` 또는 `~/.codex/auth.json`의 심볼릭 링크만 만듭니다.
자격 증명 내용은 읽거나 복사하지 않습니다. 이 명령은 명시적으로 실행해야 하며
다른 항목(config.toml·AGENTS.md·skills·plugins·memories·hooks 등)이 있으면
분석을 거부합니다. 원본 인증 경로를 지정했다면 이후 실행에도 같은 CODEX_HOME을 유지하세요.

Codex CLI는 안정 버전 0.165.0 이상이어야 합니다. 버전·필수 격리 플래그와
`features list`를 확인하고, 목록에 존재하는 위험 기능만 명시적으로 `--disable`합니다.
버전·기능 검사는 인증 없는 임시 CODEX_HOME에서 실행합니다. 분석·probe는 전용 홈의
인증 링크만 새 임시 CODEX_HOME에 연결합니다. 빈 임시 cwd와 별도 임시 HOME,
임시 CODEX_HOME 및 PATH 세 변수만 전달하며
`--skip-git-repo-check --ephemeral --ignore-user-config --ignore-rules --sandbox read-only`
및 JSON 스키마·최종 메시지 출력 옵션을 사용합니다.

`doctor --probe`는 **실제 모델을 호출**하지만 저장소 코드는 보내지 않습니다.
파일 생성 시도와 전역 지침 노출을 검사하며, 도구 실행 보고·지침 노출·임시 디렉터리
파일 생성·비정상 종료·잘못된 JSON이면 실패합니다. 성공 기록은 데이터 디렉터리에
CLI 버전·모델·비활성 기능 목록별로 저장하며 7일 동안만 유효합니다. 일반 `doctor`와
분석은 probe를 자동 호출하지 않고 유효한 성공 기록을 요구합니다.
`doctor`는 codex-home·version·flags·probe 및 gh 인증·로컬 권한을 각각 표시하고,
실패 시 종료 코드 1을 반환합니다. 런타임 로그·캐시와 인증 rename 결과는 호출 후
임시 홈과 함께 삭제합니다. 원본 인증 파일을 링크를 통해 갱신하는 동작은 Codex에 맡깁니다.
실제 0.165.0의 인증 갱신 방식은 검증하지 않았으며, rename 방식의 갱신은 원본에
반영되지 않아 재인증이 필요할 수 있습니다. 영구 전용 홈이 오염되면
`skim codex-home init --reset`으로 내부 항목을 삭제하고 링크를 재생성하세요.
원본 링크 대상은 삭제하지 않으며 기존 probe 기록은 폐기합니다. probe는 관찰한 동작의 검사이며 모든 도구·파일 읽기에 대한 증명은 아닙니다.
모델 이름을 알 수 없으면 `unknown`으로 기록하고 영속 분석 캐시는 재사용하지 않습니다.

가짜 어댑터를 주입하는 테스트에서 청크·합성 파이프라인 전체를 검증합니다.
청크는 100단위·200 KiB(프롬프트와 메타데이터 포함), PR은 200파일·500단위·
정제 전 diff 1 MiB로 제한하고 규칙 후보부터 배정합니다. 큰 단위는 잘라 보내지
않고 누락 사유를 남깁니다. 합성 입력도 200 KiB를 넘으면 실패로 남깁니다.
입출력 ID의 누락·중복·외부 ID·추가 필드를 거부하고 파일 중요도는 소속 단위의
최고값으로 계산합니다. 검증 실패는 호출당 한 번 재시도하며 합성·실패·재시도를
포함해 로컬 날짜당 최대 20회를 SQLite에서 실행 전에 예약합니다.
수집과 분석은 공통 프로세스 잠금으로 직렬화됩니다.

캐시는 정제 입력 해시·스냅샷·provider/model·CLI 버전·프롬프트/스키마 버전으로
구분하며 모델이 `unknown`이면 영속 결과를 재사용하지 않습니다. 수집용 원문
해시는 coverage에 별도로 보존해 분석 뒤 재수집도 중복 스냅샷을 만들지 않습니다.
실제 LLM·네트워크를 호출하는 테스트는 없습니다.

## Sessio 연결

PR 상세에서 로컬 Sessio 인덱스의 저장소·브랜치 후보가 정확히 하나일 때
`gh api user`로 현재 로그인 사용자를 확인합니다. PR 작성자와 일치하면
세션 ID와 링크를 표시합니다. 인증 실패·인덱스 누락/손상·복수 후보는 표시하지
않으며 `web --offline`에서는 조회와 표시를 모두 생략합니다.

인덱스 기본 경로는 `$SESSIO_HOME/index/sessions.jsonl`이며,
`SESSIO_HOME`이 없으면 `~/.sessio/index/sessions.jsonl`입니다.
skim의 `config.json`에서 `sessio_index_path`로 경로를 변경할 수 있습니다.
`sessio_web_url` 기본값은 `http://127.0.0.1:8765/`입니다.
Sessio가 다른 포트에서 실행되면 `http://localhost:8766/`처럼 설정합니다.
HTTP의 `127.0.0.1` 또는 `localhost`와 명시적 포트·루트 경로만 허용하며,
토큰·query·fragment는 설정에 넣지 않습니다. Sessio 웹 인증은 해당 앱에서
먼저 진행해야 합니다.

Sessio의 실제 인덱스는 보통 저장소 이름만 기록하므로 그 이름과 정확한
브랜치를 비교합니다(전체 `owner/repo` 값도 지원). 동일 이름의 저장소를
여러 소유자 아래 사용하면 인덱스의 이름만으로 소유자를 구별할 수 없습니다.
인덱스의 임의 URL은 사용하지 않고 검증한 harness·세션 ID로 링크를 생성합니다.

## 설치와 Mac 실행

Python 3.11 이상과 gh·Codex CLI를 미리 설치합니다. Python 외부 패키지는 없습니다.
설치 스크립트는 다운로드나 관리자 권한 없이 사용자 링크를 생성합니다.
저장소 폴더를 유지해야 링크가 동작합니다.

```bash
./setup.sh
export PATH="$HOME/.local/bin:$PATH"
gh auth login
skim codex-home init
skim doctor --probe
skim repo add owner/repo
skim collect
skim web
```

Mac에서는 위 순서로 실행하면 브라우저가 열립니다. 터미널에서 Ctrl-C로 종료합니다.
격리 검사에 실패하거나 probe 기록이 만료되면 실제 분석은 차단됩니다.
수집과 로컬 웹은 계속 사용할 수 있습니다. 등록만으로 코드 전송을 허용하지 않습니다.

로그인 시 자동 시작 등록은 MVP 범위 밖입니다. 터미널에서 `skim web`을 실행하세요.
CLI 제거는 `rm ~/.local/bin/skim`으로 합니다.
기존 일반 파일이나 다른 설치의 링크는 덮어쓰지 않습니다.
`SKIM_PYTHON`으로 Python 실행 경로를 선택할 수 있습니다.
조직 SSO 승인·비공개 저장소 읽기 권한·팀 멤버십을 확인하세요.
doctor의 gh 인증 성공만으로 모든 조직·팀 접근을 보장하지 않습니다.

## 웹 기능과 보안

skim web은 127.0.0.1 전용 대시보드와 5분 주기 수집·수동 갱신을 제공합니다.
PR 목록·필터·상세에서 규칙 후보·미분석·high를 먼저 펼치고 mid/low는 접습니다.
중요도는 검토 순서 참고이며 보안 감사나 정확성 보증이 아닙니다.
GitHub 리뷰 작성·승인·머지는 하지 않습니다.

데이터 화면은 인증이 필요합니다. fragment의 일회용 진입 토큰은 60초간 유효하며
세션 수립 후 폐기됩니다. 진입 URL을 공유하지 마세요.
Host·Origin·CSRF 검증, CSP, 콘텐츠 이스케이프를 적용합니다.
로컬 HTTP 쿠키는 HttpOnly·SameSite=Strict이며 Secure는 사용하지 않습니다.
동일 OS 사용자 권한의 악성 프로세스까지 방어하지는 않습니다.
수집 원문 diff는 로컬 캐시에 저장됩니다. 전송 전 민감 정보 제외·가림은 완전하지
않습니다. 허용한 분석 입력에는 선택 CLI 모델 서비스의 전송·보존 정책이 적용됩니다.
별도 전송 API·원격 로그·텔레메트리는 없습니다.
보존 기간 자동 정리·cache clear·uninstall --purge는 아직 구현하지 않았습니다.

외부 통신 없는 화면 확인은 별도 SKIM_HOME에서 scripts/demo_seed.py 실행 후
bin/skim web --offline으로 할 수 있습니다.
테스트: python3 -m unittest discover -s tests (실제 LLM·외부 네트워크 사용 없음).

인증 refresh token이 교체되면 임시 홈의 갱신 결과가 삭제되어 원본 Codex CLI 로그인도 무효화될 수 있습니다. 이 경우 원본 CLI에서 다시 로그인하세요.

### Codex 모델 기록

`config.json`의 `codex_model`(예: `"codex_model": "gpt-5.4"`)로 모델을
지정할 수 있습니다. `SKIM_CODEX_MODEL` 환경 변수가 설정 파일보다 우선하며,
지정한 값은 Codex의 `--model` 인수와 분석 모델·캐시 키에 사용됩니다.
**`codex_model`을 명시적으로 설정하는 것을 권장합니다.** 실제 codex-cli 0.165.0의
`codex --json` 출력은 JSON 이벤트와 stderr 모두에 모델 이름을 노출하지 않아
probe로 모델을 감지하지 못합니다.
미지정 시 `skim doctor --probe`는 JSON 이벤트의 모델 메타데이터 또는
stderr의 `model:` 배너가 있는 경우에만 최선의 노력으로 감지하고 CLI 버전별 probe 캐시에 보관합니다.
일반 검사에서는 캐시를 읽어 추가 모델 호출을 하지 않습니다. 감지 실패 시
`unknown`을 유지하고 doctor에서 명시적 모델 설정을 권장합니다.
모델은 doctor와 웹 PR 상세 화면에서 확인할 수 있습니다.
