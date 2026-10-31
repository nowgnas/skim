"""루프백 전용 대시보드와 일회용 진입 인증."""
import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets
import socket
import sqlite3
import threading
import time
from contextlib import closing
from html import escape
from http.cookies import SimpleCookie, CookieError
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .db import connect
from .collect import Collector, CollectionScheduler
from .analyze.units import parse_diff
from .sessio import session_link

CSP = "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; base-uri 'none'; object-src 'none'; frame-ancestors 'none'; form-action 'self'"
STATIC = Path(__file__).with_name('web_static')
RANK = {'high': 3, 'mid': 2, 'low': 1}


def constant_equal(left, right):
    return hmac.compare_digest(left.encode('utf-8'), right.encode('utf-8'))


def esc(value):
    return escape(str(value if value is not None else ''), quote=True)


def page(title, content):
    return ('<!doctype html><html lang="ko"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{esc(title)} · skim</title><link rel="stylesheet" href="/static/app.css">'
            '<header><a class="brand" href="/">skim</a><span>PR 검토 대시보드</span>'
            '<a href="/settings">설정 · 상태</a></header><main>' + content + '</main></html>')


class Auth:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.lock = threading.Lock()
        self.tokens = {}
        self.sessions = {}
        self.cookie = 'skim_' + secrets.token_hex(8)

    def issue(self):
        token = secrets.token_urlsafe(32)
        with self.lock:
            now = self.clock()
            self.tokens = {key: expiry for key, expiry in self.tokens.items() if expiry > now}
            self.tokens[hashlib.sha256(token.encode()).digest()] = now + 60
        return token

    def redeem(self, token):
        digest = hashlib.sha256(token.encode()).digest()
        with self.lock:
            found = next((key for key in self.tokens if hmac.compare_digest(key, digest)), None)
            expiry = self.tokens.pop(found, 0)
            if expiry <= self.clock():
                return None
            session, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            self.sessions[session] = csrf
            return session

    def csrf(self, cookie):
        try:
            jar = SimpleCookie()
            jar.load(cookie or '')
            value = jar[self.cookie].value if self.cookie in jar else ''
        except CookieError:
            return None
        with self.lock:
            return next((csrf for key, csrf in self.sessions.items() if constant_equal(key, value)), None)


def read_diff(config, snapshot):
    """캐시 루트 밖의 경로와 심볼릭 링크 대상은 읽지 않는다."""
    if not snapshot or not snapshot['diff_path']:
        return ''
    try:
        path = Path(snapshot['diff_path']).resolve()
        root = (config.data_dir / 'cache').resolve()
        if not path.is_relative_to(root) or not path.is_file() or path.stat().st_size > 1024 * 1024:
            return ''
        return path.read_text(encoding='utf-8', errors='replace')
    except OSError:
        return ''


def load_prs(conn, config):
    rows = []
    for raw in conn.execute("SELECT p.*,r.owner,r.name,EXISTS(SELECT 1 FROM pr_sources WHERE pr_id=p.id AND source='review_request') AS review FROM pull_requests p JOIN repositories r ON r.id=p.repo_id WHERE p.state='open'"):
        pr = dict(raw)
        snapshot = conn.execute('SELECT * FROM snapshots WHERE pr_id=? ORDER BY id DESC LIMIT 1', (pr['id'],)).fetchone()
        analysis = conn.execute('SELECT a.*,s.head_sha,s.base_sha FROM analyses a JOIN snapshots s ON s.id=a.snapshot_id WHERE s.pr_id=? ORDER BY s.id DESC,a.id DESC LIMIT 1', (pr['id'],)).fetchone()
        result = json.loads(analysis['output_json'] or '{}') if analysis else {}
        units = result.get('units', [])
        if not units and snapshot:
            coverage = json.loads(snapshot['coverage'])
            units, _ = parse_diff(read_diff(config, snapshot), coverage.get('files', []))
        items = {item['change_id']: item for item in result.get('items', [])}
        if snapshot and analysis and snapshot['id'] != analysis['snapshot_id']:
            units, _ = parse_diff(read_diff(config, snapshot), json.loads(snapshot['coverage']).get('files', []))
            items = {}
        pr.update(snapshot=snapshot, analysis=analysis, result=result, units=units, items=items,
                  status=analysis['status'] if analysis else 'unanalyzed',
                  stale=bool(analysis and (analysis['head_sha'] != pr['head_sha'] or analysis['base_sha'] != pr['base_sha'] or (snapshot and snapshot['id'] != analysis['snapshot_id']))))
        pr['importance'] = max((item['importance'] for item in items.values()), key=lambda x: RANK.get(x, 0), default='unanalyzed')
        pr['flags'] = sorted({flag for unit in units for flag in unit.get('candidates', [])} | {flag for item in items.values() for flag in item.get('risk_flags', [])})
        pr['unanalysed'] = not units or any(u['change_id'] not in items for u in units)
        rows.append(pr)
    return sorted(rows, key=lambda p: (-bool(p['flags']), -p['unanalysed'], -RANK.get(p['importance'], 0), -p['id']))


