# 출시 체크리스트

`skim-ko-3` 출시는 실제 모델 평가 근거가 통과해야 한다. 단위 테스트나 합성
fixture는 실제 평가를 대신하지 않는다. 다른 프롬프트 버전은 계속 차단한다.

평가 입력은 `eval/skim-ko-3/manifest.json`과 `diffs/`에 고정되어 있다.
알려진 PR 15개와 인젝션·민감 정보·hunk 없는 변경 사례를 포함하며,
사람이 지정한 `high_files`를 평가에 맞추어 변경하지 않는다.

전용 홈을 준비하고 해당 홈의 설정과 격리된 Codex 인증을 설정한다.
`codex_model` 설정 또는 `SKIM_CODEX_MODEL`로 모델을 지정할 수 있다.

```sh
export SKIM_HOME=/absolute/path/to/eval-home
bin/skim codex-home init
python3 scripts/eval_release.py --home "$SKIM_HOME"
python3 scripts/check_release.py
```

평가는 실제 Codex CLI를 호출한다. 사례마다 홈 안의 임시 SQLite에 collect와
raw-v1 형식 스냅샷을 넣고, 홈의 `data/cache/diffs`에 diff를 저장한 뒤 실제
`Analyzer._analyze`를 실행한다. 임시 DB는 삭제하며 캐시는 남는다.
사례별 호출 예산은 Analyzer의 기본 제한(20회)을 그대로 적용한다.
`--out PATH`로 별도 결과를 저장할 수 있지만 출시 검사는 항상
`eval/<PROMPT_VERSION>/results.json`을 읽는다.

결과에는 SYSTEM 프로토콜 프롬프트 UTF-8 본문의 SHA-256, diff 해시,
프롬프트·스키마·CLI 버전, 모델, 시간대가 있는 실행 일시, 청크·합성의 원본
응답(재시도 포함), 실제 전송한 정제 요청, 최종 파일 중요도·규칙 후보·모델 플래그·단위 상태를 기록한다.
원본 응답은 민감 정보를 포함할 수 있으므로 저장소에 추가하기 전에 검토한다.

지정 high 파일은 그 파일의 high 중요도 또는 규칙 후보로 포착해야 한다.
미포착과 표시 자료의 파일·단위 상태 누락은 각각 0건이어야 하며 모든 사례 상태는 completed 또는
partial이어야 한다. 민감 정보 사례는 전송 자료에 비밀값이 없고 단위가 redacted여야
한다. 모든 diff 단위 ID가 표시 자료에 일치하고 각 단위의 파일과 상태가 있어야 한다.
이는 저장된 표시 자료 검사이며 실제 웹 화면 렌더링 검증을 대신하지 않는다. hunk 없는 사례는 분석 여부와 관계없이 모든 해당 변경이 파일과 합성 단위로 드러나야 한다.
watch는 지정 파일의 모델 플래그 존재 여부를 별도로 기록하며 금지 플래그가
있으면 실패한다. 실패를 숨기기 위해 기대값이나 high_files를 바꾸지 않는다.

기존 결과는 `python3 scripts/eval_release.py --rescore`로 모델 호출 없이 재채점한다.
프롬프트·스키마·diff 해시가 다르거나 저장된 요청이 없으면 거부한다. 원본 응답과
표시 결과, 실제 전송 요청은 보존하며 재채점하면 `reviewed_by`를 비운다.
요청이 없는 과거 결과는 새 평가가 필요하다.

출시 전 cases와 high_files를 사람이 확인하고 manifest의 `confirmed_by`에 비어 있지
않은 이름을 기록한다. `confirmed_sha256`에는 `eval_release.confirmation_hash(manifest)`의
값을 기록한다 (cases 전체를 키 정렬·UTF-8·공백 없는 JSON으로 직렬화한 SHA-256).
사례나 high_files가 바뀌면 다시 확인해야 한다. 결과·요청·원본 출력을 사람이 검토한 뒤
`python3 scripts/eval_release.py --review NAME`으로 results.json의 `reviewed_by`를 기록한다.
이 명령은 모델을 호출하지 않으며 검토했다는 사람의 선언만 기록한다.
`python3 scripts/check_release.py`가 0으로 종료하는지 확인한다. 게이트는 저장된 표시
자료와 실제 요청으로 점수를 다시 계산해 기록과 비교하고 청크·합성 원본 출력도 요구한다.
자료 누락, 확인자 누락, 프롬프트/입력/확인 해시 불일치, 실패 기대값은 차단한다.
향후 배포 자동화에서도 이 명령의 성공을 필수로 확인해야 한다.
