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
`web`, `doctor`는 아직 종료 코드 2를 반환하는 스텁입니다.

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