def refresh_form(csrf):
    return f'<form method="post" action="/refresh"><input type="hidden" name="csrf" value="{esc(csrf)}"><button>수동 새로고침</button></form>'


def badges(pr):
    return (f'<span class="badge {esc(pr["importance"])}">{esc(pr["importance"])}</span> '
            + esc(' · '.join(pr['flags'])) + (' · 미분석' if pr['unanalysed'] else '')
            + (' · 이전 SHA 결과' if pr['stale'] else '') + (' · draft' if pr['draft'] else ''))


def listing(prs, query, csrf):
    fields = [('repo', '저장소', sorted({p['owner'] + '/' + p['name'] for p in prs})),
              ('importance', '중요도', ['high', 'mid', 'low', 'unanalyzed']),
              ('status', '상태', ['completed', 'partial', 'failed', 'blocked', 'deferred', 'unanalyzed', 'stale']),
              ('review', '리뷰 요청', ['1'])]
    content = '<div class="heading"><h1>먼저 볼 변경</h1>' + refresh_form(csrf) + '</div><form class="filters" method="get">'
    for name, label, values in fields:
        content += f'<label>{label}<select name="{name}"><option value="">전체</option>'
        for value in values:
            selected = ' selected' if query.get(name, [''])[0] == value else ''
            content += f'<option value="{esc(value)}"{selected}>{esc(value)}</option>'
        content += '</select></label>'
    content += '<button>필터 적용</button></form>'
    for pr in prs:
        values = dict(repo=pr['owner'] + '/' + pr['name'], importance=pr['importance'], status=pr['status'], review=str(pr['review']))
        if any(query.get(key, [''])[0] and query[key][0] != value and not (key == 'status' and query[key][0] == 'stale' and pr['stale']) for key, value in values.items()):
            continue
        a, s = pr['analysis'], pr['snapshot']
        content += (f'<article><div class="muted">{esc(values["repo"])} #{pr["number"]} · {esc(pr["author"])}'
                    + (' · 리뷰 요청' if pr['review'] else '') + '</div>'
                    f'<h2><a href="/pr/{pr["id"]}">{esc(pr["title"])}</a></h2>'
                    f'<p>{esc(a["intent"] if a and a["intent"] else "의도 미분석")}</p><p>{badges(pr)}</p>'
                    f'<div class="muted">{esc(pr["status"])} · 수집 {esc(s["collected_at"] if s else "대기")} · 분석 {esc(a["created_at"] if a else "대기")}</div></article>')
    return content + ('<p class="muted">열린 PR이 없습니다.</p>' if not prs else '')


def github(pr):
    # 저장된 URL을 신뢰하지 않고 검증된 좌표만 사용한다.
    if re.fullmatch(r'[A-Za-z0-9-]+', pr['owner']) and re.fullmatch(r'[A-Za-z0-9_.-]+', pr['name']) and pr['name'] not in ('.', '..'):
        return f'https://github.com/{pr["owner"]}/{pr["name"]}/pull/{int(pr["number"])}'
    return None


