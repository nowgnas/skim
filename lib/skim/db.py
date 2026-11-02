"""SQLite 스키마와 트랜잭션 마이그레이션."""
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from .config import private_file

SCHEMA = """
CREATE TABLE repositories (
 id INTEGER PRIMARY KEY, owner TEXT NOT NULL COLLATE NOCASE,
 name TEXT NOT NULL COLLATE NOCASE, source TEXT NOT NULL DEFAULT 'responsible'
 CHECK(source IN ('owned','managed','responsible')),
 tracked INTEGER NOT NULL DEFAULT 0 CHECK(tracked IN (0,1)),
 enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
 analysis_policy TEXT NOT NULL DEFAULT 'inherit' CHECK(analysis_policy IN ('inherit','allow','deny')),
 last_synced_at TEXT, UNIQUE(owner,name));
CREATE TABLE organization_policies (
 owner TEXT PRIMARY KEY COLLATE NOCASE, policy TEXT NOT NULL CHECK(policy IN ('allow','deny')));
CREATE TABLE pull_requests (
 id INTEGER PRIMARY KEY, repo_id INTEGER NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
 number INTEGER NOT NULL CHECK(number > 0), url TEXT, title TEXT, body TEXT, author TEXT,
 state TEXT NOT NULL DEFAULT 'open' CHECK(state IN ('open','closed','merged')),
 draft INTEGER NOT NULL DEFAULT 0 CHECK(draft IN (0,1)), head_branch TEXT,
 base_sha TEXT, head_sha TEXT, updated_at TEXT, last_seen_at TEXT, UNIQUE(repo_id,number));
CREATE TABLE pr_sources (
 pr_id INTEGER NOT NULL REFERENCES pull_requests(id) ON DELETE CASCADE,
 source TEXT NOT NULL CHECK(source IN ('repository','review_request')), PRIMARY KEY(pr_id,source));
CREATE TABLE snapshots (
 id INTEGER PRIMARY KEY, pr_id INTEGER NOT NULL REFERENCES pull_requests(id) ON DELETE CASCADE,
 base_sha TEXT NOT NULL, head_sha TEXT NOT NULL, diff_hash TEXT NOT NULL, input_hash TEXT NOT NULL,
 diff_path TEXT, coverage TEXT NOT NULL DEFAULT '{}', collected_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE changes (
 id INTEGER PRIMARY KEY, snapshot_id INTEGER NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
 file_path TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('file','hunk','synthetic')),
 parent_id INTEGER REFERENCES changes(id) ON DELETE CASCADE, old_range TEXT, new_range TEXT,
 binary INTEGER NOT NULL DEFAULT 0, generated INTEGER NOT NULL DEFAULT 0);
CREATE TABLE analyses (
 id INTEGER PRIMARY KEY, snapshot_id INTEGER NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
 input_hash TEXT NOT NULL, provider TEXT NOT NULL, model TEXT NOT NULL, cli_version TEXT NOT NULL,
 prompt_version TEXT NOT NULL, schema_version TEXT NOT NULL, status TEXT NOT NULL,
 intent TEXT, output_json TEXT, error TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE analysis_items (
 analysis_id INTEGER NOT NULL REFERENCES analyses(id) ON DELETE CASCADE,
 change_id INTEGER NOT NULL REFERENCES changes(id) ON DELETE CASCADE,
 importance TEXT NOT NULL CHECK(importance IN ('high','mid','low')), reason TEXT NOT NULL,
 risk_flags TEXT NOT NULL DEFAULT '[]', PRIMARY KEY(analysis_id,change_id));
CREATE TABLE jobs (
 id INTEGER PRIMARY KEY, type TEXT NOT NULL CHECK(type IN ('collect','analyze')),
 target_id INTEGER NOT NULL, input_hash TEXT NOT NULL DEFAULT '',
 status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','running','completed','failed','cancelled')),
 attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
 next_run_at TEXT, last_error TEXT);
CREATE UNIQUE INDEX jobs_active_dedupe ON jobs(type,target_id,input_hash)
 WHERE status IN ('pending','running');
"""
MIGRATIONS = (SCHEMA, """
ALTER TABLE changes ADD COLUMN unit_key TEXT;
CREATE UNIQUE INDEX changes_unit_key ON changes(snapshot_id,unit_key) WHERE unit_key IS NOT NULL;
CREATE TABLE analysis_budget (
 day TEXT PRIMARY KEY, calls INTEGER NOT NULL DEFAULT 0 CHECK(calls BETWEEN 0 AND 20));
""", """
ALTER TABLE jobs ADD COLUMN refresh_requested INTEGER NOT NULL DEFAULT 0 CHECK(refresh_requested IN (0,1));
ALTER TABLE jobs ADD COLUMN retry_not_before TEXT;
-- 기존 대기 시각도 보존해 업그레이드 직후 요청 제한을 무시하지 않는다.
UPDATE jobs SET retry_not_before=next_run_at WHERE status='pending';
""", """
-- 종류·상태 CHECK를 넓히려면 테이블을 재생성해야 한다. jobs를 참조하는 외래 키는 없다.
CREATE TABLE jobs_v4 (
 id INTEGER PRIMARY KEY, type TEXT NOT NULL CHECK(type IN ('collect','analyze','doctor','repos')),
 target_id INTEGER NOT NULL, input_hash TEXT NOT NULL DEFAULT '',
 status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','running','completed','failed','deferred','cancelled')),
 attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
 next_run_at TEXT, last_error TEXT,
 refresh_requested INTEGER NOT NULL DEFAULT 0 CHECK(refresh_requested IN (0,1)),
 retry_not_before TEXT,
 target TEXT NOT NULL DEFAULT '', params TEXT NOT NULL DEFAULT '{}', model TEXT, input_sha TEXT,
 stage TEXT, processed INTEGER CHECK(processed IS NULL OR processed >= 0),
 total INTEGER CHECK(total IS NULL OR total >= 0), result TEXT,
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, started_at TEXT, finished_at TEXT);
INSERT INTO jobs_v4(id,type,target_id,input_hash,status,attempts,next_run_at,last_error,refresh_requested,retry_not_before)
 SELECT id,type,target_id,input_hash,status,attempts,next_run_at,last_error,refresh_requested,retry_not_before FROM jobs;
DROP TABLE jobs;
ALTER TABLE jobs_v4 RENAME TO jobs;
-- 종류별 활성 작업 하나 제약 전에 오래된 중복 활성 작업을 정리한다.
UPDATE jobs SET status='cancelled',last_error='종류별 단일 작업 전환으로 정리했습니다'
 WHERE status IN ('pending','running') AND id NOT IN
 (SELECT MIN(id) FROM jobs WHERE status IN ('pending','running') GROUP BY type);
CREATE UNIQUE INDEX jobs_active_dedupe ON jobs(type,target_id,input_hash)
 WHERE status IN ('pending','running');
CREATE UNIQUE INDEX jobs_active_kind ON jobs(type) WHERE status IN ('pending','running');
""")

