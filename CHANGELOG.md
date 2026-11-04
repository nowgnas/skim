# Changelog

이 프로젝트의 주요 변경 사항을 기록합니다.

형식은 [Keep a Changelog](https://keepachangelog.com/ko/1.1.0/)를 따르고,
버전은 [Semantic Versioning](https://semver.org/lang/ko/)을 따릅니다.

## [Unreleased]

## [1.0.0] - 2026-11-04

첫 정식 릴리스입니다.

### Added

- 실행과 설정
  - `skim`만 실행하면 `127.0.0.1`에 로컬 서버를 시작하고 브라우저를 엽니다 (`skim web`과 동일, `--port`·`--no-browser`·`--offline` 지원).
  - `setup.sh`로 `~/.local/bin/skim` 심볼릭 링크를 만들고, 래퍼가 Python 3.11 이상 인터프리터를 자동으로 고릅니다 (`SKIM_PYTHON`으로 지정 가능).
  - `skim app`으로 macOS 앱(`~/Applications/skim PR.app`)과 로그인 시 자동 시작을 설정합니다. `--restart`로 서버 재시작, `--uninstall`로 관리 마커가 확인된 앱과 자동 시작만 제거합니다.
  - `skim update --check`로 GitHub Releases에 게시된 정식 릴리스를 조회합니다.
  - `skim uninstall`로 skim이 만든 것으로 확인된 앱, 관리 설치, 실행 파일만 제거하고 사용자 데이터는 기본 보존합니다 (`--purge-data`로 데이터도 삭제, 비대화형 실행은 `--yes` 필요).
  - `skim --version`으로 skim 버전을 확인합니다.
  - 설정·DB·diff 캐시를 전용 디렉터리(0700)와 파일(0600)에 저장하고, `SKIM_HOME`으로 위치를 바꿀 수 있습니다. DB 스키마는 `PRAGMA user_version`으로 관리하며 변경 전에 백업합니다.
- 저장소와 수집
  - `gh` 로그인으로 열린 PR과 개인·팀 리뷰 요청을 읽기 전용으로 수집하고 중복 없이 정리합니다. 온라인 서버는 5분 주기로도 수집합니다.
  - 저장소 화면에서 접근 가능한 개인·조직·팀 저장소를 불러오거나 `owner/repo`를 직접 입력해 연결합니다. 호스트별 한 목록으로 보여줍니다.
  - GitHub Enterprise(회사 GitHub) 호스트를 추가해 같은 방식으로 연결합니다.
  - `skim repo add|list|remove`와 `skim collect` 명령을 제공합니다.
- 분석
  - Codex CLI로 PR의 변경 의도와 hunk·파일별 중요도, 선정 이유를 분석합니다. 규칙 기반 위험 후보와 모델 판단은 출처를 나누어 표시합니다.
  - 분석 허용은 기본 OFF이며, GitHub 앱 설치처럼 호스트별 범위(분석 안 함, 모든 저장소, 선택한 저장소만)로 정합니다. 범위를 넓힐 때는 전송 확인이 필요하고 실행 직전에도 정책을 재확인합니다 (`skim analysis scope|allow|deny|reset`).
  - 민감 정보로 보이는 파일과 내용을 분석 입력에서 제외하고, 미분석·제외 범위와 사유를 보여줍니다.
  - 입력 해시·모델·CLI·프롬프트 버전 기준으로 분석 결과를 캐시하고, 입력이 바뀌면 다시 분석합니다.
  - 자동 분석에는 일일 호출 예산을 적용하고 수동 분석(`skim analyze`, 웹의 이 PR 분석·다시 분석)은 예산과 별도로 실행합니다. 스케줄러용 `skim analyze --automatic`을 제공합니다.
  - 분석 전용 Codex 홈(`skim codex-home init`)과 격리 플래그 검사, 코드 없이 실제 모델을 호출하는 격리 검사(`skim doctor --probe`, 7일 유효)를 거쳐야 분석합니다.
  - 실제로 응답한 모델명을 기록하고, 웹 설정에서 Codex 모델 또는 자동 선택을 고를 수 있습니다 (`SKIM_CODEX_MODEL` 우선).
  - 실제 모델 회귀 평가 세트와 출시 게이트를 추가했습니다.
- 웹 대시보드
  - 메인 화면에서 PR을 중요도와 리뷰 요청 순으로 정렬하고, 탭별 PR 수, PR별 변경 의도와 먼저 볼 파일을 보여줍니다. 제목·저장소·작성자 검색, 저장소 필터, 20개 단위 페이지 이동을 지원합니다.
  - PR 상세에서 변경 의도, 먼저 볼 파일의 선정 이유와 diff, 파일별 변경(우선순위·원래 순서 전환, 변경 단위 검색)을 확인하고 GitHub 원문으로 이동합니다.
  - 수집·분석·저장소 조회·상태 점검을 공통 백그라운드 큐에서 실행하고 단계와 진행 건수를 표시합니다. 상태 화면에서 점검 결과 요약과 작업 기록, 실패 시 복구 안내를 한 목록으로 보여줍니다.
  - 본인이 작성한 PR을 Sessio 로컬 세션으로 연결합니다.
  - `scripts/demo_seed.py`로 GitHub·Codex 로그인 없이 데모 데이터를 볼 수 있습니다.
- 점검
  - `skim doctor`로 모델 호출 없이 `gh` 인증, Python, Codex 설치·전용 홈, probe 기록, 모델, 로컬 권한을 점검합니다.

### Security

- GitHub API는 읽기 전용으로만 사용하며 리뷰 작성·승인·머지를 하지 않습니다.
- 웹은 `127.0.0.1`에만 바인딩하고, 60초 1회용 진입 토큰, `HttpOnly; SameSite=Strict` 세션 쿠키, CSP를 사용합니다.
- 변경 요청은 POST만 허용하며 세션별 CSRF 토큰과 정확한 `Origin`·`Host` 검사로 보호합니다. GET 조회는 작업을 시작하지 않습니다.

[Unreleased]: https://github.com/nowgnas/skim/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/nowgnas/skim/releases/tag/v1.0.0
