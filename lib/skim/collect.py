"""기존 gh 인증으로 읽기 전용 수집과 프로세스 내 작업 예약을 수행한다."""
import fcntl
import os
import tempfile
import threading
from contextlib import contextmanager
import hashlib
import json
import re
import subprocess
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlencode

from .config import private_dir, private_file
from .hosts import hostname


class CollectionError(ValueError):
    """원문 CLI 출력을 노출하지 않는 수집 오류."""


def error_message(exc):
    # 예상하지 못한 예외의 원문에는 응답이나 비밀값이 포함될 수 있다.
    if isinstance(exc, CollectionError):
        return str(exc)
    if isinstance(exc, OSError):
        return '네트워크·파일 접근 실패: 연결과 로컬 저장소를 확인하세요'
    return '수집 내부 오류: 응답 형식과 로컬 DB를 확인하세요'


class Inaccessible(CollectionError):
    """삭제되었거나 현재 인증으로 접근할 수 없는 PR."""


class Oversized(CollectionError):
    """GitHub가 전체 변경 내용을 제공할 수 없는 PR."""


class Deferred(CollectionError):
    def __init__(self, retry_at):
        self.retry_at = retry_at
        super().__init__(f'GitHub 요청 제한: 요청을 {datetime.fromtimestamp(retry_at, timezone.utc).isoformat()} 이후 재개합니다')


class GitHub:
    def __init__(self, runner=None, clock=time.time, host="github.com"):
        self.host = hostname(host)
        self.runner = runner or subprocess.run
        self.clock = clock
        self.not_before = 0

    def get(self, endpoint, *, diff=False):
        if self.clock() < self.not_before:
            raise Deferred(self.not_before)
        command = ['gh', 'api', '--hostname', hostname(self.host), '--method', 'GET', '--include', endpoint]
        if diff:
            command += ['-H', 'Accept: application/vnd.github.diff']
        try:
            result = self.runner(command, capture_output=True, text=True, timeout=120)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise CollectionError('gh 실행·연결 실패: 설치와 네트워크 연결을 확인하세요') from exc
        raw = result.stdout
        headers = {}
        status = 200
        if raw.startswith('HTTP/'):
            separator = '\r\n\r\n' if '\r\n\r\n' in raw else '\n\n'
            block, _, raw = raw.partition(separator)
            status = int(block.splitlines()[0].split()[1])
            headers = {k.lower(): v.strip() for line in block.splitlines()[1:]
                       if ':' in line for k, v in [line.split(':', 1)]}
        delay = 0
        if 'retry-after' in headers:
            try:
                delay = float(headers['retry-after'])
            except ValueError:
                try:
                    date = parsedate_to_datetime(headers['retry-after'])
                    delay = date.replace(tzinfo=date.tzinfo or timezone.utc).timestamp() - self.clock()
                except (TypeError, ValueError, OverflowError):
                    delay = 0
        if headers.get('x-ratelimit-remaining') == '0':
            delay = max(delay, float(headers.get('x-ratelimit-reset', self.clock() + 60)) - self.clock())
        if status == 429 or (status == 403 and delay):
            delay = max(delay, 60)
        if delay > 0:
            self.not_before = self.clock() + delay
        if result.returncode or status >= 400:
            if self.not_before > self.clock():
                raise Deferred(self.not_before)
            if diff and status in (406, 413):
                raise Oversized('GitHub diff 크기 한도 초과: 미분석')
            if status == 404:
                raise Inaccessible('GitHub 접근 불가: 저장소 삭제 또는 접근 권한을 확인하세요')
            if status in (401, 403):
                raise CollectionError('GitHub 인증·권한 실패: gh auth login과 저장소 권한을 확인하세요')
            raise CollectionError('GitHub 조회 실패: 네트워크 연결과 GitHub 상태를 확인하세요')
        return raw if diff else json.loads(raw)

    def pages(self, endpoint, **params):
        items = []
        for page in range(1, 10001):
            payload = self.get(endpoint + '?' + urlencode(dict(params, per_page=100, page=page)))
            if isinstance(payload, dict):
                if payload.get('incomplete_results') or payload.get('total_count', 0) > 1000:
                    raise CollectionError('리뷰 요청 검색이 불완전하여 기존 결과를 보존합니다')
                batch = payload['items']
            else:
                batch = payload
            items.extend(batch)
            if len(batch) < 100 or (endpoint.endswith('/files') and page == 30):
                return items
        raise CollectionError('페이지 한도를 초과했습니다')


