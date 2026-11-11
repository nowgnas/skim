# 출시 체크리스트

공개 및 첫 정식 릴리스의 작업 순서와 완료 조건은 [릴리스 준비 작업](release-roadmap.md)을 참고한다.

`skim-ko-5` 출시는 실제 모델 평가 근거가 통과해야 한다. 단위 테스트나 합성
fixture는 실제 평가를 대신하지 않는다. 다른 프롬프트 버전은 계속 차단한다. 민감 정보 정제 정책이 바뀌면 같은 프롬프트 버전이라도 실제 모델 재평가와 사람 검토가 필요하다.

평가 입력은 `eval/skim-ko-5/manifest.json`과 `diffs/`에 고정되어 있다.
알려진 PR 15개와 인젝션·민감 정보·hunk 없는 변경 사례를 포함하며,
사람이 지정한 `high_files`를 평가에 맞추어 변경하지 않는다.
`fx-pr-purpose`는 핵심 로직과 배포 변경을 점검하기 위해 새로 추가한 후보이며,
전체 사례와 함께 사람이 기대 파일을 확인해야 한다. `fx-multichunk`는 청크를 나누는 동작을 확인하기 위한 합성 사례다.

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

`metrics`는 모델 high와 규칙 후보를 분리하고 메인 화면과 같은 순서의 상위 3개 및 precision/recall을 기록한다. 목적 요약은 사람이 검토해야 하며 이 수치만으로 정확도 개선을 판단하지 않는다. 변경 전후 비교에는 동일한 사람이 확인한 기준과 모델·CLI, 반복 실행 결과가 필요하다.

## skim-ko-6 분리 채점

`eval/skim-ko-6`은 경로만 보고 붙은 인증 힌트가 거짓 높음을 만드는지, 그러면서 실제 높음을
놓치지 않는지를 재는 격리 fixture 18건이다(새 fixture 13건 + `skim-ko-5`에서 바이트 그대로
복사한 `fx-secret`·`fx-injection`·`fx-pr-purpose`·`fx-multichunk`·`fx-nohunk`).
`skim-ko-3`~`skim-ko-5`의 자료와 채점은 바꾸지 않는다. 채점은 코퍼스 버전으로 갈린다.

| 라벨 | 채점 |
| --- | --- |
| `high_files` | 모델이 high로 본 경우만 포착. 규칙 후보·경로 힌트·강제 바닥·테스트 공백은 대신하지 않는다 |
| `not_high_files` | 모델 high이면 `false_high`로 실패 |
| `location_hint_files` | 위치 힌트가 없으면 `location_hint_missing`, 모델 high·핵심이 아닌데 먼저 볼 파일에 오르면 `location_hint_promoted` |
| `kind_signal_files`, `floor_files` | 신호가 붙고 먼저 볼 파일이나 안전 확인 줄에 보여야 한다. 아니면 `signal_hidden`. 중요도와 따로 잰다 |
| `no_floor_files` | 강제 바닥이 붙으면 `unexpected_floor` |
| `not_mechanical_files` | 기계적 변경으로 분류되거나 접히면 `mechanical_folded` |
| `core_files`, `related_of` | `metrics.core_hit`·`related_hit`로 보고만 하고 실패로 세지 않는다 |

상위 3개 precision/recall의 기준은 `core_files`와 `high_files`의 합집합이며, 기준이 3개를 넘으면
`top3_recall_ceiling`이 recall 상한을 알려 준다. 위 실패 목록은 사례별로 기록되고 요약과
`check_release.py`가 모두 0건을 요구한다. `confirmation_hash`는 `skim-ko-6`부터 `cases`와 함께
manifest의 `rule` 문구를 묶으므로 정책 문구가 바뀌어도 다시 확인해야 한다.

manifest의 `confirmed_by`는 비어 있다. sangwon이 라벨과 `rule`을 확인해 `confirmed_by`,
`confirmed_sha256`, `confirmed_at`을 기록하고, 실제 모델 실행 결과를 검토해 `reviewed_by`를 남기기
전에는 `check_release.py`가 `skim-ko-6`을 계속 차단한다. 부정 기대(`not_high_files` 등)는 1회
실행으로 단정하지 않고 반복 실행으로 확인한다.

현재 분석기를 다른 코퍼스의 diff에 돌려 전후를 비교할 수 있다. 게이트와 무관한 보고서만 쓰고
`eval/` 아래에는 쓰지 않는다. 기존 `results.json`이 있으면 사례별 `baseline`(이전 미스·모델 high·
상위 3개)을 함께 싣고, `model_only_misses`로 규칙 후보 없이 모델만으로 놓친 high를 보여 준다.

```sh
python3 scripts/eval_release.py --home "$SKIM_HOME" --corpus eval/skim-ko-5 --compare --out /tmp/skim-ko-5-compare.json
```
