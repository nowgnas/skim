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

**현재 실제 Codex 분석은 차단됩니다.** 설치 CLI의 빈 작업 디렉터리·read-only만으로는
전역 설정·메모리·플러그인·hooks·MCP 로딩 및 모든 도구 실행 금지까지 보장할 수
없습니다. 검증된 버전별 격리 계약이 아직 없어 `CodexAdapter.verify()`가 실패하고
`blocked` 상태와 이유를 기록합니다. 도움말 플래그 존재나 프롬프트만으로 우회하지
않으며, 사용자 설정으로 이 검사를 해제하는 옵션도 없습니다. 어댑터의 실행 경로는
검증 성공 후 빈 임시 디렉터리에서 `codex exec`를 호출하도록 분리되어 있습니다.
`doctor`는 인증·설치·격리·권한을 점검하며 실패 시 종료 코드 1을 반환합니다.

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
skim doctor
skim repo add owner/repo
skim collect
skim web
```

Mac에서는 위 순서로 실행하면 브라우저가 열립니다. 터미널에서 Ctrl-C로 종료합니다.
현재 Codex 격리 계약 미검증으로 doctor는 실패하고 실제 분석은 차단됩니다.
수집과 로컬 웹은 사용할 수 있습니다. 등록만으로 코드 전송을 허용하지 않습니다.

```bash
./setup.sh --web
./setup.sh --remove-web
```

--web은 로그인 시 macOS launchd 사용자 LaunchAgent
(~/Library/LaunchAgents/dev.skim.web.plist) 또는 Linux systemd --user
(~/.config/systemd/user/skim-web.service)를 등록합니다.
브라우저는 자동으로 열지 않습니다. 로그인 후 skim web으로 기존 서버의 새 진입 URL을
받으세요. --remove-web은 자동 시작과 서비스 파일만 제거하고 설정·DB는 보존합니다.
CLI 제거는 자동 시작 제거 후 rm ~/.local/bin/skim으로 합니다.
기존 일반 파일이나 다른 설치의 링크는 덮어쓰지 않습니다.
SKIM_PYTHON으로 Python 실행 경로를 선택할 수 있습니다.
서비스에는 설치 당시 PATH·Python·명시한 SKIM_HOME을 기록하므로 환경 변경 후
./setup.sh --web을 다시 실행하세요. Linux에는 로그인한 systemd 사용자 세션이 필요합니다.
Mac 상태는 launchctl print gui/$(id -u)/dev.skim.web,
Linux 상태는 systemctl --user status skim-web.service로 확인합니다.
서비스에서도 gh 인증과 로그인 키체인 접근이 가능해야 합니다.
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