# v4의 모든 열·기록을 보존하면서 격리 종료 상태만 추가한다.
_JOB_V5_SCHEMA = MIGRATIONS[3].split('INSERT INTO jobs_v4', 1)[0].replace(
    'jobs_v4', 'jobs_v5').replace("'deferred','cancelled'", "'deferred','blocked','cancelled'")
MIGRATIONS += (_JOB_V5_SCHEMA + """
INSERT INTO jobs_v5 SELECT * FROM jobs;
DROP TABLE jobs;
ALTER TABLE jobs_v5 RENAME TO jobs;
CREATE UNIQUE INDEX jobs_active_dedupe ON jobs(type,target_id,input_hash)
 WHERE status IN ('pending','running');
CREATE UNIQUE INDEX jobs_active_kind ON jobs(type) WHERE status IN ('pending','running');
""",)


# 부모 테이블 이름을 변경하지 않고 교체해 자식 참조와 ID를 보존한다.
_REPOS_V6 = SCHEMA.split('CREATE TABLE organization_policies', 1)[0].replace(
    'CREATE TABLE repositories', 'CREATE TABLE repositories_v6').replace(
    "id INTEGER PRIMARY KEY, owner", "id INTEGER PRIMARY KEY, host TEXT NOT NULL DEFAULT 'github.com', owner").replace(
    'UNIQUE(owner,name)', 'UNIQUE(host,owner,name)')
