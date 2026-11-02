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
import sys
import threading
import time
from contextlib import closing
from html import escape
from http.cookies import SimpleCookie, CookieError
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

from .db import connect
from .web_settings import POST_PATHS, RepoLookup, render, mutate
from . import web_jobs
from .jobs import worker, get as get_job
from .analyze.units import parse_diff
from .sessio import session_link

CSP = "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; base-uri 'none'; object-src 'none'; frame-ancestors 'none'; form-action 'self'"
STATIC = Path(__file__).with_name('web_static')
RULE_LABELS = {'install_script': '설치 스크립트'}
RANK = {'high': 3, 'mid': 2, 'low': 1}


def constant_equal(left, right):
    return hmac.compare_digest(left.encode('utf-8'), right.encode('utf-8'))


def esc(value):
    return escape(str(value if value is not None else ''), quote=True)


def meta(*values):
    content = ''.join(f'<span>{esc(value)}</span>' for value in values if value not in (None, ''))
    return '<p class="meta">' + content + '</p>' if content else ''


def page(title, content, active=None):
    links = [('/', 'PR 리뷰'), ('/repos', '저장소'), ('/status', '상태'), ('/settings', '설정')]
    nav = ''.join(f'<a href="{href}"' + (' aria-current="page"' if active == href else '')
                  + f'>{label}</a>' for href, label in links)
    return ('<!doctype html><html lang="ko"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{esc(title)} | skim</title><link rel="stylesheet" href="/static/app.css"></head><body>'
            '<a class="skip-link" href="#main">본문으로 건너뛰기</a>'
            '<header class="app-header"><a class="brand" href="/" aria-label="skim 홈">'
            '<span class="brand-mark"><svg viewBox="0 0 24 24" width="22" height="22" fill="none" aria-hidden="true">'
            '<path d="M5 6.5h10M5 10h7M5 13.5h5" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/>'
            '<path d="m13 16 2.1 2.1L19.5 13" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"/></svg></span>'
            'skim</a><span class="workspace-label">PR 검토</span><nav aria-label="주 메뉴">'
            + nav + '</nav></header><main id="main">' + content + '</main></body></html>')


class Auth:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.lock = threading.Lock()
        self.tokens = {}
        self.sessions = {}
        self.posts = {}
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


    def allow_post(self, csrf):
        with self.lock:
            now = self.clock()
            recent = [t for t in self.posts.get(csrf, []) if now - t < 60]
            if len(recent) >= 30:
                return False
            self.posts[csrf] = recent + [now]
            return True


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
    for raw in conn.execute("SELECT p.*,r.host,r.owner,r.name,EXISTS(SELECT 1 FROM pr_sources WHERE pr_id=p.id AND source='review_request') AS review FROM pull_requests p JOIN repositories r ON r.id=p.repo_id WHERE p.state='open' AND r.host IN (SELECT host FROM github_hosts)"):
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
        pr['rule_flags'] = sorted({flag for unit in units for flag in unit.get('candidates', [])})
        pr['model_flags'] = sorted({flag for item in items.values() for flag in item.get('risk_flags', [])})
        pr['flags'] = sorted(set(pr['rule_flags']) | set(pr['model_flags']))
        pr['unanalysed'] = not units or any(u['change_id'] not in items for u in units)
        rows.append(pr)
    return sorted(rows, key=lambda p: (-bool(p['flags']), -p['unanalysed'], -RANK.get(p['importance'], 0), -p['id']))


def badges(pr):
    labels = []
    if pr['rule_flags']:
        labels.append('규칙 후보: ' + ', '.join(RULE_LABELS.get(flag, flag) for flag in pr['rule_flags']))
    if pr['model_flags']:
        labels.append('모델 판단: ' + ', '.join(pr['model_flags']))
    if pr['unanalysed']:
        labels.append('모델 판단: 미분석' if not pr['items'] else '모델 판단: 일부 미분석')
    return ((f'<span class="badge {esc(pr["importance"])}">{esc(pr["importance"])}</span> '
             if pr["importance"] != "unanalyzed" else "")
            + ''.join(f'<span>{esc(label)}</span>' for label in labels)
            + ('<span class="badge">리뷰 요청</span>' if pr.get('review') else '')
            + ('<span>이전 SHA 결과</span>' if pr['stale'] else '') + ('<span>draft</span>' if pr['draft'] else ''))


