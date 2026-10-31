# skim

담당 레포에 올라온 PR에서 중요한 부분만 골라 보여주는 로컬 도구입니다.

설계: docs/01-design.md (작성 중)

## PR1 실행

Python 3.11 이상과 표준 라이브러리만 사용합니다.

```bash
bin/skim repo add owner/repo --source responsible
bin/skim repo list
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
저장소 삭제는 해당 저장소의 PR·스냅샷·분석 기록도 함께 삭제합니다.

설정 파일은 `~/.config/skim/config.json`, DB는
`~/.local/share/skim/skim.db`입니다. `SKIM_HOME`을 지정하면 각각
`$SKIM_HOME/config/config.json`, `$SKIM_HOME/data/skim.db`를 사용합니다.
설정 기본값은 수집 간격 300초, 보존 기간 30일이며 실제 수집·보존 처리는
후속 구현에서 적용합니다. 디렉터리는 0700, 설정·DB·백업 파일은 0600입니다.
DB 버전은 `PRAGMA user_version`으로 관리하고 기존 DB 변경 전에 백업합니다.
`SKIM_PYTHON`으로 래퍼가 사용할 Python 실행 파일을 지정할 수 있습니다.

`collect`, `analyze`, `web`, `doctor`는 현재 안내와 종료 코드 2를 반환하는
스텁입니다. PR1은 네트워크 조회나 모델 호출을 수행하지 않습니다.