MIGRATIONS += (_REPOS_V6 + """
INSERT INTO repositories_v6(id,owner,name,source,tracked,enabled,analysis_policy,last_synced_at)
 SELECT id,owner,name,source,tracked,enabled,analysis_policy,last_synced_at FROM repositories;
DROP TABLE repositories;
ALTER TABLE repositories_v6 RENAME TO repositories;
CREATE TABLE github_hosts (host TEXT PRIMARY KEY);
INSERT INTO github_hosts VALUES('github.com');
CREATE TABLE host_organization_policies (
 host TEXT NOT NULL REFERENCES github_hosts(host), owner TEXT NOT NULL COLLATE NOCASE,
 policy TEXT NOT NULL CHECK(policy IN ('allow','deny')), PRIMARY KEY(host,owner));
""",)
# v7: GitHub 앱 설치처럼 호스트마다 분석 범위(안 함/전체/선택)를 둔다. 선택한 저장소는
# repositories.analysis_policy='allow'로 표시한다. 동의 모델이 바뀌므로 기존 허용과 조직 정책은 초기화한다.
# 이전 버전 skim은 새 DB 버전을 열지 않으므로 되돌릴 때는 자동 백업(.bak)으로 복원한다. 조직 정책 테이블은 남기되 쓰지 않는다.
MIGRATIONS += ("""
ALTER TABLE github_hosts ADD COLUMN analysis_scope TEXT NOT NULL DEFAULT 'none'
 CHECK(analysis_scope IN ('none','all','selected'));
UPDATE repositories SET analysis_policy='inherit';
DELETE FROM organization_policies;
DELETE FROM host_organization_policies;
""",)
SCOPES = ('none', 'all', 'selected')


def migrate(conn, path):
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version > len(MIGRATIONS):
        raise ValueError("지원하지 않는 새 DB 버전입니다")
    if version == len(MIGRATIONS):
        return
    # 기존 DB는 변경 전에 SQLite 백업 API로 일관된 사본을 남긴다.
    if path.stat().st_size:
        stamp = datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')
        backup_path = path.with_name(path.name + f'.v{version}.{stamp}.bak')
        private_file(backup_path)
        with closing(sqlite3.connect(backup_path)) as backup:
            conn.backup(backup)
    for index in range(version, len(MIGRATIONS)):
        # 부모 테이블 교체 전에 트랜잭션 밖에서 FK 연쇄 삭제를 끈다.
        conn.execute('PRAGMA foreign_keys = OFF')
        try:
            conn.executescript('BEGIN IMMEDIATE;\n' + MIGRATIONS[index] +
                               f'\nPRAGMA user_version = {index + 1};')
            if conn.execute('PRAGMA foreign_key_check').fetchone():
                raise ValueError('DB 외래 키 무결성 오류')
            conn.commit()
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.execute('PRAGMA foreign_keys = ON')


def connect(config):
    path = config.db_path
    private_file(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute('PRAGMA foreign_keys = ON')
        migrate(conn, path)
        conn.execute('PRAGMA journal_mode = WAL')
        for suffix in ('-wal', '-shm'):
            sidecar = path.with_name(path.name + suffix)
            if sidecar.exists():
                sidecar.chmod(0o600)
        return conn
    except BaseException:
        conn.close()
        raise


def analysis_allowed(conn, repo):
    """호스트 범위가 전체면 허용, 선택이면 선택한 저장소만, 그 외(없음·삭제된 호스트)는 거부."""
    row = conn.execute('SELECT r.analysis_policy, h.analysis_scope FROM repositories r '
                       'LEFT JOIN github_hosts h ON h.host=r.host WHERE r.id=?', (repo,)).fetchone()
    if row is None or row['analysis_scope'] is None:
        return False
    return row['analysis_scope'] == 'all' or (row['analysis_scope'] == 'selected' and row['analysis_policy'] == 'allow')


def analysis_scope(conn, host):
    row = conn.execute('SELECT analysis_scope FROM github_hosts WHERE host=?', (host,)).fetchone()
    return row[0] if row else 'none'
