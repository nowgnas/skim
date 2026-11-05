# 로컬 데이터 보관 완료 게이트

- [x] G1: 만료 경계, 공유 diff, 비정상 종료, 잠금, 경로와 백업 보호가 임시 데이터에서 통과한다
  CHECK: PYTHONPATH=lib:tests python3 -m unittest test_lifecycle -v 2>&1
  EXPECT: /^OK$/m
  EVIDENCE: auto-v1 def=d7e3e367798e92b0 result=pass exit=0 expect=matched out=dcd09124d9bb at=2026-11-05T14:55:56+0900

- [x] G2: 기존 닫힌 PR과 마이그레이션 백업이 보존된다
  CHECK: PYTHONPATH=lib:tests python3 -m unittest test_scaffold.ScaffoldTests.test_lifecycle_migration_preserves_existing_closed_pr_and_backup -v 2>&1
  EXPECT: /^OK$/m
  EVIDENCE: auto-v1 def=1fb63e629a2a7f92 result=pass exit=0 expect=matched out=82f41fc66e91 at=2026-11-05T14:55:56+0900

- [x] G3: 보관 설정은 웹과 CLI에서 같고 업데이트 중 백업 삭제는 거부된다
  CHECK: PYTHONPATH=lib:tests python3 -m unittest test_web_settings.SettingsTests.test_retention_settings_and_cli_read_same_policy test_web_settings.SettingsTests.test_backup_removal_during_update_is_explained -v 2>&1
  EXPECT: /^OK$/m
  EVIDENCE: auto-v1 def=ad2fd1f7f1a393b0 result=pass exit=0 expect=matched out=99350bda4e4d at=2026-11-05T14:55:56+0900
