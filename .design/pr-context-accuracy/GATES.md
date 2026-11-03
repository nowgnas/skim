# PR 전체 맥락 분석 완료 게이트

> 설계: [PLAN.md](PLAN.md). 명령은 구현 후 실행하며, 테스트 이름은 구현 시 정확한 성공 기준을 증명하도록 작성한다.

- [x] G1: 개요 입력과 출력은 제외된 원문·민감값을 재노출하지 않고, 모델이 제시한 ID를 전달된 입력에 한정한다.
  CHECK: PYTHONPATH=lib:tests python3 -m unittest discover -s tests -p test_analyze.py -k overview_sensitive_and_ids -v
  EXPECT: /Ran [1-9][0-9]* tests?.*OK/s
  EVIDENCE: auto-v1 def=e6add1c39eabd3e0 result=pass exit=0 expect=matched out=0c397d022ee9 at=2026-11-03T14:09:21+0900

- [x] G2: 멀티바이트·긴 파일명·최대 단위와 긴 모델 결과에서도 개요, 각 청크, 합성 요청의 실제 크기 한도를 지킨다.
  CHECK: PYTHONPATH=lib:tests python3 -m unittest discover -s tests -p test_analyze.py -k overview_request_size_bounds -v
  EXPECT: /Ran [1-9][0-9]* tests?.*OK/s
  EVIDENCE: auto-v1 def=7eb31d6ecc2386bf result=pass exit=0 expect=matched out=48d6172c4f53 at=2026-11-03T14:09:21+0900

- [x] G3: 자동 분석에만 일일 호출 예산을 적용하고 사용자의 명시적 수동 분석은 횟수 제한 없이 실행한다. 잠금·격리·호출별 재시도를 유지하고 실패 시 이전 성공 결과를 보존한다.
  CHECK: PYTHONPATH=lib:tests python3 -m unittest discover -s tests -p test_analyze.py -k overview_budget_and_previous_result -v
  EXPECT: /Ran [1-9][0-9]* tests?.*OK/s
  EVIDENCE: auto-v1 def=10fca17d14e4cf2f result=pass exit=0 expect=matched out=2a9837454ebf at=2026-11-03T14:09:21+0900

- [x] G4: 중단 후 재시도와 부모 종료 시 자식 프로세스 경합을 격리 환경에서 확인하고 중복 호출 및 미완료 상태를 올바르게 표시한다.
  CHECK: PYTHONPATH=lib:tests python3 -m unittest test_analyze.AnalysisTests.test_overview_process_interruption test_analysis_guard test_web_jobs.JobTests.test_restart_recovers_interrupted_jobs_without_rerunning_model -v
  EXPECT: /Ran [1-9][0-9]* tests?.*OK/s
  EVIDENCE: auto-v1 def=857674c5584b588d result=pass exit=0 expect=matched out=f308f7afa7e0 at=2026-11-03T14:09:22+0900

- [x] G5: 새 프롬프트와 개요 정책은 기존 캐시·기존 결과·출시 증거와 구분되며 단일 청크의 동작과 비용을 검증한다.
  CHECK: PYTHONPATH=lib:tests python3 -m unittest discover -s tests -p test_analyze.py -k overview_cache_and_single_chunk -v
  EXPECT: /Ran [1-9][0-9]* tests?.*OK/s
  EVIDENCE: auto-v1 def=8d5174e4c049e807 result=pass exit=0 expect=matched out=75737e743530 at=2026-11-03T14:09:22+0900

- [x] G6: 합성과 화면은 파일 경로·변경 ID·분석 범위 및 누락을 일관되게 표시하고 요청 밖 ID를 허용하지 않는다.
  CHECK: PYTHONPATH=lib:tests python3 -m unittest discover -s tests -p test_analyze.py -k overview_file_mapping_and_coverage -v
  EXPECT: /Ran [1-9][0-9]* tests?.*OK/s
  EVIDENCE: auto-v1 def=f9f45d890a72023b result=pass exit=0 expect=matched out=b22be8c3ce77 at=2026-11-03T14:09:22+0900

- [x] G7: 모델 high와 규칙 후보를 분리하고 다중 청크·단일 청크 대조군, top-3 및 목적 평가를 비교할 수 있다.
  CHECK: PYTHONPATH=lib:tests:scripts python3 -m unittest discover -s tests -p test_eval_release.py -k overview_paired_metrics -v
  EXPECT: /Ran [1-9][0-9]* tests?.*OK/s
  EVIDENCE: auto-v1 def=78ce8f9864cbb567 result=pass exit=0 expect=matched out=a08bdd24b391 at=2026-11-03T14:09:23+0900

- [ ] G8: 같은 모델·CLI·정답을 사용한 실제 반복 A/B 평가와 사람의 목적·파일 기준 검토 결과를 확인한다. 미수행이면 품질 개선이나 출시 가능을 주장하지 않는다.
  EVIDENCE: pending

- [x] G9: 메인의 실제 파일 정렬은 중요도와 검증된 목적 관련성을 반영하고 이전 분석 결과는 안전하게 표시한다. 규칙 후보가 상위 목록에서 빠져도 별도 경로로 확인할 수 있다.
  CHECK: PYTHONPATH=lib:tests python3 -m unittest discover -s tests -p test_web_home.py -k overview_focus_ranking -v
  EXPECT: /Ran [1-9][0-9]* tests?.*OK/s
  EVIDENCE: auto-v1 def=6b451b18dc547e18 result=pass exit=0 expect=matched out=9191e6003850 at=2026-11-03T14:09:23+0900
