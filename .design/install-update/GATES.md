# 설치·업데이트 완료 게이트

> 설계: [PLAN.md](PLAN.md) · 상태: `knack gate status .design/install-update/GATES.md`
> 이 작업에는 진행 중인 워커와 서버 프로세스의 전환이 포함된다. 승인 전에는 구현을 시작하지 않는다.

- [x] G1: 정식 릴리스가 없거나 검증에 실패하면 기존 실행본과 데이터가 유지된다.
  CHECK: PYTHONPATH=lib python3 -m unittest discover -s tests -p test_install_update.py -k invalid_release_preserves_installed -v
  EXPECT: /Ran [1-9][0-9]* tests?.*OK/s
  EVIDENCE: 2026-11-04 `python3 -m unittest discover -s tests -k invalid_release_preserves_installed` Ran 2 tests OK. 체크섬 불일치, manifest 버전 불일치, 다운로드 실패 시 업데이트 기록 없이 v1.0.0 실행본과 DB 유지.

- [x] G2: 업데이트와 수집·분석이 겹쳐도 실행 중인 작업을 임의 종료하거나 새 작업을 잘못 시작하지 않는다.
  CHECK: PYTHONPATH=lib python3 -m unittest discover -s tests -p test_install_update.py -k concurrent_update_drains_jobs -v
  EXPECT: /Ran [1-9][0-9]* tests?.*OK/s
  EVIDENCE: 2026-11-04 `-k concurrent_update_drains_jobs` Ran 2 tests OK. 공유 잠금을 쥔 작업이 끝날 때까지 기다린 뒤 적용하고, 끝나지 않으면 기록 없이 중단. 기록 직후 시작한 작업은 종료하지 않고 새 CLI 진입은 차단.

- [x] G3: 전환·재시작 중 장애가 나더라도 상태를 확인하고 기존 실행본과 백업을 복구할 수 있다.
  CHECK: PYTHONPATH=lib python3 -m unittest discover -s tests -p test_install_update.py -k recovery_after_process_failure -v
  EXPECT: /Ran [1-9][0-9]* tests?.*OK/s
  EVIDENCE: 2026-11-04 `-k recovery_after_process_failure` Ran 2 tests OK. switched 단계에서 강제 종료한 뒤 재실행하면 DB 백업과 이전 버전을 복구하고, ready 단계에서 종료한 뒤 재실행하면 committed를 기록.

- [ ] G4: 다른 설치, 사용자 파일, 기존 개발 체크아웃을 덮어쓰지 않고 정식 릴리스만 선택한다.
  CHECK: PYTHONPATH=lib python3 -m unittest discover -s tests -p test_install_update.py -k ownership_preserves_existing_install -v
  EXPECT: /Ran [1-9][0-9]* tests?.*OK/s
  EVIDENCE: pending

- [ ] G5: 웹과 CLI가 같은 적용 결과와 실패 상태를 보여 주고 자동 시작 앱이 올바른 버전을 사용한다.
  CHECK: PYTHONPATH=lib python3 -m unittest discover -s tests -p test_install_update.py -k entrypoints_report_same_result -v
  EXPECT: /Ran [1-9][0-9]* tests?.*OK/s
  EVIDENCE: 2026-11-04 `-k entrypoints_report_same_result` Ran 1 test OK. CLI 진입점은 같은 차단 결과를 보이고 영구 launcher가 새 버전을 실행. 웹 설정 화면 업데이트와 정식 설치본 앱 연결은 미구현이라 미충족으로 둔다.

- [ ] G6: 유효한 정식 릴리스로 최초 설치와 이전 버전에서 새 버전으로 업데이트가 성공한다.
  CHECK: PYTHONPATH=lib python3 -m unittest discover -s tests -p test_install_update.py -k happy_path_install_and_update -v
  EXPECT: /Ran [1-9][0-9]* tests?.*OK/s
  EVIDENCE: pending

- [ ] G7: 릴리스 없음과 네트워크 실패를 구분하며 출시 평가 실패 시 배포물을 만들지 않는다.
  CHECK: PYTHONPATH=lib python3 -m unittest discover -s tests -p test_install_update.py -k release_gate_blocks_packaging -v
  EXPECT: /Ran [1-9][0-9]* tests?.*OK/s
  EVIDENCE: pending

- [ ] G8: 새 서버 버전·DB·워커 준비가 확인되기 전에는 성공을 기록하거나 사용자 쓰기를 열지 않는다.
  CHECK: PYTHONPATH=lib python3 -m unittest discover -s tests -p test_install_update.py -k readiness_blocks_writes_on_actual_server -v
  EXPECT: /Ran [1-9][0-9]* tests?.*OK/s
  EVIDENCE: 2026-11-04 `-k readiness_blocks_writes_on_actual_server` Ran 1 test OK. 실제 launcher의 --version 확인 전에는 쓰기 차단과 미커밋을 확인. 서버 재시작, DB 초기화와 워커 준비 확인은 미구현이라 미충족으로 둔다.