def detail(pr, query, config, offline=False):
    a, s = pr['analysis'], pr['snapshot']
    content = f'<p><a href="/">← PR 목록</a></p><h1>{esc(pr["title"])}</h1>'
    link = github(pr)
    if link:
        content += f'<p><a href="{esc(link)}" rel="noreferrer">GitHub 원문 ↗</a></p>'
    session = None if offline else session_link(config, pr)
    if session:
        sid, url = session
        content += f'<p><a href="{esc(url)}" rel="noreferrer">Sessio · {esc(sid)} ↗</a></p>'
    content += f'<article><h2>의도</h2><p>{esc(a["intent"] if a and a["intent"] else "미분석 — 분석 결과가 없습니다.")}</p><h2>위험 요약</h2><p>{badges(pr)}</p><p>{esc(pr["status"])} {esc(a["error"] if a else "")}</p>'
    if s:
        coverage = json.loads(s['coverage'])
        stats = coverage.get('analysis', {})
        content += '<p>수집 상태: ' + esc(coverage.get('status', '대기')) + ' · ' + esc(coverage.get('collection_error', '')) + '</p>'
        content += '<p>분석 ' + esc(stats.get('analyzed', 0)) + ' · 제외 ' + esc(stats.get('excluded', 0)) + ' · 누락 ' + esc(stats.get('omitted', 0)) + '</p>'
        content += f'<p class="muted">수집 {esc(s["collected_at"])} · 분석 {esc(a["created_at"] if a else "대기")}</p>'
    content += '</article><p><a href="?order=original">원래 파일 순서</a> · <a href="?order=priority">검토 우선순위</a> · <a href="?diff=1">전체 diff 보기</a></p>'
    units = list(pr['units'])
    def priority(unit):
        item = pr['items'].get(unit['change_id'])
        return (not bool(unit.get('candidates')), item is not None, -RANK.get(item['importance'], 0) if item else 0)
    if query.get('order', [''])[0] == 'original' and s:
        files = json.loads(s['coverage']).get('files', [])
        file_order = {entry['filename']: index for index, entry in enumerate(files)}
        units.sort(key=lambda unit: file_order.get(unit['file_path'], len(files)))
    else:
        units.sort(key=priority)
    for unit in units:
        item = pr['items'].get(unit['change_id'])
        flags = unit.get('candidates', [])
        opened = bool(flags) or not item or item['importance'] == 'high'
        importance = item['importance'] if item else '미분석'
        content += (f'<details{" open" if opened else ""}><summary>{esc(unit["file_path"])} · {esc(unit["change_id"])} · {esc(importance)}'
                    + (' · 규칙 기반 후보' if flags else '') + '</summary>'
                    f'<p>{esc(item["reason"] if item else unit.get("exclusion_reason") or unit.get("omission_reason") or "분석되지 않은 변경입니다")}</p>'
                    f'<p>{esc(" · ".join(flags + (item.get("risk_flags", []) if item else [])))}</p>'
                    f'<p class="muted">이전 {esc(unit.get("old_range"))} → 새 줄 {esc(unit.get("new_range"))}</p>')
        if 'logic_without_tests' in flags:
            content += '<p>제공된 diff에서 관련 테스트 변경을 확인하지 못함</p>'
        if link:
            anchor = hashlib.sha256(unit['file_path'].encode()).hexdigest()
            content += f'<a href="{esc(link)}/files#diff-{anchor}" rel="noreferrer">GitHub 변경</a>'
        content += f'<pre>{esc(unit.get("diff", ""))}</pre></details>'
    if not units:
        content += '<article>미분석 — 수집된 변경 단위가 없습니다.</article>'
    if query.get('diff') == ['1']:
        content += '<h2>전체 원본 diff</h2><pre>' + esc(read_diff(config, s) or 'diff 캐시가 없습니다.') + '</pre>'
    return content


