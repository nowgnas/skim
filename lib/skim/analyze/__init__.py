"""명시적으로 허용한 저장소의 스냅샷만 로컬 분석한다."""
import hashlib
import json
import subprocess
from datetime import datetime
from pathlib import Path
from .codex import CodexAdapter, IsolationError
from .protocol import (PROMPT_VERSION, SCHEMA_VERSION, CHUNK_SCHEMA, SYNTHESIS_SCHEMA,
                       encode, prompt, chunk_units, validate_chunk, validate, strict_json)
from .units import parse_diff, sanitize
from ..db import analysis_allowed
from ..collect import Collector


class BudgetExceeded(ValueError):
    pass


class Analyzer:
    def __init__(self, conn, config, adapter=None, today=None):
        self.conn, self.config = conn, config
        self.adapter = adapter if adapter is not None else CodexAdapter(data_dir=config.data_dir, model=config.codex_model)
        self.today = today or (lambda: datetime.now().astimezone().date().isoformat())
        # 웹 작업 진행 표시용 콜백(stage, processed, total). 실패해도 분석을 막지 않는다.
        self.progress = None

    def _report(self, stage, processed=None, total=None):
        if self.progress:
            try:
                self.progress(stage, processed, total)
            except Exception:
                pass

    def _reserve(self):
        # 실행 직전 예약하고 실패·재시도도 회수하지 않는다. 날짜는 로컬 날짜다.
        self.conn.execute('BEGIN IMMEDIATE')
        try:
            day = self.today()
            self.conn.execute('INSERT OR IGNORE INTO analysis_budget(day) VALUES(?)', (day,))
            row = self.conn.execute('UPDATE analysis_budget SET calls=calls+1 WHERE day=? AND calls<20', (day,))
            if not row.rowcount:
                raise BudgetExceeded('일일 CLI 호출 20회 소진: 다음 로컬 자정까지 보류')
            self.conn.commit()
        except BaseException:
            self.conn.rollback()
            raise

    def _call(self, stage, data, schema, ids=None):
        request = prompt(stage, data)
        if len(request.encode()) > 200*1024:
            raise ValueError('메타데이터 또는 합성 입력이 200 KiB를 초과했습니다')
        for attempt in range(2):
            self._reserve()
            try:
                raw = self.adapter.generate(request, schema)
                if ids is not None:
                    value = validate_chunk(raw, ids)
                else:
                    value = strict_json(raw)
                    validate(value, schema)
                # 모델 출력에도 비밀값을 저장하거나 합성 입력으로 넘기지 않는다.
                def clean(value):
                    if isinstance(value, str):
                        return sanitize(value, assignments=False)
                    if isinstance(value, list):
                        return [clean(item) for item in value]
                    if isinstance(value, dict):
                        return {key: clean(item) for key, item in value.items()}
                    return value
                value = clean(value)
                validate(value, schema)
                return value
            except (IsolationError, BudgetExceeded):
                raise
            except (ValueError, OSError, subprocess.SubprocessError):
                if attempt:
                    raise ValueError('분석 응답 검증 또는 CLI 실행 실패 (재시도 소진)') from None

    def run(self, snapshot_id=None, include_drafts=False):
        results = []
        # 별도 분석 잠금으로 계정 동시 호출 1개를 보장한다.
        with Collector(self.conn, self.config).lock("analysis.lock"):
            rows = self.conn.execute('SELECT s.*,p.repo_id,p.title,p.body,p.draft,p.state,p.base_sha AS current_base,'
                                     'p.head_sha AS current_head FROM snapshots s JOIN pull_requests p ON p.id=s.pr_id '
                                     'WHERE (? IS NOT NULL OR s.id IN (SELECT MAX(id) FROM snapshots GROUP BY pr_id)) '
                                     'AND (? IS NULL OR s.id=?) ORDER BY s.id', (snapshot_id, snapshot_id, snapshot_id)).fetchall()
            for row in rows:
                row = dict(row)
                if row['state'] != 'open' or (row['draft'] and not include_drafts):
                    continue
                if not analysis_allowed(self.conn, row['repo_id']):
                    continue
                latest_id = self.conn.execute(
                    'SELECT MAX(id) FROM snapshots WHERE pr_id=?', (row['pr_id'],)).fetchone()[0]
                if (row['base_sha'] != row['current_base'] or
                        row['head_sha'] != row['current_head'] or row['id'] != latest_id):
                    if snapshot_id is not None:
                        results.append(self._failure(row, '최신 SHA의 최신 스냅샷이 아닙니다', 'blocked', persist=False))
                    continue
                try:
                    results.append(self._analyze(row, retry_missing_intent=snapshot_id is not None))
                except Exception:
                    self.conn.rollback()
                    results.append(self._failure(row, '분석 실패: diff 캐시 또는 로컬 DB를 확인하세요'))
        return results

    def _failure(self, row, error, status='failed', persist=True):
        result = dict(snapshot_id=row['id'], status=status, error=error, items=[], intent=None)
        if not persist:
            return result
        with self.conn:
            # 존재 확인부터 저장까지 삭제와 경합하지 않도록 쓰기 잠금을 잡는다.
            self.conn.execute('BEGIN IMMEDIATE')
            if not self.conn.execute('SELECT 1 FROM snapshots WHERE id=?', (row['id'],)).fetchone():
                return dict(result, status='cancelled', error='분석 도중 스냅샷이 삭제되었습니다')
            previous = self.conn.execute(
                'SELECT output_json FROM analyses WHERE snapshot_id=? AND input_hash=? AND provider=? AND model=? AND cli_version=? AND prompt_version=? AND schema_version=? ORDER BY id DESC LIMIT 1',
                (row['id'], row.get('analysis_input_hash', ''), self.adapter.provider, self.adapter.model, self.adapter.cli_version, PROMPT_VERSION, SCHEMA_VERSION)).fetchone()
            if previous and previous['output_json'] == encode(result):
                return result
            self.conn.execute('INSERT INTO analyses(snapshot_id,input_hash,provider,model,cli_version,prompt_version,schema_version,status,output_json,error) VALUES(?,?,?,?,?,?,?,?,?,?)',
                              (row['id'], row.get('analysis_input_hash', ''), self.adapter.provider, self.adapter.model, self.adapter.cli_version, PROMPT_VERSION, SCHEMA_VERSION, status, encode(result), error))
        return result

    def _analyze(self, row, retry_missing_intent=False):
        source_coverage = json.loads(row['coverage'])
        source_coverage.setdefault('raw_input_hash', row['input_hash'])
        if source_coverage.get('status') in ('incomplete', 'inaccessible'):
            return self._failure(row, '수집이 불완전한 스냅샷', 'blocked')
        diff_path = Path(row['diff_path']).resolve()
        cache_dir = (self.config.data_dir / 'cache').resolve()
        if not diff_path.is_relative_to(cache_dir):
            raise ValueError('diff 캐시 경로가 데이터 디렉터리 밖입니다')
        with Collector(self.conn, self.config).lock():
            diff = diff_path.read_text()
        units, tests = parse_diff(diff, source_coverage.get('files', []))
        metadata = dict(title=sanitize(row['title']), body=sanitize(row['body']), tests=[sanitize(t) for t in tests])
        # 정제 전 1 MiB 한도는 후보 우선 순서로 배정한다.
        used = 0
        for unit in sorted(units, key=lambda u: (not bool(u['candidates']), int(u['change_id'][1:]))):
            if unit['excluded']:
                continue
            if used + unit['raw_bytes'] > 1024*1024:
                unit['omission_reason'] = '정제 전 diff 1 MiB 초과'
            else:
                used += unit['raw_bytes']
        eligible = [u for u in units if not u.get('omission_reason')]
        chunks = chunk_units(eligible, metadata)
        material = dict(metadata=metadata, units=units, policy='redact-v2/limits-v1/rules-v7', prompt_version=PROMPT_VERSION)
        input_hash = hashlib.sha256(encode(material).encode()).hexdigest()
        row['analysis_input_hash'] = input_hash
        adapter = self.adapter
        try:
            if chunks:
                adapter.verify()
        except (ValueError, OSError, subprocess.SubprocessError) as exc:
            return self._failure(row, str(exc) if isinstance(exc, IsolationError) else 'CLI 검증 실패',
                                 'blocked' if isinstance(exc, IsolationError) else 'failed')
        key = (row['id'], input_hash, adapter.provider, adapter.model, adapter.cli_version, PROMPT_VERSION, SCHEMA_VERSION)
        if adapter.model != 'unknown':
            cached = self.conn.execute("SELECT id,output_json FROM analyses WHERE snapshot_id=? AND input_hash=? AND provider=? AND model=? AND cli_version=? AND prompt_version=? AND schema_version=? AND status IN ('completed','partial') ORDER BY id DESC LIMIT 1", key).fetchone()
            if cached:
                cached_result = json.loads(cached['output_json'])
                if cached_result.get('intent') or not chunks or not retry_missing_intent:
                    if retry_missing_intent:
                        with self.conn:
                            self.conn.execute('BEGIN IMMEDIATE')
                            latest = self.conn.execute('SELECT id,status FROM analyses WHERE snapshot_id=? ORDER BY id DESC LIMIT 1', (row['id'],)).fetchone()
                            if latest and latest['id'] != cached['id'] and latest['status'] not in ('completed', 'partial'):
                                copied = self.conn.execute('INSERT INTO analyses(snapshot_id,input_hash,provider,model,cli_version,prompt_version,schema_version,status,intent,output_json,error) SELECT snapshot_id,input_hash,provider,model,cli_version,prompt_version,schema_version,status,intent,output_json,error FROM analyses WHERE id=?', (cached['id'],))
                                self.conn.execute('INSERT INTO analysis_items SELECT ?,change_id,importance,reason,risk_flags FROM analysis_items WHERE analysis_id=?', (copied.lastrowid, cached['id']))
                    return cached_result
        coverage = dict(total=len(units), analyzed=0, excluded=sum(u['excluded'] for u in units),
                        omitted=sum(bool(u.get('omission_reason')) for u in units),
                        redacted=sum(u.get('redacted', False) for u in units))
        result = dict(snapshot_id=row['id'], status='partial', intent=None, items=[], files=[],
                      units=units, uncertainties=[], coverage=coverage,
                      notice='민감 정보 탐지는 완전하지 않습니다',
                      logic_without_tests_reason='제공된 diff에서 관련 테스트 변경을 확인하지 못함',
                      metadata_exclusions=[key for key in ('title', 'body') if sanitize(row[key]) != (row[key] or '')], error=None)
        try:
            if chunks:
                collected = []
                uncertainties = []
                for index, chunk in enumerate(chunks):
                    self._report('모델 분석', index, len(chunks) + 1)
                    value = self._call('chunk', chunk, CHUNK_SCHEMA, {u['change_id'] for u in chunk['units']})
                    collected.extend(value['items'])
                    uncertainties.extend(value['uncertainties'])
                self._report('결과 합성', len(chunks), len(chunks) + 1)
                synthesis = self._call('synthesis', dict(metadata=metadata, items=collected, uncertainties=uncertainties), SYNTHESIS_SCHEMA)
                result.update(intent=synthesis['intent'], items=collected, uncertainties=uncertainties)
                coverage['analyzed'] = len(collected)
                result['status'] = 'partial' if coverage['excluded'] or coverage['omitted'] or coverage['redacted'] else 'completed'
        except (ValueError, OSError, subprocess.SubprocessError) as exc:
            result['status'] = 'deferred' if isinstance(exc, BudgetExceeded) else 'blocked' if isinstance(exc, IsolationError) else 'failed'
            result['error'] = str(exc) if isinstance(exc, (BudgetExceeded, IsolationError)) else '분석 실패: 입력 또는 응답 계약을 확인하세요'
        coverage['unanalysed'] = coverage['total'] - coverage['analyzed']
        by_id = {item['change_id']: item for item in result['items']}
        for unit in units:
            unit['status'] = 'analyzed' if unit['change_id'] in by_id else 'excluded' if unit['excluded'] else 'omitted' if unit.get('omission_reason') else 'unanalysed'
        ranks = {'low': 0, 'mid': 1, 'high': 2}
        for path in sorted({u['file_path'] for u in units}):
            members = [u for u in units if u['file_path'] == path]
            importance = [by_id[u['change_id']]['importance'] for u in members if u['change_id'] in by_id]
            result['files'].append(dict(file_path=path, importance=max(importance, key=ranks.get) if importance else None,
                                        candidates=sorted({flag for u in members for flag in u['candidates']}),
                                        partial=any(u['change_id'] not in by_id or u.get('redacted') for u in members)))
        with self.conn:
            self.conn.execute('BEGIN IMMEDIATE')
            current = self.conn.execute('SELECT coverage FROM snapshots WHERE id=?', (row['id'],)).fetchone()
            if current is None:
                return dict(result, status='cancelled', error='분석 도중 스냅샷이 삭제되었습니다')
            self.conn.execute('UPDATE snapshots SET input_hash=? WHERE id=?', (input_hash, row['id']))
            # 수집기가 갱신한 상태·오류를 보존하고 분석 통계만 합친다.
            stored_coverage = json.loads(current['coverage'])
            stored_coverage.setdefault('raw_input_hash', source_coverage['raw_input_hash'])
            stored_coverage['analysis'] = coverage
            self.conn.execute('UPDATE snapshots SET coverage=? WHERE id=?', (encode(stored_coverage), row['id']))
            # 기존 성공 분석을 남겨 실패 시에도 이전 결과를 볼 수 있게 한다.
            analysis_id = self.conn.execute('INSERT INTO analyses(snapshot_id,input_hash,provider,model,cli_version,prompt_version,schema_version,status,intent,output_json,error) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                                            (*key, result['status'], result['intent'], encode(result), result['error'])).lastrowid
            parents = {}
            for unit in units:
                path = unit['file_path']
                if path not in parents:
                    existing = self.conn.execute("SELECT id FROM changes WHERE snapshot_id=? AND file_path=? AND kind='file'", (row['id'], path)).fetchone()
                    parents[path] = existing[0] if existing else self.conn.execute("INSERT INTO changes(snapshot_id,file_path,kind) VALUES(?,?,'file')", (row['id'], path)).lastrowid
                existing = self.conn.execute('SELECT id FROM changes WHERE snapshot_id=? AND unit_key=?', (row['id'], unit['change_id'])).fetchone()
                change = existing[0] if existing else self.conn.execute('INSERT INTO changes(snapshot_id,file_path,kind,parent_id,old_range,new_range,binary,unit_key) VALUES(?,?,?,?,?,?,?,?)',
                                           (row['id'], path, unit['kind'], parents[path], unit['old_range'], unit['new_range'], unit['binary'], unit['change_id'])).lastrowid
                item = by_id.get(unit['change_id'])
                if item:
                    self.conn.execute('INSERT INTO analysis_items VALUES(?,?,?,?,?)', (analysis_id, change, item['importance'], item['reason'], encode(item['risk_flags'])))
            for file in result['files']:
                if file['importance']:
                    self.conn.execute('INSERT INTO analysis_items VALUES(?,?,?,?,?)', (analysis_id, parents[file['file_path']], file['importance'], '소속 분석 단위의 최고 중요도', '[]'))
        return result