def repo_parts(full_name):
    if not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?/[A-Za-z0-9_.-]+', full_name):
        raise CollectionError('잘못된 GitHub 저장소 이름입니다')
    owner, name = full_name.split('/')
    if name in ('.', '..'):
        raise CollectionError('잘못된 GitHub 저장소 이름입니다')
    return owner, name


class Collector:
    def __init__(self, conn, config, runner=None, clock=time.time):
        self.conn, self.config = conn, config
        self.gh = GitHub(runner, clock)
        self._host_limits = {}
        # 웹 작업 진행 표시용 콜백(stage, processed, total). 실패해도 수집을 막지 않는다.
        self.progress = None
        self._mutex = threading.RLock()
        self._lock_depth = {}

    @contextmanager
    def lock(self, name="collection.lock"):
        # 수집·추적 해제·캐시 정리는 같은 프로세스 간 잠금을 공유한다.
        with self._mutex:
            if self._lock_depth.get(name):
                self._lock_depth[name] += 1
                try:
                    yield
                finally:
                    self._lock_depth[name] -= 1
                return
            path = self.config.data_dir / name
            private_file(path)
            with path.open('r+') as handle:
                fcntl.flock(handle, fcntl.LOCK_EX)
                self._lock_depth[name] = 1
                try:
                    yield
                finally:
                    del self._lock_depth[name]
                    fcntl.flock(handle, fcntl.LOCK_UN)

    def collect(self):
        with self.lock():
            total = 0
            errors = []
            for row in self.conn.execute('SELECT host FROM github_hosts ORDER BY host').fetchall():
                self.gh.host = row['host']
                self.gh.not_before = self._host_limits.get(row['host'], 0)
                try:
                    total += self._collect()
                except Exception as exc:
                    errors.append((row['host'], exc))
                finally:
                    self._host_limits[row['host']] = self.gh.not_before
            if errors:
                message = '; '.join(host + ': ' + error_message(exc) for host, exc in errors)
                first = errors[0][1]
                if isinstance(first, CollectionError):
                    first.args = (message,)
                    raise first
                if isinstance(first, OSError):
                    raise OSError(message) from first
                raise CollectionError(message) from first
            return total

    def _collect(self):
        gh, conn = self.gh, self.conn
        host = gh.host
        login = gh.get('user')['login']
        discovered = gh.pages('user/repos', affiliation='owner,organization_member,collaborator')
        candidates = []
        for repo in discovered:
            owned = repo['owner']['login'].lower() == login.lower()
            if owned or repo.get('permissions', {}).get('admin'):
                candidates.append((*repo_parts(repo['full_name']), 'owned' if owned else 'managed'))
        targets = {}
        names = {}
        listing_failures = []
        for row in conn.execute('SELECT * FROM repositories WHERE host=? AND tracked=1 AND enabled=1', (host,)):
            full = row['owner'] + '/' + row['name']
            names[full.lower()] = full
            try:
                for pr in gh.pages(f'repos/{full}/pulls', state='open'):
                    targets.setdefault((full.lower(), pr['number']), set()).add('repository')
            except Exception as exc:
                listing_failures.append(exc)
                # 목록 조회 실패를 PR 삭제·출처 해제로 오인하지 않는다.
                for previous in conn.execute("SELECT p.number FROM pull_requests p JOIN pr_sources s ON s.pr_id=p.id WHERE p.repo_id=? AND p.state='open' AND s.source='repository'", (row['id'],)):
                    targets.setdefault((full.lower(), previous['number']), set()).add('repository')
        queries = [f'is:open is:pr review-requested:{login}']
        for team in gh.pages('user/teams'):
            queries.append(f"is:open is:pr team-review-requested:{team['organization']['login']}/{team['slug']}")
        for query in queries:
            for item in gh.pages('search/issues', q=query):
                full = '/'.join(item['repository_url'].split('/')[-2:])
                repo_parts(full)
                names.setdefault(full.lower(), full)
                targets.setdefault((full.lower(), item['number']), set()).add('review_request')
        # 목록에서 사라진 PR도 상세 조회하여 닫힘·머지를 구분한다.
        old = {(r['owner'].lower() + '/' + r['name'].lower(), r['number']) for r in conn.execute(
            "SELECT r.owner,r.name,p.number FROM pull_requests p JOIN repositories r ON p.repo_id=r.id WHERE r.host=? AND p.state='open'", (host,))}
        for r in conn.execute("SELECT owner,name FROM repositories WHERE host=?", (host,)):
            names.setdefault((r['owner'] + '/' + r['name']).lower(), r['owner'] + '/' + r['name'])
        collected = []
        failures = []
        pending = sorted(set(targets) | old)
        for index, (full, number) in enumerate(pending):
            self._report('PR 수집', index, len(pending))
            endpoint = f'repos/{full}/pulls/{int(number)}'
            try:
                for attempt in range(2):
                    pr = gh.get(endpoint)
                    # 저장 단계까지 잘못된 응답이 흘러가 다른 PR을 막지 않게 검증한다.
                    for value in (pr['title'], pr['user']['login'], pr['head']['ref'],
                                  pr['head']['sha'], pr['base']['sha'], pr['updated_at']):
                        if not isinstance(value, str):
                            raise CollectionError('GitHub PR 응답 형식이 잘못되었습니다')
                    if pr['state'] not in ('open', 'closed'):
                        raise CollectionError('GitHub PR 상태가 잘못되었습니다')
                    state = 'merged' if pr.get('merged_at') else pr['state']
                    snapshot = None
                    if state == 'open' and (full, number) in targets:
                        oversized = None
                        try:
                            files = gh.pages(endpoint + '/files')
                            if len(files) != pr.get('changed_files', len(files)):
                                raise Oversized('GitHub 파일 목록 한도 초과: 미분석')
                            diff = gh.get(endpoint, diff=True)
                        except Oversized as exc:
                            oversized = exc
                        after = gh.get(endpoint)
                        if (pr['head']['sha'], pr['base']['sha']) != (after['head']['sha'], after['base']['sha']) or after['state'] != 'open':
                            if attempt == 0:
                                continue
                            raise CollectionError('수집 도중 PR이 변경되어 기존 스냅샷을 보존합니다')
                        if oversized is not None:
                            raise oversized
                        snapshot = (files, diff)
                    collected.append((full, number, pr, state, snapshot))
                    break
            except Exception as exc:
                failures.append((full, number, exc))
                if isinstance(exc, Oversized):
                    collected.append((full, number, pr, state, ([], '')))
        self._report('저장', len(pending), len(pending))
        now = datetime.now(timezone.utc).isoformat()
        cache = private_dir(self.config.data_dir / 'cache' / 'diffs')
        # 크기 초과는 미분석 스냅샷으로 남기고 접근 불가는 출처를 제거한다.
        with conn:
            conn.execute("UPDATE repositories SET source='responsible' WHERE host=? AND tracked=0 AND source IN ('owned','managed')", (host,))
            conn.executemany('INSERT INTO repositories(owner,name,source,host) VALUES(?,?,?,?) ON CONFLICT(host,owner,name) DO UPDATE SET source=excluded.source WHERE repositories.tracked=0', [(*c, host) for c in candidates])
            for key, number, pr, state, snapshot in collected:
                full = names[key]
                conn.execute('INSERT INTO repositories(owner,name,host) VALUES(?,?,?) ON CONFLICT(host,owner,name) DO NOTHING', (*repo_parts(full), host))
                repo_id = conn.execute('SELECT id FROM repositories WHERE host=? AND owner=? AND name=?', (host, *repo_parts(full))).fetchone()[0]
                conn.execute('INSERT INTO pull_requests(repo_id,number,url,title,body,author,state,draft,head_branch,base_sha,head_sha,updated_at,last_seen_at) '
                             'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(repo_id,number) DO UPDATE SET '
                             'url=excluded.url,title=excluded.title,body=excluded.body,author=excluded.author,state=excluded.state,draft=excluded.draft,'
                             'head_branch=excluded.head_branch,base_sha=excluded.base_sha,head_sha=excluded.head_sha,updated_at=excluded.updated_at,last_seen_at=excluded.last_seen_at',
                             (repo_id, number, f'https://{host}/{full}/pull/{number}', pr['title'], pr.get('body') or '', pr['user']['login'], state,
                              int(pr.get('draft', False)), pr['head']['ref'], pr['base']['sha'], pr['head']['sha'], pr['updated_at'], now))
                pr_id = conn.execute('SELECT id FROM pull_requests WHERE repo_id=? AND number=?', (repo_id, number)).fetchone()[0]
                conn.execute('DELETE FROM pr_sources WHERE pr_id=?', (pr_id,))
                for source in targets.get((key, number), ()):
                    if state == 'open':
                        conn.execute('INSERT INTO pr_sources VALUES(?,?)', (pr_id, source))
                if snapshot:
                    files, diff = snapshot
                    diff_hash = hashlib.sha256(diff.encode()).hexdigest()
                    material = dict(title=pr['title'], body=pr.get('body') or '', files=files, diff=diff, policy='raw-v1')
                    input_hash = hashlib.sha256(json.dumps(material, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
                    path = cache / (diff_hash + '.diff')
                    fd, temporary = tempfile.mkstemp(dir=cache, suffix='.tmp')
                    try:
                        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
                            handle.write(diff)
                        os.replace(temporary, path)
                    finally:
                        if os.path.exists(temporary):
                            os.unlink(temporary)
                    existing = conn.execute("SELECT 1 FROM snapshots WHERE pr_id=? AND base_sha=? AND head_sha=? AND (input_hash=? OR json_extract(coverage,'$.raw_input_hash')=?)",
                                            (pr_id, pr['base']['sha'], pr['head']['sha'], input_hash, input_hash)).fetchone()
                    if existing:
                        for row in conn.execute("SELECT id,coverage FROM snapshots WHERE pr_id=? AND base_sha=? AND head_sha=? AND (input_hash=? OR json_extract(coverage,'$.raw_input_hash')=?)",
                                                (pr_id, pr['base']['sha'], pr['head']['sha'], input_hash, input_hash)):
                            coverage = json.loads(row['coverage'])
                            coverage.pop('collection_error', None)
                            coverage['status'] = 'collected'
                            conn.execute('UPDATE snapshots SET coverage=? WHERE id=?', (json.dumps(coverage, ensure_ascii=False), row['id']))
                    else:
                        coverage = json.dumps(dict(files=[{k: v for k, v in f.items() if k in ('filename', 'status', 'previous_filename')} for f in files], total_files=pr.get('changed_files', len(files)), status='collected', policy='raw-v1'), ensure_ascii=False)
                        conn.execute('INSERT INTO snapshots(pr_id,base_sha,head_sha,diff_hash,input_hash,diff_path,coverage) VALUES(?,?,?,?,?,?,?)',
                                     (pr_id, pr['base']['sha'], pr['head']['sha'], diff_hash, input_hash, str(path), coverage))
            for full, number, exc in failures:
                row = conn.execute('SELECT p.id FROM pull_requests p JOIN repositories r ON p.repo_id=r.id WHERE r.host=? AND r.owner=? AND r.name=? AND p.number=?',
                                   (host, *repo_parts(full), number)).fetchone()
                if row:
                    if isinstance(exc, Inaccessible):
                        conn.execute('DELETE FROM pr_sources WHERE pr_id=?', (row['id'],))
                    snapshot_query = 'SELECT id,coverage FROM snapshots WHERE pr_id=?'
                    if isinstance(exc, Oversized):
                        snapshot_query += ' AND id=(SELECT MAX(id) FROM snapshots WHERE pr_id=?)'
                    params = (row['id'], row['id']) if isinstance(exc, Oversized) else (row['id'],)
                    for snapshot in conn.execute(snapshot_query, params):
                        coverage = json.loads(snapshot['coverage'])
                        coverage.update(status='inaccessible' if isinstance(exc, Inaccessible) else 'incomplete', collection_error=error_message(exc))
                        if isinstance(exc, Oversized):
                            coverage.update(omitted=coverage.get('total_files', 0), omission_reason=error_message(exc))
                        conn.execute('UPDATE snapshots SET coverage=? WHERE id=?', (json.dumps(coverage, ensure_ascii=False), snapshot['id']))
            conn.execute("DELETE FROM pull_requests WHERE repo_id IN (SELECT id FROM repositories WHERE host=?) AND state='open' AND NOT EXISTS (SELECT 1 FROM pr_sources WHERE pr_id=pull_requests.id)", (host,))
            conn.execute('UPDATE repositories SET last_synced_at=? WHERE host=? AND tracked=1 AND enabled=1', (now, host))
        self.clean_cache()
        deferred = [exc.retry_at for exc in listing_failures + [f[2] for f in failures] if isinstance(exc, Deferred)]
        if deferred:
            raise Deferred(max(deferred))
        if listing_failures:
            error = next((exc for exc in listing_failures if not isinstance(exc, Inaccessible)), listing_failures[0])
            raise CollectionError('저장소 목록 수집 실패: ' + error_message(error))
        errors = [(full, number, exc) for full, number, exc in failures
                  if not isinstance(exc, (Inaccessible, Oversized))]
        if errors:
            full, number, exc = errors[0]
            raise CollectionError(f'PR 수집 실패 ({len(errors)}건, {full}#{number}): ' + error_message(exc))
        return sum(snapshot is not None and not any(f == full and n == number for f, n, _ in failures)
                   for full, number, pr, state, snapshot in collected)

    def _report(self, stage, processed, total):
        if self.progress:
            try:
                self.progress(stage, processed, total)
            except Exception:
                pass

    def clean_cache(self):
        with self.lock():
            self._clean_cache()

    def _clean_cache(self):
        live = {r[0] for r in self.conn.execute('SELECT diff_path FROM snapshots WHERE diff_path IS NOT NULL')}
        for path in (self.config.data_dir / 'cache' / 'diffs').glob('*.diff'):
            if str(path) not in live:
                path.unlink()


class CollectionScheduler:
    """웹 루프에서 tick을 호출한다. 수동 갱신도 동일한 SQLite 큐를 쓴다."""
    def __init__(self, conn, interval=300, clock=time.time):
        self.conn, self.interval, self.clock = conn, interval, clock
        self.next_poll = 0
        # 마지막 tick이 수집에 성공했는지. 수집 후 자동 분석 등록에 사용한다.
        self.last_ok = False

    def request(self, manual=True):
        with self.conn:
            if manual:
                self.conn.execute("UPDATE jobs SET next_run_at=retry_not_before WHERE type='collect' AND status='pending'")
                self.conn.execute("UPDATE jobs SET refresh_requested=1 WHERE type='collect' AND status='running'")
            self.conn.execute("INSERT OR IGNORE INTO jobs(type,target_id) VALUES('collect',0)")

    def tick(self, collector):
        with collector.lock():
            # 잠금을 확보했으므로 이전 running 작업을 실행하는 수집기는 없다.
            # 프로세스 중단·DB 기록 실패 후에도 다음 tick에서 복구한다.
            with self.conn:
                self.conn.execute("UPDATE jobs SET status='pending',next_run_at=retry_not_before,last_error='중단된 수집 작업을 복구했습니다',stage='재시도 대기' WHERE type='collect' AND status='running'")
            return self._tick(collector)

    def _tick(self, collector):
        now = self.clock()
        if now >= self.next_poll:
            self.request(manual=False)
            self.next_poll = now + self.interval
        stamp = datetime.fromtimestamp(now, timezone.utc).isoformat()
        with self.conn:
            self.conn.execute('BEGIN IMMEDIATE')
            row = self.conn.execute("SELECT id,attempts FROM jobs WHERE type='collect' AND status='pending' AND (next_run_at IS NULL OR next_run_at<=?) ORDER BY id LIMIT 1", (stamp,)).fetchone()
            if row is not None:
                self.conn.execute("UPDATE jobs SET status='running',attempts=attempts+1,started_at=?,finished_at=NULL,stage='수집 중',processed=NULL,total=NULL WHERE id=?", (stamp, row['id']))
        self.last_ok = False
        if row is None:
            return False
        try:
            collector.collect()
        except Exception as exc:
            # 점유 전 attempts이므로 첫 실패의 지연은 30초다.
            retry = exc.retry_at if isinstance(exc, Deferred) else now + min(3600, 30 * 2 ** min(row['attempts'], 7))
            retry_stamp = datetime.fromtimestamp(retry, timezone.utc).isoformat()
            with self.conn:
                self.conn.execute('BEGIN IMMEDIATE')
                followup = self.conn.execute('SELECT refresh_requested FROM jobs WHERE id=?', (row['id'],)).fetchone()[0]
                self.conn.execute("UPDATE jobs SET status='pending',next_run_at=?,last_error=?,retry_not_before=?,refresh_requested=0,stage='재시도 대기' WHERE id=?",
                                  (None if followup and not isinstance(exc, Deferred) else retry_stamp,
                                   error_message(exc), retry_stamp if isinstance(exc, Deferred) else None, row['id']))
        else:
            self.last_ok = True
            done = datetime.fromtimestamp(self.clock(), timezone.utc).isoformat()
            with self.conn:
                self.conn.execute('BEGIN IMMEDIATE')
                followup = self.conn.execute('SELECT refresh_requested FROM jobs WHERE id=?', (row['id'],)).fetchone()[0]
                self.conn.execute("UPDATE jobs SET status='completed',last_error=NULL,refresh_requested=0,retry_not_before=NULL,stage='완료',finished_at=?,result='수집 완료' WHERE id=?", (done, row['id']))
                if followup:
                    self.request()
        return True