class Handler(BaseHTTPRequestHandler):
    """모든 동적 데이터는 인증 뒤에만 반환한다. 접근 로그에는 URL을 남기지 않는다."""
    server_version = 'skim'

    def log_message(self, *args):
        pass

    def reply(self, status, body='', content_type='text/html; charset=utf-8', extra=()):
        body = body.encode() if isinstance(body, str) else body
        self.send_response(status)
        for name, value in [('Content-Type', content_type), ('Content-Length', str(len(body))),
                            ('Content-Security-Policy', CSP), ('Referrer-Policy', 'no-referrer'),
                            ('X-Content-Type-Options', 'nosniff'), ('Cache-Control', 'no-store'),
                            ('X-Frame-Options', 'DENY'), *extra]:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def check(self, mutation=False):
        if self.headers.get_all('Host') != [self.server.authority]:
            self.reply(403, 'Host 거부')
            return False
        origins = self.headers.get_all('Origin')
        if (mutation or origins is not None) and origins != [self.server.origin]:
            self.reply(403, 'Origin 거부')
            return False
        return True

    def send_error(self, code, message=None, explain=None):
        if hasattr(self, 'headers') and self.headers.get_all('Host') != [self.server.authority]:
            code = 403
        self.reply(code, '요청을 처리할 수 없습니다')

    def do_GET(self):
        if not self.check():
            return
        url = urlsplit(self.path)
        if url.query and 'token' in parse_qs(url.query):
            return self.reply(400, '토큰은 fragment로만 전달합니다')
        if url.path in ('/static/app.css', '/static/entry.js'):
            name = url.path.rsplit('/', 1)[1]
            return self.reply(200, (STATIC / name).read_bytes(), 'text/css' if name.endswith('css') else 'text/javascript')
        if url.path == '/entry':
            return self.reply(200, page('진입', '<p id="status">인증 중…</p><script src="/static/entry.js" defer></script>'))
        csrf = self.server.auth.csrf(self.headers.get('Cookie'))
        if not csrf:
            return self.reply(401, page('인증 필요', '<p>skim web으로 새 진입 URL을 발급받으세요.</p>'))
        with closing(connect(self.server.config)) as conn:
            if url.path == '/settings':
                jobs = conn.execute('SELECT type,status,last_error FROM jobs ORDER BY id DESC LIMIT 20').fetchall()
                body = '<h1>설정 · 상태</h1><p>수집 간격 ' + str(self.server.config.poll_interval_seconds) + '초 · 보존 ' + str(self.server.config.retention_days) + '일</p>'
                body += '<p>분석 CLI: Codex · 전송은 명시적 허용 필요 · 격리 계약 미검증 시 차단</p>'
                for row in conn.execute('SELECT owner,name,tracked,analysis_policy FROM repositories ORDER BY owner,name'):
                    body += '<article>' + esc(dict(row)) + '</article>'
                body += '<h2>작업 큐</h2>' + ''.join('<article>' + esc(dict(row)) + '</article>' for row in jobs)
            else:
                prs = load_prs(conn, self.server.config)
                if url.path == '/':
                    body = listing(prs, parse_qs(url.query), csrf)
                else:
                    match = re.fullmatch(r'/pr/([1-9][0-9]*)', url.path)
                    pr = next((p for p in prs if match and p['id'] == int(match[1])), None)
                    if pr is None:
                        return self.reply(404, 'PR이 없습니다')
                    body = detail(pr, parse_qs(url.query), self.server.config, getattr(self.server, 'offline', False))
        self.reply(200, page('대시보드', body))

    def do_POST(self):
        if not self.check(mutation=True):
            return
        lengths = self.headers.get_all('Content-Length') or []
        if len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdigit() or int(lengths[0]) > 4096 or self.headers.get('Transfer-Encoding'):
            return self.reply(400, '잘못된 요청 크기')
        if self.headers.get_content_type() != 'application/x-www-form-urlencoded':
            return self.reply(415, '폼 요청만 허용합니다')
        try:
            data = parse_qs(self.rfile.read(int(lengths[0])).decode('utf-8'), strict_parsing=True)
        except (ValueError, UnicodeError):
            return self.reply(400, '잘못된 폼')
        if self.path == '/session':
            if set(data) != {'token'} or len(data['token']) != 1:
                return self.reply(400, '잘못된 토큰 요청')
            session = self.server.auth.redeem(data['token'][0])
            if not session:
                return self.reply(401, '진입 토큰 만료 또는 사용됨')
            # HTTP 루프백에서 브라우저별 Secure 처리 차이를 피한다. 정확한 Origin 검사가 포트 경계를 지킨다.
            return self.reply(303, extra=[('Location', '/'), ('Set-Cookie', f'{self.server.auth.cookie}={session}; Path=/; HttpOnly; SameSite=Strict')])
        csrf = self.server.auth.csrf(self.headers.get('Cookie'))
        submitted = data.get('csrf', [])
        if not csrf or len(submitted) != 1 or not constant_equal(csrf, submitted[0]):
            return self.reply(403, 'CSRF 거부')
        if self.path != '/refresh' or set(data) != {'csrf'}:
            return self.reply(400, '허용되지 않은 작업')
        with closing(connect(self.server.config)) as conn:
            CollectionScheduler(conn, self.server.config.poll_interval_seconds).request()
        self.reply(303, extra=[('Location', '/settings')])


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, config, port=0):
        self.config, self.auth = config, Auth()
        super().__init__(('127.0.0.1', port), Handler)
        self.authority = f'127.0.0.1:{self.server_port}'
        self.origin = 'http://' + self.authority

    def entry_url(self):
        return self.origin + '/entry#' + self.auth.issue()


