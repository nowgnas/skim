"""웹 백그라운드 작업의 영속 상태와 단일 워커 실행.

HTTP 처리는 검증·등록만 하고 외부 호출은 이 모듈의 워커 하나에서 직렬 실행한다.
종류별 대기·실행 작업은 DB 부분 고유 인덱스로 하나만 허용한다.
"""
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timezone
import json
import sqlite3
import time

from .collect import Collector, CollectionScheduler, error_message
from .config import Config
from .db import analysis_allowed

KINDS = {'collect': '수집', 'analyze': 'PR 분석', 'doctor': '상태 점검', 'repos': '저장소 조회'}
STATUS = {'pending': '대기', 'running': '실행 중', 'completed': '완료', 'failed': '실패',
          'deferred': '보류', 'blocked': '격리', 'cancelled': '취소'}
ACTIVE = ('pending', 'running')
# 종류별 하나이므로 전체 활성 작업은 최대 len(KINDS)개다.
INTERRUPTED = '서버 재시작으로 중단되었습니다. 다시 요청하세요.'


def stamp(clock=time.time):
    return datetime.fromtimestamp(clock(), timezone.utc).isoformat()


def get(conn, job_id):
    return conn.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()


def active(conn, kind):
    return conn.execute("SELECT * FROM jobs WHERE type=? AND status IN ('pending','running') ORDER BY id LIMIT 1", (kind,)).fetchone()


def latest(conn, kind, target_id=None):
    if target_id is None:
        return conn.execute('SELECT * FROM jobs WHERE type=? ORDER BY id DESC LIMIT 1', (kind,)).fetchone()
    return conn.execute('SELECT * FROM jobs WHERE type=? AND target_id=? ORDER BY id DESC LIMIT 1', (kind, target_id)).fetchone()


def recent(conn, limit=10):
    return conn.execute('SELECT * FROM jobs ORDER BY id DESC LIMIT ?', (limit,)).fetchall()


def enqueue(conn, kind, target_id=0, target='', input_sha=None, params=None, clock=time.time):
    """새 작업이면 (row, True), 같은 종류의 활성 작업이 있으면 그 작업과 False를 반환한다."""
    if kind not in KINDS or kind == 'collect':
        # 수집은 기존 스케줄러의 후속 1회 병합 규칙을 쓰도록 request_collect를 사용한다.
        raise ValueError('지원하지 않는 작업 종류입니다')
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        existing = active(conn, kind)
        if existing is not None:
            return existing, False
        job_id = conn.execute('INSERT INTO jobs(type,target_id,target,input_sha,params,stage,created_at) VALUES(?,?,?,?,?,?,?)',
                              (kind, target_id, target, input_sha, json.dumps(params or {}, ensure_ascii=False), '대기', stamp(clock))).lastrowid
    return get(conn, job_id), True


def request_collect(conn):
    """실행 중이면 후속 1회로 합치고, 대기 중이면 백오프 대기를 재시도 하한까지 당긴다."""
    before = active(conn, 'collect')
    CollectionScheduler(conn).request(manual=True)
    row = active(conn, 'collect')
    return row, before is None or before['id'] != row['id']


def recover(conn, clock=time.time):
    """수집 외 중단 작업은 모델 호출·외부 조회를 자동 반복하지 않도록 실패로 닫는다.
    수집은 기존 스케줄러가 잠금 확보 후 대기로 복구한다."""
    with conn:
        conn.execute("UPDATE jobs SET status='failed',last_error=?,stage='중단',finished_at=? WHERE type!='collect' AND status='running'",
                     (INTERRUPTED, stamp(clock)))


def claim(conn, clock=time.time):
    now = stamp(clock)
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute("SELECT * FROM jobs WHERE type!='collect' AND status='pending' AND (next_run_at IS NULL OR next_run_at<=?) ORDER BY id LIMIT 1", (now,)).fetchone()
        if row is None:
            return None
        conn.execute("UPDATE jobs SET status='running',attempts=attempts+1,started_at=?,stage='시작' WHERE id=?", (now, row['id']))
    return get(conn, row['id'])


def progress(conn, job_id, stage, processed=None, total=None):
    with conn:
        conn.execute('UPDATE jobs SET stage=?,processed=?,total=? WHERE id=? AND status=?', (stage, processed, total, job_id, 'running'))


def finish(conn, job_id, status, result=None, error=None, clock=time.time):
    with conn:
        conn.execute('UPDATE jobs SET status=?,result=?,last_error=?,stage=?,finished_at=? WHERE id=?',
                     (status, result, error, STATUS[status], stamp(clock), job_id))


