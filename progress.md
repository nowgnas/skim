
## PR1 — 2026-10-31

- Sessio 스타일의 심볼릭 링크 대응 bash 래퍼와 Python CLI 골격을 구현했다.
- JSON 설정, SKIM_HOME 경로 격리, 0700/0600 권한, 전체 초기 SQLite 스키마·WAL·트랜잭션 마이그레이션·사전 백업을 추가했다.
- 저장소 등록/목록/삭제 및 조직·저장소 분석 allow/deny/reset을 구현했다. 등록 기본값은 전송 불허이며 저장소 정책이 우선한다.
- collect/analyze/web/doctor는 명시적 미구현 스텁이다. 외부 통신 없이 unittest 5개가 통과했다.