def worker(config, stop):
    """SQLite 연결은 워커 스레드 전용이며 수집 중에도 HTTP 응답을 계속한다."""
    from .analyze import Analyzer
    with closing(connect(config)) as conn:
        scheduler = CollectionScheduler(conn, config.poll_interval_seconds)
        collector = Collector(conn, config)
        while not stop.is_set():
            try:
                if scheduler.tick(collector):
                    Analyzer(conn, config).run()
            except (OSError, ValueError, sqlite3.Error):
                conn.rollback()
            stop.wait(1)


def serve(config, port=0, open_browser=True, offline=False):
    """단일 서버 잠금과 0600 Unix 소켓으로 기존 서버의 새 URL을 발급한다."""
    import webbrowser
    path = config.data_dir / 'web.sock'
    fd = os.open(config.data_dir / 'web.lock', os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            with socket.socket(socket.AF_UNIX) as client:
                client.settimeout(5)
                client.connect(str(path))
                url = client.recv(4096).decode()
            print(url, flush=True)
            if open_browser:
                webbrowser.open(url)
            return 0
        with closing(connect(config)):
            pass
        with Server(config, port) as server, socket.socket(socket.AF_UNIX) as control:
            server.offline = offline
            path.unlink(missing_ok=True)
            # bind 시점에도 소켓이 공개되지 않도록 umask를 제한한다.
            mask = os.umask(0o177)
            try:
                control.bind(str(path))
            finally:
                os.umask(mask)
            path.chmod(0o600)
            control.listen(4)
            control.settimeout(0.5)
            stop = threading.Event()
            def issue_urls():
                while not stop.is_set():
                    try:
                        client, _ = control.accept()
                    except socket.timeout:
                        continue
                    except OSError:
                        break
                    with client:
                        client.settimeout(1)
                        try:
                            client.sendall(server.entry_url().encode())
                        except OSError:
                            pass
            threads = [threading.Thread(target=issue_urls, daemon=True)]
            if not offline:
                threads.append(threading.Thread(target=worker, args=(config, stop), daemon=True))
            for thread in threads:
                thread.start()
            url = server.entry_url()
            print(url, flush=True)
            if open_browser:
                webbrowser.open(url)
            try:
                server.serve_forever(poll_interval=0.5)
            except KeyboardInterrupt:
                pass
            finally:
                stop.set()
                for thread in threads:
                    thread.join(timeout=2)
                path.unlink(missing_ok=True)
        return 0
    finally:
        os.close(fd)