def describe(row):
    """폴링 응답. 저장된 값은 이미 안전한 한국어 메시지만 포함한다."""
    return dict(id=row['id'], kind=row['type'], kind_label=KINDS.get(row['type'], row['type']),
                status=row['status'], status_label=STATUS.get(row['status'], row['status']),
                active=row['status'] in ACTIVE, target=row['target'], stage=row['stage'],
                processed=row['processed'], total=row['total'], model=row['model'], input_sha=row['input_sha'],
                created_at=row['created_at'], started_at=row['started_at'], finished_at=row['finished_at'],
                retry_at=row['next_run_at'] if row['status'] == 'pending' else None,
                followup=bool(row['refresh_requested']), error=row['last_error'], result=row['result'])


def current_model(config):
    # 다음 작업 시작 시 파일과 환경변수를 다시 읽어 확정한다. 실행 중 작업의 모델은 바꾸지 않는다.
    settings = json.loads((config.config_dir / 'config.json').read_text())
    return Config.parse_codex_model(settings)


class Runner:
    """단일 워커의 한 단계. 수집 tick 뒤 다른 종류의 대기 작업 하나를 실행한다."""

    def __init__(self, config, conn, state, lookup=None, clock=time.time, analyzer=None, doctor=None):
        # conn은 수집·분석용, state는 진행 기록용 연결이다. 분석 트랜잭션과 진행 기록을 섞지 않는다.
        self.config, self.conn, self.state, self.lookup, self.clock = config, conn, state, lookup, clock
        self.scheduler = CollectionScheduler(conn, config.poll_interval_seconds, clock)
        self.collector = Collector(conn, config)
        self.collector.progress = lambda stage, done, total: self._collect_progress(stage, done, total)
        self.analyzer = analyzer
        self.doctor = doctor
        self.auto_analyze = False
        recover(state, clock)

    def _collect_progress(self, stage, done, total):
        with self.state:
            self.state.execute("UPDATE jobs SET stage=?,processed=?,total=? WHERE type='collect' AND status='running'", (stage, done, total))

    def step(self):
        ran = self.scheduler.tick(self.collector)
        if ran and self.scheduler.last_ok:
            self.auto_analyze = True
        if self.auto_analyze:
            # 수집 후 자동 분석도 같은 큐를 쓴다. PR 단위 분석이 대기 중이면 그 작업이 끝난 뒤
            # 다음 단계에서 다시 등록한다. 대상 0 작업이 이미 대기 중이면 그 작업이 최신 스냅샷을 처리한다.
            row, _ = enqueue(self.state, 'analyze', 0, '허용된 최신 스냅샷 전체', clock=self.clock)
            self.auto_analyze = row['target_id'] != 0
        job = claim(self.state, self.clock)
        if job is None:
            return ran
        try:
            status, result, error = getattr(self, 'run_' + job['type'])(job)
        except Exception as exc:
            if self.conn.in_transaction:
                self.conn.rollback()
            status, result, error = 'failed', None, safe_error(job['type'], exc)
        finish(self.state, job['id'], status, result, error, self.clock)
        return True

    def run_analyze(self, job):
        from .analyze import Analyzer
        model = current_model(self.config)
        with self.state:
            self.state.execute('UPDATE jobs SET model=?,stage=? WHERE id=?', (model or '자동 감지', '분석 준비', job['id']))
        factory = self.analyzer or Analyzer
        analyzer = factory(self.conn, replace(self.config, codex_model=model))
        analyzer.progress = lambda stage, done, total: progress(self.state, job['id'], stage, done, total)
        if job['target_id'] == 0:
            results = analyzer.run()
            counts = {}
            for item in results:
                counts[item['status']] = counts.get(item['status'], 0) + 1
            # 일부만 성공해도 실패·격리·보류 결과를 완료로 숨기지 않는다.
            status = next((key for key in ('blocked', 'failed', 'deferred') if counts.get(key)), 'completed')
            error = None
            if status == 'blocked':
                error = '분석 격리 항목이 있습니다. 상태 점검 화면의 복구 안내를 확인하세요.'
            elif status == 'deferred':
                error = '자동 분석 예산이 소진되었거나 분석이 보류되었습니다. 직접 분석을 요청할 수 있습니다.'
            elif status == 'failed':
                error = '분석 실패 항목이 있습니다. PR별 결과와 상태 점검을 확인하세요.'
            summary = ', '.join(f'{STATUS.get(key, "부분 완료" if key == "partial" else key)} {value}'
                                for key, value in sorted(counts.items()))
            return status, f'분석 대상 {len(results)}개' + (f' ({summary})' if summary else ' — 허용된 미분석 스냅샷 없음'), error
        params = json.loads(job['params'] or '{}')
        snapshot_id = params.get('snapshot_id')
        # 실행 직전에 대상·SHA·동의를 다시 확인한다.
        row = self.conn.execute("SELECT p.id,p.repo_id,p.state,p.head_sha,(SELECT MAX(id) FROM snapshots WHERE pr_id=p.id) AS latest "
                                'FROM pull_requests p WHERE p.id=?', (job['target_id'],)).fetchone()
        if row is None or row['state'] != 'open':
            return 'failed', None, '대상 PR이 닫혔거나 더 이상 추적되지 않습니다.'
        if row['latest'] != snapshot_id or row['head_sha'] != job['input_sha']:
            return 'failed', None, '요청 이후 새 스냅샷이 수집되었습니다. 최신 화면에서 다시 요청하세요.'
        if not analysis_allowed(self.conn, row['repo_id']):
            return 'failed', None, '분석 허용이 꺼져 있어 모델을 호출하지 않았습니다. 분석 허용 화면을 확인하세요.'
        results = analyzer.run(snapshot_id, include_drafts=True, budgeted=False)
        if not results:
            return 'failed', None, '분석 대상이 없습니다. 허용 정책과 PR 상태를 확인하세요.'
        result = results[0]
        status = result['status']
        if status in ('completed', 'partial'):
            return 'completed', '분석 ' + ('완료' if status == 'completed' else '부분 완료 (분석 범위 또는 요약 일부 확인 필요)'), None
        if status == 'deferred':
            return 'deferred', None, result.get('error') or '분석 보류'
        if status == 'blocked':
            return 'blocked', None, '분석 격리: ' + (result.get('error') or '격리 검증 실패') + ' 상태 점검 화면의 복구 안내를 확인하세요.'
        return 'failed', None, result.get('error') or '분석 실패'

    def run_doctor(self, job):
        from . import doctor
        from .analyze.codex import CodexAdapter, IsolationError
        params = json.loads(job['params'] or '{}')
        action = params.get('action', 'check')
        if action not in ('check', 'init', 'probe'):
            return 'failed', None, '지원하지 않는 상태 점검 작업입니다.'
        if action == 'init':
            progress(self.state, job['id'], '인증 링크 준비')
            try:
                with self.collector.lock('analysis.lock'):
                    CodexAdapter(data_dir=self.config.data_dir).init_home(reset=False)
            except (IsolationError, OSError, ValueError):
                return 'failed', None, '초기화 실패: Codex 로그인과 전용 홈의 소유권·경로를 확인하세요. 기존 파일은 자동 삭제하지 않습니다.'
            lines = []
            code = (self.doctor or doctor.run)(self.config, probe=False, out=lines.append)
            message = ('분석 준비 상태 점검 실패. 아래 점검 항목을 해결한 뒤 다시 초기화하세요.' if code
                       else '분석 준비 초기화 완료. 상태 점검 정상. 이어서 격리 검증을 실행하세요.')
            return 'failed' if code else 'completed', message + ('\n' + '\n'.join(lines) if lines else ''), None
        lines = []
        if action == 'probe':
            progress(self.state, job['id'], '격리 검증 중')
            code = (self.doctor or doctor.run)(self.config, probe=True, out=lines.append)
        else:
            code = (self.doctor or doctor.run)(self.config, probe=False, out=lines.append)
        summary = '점검 실패 항목이 있습니다. 아래 안내를 따르세요.' if code else '모든 점검 항목 정상'
        return 'failed' if code else 'completed', summary + '\n' + '\n'.join(lines), None

    def run_repos(self, job):
        if self.lookup is None:
            return 'failed', None, '저장소 조회 워커가 없습니다.'
        params = json.loads(job['params'] or '{}')
        parts = tuple(params['repository'].split('/')) if params.get('repository') else None
        args = (parts, params.get('source'), params.get('host', 'github.com'), params.get('mode', 'all'))
        result = self.lookup.run(*args, team=params['team']) if params.get('team') else self.lookup.run(*args)
        return 'completed', result, None


def safe_error(kind, exc):
    # 예외 원문에는 CLI 출력·비밀값이 섞일 수 있어 안전한 메시지만 저장한다.
    if kind in ('collect', 'repos'):
        return KINDS[kind] + ' 실패: ' + error_message(exc)
    if isinstance(exc, sqlite3.Error):
        return KINDS[kind] + ' 실패: 로컬 DB를 확인하세요'
    return KINDS[kind] + ' 실패: 설정·로컬 파일과 상태 점검 결과를 확인하세요'


def worker(config, stop, lookup=None):
    """SQLite 연결은 워커 스레드 전용이며 작업 중에도 HTTP 응답을 계속한다."""
    from .db import connect
    with closing(connect(config)) as conn, closing(connect(config)) as state:
        runner = Runner(config, conn, state, lookup)
        while not stop.is_set():
            try:
                busy = runner.step()
            except (OSError, ValueError, sqlite3.Error):
                for each in (conn, state):
                    if each.in_transaction:
                        each.rollback()
                busy = False
            if not busy:
                stop.wait(1)
