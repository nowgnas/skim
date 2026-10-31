"""기존 gh 인증으로 읽기 전용 수집과 프로세스 내 작업 예약을 수행한다."""
import hashlib
import json
import re
import subprocess
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlencode

from .config import private_dir, private_file


class CollectionError(ValueError):
    """원문 CLI 출력을 노출하지 않는 수집 오류."""


class Deferred(CollectionError):
    def __init__(self, retry_at):
        self.retry_at = retry_at
        super().__init__(f'GitHub 요청을 {datetime.fromtimestamp(retry_at, timezone.utc).isoformat()} 이후 재개합니다')


class GitHub:
    def __init__(self, runner=None, clock=time.time):
        self.runner = runner or subprocess.run
        self.clock = clock
        self.not_before = 0

    def get(self, endpoint, *, diff=False):
        if self.clock() < self.not_before:
            raise Deferred(self.not_before)
        command = ['gh', 'api', '--hostname', 'github.com', '--method', 'GET', '--include', endpoint]
        if diff:
            command += ['-H', 'Accept: application/vnd.github.diff']
        try:
            result = self.runner(command, capture_output=True, text=True, timeout=120)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise CollectionError('gh 실행 실패: 설치와 로그인을 확인하세요') from exc
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
                delay = parsedate_to_datetime(headers['retry-after']).timestamp() - self.clock()
        if headers.get('x-ratelimit-remaining') == '0':
            delay = max(delay, float(headers.get('x-ratelimit-reset', self.clock() + 60)) - self.clock())
        if status == 429 or (status == 403 and delay):
            delay = max(delay, 60)
        if delay > 0:
            self.not_before = self.clock() + delay
        if result.returncode or status >= 400:
            if self.not_before > self.clock():
                raise Deferred(self.not_before)
            raise CollectionError('GitHub 조회 실패: gh 인증과 저장소 접근 권한을 확인하세요')
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
            if len(batch) < 100:
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

    def collect(self):
        gh, conn = self.gh, self.conn
        login = gh.get('user')['login']
        discovered = gh.pages('user/repos', affiliation='owner,organization_member,collaborator')
        candidates = []
        for repo in discovered:
            owned = repo['owner']['login'].lower() == login.lower()
            if owned or repo.get('permissions', {}).get('admin'):
                candidates.append((*repo_parts(repo['full_name']), 'owned' if owned else 'managed'))
        targets = {}
        for row in conn.execute('SELECT * FROM repositories WHERE tracked=1 AND enabled=1'):
            full = row['owner'] + '/' + row['name']
            for pr in gh.pages(f'repos/{full}/pulls', state='open'):
                targets.setdefault((full.lower(), pr['number']), set()).add('repository')
        queries = [f'is:open is:pr review-requested:{login}']
        for team in gh.pages('user/teams'):
            queries.append(f"is:open is:pr team-review-requested:{team['organization']['login']}/{team['slug']}")
        for query in queries:
            for item in gh.pages('search/issues', q=query):
                full = '/'.join(item['repository_url'].split('/')[-2:])
                repo_parts(full)
                targets.setdefault((full.lower(), item['number']), set()).add('review_request')
        # 목록에서 사라진 PR도 상세 조회하여 닫힘·머지를 구분한다.
        old = {(r['owner'].lower() + '/' + r['name'].lower(), r['number']) for r in conn.execute(
            'SELECT r.owner,r.name,p.number FROM pull_requests p JOIN repositories r ON p.repo_id=r.id')}
        collected = []
        for (full, number) in sorted(set(targets) | old):
            endpoint = f'repos/{full}/pulls/{int(number)}'
            for attempt in range(2):
                pr = gh.get(endpoint)
                state = 'merged' if pr.get('merged_at') else pr['state']
                snapshot = None
                if state == 'open' and (full, number) in targets:
                    files = gh.pages(endpoint + '/files')
                    if len(files) != pr.get('changed_files', len(files)):
                        raise CollectionError('GitHub 파일 목록이 불완전하여 기존 결과를 보존합니다')
                    diff = gh.get(endpoint, diff=True)
                    after = gh.get(endpoint)
                    if (pr['head']['sha'], pr['base']['sha']) != (after['head']['sha'], after['base']['sha']) or after['state'] != 'open':
                        if attempt == 0:
                            continue
                        raise CollectionError('수집 도중 PR이 변경되어 기존 스냅샷을 보존합니다')
                    snapshot = (files, diff)
                collected.append((full, number, pr, state, snapshot))
                break
        now = datetime.now(timezone.utc).isoformat()
        cache = private_dir(self.config.data_dir / 'cache' / 'diffs')
        # 모든 원격 조회가 성공한 뒤 한 트랜잭션으로 출처와 스냅샷을 교체한다.
        with conn:
            conn.executemany('INSERT INTO repositories(owner,name,source) VALUES(?,?,?) ON CONFLICT(owner,name) DO UPDATE SET source=excluded.source WHERE repositories.tracked=0', candidates)
            conn.execute('DELETE FROM pr_sources')
            for full, number, pr, state, snapshot in collected:
                conn.execute('INSERT INTO repositories(owner,name) VALUES(?,?) ON CONFLICT(owner,name) DO NOTHING', repo_parts(full))
                repo_id = conn.execute('SELECT id FROM repositories WHERE owner=? AND name=?', repo_parts(full)).fetchone()[0]
                conn.execute('INSERT INTO pull_requests(repo_id,number,url,title,body,author,state,draft,head_branch,base_sha,head_sha,updated_at,last_seen_at) '
                             'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(repo_id,number) DO UPDATE SET '
                             'url=excluded.url,title=excluded.title,body=excluded.body,author=excluded.author,state=excluded.state,draft=excluded.draft,'
                             'head_branch=excluded.head_branch,base_sha=excluded.base_sha,head_sha=excluded.head_sha,updated_at=excluded.updated_at,last_seen_at=excluded.last_seen_at',
                             (repo_id, number, f'https://github.com/{full}/pull/{number}', pr['title'], pr.get('body') or '', pr['user']['login'], state,
                              int(pr.get('draft', False)), pr['head']['ref'], pr['base']['sha'], pr['head']['sha'], pr['updated_at'], now))
                pr_id = conn.execute('SELECT id FROM pull_requests WHERE repo_id=? AND number=?', (repo_id, number)).fetchone()[0]
                for source in targets.get((full, number), ()):
                    if state == 'open':
                        conn.execute('INSERT INTO pr_sources VALUES(?,?)', (pr_id, source))
                if snapshot:
                    files, diff = snapshot
                    diff_hash = hashlib.sha256(diff.encode()).hexdigest()
                    material = dict(title=pr['title'], body=pr.get('body') or '', files=files, diff=diff, policy='raw-v1')
                    input_hash = hashlib.sha256(json.dumps(material, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
                    path = cache / (diff_hash + '.diff')
                    private_file(path)
                    path.write_text(diff, encoding='utf-8')
                    existing = conn.execute('SELECT 1 FROM snapshots WHERE pr_id=? AND base_sha=? AND head_sha=? AND input_hash=?',
                                            (pr_id, pr['base']['sha'], pr['head']['sha'], input_hash)).fetchone()
                    if not existing:
                        coverage = json.dumps(dict(files=files, status='collected', policy='raw-v1'), ensure_ascii=False)
                        conn.execute('INSERT INTO snapshots(pr_id,base_sha,head_sha,diff_hash,input_hash,diff_path,coverage) VALUES(?,?,?,?,?,?,?)',
                                     (pr_id, pr['base']['sha'], pr['head']['sha'], diff_hash, input_hash, str(path), coverage))
            conn.execute("DELETE FROM pull_requests WHERE state='open' AND NOT EXISTS (SELECT 1 FROM pr_sources WHERE pr_id=pull_requests.id)")
            conn.execute('UPDATE repositories SET last_synced_at=? WHERE tracked=1 AND enabled=1', (now,))
        self.clean_cache()
        return len(targets)

    def clean_cache(self):
        live = {r[0] for r in self.conn.execute('SELECT diff_path FROM snapshots WHERE diff_path IS NOT NULL')}
        for path in (self.config.data_dir / 'cache' / 'diffs').glob('*.diff'):
            if str(path) not in live:
                path.unlink()


class CollectionScheduler:
    """웹 루프에서 tick을 호출한다. 수동 갱신도 동일한 SQLite 큐를 쓴다."""
    def __init__(self, conn, interval=300, clock=time.time):
        self.conn, self.interval, self.clock = conn, interval, clock
        self.next_poll = 0

    def request(self):
        with self.conn:
            self.conn.execute("INSERT OR IGNORE INTO jobs(type,target_id) VALUES('collect',0)")

    def tick(self, collector):
        now = self.clock()
        if now >= self.next_poll:
            self.request()
            self.next_poll = now + self.interval
        stamp = datetime.fromtimestamp(now, timezone.utc).isoformat()
        with self.conn:
            row = self.conn.execute("UPDATE jobs SET status='running',attempts=attempts+1 WHERE id=(SELECT id FROM jobs WHERE type='collect' AND status='pending' AND (next_run_at IS NULL OR next_run_at<=?) ORDER BY id LIMIT 1) RETURNING id,attempts", (stamp,)).fetchone()
        if row is None:
            return False
        try:
            collector.collect()
        except (CollectionError, OSError, ValueError) as exc:
            retry = exc.retry_at if isinstance(exc, Deferred) else now + min(3600, 30 * 2 ** min(row['attempts'] - 1, 7))
            with self.conn:
                self.conn.execute("UPDATE jobs SET status='pending',next_run_at=?,last_error=? WHERE id=?",
                                  (datetime.fromtimestamp(retry, timezone.utc).isoformat(), '수집 실패: 인증·연결 또는 요청 제한을 확인하세요', row['id']))
        else:
            with self.conn:
                self.conn.execute("UPDATE jobs SET status='completed',last_error=NULL WHERE id=?", (row['id'],))
        return True
