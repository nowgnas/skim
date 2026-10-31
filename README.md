# skim

담당 레포에 올라온 PR에서 중요한 부분만 골라 보여주는 로컬 도구입니다.

설계: docs/01-design.md (작성 중)

## 실행

Python 3.11 이상과 표준 라이브러리만 사용합니다.

```bash
bin/skim repo add owner/repo --source responsible
bin/skim repo list
bin/skim collect
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
현재 `analyze`, `web`, `doctor`는 종료 코드 2를 반환하는 스텁입니다.
스냅샷의 `raw-v1` 입력 해시는 수집용이며, 후속 분석은 민감 정보 정제·분할 정책을
적용한 별도 입력 해시를 만들어야 합니다. 실제 모델 호출은 구현하지 않았습니다.