def repository_label(pr):
    prefix = '' if pr.get('host', 'github.com') == 'github.com' else pr['host'] + '/'
    return prefix + pr['owner'] + '/' + pr['name']


def list_controls(items, query, prefix, label, text):
    """각 목록의 검색과 페이지 상태를 독립적으로 유지한다."""
    search_key, page_key = prefix + '_q', prefix + '_page'
    term = query.get(search_key, [''])[0][:200]
    filtered = [item for item in items if term.casefold() in text(item).casefold()]
    pages = max(1, (len(filtered) + 19) // 20)
    raw_page = query.get(page_key, ['1'])[0]
    number = min(int(raw_page), pages) if raw_page.isascii() and raw_page.isdigit() and len(raw_page) < 10 else 1
    number = max(1, number)
    hidden = ''.join(f'<input type="hidden" name="{esc(key)}" value="{esc(value)}">'
                     for key, values in query.items() if key not in (search_key, page_key)
                     for value in values)
    controls = (f'<form class="filters" method="get">{hidden}<label>{esc(label)} 검색 '
                f'<input type="search" name="{search_key}" value="{esc(term)}" maxlength="200"></label>'
                '<button class="btn secondary">검색</button></form>')
    controls += f'<p class="meta">{len(filtered)}개 / {number}/{pages} 페이지</p>'
    if not filtered:
        controls += '<p class="muted">검색 결과가 없습니다.</p>' if term else ''
    def link(target, title):
        params = [(key, value) for key, values in query.items() if key != page_key for value in values]
        params.append((page_key, str(target)))
        return f'<a class="btn secondary sm" href="?{esc(urlencode(params))}">{title}</a>'
    navigation = '<nav class="toolbar" aria-label="' + esc(label) + ' 페이지">'
    if number > 1:
        navigation += link(number - 1, '이전')
    if number < pages:
        navigation += link(number + 1, '다음')
    navigation += '</nav>'
    return filtered[(number - 1) * 20:number * 20], controls, navigation


def home_team_controls(conn, csrf, lookup, offline, query):
    from .web_settings import form, host_field
    host, mode, truncated, loaded, items, teams, selected = lookup.snapshot(with_teams=True) if lookup else ('github.com', 'all', False, False, [], [], '')
    requested = query.get('team', [''])[0]
    key = host + '|' + selected if mode == 'team' and selected else ''
    requested_target = requested.replace('|', ' / ', 1)
    requested_job = conn.execute("SELECT * FROM jobs WHERE type='repos' AND target=? ORDER BY id DESC LIMIT 1", (requested_target,)).fetchone() if requested else None
    ready = bool(requested and loaded and requested == key and requested_job and requested_job['status'] == 'completed')
    filters = [(name, query[name][0][:200]) for name in ('repo', 'importance', 'status', 'review', 'pr_q') if query.get(name, [''])[0]]
    hidden = ''.join(f'<input type="hidden" name="{esc(name)}" value="{esc(value)}">' for name, value in filters)
    clear_link = '/?' + urlencode(filters) if filters else '/'
    body = '<section class="home-team" aria-labelledby="home-team-title"><h2 id="home-team-title">팀 저장소로 보기</h2>'
    if offline:
        body += '<p class="muted">오프라인에서는 팀 저장소를 조회할 수 없습니다.</p>'
    else:
        body += '<p class="muted">호스트의 소속 팀을 불러온 뒤 팀을 선택하세요.</p>'
        body += form('/home/team', csrf, hidden + host_field(conn, '팀 조회', host) + '<button>소속 팀 불러오기</button>')
        if mode == 'team' and loaded and teams:
            options = '<option value="">팀을 선택하세요</option>'
            options += ''.join('<option value="' + esc(team) + '"' + (' selected' if team == selected else '') + '>' + esc(team) + '</option>' for team in sorted(teams, key=str.casefold))
            body += form('/home/team', csrf, hidden + '<input type="hidden" name="host" value="' + esc(host) + '"><label>소속 팀<select name="team" required>' + options + '</select></label><button>선택한 팀 보기</button>')
    latest_job = web_jobs.jobs.latest(conn, 'repos')
    body += web_jobs.job_box(requested_job or latest_job)
    if not requested and mode == 'team' and loaded and latest_job and latest_job['status'] != 'completed':
        body += '<p class="muted">표시된 팀 목록은 이전 조회 결과일 수 있습니다. 작업 결과를 확인하세요.</p>'
    if requested:
        if ready:
            body += '<p>적용 중: ' + esc(host + ' / ' + selected) + ' <a href="' + esc(clear_link) + '">팀 필터 해제</a></p>'
            if truncated:
                body += '<p>조회 한도에 도달하여 일부 저장소만 표시합니다.</p>'
        else:
            message = ('선택한 팀 조회가 실패했거나 중단되었습니다. 다시 조회하세요.' if requested_job and requested_job['status'] not in ('pending', 'running', 'completed') else '선택한 팀의 조회 결과가 없습니다. 조회가 완료되면 화면을 새로고침하세요.')
            body += '<p>' + message + '</p><p><a href="' + esc(clear_link) + '">팀 필터 해제</a></p>'
    elif mode == 'team' and loaded and not teams:
        body += '<p class="muted">조회된 소속 팀이 없습니다. 팀 접근 권한과 조직 SSO를 확인하세요.</p>'
    return body + '</section>', requested, ready, host, {name.casefold() for name in items}


def listing(prs, query, csrf, heading_toolbar="", team_controls=None):
    fields = [('repo', '저장소', sorted({repository_label(p) for p in prs})),
              ('importance', '중요도', ['high', 'mid', 'low', 'unanalyzed']),
              ('status', '상태', ['completed', 'partial', 'failed', 'blocked', 'deferred', 'unanalyzed', 'stale']),
              ('review', '리뷰 요청', ['1'])]
    content = ('<div class="heading home-heading"><div><p class="eyebrow">PULL REQUESTS</p><h1>PR 리뷰</h1>'
               '<p class="home-description">연결한 저장소의 열린 PR과 분석 결과를 확인하세요.</p></div>' + heading_toolbar + '</div>'
               '<section class="home-results" aria-labelledby="pr-list-title"><div class="section-heading"><div>'
               '<h2 id="pr-list-title">열린 PR</h2><p class="muted">필터와 검색으로 살펴볼 변경을 찾아보세요.</p></div></div>'
               + (team_controls[0] if team_controls else '') + '<form class="filters" method="get">')
    if query.get('team', [''])[0]:
        content += f'<input type="hidden" name="team" value="{esc(query["team"][0])}">'
    if query.get('pr_q', [''])[0]:
        content += f'<input type="hidden" name="pr_q" value="{esc(query["pr_q"][0])}">'
    for name, label, values in fields:
        content += f'<label><span class="sr-only">{label}</span><select name="{name}"><option value="">{label}: 전체</option>'
        for value in values:
            selected = ' selected' if query.get(name, [''])[0] == value else ''
            content += f'<option value="{esc(value)}"{selected}>{esc(value)}</option>'
        content += '</select></label>'
    content += '<button class="btn secondary">필터 적용</button></form>'
    has_prs = bool(prs)
    if team_controls and team_controls[1]:
        _, _, ready, host, items = team_controls
        prs = [pr for pr in prs if ready and pr.get('host', 'github.com').casefold() == host.casefold()
               and (pr['owner'] + '/' + pr['name']).casefold() in items]
    prs = [pr for pr in prs if all(
        not query.get(key, [''])[0] or query[key][0] == value
        or (key == 'status' and query[key][0] == 'stale' and pr['stale'])
        for key, value in dict(repo=repository_label(pr), importance=pr['importance'],
                               status=pr['status'], review=str(pr['review'])).items())]
    prs, controls, navigation = list_controls(prs, query, 'pr', 'PR', lambda p: ' '.join([repository_label(p), str(p['number']), p['title'] or '', p['author'] or '', (p['analysis']['intent'] or '') if p['analysis'] else '']))
    content += controls
    collected = {pr['snapshot']['collected_at'] if pr['snapshot'] else '대기' for pr in prs}
    shared_collection = len(collected) == 1
    analyzed = {pr['analysis']['created_at'] if pr['analysis'] else '대기' for pr in prs}
    shared_analysis = len(analyzed) == 1
    if shared_collection or shared_analysis:
        content += meta(*(['수집 ' + next(iter(collected))] if shared_collection else []),
                        *(['분석 ' + next(iter(analyzed))] if shared_analysis else []))
    content += '<div class="list">'
    for pr in prs:
        values = dict(repo=repository_label(pr), importance=pr['importance'], status=pr['status'], review=str(pr['review']))
        a, s = pr['analysis'], pr['snapshot']
        content += ('<article class="issue-row"><div class="row-content">' + f'<h2><a href="/pr/{pr["id"]}">{esc(pr["title"])}</a></h2>'
                    + meta(values['repo'] + ' #' + str(pr['number']), pr['author'])
                    + f'<p class="issue-intent">{esc(a["intent"] if a and a["intent"] else "의도 미분석")}</p><p class="meta">{badges(pr)}</p>'
                    + '<p class="meta"><span class="badge">' + esc(pr['status']) + '</span></p>' + meta(*([] if shared_collection else ['수집 ' + (s['collected_at'] if s else '대기')]),
                           *([] if shared_analysis else ['분석 ' + (a['created_at'] if a else '대기')])) + '</div></article>')
    return content + '</div>' + navigation + ('<p class="muted">열린 PR이 없습니다. <a href="/repos">저장소 연결</a>과 <a href="/status">상태 점검</a>, <a href="/settings">설정</a>을 확인하세요.</p>' if not has_prs else '') + '</section>'


def github(pr):
    from .hosts import hostname
    try:
        host = hostname(pr.get('host', 'github.com'))
    except ValueError:
        return None
    # 저장된 URL을 신뢰하지 않고 검증된 좌표만 사용한다.
    if re.fullmatch(r'[A-Za-z0-9-]+', pr['owner']) and re.fullmatch(r'[A-Za-z0-9_.-]+', pr['name']) and pr['name'] not in ('.', '..'):
        return f'https://{host}/{pr["owner"]}/{pr["name"]}/pull/{int(pr["number"])}'
    return None


def render_diff(diff):
    lines = []
    in_hunk = False
    for line in diff.splitlines(keepends=True):
        if line.startswith('diff --git '):
            in_hunk = False
        if line.startswith('@@'):
            kind = 'hunk'
            in_hunk = True
        elif not in_hunk and line.startswith(('+++ ', '--- ')):
            kind = 'header'
        elif line.startswith('+'):
            kind = 'add'
        elif line.startswith('-'):
            kind = 'delete'
        else:
            kind = 'context'
        lines.append(f'<span class="diff-{kind}">{esc(line)}</span>')
    return '<pre class="diff">' + ''.join(lines) + '</pre>'


def detail(pr, query, config, offline=False, analysis_panel=""):
    a, s = pr['analysis'], pr['snapshot']
    content = f'<p><a href="/">← PR 목록</a></p><header class="detail-header"><p class="eyebrow">PR REVIEW</p><h1>{esc(pr["title"])}</h1>'
    content += meta(repository_label(pr) + ' #' + str(pr['number']), pr['author'])
    link = github(pr)
    if link:
        content += f'<p><a href="{esc(link)}" rel="noreferrer">GitHub 원문 ↗</a></p>'
    session = None if offline else session_link(config, pr)
    if session:
        sid, url = session
        content += f'<p><a href="{esc(url)}" rel="noreferrer">Sessio: {esc(sid)} ↗</a></p>'
    content += f'</header><section class="detail-overview" aria-label="PR 검토 요약"><article class="detail-insight"><p class="detail-kicker">검토의 시작</p><h2>변경 의도</h2><p class="detail-intent">{esc(a["intent"] if a and a["intent"] else "미분석: 분석 결과가 없습니다.")}</p><h3>검토 포인트</h3><p class="meta">{badges(pr)}</p></article><article class="detail-status"><p class="detail-kicker">진행 상황</p><h2>수집과 분석 상태</h2><p><span class="badge">{esc(pr["status"])}</span> {esc(a["error"] if a else "")}</p>'
    if a:
        content += meta('모델 ' + a['model'], 'Codex CLI ' + a['cli_version'])
    if s:
        coverage = json.loads(s['coverage'])
        stats = coverage.get('analysis', {})
        content += meta('수집 상태: ' + coverage.get('status', '대기'), coverage.get('collection_error', ''))
        content += meta('분석 ' + str(stats.get('analyzed', 0)), '제외 ' + str(stats.get('excluded', 0)), '누락 ' + str(stats.get('omitted', 0)))
        content += meta('수집 ' + s['collected_at'], '분석 ' + (a['created_at'] if a else '대기'))
    content += '</article></section>' + analysis_panel + '<section class="detail-changes"><div class="section-heading"><h2>변경 살펴보기</h2><p class="muted">중요한 변경부터 확인하거나 원래 파일 순서로 볼 수 있습니다.</p></div><nav class="toolbar segments" aria-label="변경 보기">'
    for href, label, selected in [('?order=original', '원래 파일 순서', query.get('order', [''])[0] == 'original' and query.get('diff') != ['1']),
                                  ('?order=priority', '검토 우선순위', query.get('order', [''])[0] != 'original' and query.get('diff') != ['1']),
                                  ('?diff=1', '전체 diff 보기', query.get('diff', [''])[0] == '1')]:
        params = parse_qs(href[1:])
        if query.get('unit_q', [''])[0]:
            params['unit_q'] = query['unit_q'][:1]
        href = '?' + urlencode(params, doseq=True)
        content += f'<a class="btn ghost sm" href="{esc(href)}"' + (' aria-current="true"' if selected else '') + f'>{label}</a>'
    content += '</nav>'
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
    has_units = bool(units)
    units, controls, navigation = list_controls(units, query, 'unit', '변경 단위', lambda u: ' '.join([u['file_path'], u['change_id'], u.get('diff', '')]))
    content += controls
    for unit in units:
        item = pr['items'].get(unit['change_id'])
        flags = unit.get('candidates', [])
        opened = bool(flags) or not item or item['importance'] == 'high'
        importance = item['importance'] if item else '미분석'
        content += (f'<details{" open" if opened else ""}><summary><span class="meta"><span>{esc(unit["file_path"])}</span><span>{esc(unit["change_id"])}</span><span>{esc(importance)}</span>'
                    + ('<span>규칙 기반 후보</span>' if flags else '') + '</span></summary>'
                    f'<p>{esc(item["reason"] if item else unit.get("exclusion_reason") or unit.get("omission_reason") or "분석되지 않은 변경입니다")}</p>'
                    f'<p>규칙 후보: {esc(", ".join(RULE_LABELS.get(flag, flag) for flag in flags) or "없음")}</p>'
                    f'<p>모델 판단: {esc((", ".join(item.get("risk_flags", [])) or "없음") if item else "미분석")}</p>'
                    f'<p class="muted">이전 {esc(unit.get("old_range"))} → 새 줄 {esc(unit.get("new_range"))}</p>')
        if 'logic_without_tests' in flags:
            content += '<p>제공된 diff에서 관련 테스트 변경을 확인하지 못함</p>'
        if link:
            anchor = hashlib.sha256(unit['file_path'].encode()).hexdigest()
            content += f'<a href="{esc(link)}/files#diff-{anchor}" rel="noreferrer">GitHub 변경</a>'
        content += render_diff(unit.get("diff", "")) + '</details>'
    if not has_units:
        content += '<article>미분석: 수집된 변경 단위가 없습니다.</article>'
    content += navigation
    if query.get('diff') == ['1']:
        content += '<h2>전체 원본 diff</h2>' + render_diff(read_diff(config, s) or 'diff 캐시가 없습니다.')
    return content + '</section>'


class Handler(BaseHTTPRequestHandler):
    """모든 동적 데이터는 인증 뒤에만 반환한다. 접근 로그에는 URL을 남기지 않는다."""
    server_version = 'skim'

    def log_message(self, *args):
        pass

    def reply(self, status, body='', content_type='text/html; charset=utf-8', extra=()):
        body = body.encode() if isinstance(body, str) else body
        self.send_response(status)
        for name, value in [('Content-Type', content_type), ('Content-Length', str(len(body))),
                            ('Content-Security-Policy', CSP), ('Referrer-Policy', 'same-origin'),
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
        if code == 501:
            code = 405
        if hasattr(self, 'headers') and self.headers.get_all('Host') != [self.server.authority]:
            code = 403
        self.reply(code, '요청을 처리할 수 없습니다')

    def do_GET(self):
        if not self.check():
            return
        url = urlsplit(self.path)
        if url.query and 'token' in parse_qs(url.query):
            return self.reply(400, '토큰은 fragment로만 전달합니다')
        if url.path in ('/static/app.css', '/static/entry.js', '/static/jobs.js'):
            name = url.path.rsplit('/', 1)[1]
            return self.reply(200, (STATIC / name).read_bytes(), 'text/css' if name.endswith('css') else 'text/javascript')
        if url.path == '/entry':
            return self.reply(200, page('진입', '<p id="status">인증 중…</p><script src="/static/entry.js" defer></script>'))
        csrf = self.server.auth.csrf(self.headers.get('Cookie'))
        if not csrf:
            return self.reply(401, page('인증 필요', '<p>skim web으로 새 진입 URL을 발급받으세요.</p>'))
        if url.path in POST_PATHS or url.path in ('/refresh', '/session'):
            return self.reply(405, 'POST만 허용합니다', extra=[('Allow', 'POST')])
        offline = getattr(self.server, 'offline', False)
        job = re.fullmatch(r'/jobs/([1-9][0-9]{0,17})', url.path)
        with closing(connect(self.server.config)) as conn:
            if job:
                # 읽기 전용 폴링. 조회가 작업을 만들거나 실행하지 않는다.
                row = get_job(conn, int(job[1]))
                if row is None:
                    return self.reply(404, '{}', 'application/json')
                return self.reply(200, json.dumps(web_jobs.describe(row), ensure_ascii=False), 'application/json; charset=utf-8')
            title = {'/settings': '설정', '/repos': '저장소', '/status': '상태'}.get(url.path, '대시보드')
            if url.path in ('/settings', '/repos'):
                body = render(self.server, conn, csrf, url.path, parse_qs(url.query))
            elif url.path == '/status':
                body = web_jobs.status_page(conn, csrf, offline)
            else:
                prs = load_prs(conn, self.server.config)
                if url.path == '/':
                    query = parse_qs(url.query)
                    team_controls = home_team_controls(conn, csrf, getattr(self.server, 'repo_lookup', None), offline, query)
                    body = listing(prs, query, csrf, web_jobs.collect_panel(conn, csrf, offline, compact=True), team_controls) + web_jobs.script()
                else:
                    match = re.fullmatch(r'/pr/([1-9][0-9]*)', url.path)
                    pr = next((p for p in prs if match and p['id'] == int(match[1])), None)
                    if pr is None:
                        return self.reply(404, 'PR이 없습니다')
                    title = pr['title']
                    body = (detail(pr, parse_qs(url.query), self.server.config, offline,
                                   web_jobs.analysis_panel(conn, pr, csrf, offline)) + web_jobs.script())
        self.reply(200, page(title, body, '/' if url.path.startswith('/pr/') else url.path))

    def do_POST(self):
        if not self.check(mutation=True):
            return
        lengths = self.headers.get_all('Content-Length') or []
        if len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdigit() or int(lengths[0]) > 16384 or self.headers.get('Transfer-Encoding'):
            return self.reply(400, '잘못된 요청 크기')
        if self.headers.get_content_type() != 'application/x-www-form-urlencoded':
            return self.reply(415, '폼 요청만 허용합니다')
        try:
            data = parse_qs(self.rfile.read(int(lengths[0])).decode('utf-8'), strict_parsing=True, keep_blank_values=True)
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
        headers = self.headers.get_all('X-CSRF-Token') or []
        submitted = data.pop('csrf', [])
        if (not csrf or len(headers) > 1 or len(submitted) > 1
                or not (headers or submitted)
                or any(not constant_equal(csrf, value) for value in headers + submitted)):
            return self.reply(403, 'CSRF 거부')
        if self.path not in POST_PATHS:
            return self.reply(405, '허용되지 않은 작업')
        if not self.server.auth.allow_post(csrf):
            return self.reply(429, '요청이 많습니다. 60초 후 다시 시도하세요', extra=[('Retry-After', '60')])
        try:
            return_values = data.pop('return_query', [])
            if len(return_values) > 1:
                raise ValueError('잘못된 목록 상태')
            return_query = return_values[0] if return_values else ''
            if return_query:
                from .web_settings import validate_return_query
                return_query = validate_return_query(self.path, return_query)
            if self.path in web_jobs.JOB_PATHS:
                with closing(connect(self.server.config)) as conn:
                    location = web_jobs.mutate(self.server, conn, self.path, data)
            else:
                location = mutate(self.server, self.path, data)
        except web_jobs.Rejected as exc:
            return self.reply(exc.status, page('요청 거부', f'<p>{esc(exc)}</p><p><a href="/status">상태</a> <a href="/">PR 목록</a></p>'))
        except (OSError, ValueError, sqlite3.Error):
            back = '/repos' if self.path.startswith(('/repos/', '/hosts/')) else '/settings' if self.path.startswith('/settings/') else '/status'
            return self.reply(400, page('요청 실패', '<h1>요청을 처리하지 못했습니다</h1><p>입력과 설정을 확인하세요. 허용하려면 모델 서비스 전송 확인 체크가 필요합니다.</p><p><a href="' + back + '">← 돌아가기</a></p>'))
        if return_query and location in ('/repos', '/settings'):
            location += '?' + return_query
        self.reply(303, extra=[('Location', location)])

    def do_PUT(self):
        if self.check(mutation=True):
            self.reply(405, 'POST만 허용합니다', extra=[('Allow', 'GET, POST')])

    do_DELETE = do_PATCH = do_OPTIONS = do_TRACE = do_PUT



class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, config, port=0):
        self.config, self.auth = config, Auth()
        super().__init__(('127.0.0.1', port), Handler)
        self.authority = f'127.0.0.1:{self.server_port}'
        self.origin = 'http://' + self.authority
        self.repo_lookup = RepoLookup(config)

    def handle_error(self, request, client_address):
        # 예외 메시지나 요청 원문에는 일회용 진입 토큰이 있을 수 있다.
        print('skim 웹 요청 처리 실패. 서버 상태를 확인하세요.', file=sys.stderr)

    def entry_url(self):
        return self.origin + '/entry#' + self.auth.issue()


def open_entry(url):
    """토큰 URL은 macOS 시스템 열기에만 전달한다. BROWSER에는 전달하지 않는다."""
    import subprocess
    import webbrowser
    if not re.fullmatch(r'http://127\.0\.0\.1:[0-9]{1,5}/entry#[A-Za-z0-9_-]+', url):
        return False
    try:
        if sys.platform == 'darwin':
            if subprocess.run(['/usr/bin/open', url], stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL).returncode == 0:
                return True
        else:
            # 일반 브라우저 실행 인자에는 인증 토큰을 포함하지 않는다.
            webbrowser.open(url.split('/entry#', 1)[0] + '/')
    except (OSError, webbrowser.Error):
        pass
    if sys.platform == 'darwin':
        print('토큰 URL의 시스템 열기에 실패했습니다. 터미널에서 skim web을 다시 실행하세요.', flush=True)
        return False
    print('토큰 없는 기본 주소의 브라우저 열기를 시도했습니다. 진입 URL은 출력하지 않습니다. 새 진입 URL이 필요하면 터미널에서 skim web을 다시 실행하세요.', flush=True)
    return False


def open_running(config):
    """실행 중인 서버의 일회용 진입 URL을 로그에 남기지 않고 연다."""
    try:
        with socket.socket(socket.AF_UNIX) as client:
            client.settimeout(5)
            client.connect(str(config.data_dir / 'web.sock'))
            url = client.recv(4096).decode('utf-8')
        if not re.fullmatch(r'http://127\.0\.0\.1:[0-9]{1,5}/entry#[A-Za-z0-9_-]+', url):
            raise ValueError('잘못된 진입 URL')
        if not 1 <= urlsplit(url).port <= 65535:
            raise ValueError('잘못된 포트')
        if not open_entry(url):
            raise OSError('브라우저 열기 실패')
        return 0
    except (OSError, UnicodeError, ValueError):
        print('실행 중인 skim 서버 또는 브라우저를 확인하세요.', file=sys.stderr)
        return 1


def serve(config, port=0, open_browser=True, offline=False):
    """단일 서버 잠금과 0600 Unix 소켓으로 기존 서버의 새 URL을 발급한다."""
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
                open_entry(url)
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
                # 수집·분석·doctor·저장소 조회를 하나의 워커가 직렬 실행한다.
                threads.append(threading.Thread(target=worker, args=(config, stop, server.repo_lookup), daemon=True))
            for thread in threads:
                thread.start()
            url = server.entry_url()
            print(url, flush=True)
            if open_browser:
                open_entry(url)
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
