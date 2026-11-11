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

from .db import analysis_allowed, connect
from .web_settings import POST_PATHS, RepoLookup, render, mutate
from . import web_jobs
from .timefmt import human_time
from .jobs import worker, get as get_job
from .analyze.units import parse_diff, sanitize
from .sessio import session_link

CSP = "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; base-uri 'none'; object-src 'none'; frame-ancestors 'none'; form-action 'self'"
STATIC = Path(__file__).with_name('web_static')
RULE_LABELS = {'install_script': '설치 스크립트'}
RANK = {'high': 3, 'mid': 2, 'low': 1}
# 값(쿼리·클래스)은 그대로 두고 화면 표시만 한국어로 바꾼다.
LABELS = {'high': '높음', 'mid': '보통', 'low': '낮음', 'unanalyzed': '미분석',
          'completed': '분석 완료', 'partial': '일부 분석', 'failed': '분석 실패', 'blocked': '분석 차단',
          'deferred': '분석 보류', 'stale': '이전 SHA 결과', '1': '리뷰 요청 받음'}


def label(value):
    return LABELS.get(value, value)


# 카드에는 내부 플래그 이름 대신 읽을 수 있는 말을 쓴다. 상세 화면의 원래 표기는 그대로 둔다.
FLAG_LABELS = {'pure_rename': '내용 변경 없는 이름 변경', 'auth': '인증', 'payments': '결제', 'db_schema_migration': 'DB 스키마', 'config': '설정',
               'ci_workflow': 'CI 워크플로', 'dependency': '의존성', 'secrets': '민감 정보',
               'logic_without_tests': '관련 테스트 변경 미확인', 'install_script': '설치 스크립트'}


RELATION_LABELS = {'caller': '호출부', 'test': '테스트', 'wiring': '연결', 'rename': '이름 변경', 'import': '임포트',
                   'docs': '문서', 'config': '설정', 'other': '기타'}
ROLE_LABELS = {'core': '핵심', 'supporting': '연관', 'mechanical': '기계적'}
SIGNAL_KEYS = ('location_hints', 'kind_signals', 'content_hits', 'hard_floors', 'mechanical_hints')
SAFETY_LIMIT = 3


def file_anchor(path):
    return 'file-' + hashlib.sha256(path.encode()).hexdigest()[:12]


def priority_rules(row):
    return row['rule'] - {'logic_without_tests'}


def is_current(pr):
    """홈과 상세가 같이 쓰는 판정. 이전 SHA·미허용·실패 결과는 현재 중요도로 쓰지 않는다."""
    return bool(pr.get('analysis', True) and not pr.get('stale') and pr.get('allowed', True)
                and (pr.get('status', 'completed') in ('completed', 'partial') or pr.get('previous_result')))


def current_view(pr):
    return pr if is_current(pr) else dict(pr, items={}, result={})


def level_of(row):
    return {3: 'high', 2: 'mid', 1: 'low'}.get(row['rank'], 'unanalyzed')


def valid_groups(result, items):
    groups = result.get('groups', [])
    return [group for group in groups if isinstance(group, dict) and group.get('core_change_id') in items
            and isinstance(group.get('reason'), str) and isinstance(group.get('related'), list)] if isinstance(groups, list) else []


def foldable(row):
    """접어도 되는 기계적 변경. 높음·바닥·종류 신호·불확실성·미분석이 섞이면 접지 않는다."""
    return (row['role'] == 'mechanical' and not row['hint'] and not row['partial'] and not row['uncertain']
            and row['rank'] < 3 and not row['hard_floors'] and not row['kind_signals'])


def review_bucket(row):
    core = row['role'] == 'core'
    if row['rank'] == 3:
        return 0 if core else 1
    if core:
        return 2
    if row['kind_signals'] or row['hard_floors']:
        return 3
    if row['rank'] == 2:
        return 4
    if row['role'] == 'supporting':
        return 5
    if row['hint'] or (not row['rank'] and row['location_hints']):
        return 6
    return 8 if foldable(row) else 7


def file_rows(pr):
    """변경 단위를 파일로 묶는다. 파일 중요도는 그 파일 단위 중 가장 높은 값이다."""
    files = {}
    result = pr.get('result') or {}
    usable = isinstance(result, dict) and not pr.get('stale') and pr.get('allowed', True)
    focus = result.get('focus', []) if usable else []
    if not isinstance(focus, list):
        focus = []
    focus_by_id = {item['change_id']: (index, item['reason']) for index, item in enumerate(focus)
                   if isinstance(item, dict) and isinstance(item.get('change_id'), str)
                   and isinstance(item.get('reason'), str) and item['change_id'] in pr['items']}
    # 구조화 신호가 없는 단위만 있으면 스키마 2 결과다. 예전 선정과 문구를 그대로 쓴다.
    legacy = not any('location_hints' in unit or 'kind_signals' in unit for unit in pr['units'])
    paths = {unit['change_id']: unit['file_path'] for unit in pr['units']}
    core, supports = {}, {}
    groups = valid_groups(result, pr['items']) if usable and not legacy else []
    for index, group in enumerate(groups):
        related = [dict(path=paths[entry['change_id']], relation=entry.get('relation'))
                   for entry in group['related'] if isinstance(entry, dict) and entry.get('change_id') in paths]
        core[group['core_change_id']] = (index, group['reason'], related)
        for entry in group['related']:
            if isinstance(entry, dict) and entry.get('change_id') in paths:
                supports.setdefault(entry['change_id'], []).append(paths[group['core_change_id']])
    if groups:
        for change_id, (position, reason) in focus_by_id.items():
            core.setdefault(change_id, (len(groups) + position, reason, []))
    uncertain = {entry.get('change_id') for entry in result.get('uncertainties', []) if isinstance(entry, dict)} if usable and isinstance(result.get('uncertainties', []), list) else set()
    for unit in pr['units']:
        item = pr['items'].get(unit['change_id'])
        row = files.setdefault(unit['file_path'], dict(
            path=unit['file_path'], rank=0, reason='', rule=set(), model=set(), focus_rank=None, focus_reason='',
            legacy=legacy, test_gap=False, role='', hint=False, partial=False, uncertain=False,
            group_rank=None, group_reason='', related=[], supports=[], behaviors=[],
            **{key: set() for key in SIGNAL_KEYS}))
        row['rule'].update(unit.get('candidates', []))
        for key in SIGNAL_KEYS:
            row[key].update(unit.get(key) or [])
        row['test_gap'] = row['test_gap'] or bool(unit.get('test_gap'))
        row['uncertain'] = row['uncertain'] or unit['change_id'] in uncertain
        if not item:
            row['partial'] = True
            continue
        if unit['change_id'] in focus_by_id:
            position, focus_reason = focus_by_id[unit['change_id']]
            if row['focus_rank'] is None or position < row['focus_rank']:
                row['focus_rank'], row['focus_reason'] = position, focus_reason
        if unit['change_id'] in core:
            position, group_reason, related = core[unit['change_id']]
            if row['group_rank'] is None or position < row['group_rank']:
                row['group_rank'], row['group_reason'] = position, group_reason
            row['related'] += related
        row['supports'] += supports.get(unit['change_id'], [])
        rank = RANK.get(item['importance'], 0)
        if rank > row['rank'] or (rank == row['rank'] and not row['reason']):
            row['rank'], row['reason'] = rank, item.get('reason') or ''
        row['model'].update(item.get('risk_flags', []))
        row['behaviors'].append(item.get('behavior'))
    if legacy:
        return sorted(files.values(), key=lambda r: (-r['rank'], r['focus_rank'] if r['focus_rank'] is not None else 999999,
                                                      -bool(r['rule']), r['path']))
    for row in files.values():
        seen = {row['path']}
        row['related'] = [entry for entry in row['related'] if not (entry['path'] in seen or seen.add(entry['path']))]
        row['supports'] = [path for path in dict.fromkeys(row['supports']) if path != row['path']]
        if row['group_rank'] is not None:
            row['role'] = 'core'
        elif row['supports']:
            row['role'] = 'supporting'
        elif row['behaviors'] and all(b == 'unchanged' for b in row['behaviors']) and row['rank'] < 3 and not row['hard_floors']:
            row['role'] = 'mechanical'
        # 경로만 보고 붙은 힌트다. 종류 신호·바닥·내용 근거가 있으면 그쪽으로 보여 준다.
        row['hint'] = bool(row['rank'] == 1 and row['location_hints'] and row['role'] != 'core'
                           and not (row['kind_signals'] or row['content_hits'] or row['hard_floors']))
    return sorted(files.values(), key=lambda r: (review_bucket(r), -r['rank'],
                                                  r['group_rank'] if r['group_rank'] is not None else
                                                  r['focus_rank'] if r['focus_rank'] is not None else 999999, r['path']))


def file_counts(rows):
    counts = {'high': 0, 'mid': 0, 'low': 0, 'unanalyzed': 0}
    for row in rows:
        counts[level_of(row)] += 1
    return counts


def top_file_rows(rows):
    if not rows or rows[0].get('legacy', True):
        # 낮음·미분석 파일은 규칙 후보가 없으면 숫자로만 보여준다.
        return [row for row in rows if row['rank'] >= 2 or row['rule']][:3]
    cores = sorted((row for row in rows if row['role'] == 'core'), key=lambda r: (-r['rank'], r['group_rank']))[:3]
    if not cores:
        cores = [row for row in rows if row['rank'] == 2][:max(0, 3 - sum(row['rank'] == 3 for row in rows))]
    chosen = {row['path'] for row in cores}
    # 높음은 개수와 상관없이 이름으로 남긴다. 역할 판단이 틀려도 높음이 숫자 뒤로 사라지지 않는다.
    highs = [row for row in rows if row['rank'] == 3 and row['path'] not in chosen]
    return cores + highs if cores and cores[0]['role'] == 'core' else highs + cores


def first_look(rows):
    """홈 카드와 상세 ‘먼저 볼 파일’이 같이 쓰는 선정 결과."""
    top = top_file_rows(rows)
    shown = {row['path'] for row in top}
    safety = [row for row in rows if row['path'] not in shown
              and (row['kind_signals'] or row['hard_floors'] or (not row['rank'] and row['location_hints']))]
    return dict(top=top, safety=safety[:SAFETY_LIMIT], safety_more=max(0, len(safety) - SAFETY_LIMIT),
                hints=sum(row['hint'] for row in rows), mechanical=sum(foldable(row) for row in rows))


def related_summary(row):
    tests = sum(entry['relation'] == 'test' for entry in row['related'])
    others = len(row['related']) - tests
    return ' · '.join(([f'연관 {others}'] if others else []) + ([f'테스트 {tests}'] if tests else []))


def signal_names(row):
    flags = sorted(row['kind_signals'] | row['hard_floors']) or sorted(row['location_hints'])
    return ', '.join(FLAG_LABELS.get(flag, flag) for flag in flags)


def file_badges(row, prefix=''):
    """역할과 중요도를 나란히 적는다. 위험색은 모델이 높음으로 본 파일에만 쓴다."""
    level = level_of(row)
    if row.get('legacy', True):
        return f'<span class="badge {level}">{prefix}{esc(label(level))}</span>'
    signalled = row['kind_signals'] or row['hard_floors'] or row['location_hints']
    name = ('확인' if signalled else '미분석') if level == 'unanalyzed' else prefix + label(level)
    chips = ''
    if row['role'] in ('core', 'supporting') or (row['role'] == 'mechanical' and not row['hint']):
        chips += f'<span class="badge role">{ROLE_LABELS[row["role"]]}</span>'
    if row['hint']:
        chips += '<span class="badge hint">경로 확인</span>'
    return f'<span class="file-badges">{chips}<span class="badge {level}">{esc(name)}</span></span>'


def look_footer(look, file_href, base=''):
    """목록에 못 든 종류 신호·바닥은 ‘안전 확인’ 줄로, 경로 힌트와 기계적 변경은 숫자로 남긴다."""
    body = ''
    if look['safety']:
        body += '<p class="file-summary safety-line"><span class="safety-label">안전 확인</span>'
        for row in look['safety']:
            body += (f'<a class="safety-item" href="{esc(file_href(row["path"]))}"><span class="file-path">{esc(row["path"])}</span>'
                     f'<span class="badge hint">{esc(signal_names(row))}</span>'
                     + ('' if row['rank'] else '<span class="badge unanalyzed">확인</span>') + '</a>')
        body += (f'<span>외 {look["safety_more"]}개</span>' if look['safety_more'] else '') + '</p>'
    parts = ([f'<a href="{esc(base)}?view=hints#file-changes">경로상 확인 {look["hints"]}</a>'] if look['hints'] else []) + (
        [f'기계적 변경 {look["mechanical"]}'] if look['mechanical'] else [])
    if parts:
        body += '<p class="file-summary hint-line">' + ' · '.join(parts) + '</p>'
    return body


def distribution(rows, shown):
    # CSP가 style 속성을 막으므로 비율을 20칸으로 나눠 그린다. 0이 아닌 값은 최소 한 칸을 받는다.
    counts = file_counts(rows)
    cells = []
    for key in ('high', 'mid', 'low', 'unanalyzed'):
        if counts[key]:
            cells += [key] * max(1, round(20 * counts[key] / len(rows)))
    bar = ''.join(f'<span class="seg {key}"></span>' for key in cells[:20])
    rest = len(rows) - shown
    return (f'<p class="file-summary"><span class="dist" role="img" aria-label="파일 {len(rows)}개 중요도 분포">{bar}</span>'
            f'<span>파일 {len(rows)}개' + (f' (외 {rest}개)' if rest and shown else '') + '</span>'
            + ''.join(f'<span>{label(k)} {counts[k]}</span>' for k in ('high', 'mid', 'low', 'unanalyzed') if counts[k] or k != 'unanalyzed')
            + '</p>')


def legacy_file_card(pr, rows):
    top = top_file_rows(rows)
    body = ''
    if not top:
        body += '<p class="muted">높음, 보통 파일이 없습니다.</p>' if pr['items'] else '<p class="muted">분석 전입니다.</p>'
    for row in top:
        level = level_of(row)
        name = '확인' if level == 'unanalyzed' else label(level)
        href = f'/pr/{int(pr["id"])}?' + urlencode([('file', row['path'])]) + '#' + file_anchor(row['path'])
        notes = ([('목적 관련: ' + row['focus_reason'])] if row['focus_reason'] else []) + ([row['reason']] if row['reason'] else []) + (
            ['규칙 후보: ' + ', '.join(FLAG_LABELS.get(f, f) for f in sorted(priority_rules(row)))] if priority_rules(row) else []) + (
            ['모델 판단: ' + ', '.join(FLAG_LABELS.get(f, f) for f in sorted(row['model']))] if row['model'] else [])
        body += (f'<a class="file-row" href="{esc(href)}"><span class="badge {level}">{name}</span>'
                 f'<span class="file-path">{esc(row["path"])}</span><span class="file-note">' + ''.join(f'<span>{esc(n)}</span>' for n in notes) + '</span></a>')
    other_candidates = sum(bool(priority_rules(row)) for row in rows if row not in top)
    if other_candidates:
        body += f'<p class="file-summary"><a href="/pr/{int(pr["id"])}?order=priority">그 외 규칙 기반 후보 {other_candidates}개 확인</a></p>'
    return body + (distribution(rows, len(top)) if rows else '')


def file_card(pr):
    shown = current_view(pr)
    rows = file_rows(shown)
    body = '<div class="pr-files"><h3>먼저 볼 파일</h3>'
    if rows and rows[0]['legacy']:
        return body + legacy_file_card(shown, rows) + '</div>'
    base = f'/pr/{int(pr["id"])}'
    def file_href(path):
        return base + '?' + urlencode([('file', path)]) + '#' + file_anchor(path)
    look = first_look(rows)
    if not look['top']:
        body += ('<p class="muted">높음, 보통 파일이 없습니다.</p>' if shown['items'] else
                 '<p class="muted">최신 변경의 분석 결과가 없습니다.</p>' if pr['items'] else '<p class="muted">분석 전입니다.</p>')
    for row in look['top']:
        core = row['role'] == 'core'
        reason = (row['group_reason'] if core else '') or row['reason']
        summary = related_summary(row)
        notes = (('<span class="badge role">핵심</span>' if core else '') + (f'<span>{esc(reason)}</span>' if reason else '')
                 + (f'<span class="badge role">{esc(summary)}</span>' if summary else '')
                 + ('<span>모델 판단: ' + esc(', '.join(FLAG_LABELS.get(f, f) for f in sorted(row['model']))) + '</span>' if row['model'] else ''))
        level = level_of(row)
        body += (f'<a class="file-row" href="{esc(file_href(row["path"]))}"><span class="badge {level}">{label(level)}</span>'
                 f'<span class="file-path">{esc(row["path"])}</span><span class="file-note">{notes}</span></a>')
    body += look_footer(look, file_href, base)
    return body + (distribution(rows, len(look['top'])) if rows else '') + '</div>'


def constant_equal(left, right):
    return hmac.compare_digest(left.encode('utf-8'), right.encode('utf-8'))


def esc(value):
    return escape(str(value if value is not None else ''), quote=True)


def meta(*values):
    content = ''.join(f'<span>{esc(value)}</span>' for value in values if value not in (None, ''))
    return '<p class="meta">' + content + '</p>' if content else ''


def page(title, content, active=None, detail_page=False):
    links = [('/', 'PR 리뷰'), ('/repos', '저장소'), ('/status', '상태'), ('/settings', '설정')]
    nav = ''.join(f'<a href="{href}"' + (' aria-current="page"' if active == href else '')
                  + f'>{label}</a>' for href, label in links)
    return ('<!doctype html><html lang="ko"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{esc(title)} | skim</title><link rel="stylesheet" href="/static/app.css"></head><body' + (' class="pr-detail-page"' if detail_page else '') + '>'
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
    for raw in conn.execute("SELECT p.*,r.host,r.owner,r.name,r.tracked,EXISTS(SELECT 1 FROM pr_sources WHERE pr_id=p.id AND source='review_request') AS review FROM pull_requests p JOIN repositories r ON r.id=p.repo_id WHERE p.state='open' AND r.host IN (SELECT host FROM github_hosts)"):
        pr = dict(raw)
        snapshot = conn.execute('SELECT * FROM snapshots WHERE pr_id=? ORDER BY id DESC LIMIT 1', (pr['id'],)).fetchone()
        analysis = conn.execute('SELECT a.*,s.head_sha,s.base_sha FROM analyses a JOIN snapshots s ON s.id=a.snapshot_id WHERE s.pr_id=? ORDER BY s.id DESC,a.id DESC LIMIT 1', (pr['id'],)).fetchone()
        latest_analysis = analysis
        previous_result = False
        if analysis and analysis['status'] not in ('completed', 'partial'):
            successful = conn.execute(
                "SELECT a.*,s.head_sha,s.base_sha FROM analyses a JOIN snapshots s ON s.id=a.snapshot_id "
                "WHERE a.snapshot_id=? AND a.status IN ('completed','partial') ORDER BY a.id DESC LIMIT 1",
                (analysis['snapshot_id'],)).fetchone()
            if successful:
                analysis = successful
                previous_result = True
        result = json.loads(analysis['output_json'] or '{}') if analysis else {}
        units = result.get('units', [])
        if not units and snapshot:
            coverage = json.loads(snapshot['coverage'])
            units, _ = parse_diff(read_diff(config, snapshot), coverage.get('files', []))
        items = {item['change_id']: item for item in result.get('items', [])}
        if snapshot and analysis and snapshot['id'] != analysis['snapshot_id']:
            units, _ = parse_diff(read_diff(config, snapshot), json.loads(snapshot['coverage']).get('files', []))
            items = {}
        pr.update(snapshot=snapshot, analysis=analysis, latest_analysis=latest_analysis, previous_result=previous_result,
                  result=result, units=units, items=items,
                  status=latest_analysis['status'] if latest_analysis else 'unanalyzed',
                  stale=bool(analysis and (analysis['head_sha'] != pr['head_sha'] or analysis['base_sha'] != pr['base_sha'] or (snapshot and snapshot['id'] != analysis['snapshot_id']))))
        pr['importance'] = max((item['importance'] for item in items.values()), key=lambda x: RANK.get(x, 0), default='unanalyzed')
        pr['rule_flags'] = sorted({flag for unit in units for flag in unit.get('candidates', [])})
        pr['model_flags'] = sorted({flag for item in items.values() for flag in item.get('risk_flags', [])})
        pr['flags'] = sorted(set(pr['rule_flags']) | set(pr['model_flags']))
        pr['unanalysed'] = not units or any(u['change_id'] not in items for u in units)
        pr['allowed'] = analysis_allowed(conn, pr['repo_id'])
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
    return ((f'<span class="badge {esc(pr["importance"])}">중요도 {esc(label(pr["importance"]))}</span> '
             if pr["importance"] != "unanalyzed" else "")
            + ''.join(f'<span>{esc(label)}</span>' for label in labels)
            + ('<span class="badge">리뷰 요청</span>' if pr.get('review') else '')
            + ('<span>이전 SHA 결과</span>' if pr['stale'] else '') + ('<span>draft</span>' if pr['draft'] else ''))


def repository_label(pr):
    prefix = '' if pr.get('host', 'github.com') == 'github.com' else pr['host'] + '/'
    return prefix + pr['owner'] + '/' + pr['name']


def list_controls(items, query, prefix, label, text, search_form=True, fragment=""):
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
    action = f' action="{esc(fragment)}"' if fragment else ''
    controls = (f'<form class="filters" method="get"{action}>{hidden}<label>{esc(label)} 검색 '
                f'<input type="search" name="{search_key}" value="{esc(term)}" maxlength="200"></label>'
                '<button class="btn secondary">검색</button></form>') if search_form else ''
    if pages > 1:
        controls += f'<p class="meta">{len(filtered)}개 중 {number}/{pages} 페이지</p>'
    if not filtered and search_form:
        controls += '<p class="muted">검색 결과가 없습니다.</p>' if term else ''
    def link(target, title):
        params = [(key, value) for key, values in query.items() if key != page_key for value in values]
        params.append((page_key, str(target)))
        return f'<a class="btn secondary sm" href="?{esc(urlencode(params))}{esc(fragment)}">{title}</a>'
    navigation = '<nav class="toolbar" aria-label="' + esc(label) + ' 페이지">'
    if number > 1:
        navigation += link(number - 1, '이전')
    if number < pages:
        navigation += link(number + 1, '다음')
    navigation += '</nav>'
    return filtered[(number - 1) * 20:number * 20], controls, navigation


# 메인 탭: 지금 볼 PR을 고르는 수치이자 필터. 탭마다 하나의 조건만 건다.
TABS = [('', '전체', lambda pr: True),
        ('review', '리뷰 요청', lambda pr: bool(pr['review'])),
        ('high', '높음 파일 있음', lambda pr: priority_rank(pr) == 3),
        ('pending', '분석 전', lambda pr: pr['status'] == 'unanalyzed' or not pr['allowed'])]


def priority_rank(pr):
    if not pr['allowed'] or pr['stale'] or (pr['status'] not in ('completed', 'partial') and not pr.get('previous_result')):
        return 0
    return RANK.get(pr['importance'], 0)


def home_team_filter(server, conn, prs, query, csrf, offline):
    """팀 조회는 POST 작업으로 실행하고 유효한 결과만 메인 필터에 사용한다."""
    from . import jobs
    from .web_settings import form
    hosts = [row[0] for row in conn.execute('SELECT host FROM github_hosts ORDER BY host')]
    host = query.get('team_host', ['github.com'])[0]
    if host not in hosts:
        host = 'github.com'
    team = query.get('team', [''])[0]
    valid, truncated, teams = server.repo_lookup.team_snapshot(host)
    latest = jobs.latest(conn, 'repos')
    active = jobs.active(conn, 'repos')
    params = json.loads(latest['params'] or '{}') if latest else {}
    failed = bool(latest and params.get('home_teams') and params.get('host') == host
                  and latest['status'] in ('failed', 'blocked', 'cancelled'))
    if failed:
        valid, teams = False, {}
    description = (host + (' / ' + team if team else '') if query.get('team_host', [''])[0] else '모든 호스트')
    body = '<details class="home-team-filter"' + (' open' if query.get('team_host', [''])[0] or team else '') + '><summary>호스트 및 팀 필터: ' + esc(description) + '</summary><form method="get" class="team-filter-form">'
    body += ''.join(f'<input type="hidden" name="{key}" value="{esc(query[key][0])}">' for key in ('pr_q', 'repo', 'tab') if query.get(key))
    host_selected = query.get('team_host', [''])[0]
    body += '<label>호스트<select name="team_host"><option value="">모든 호스트</option>' + ''.join(
        f'<option value="{esc(h)}"' + (' selected' if h == host_selected else '') + f'>{esc(h)}</option>' for h in hosts) + '</select></label>'
    body += '<label>소속 팀<select name="team"><option value="">이 호스트의 모든 연결 저장소</option>'
    if team and team not in teams:
        body += f'<option value="{esc(team)}" selected>{esc(team)} (재조회 필요)</option>'
    body += ''.join(f'<option value="{esc(name)}"' + (' selected' if team == name else '') + f'>{esc(name)}</option>'
                    for name in sorted(teams, key=str.casefold)) + '</select></label>'
    body += '<button class="btn secondary sm">팀 필터 적용</button></form>'
    if not offline:
        preserved = ''.join(f'<input type="hidden" name="{key}" value="{esc(query[key][0][:200])}">' for key in ('team', 'pr_q', 'repo', 'tab') if query.get(key))
        body += form('/home/teams', csrf, f'<input type="hidden" name="host" value="{esc(host)}">' + preserved + f'<button>{esc(host)} 팀 목록 새로고침</button>')
    if query.get('team_busy'):
        body += '<p class="notice">다른 저장소 조회가 진행 중입니다. 완료 후 팀 목록을 다시 새로고침하세요.</p>'
    if active:
        body += web_jobs.job_box(active).replace('<div class="job"', '<div class="job" data-job-reload="1"', 1)
    elif failed:
        body += '<p class="muted">팀 조회에 실패했습니다. GitHub 권한과 조직 SSO를 확인한 뒤 다시 조회하세요.</p>'
    elif not valid:
        body += ('<p class="muted">오프라인 모드에서는 팀 목록을 조회할 수 없습니다.</p>' if offline else '<p class="muted">팀 목록이 없거나 만료됐습니다. 팀 목록을 새로고침하세요.</p>')
    elif truncated:
        body += '<p class="muted">조회 한도에 도달해 일부 팀 또는 저장소만 표시합니다.</p>'
    else:
        body += '<p class="muted">팀 목록은 60초 동안 유지됩니다.</p>'
    if query.get('team_host'):
        prs = [pr for pr in prs if pr.get('host', 'github.com') == host]
    if team:
        if not valid or team not in teams:
            body += '<p class="notice">선택한 팀의 현재 저장소를 확인할 수 없습니다. 팀 목록을 다시 조회하세요.</p>'
            prs = []
        else:
            names = teams[team]
            prs = [pr for pr in prs if pr.get('host', 'github.com') == host
                   and (pr['owner'] + '/' + pr['name']).casefold() in names]
    return prs, body + '</details>'


def listing(prs, query, csrf, heading_toolbar="", team_filter="", has_connected_prs=None):
    # 메인은 내가 연결(관리)한 저장소의 PR만 다룬다. 다른 저장소의 리뷰 요청은 상세 링크로만 열린다.
    prs = sorted((pr for pr in prs if pr.get('tracked', 1)),
                 key=lambda pr: (-priority_rank(pr), -bool(pr['review']), -pr['id']))
    has_prs = bool(prs) if has_connected_prs is None else has_connected_prs
    repos = sorted({repository_label(p) for p in prs})
    content = ('<div class="heading home-heading"><div><h1>PR 리뷰</h1>'
               '<p class="home-description">연결한 저장소의 열린 PR을 중요도 순으로 확인하세요.</p></div>' + heading_toolbar + '</div>'
               '<section class="home-summary" aria-label="검토 현황">'
               f'<div><span>열린 PR</span><strong>{len(prs)}</strong></div>'
               f'<div><span>내 리뷰 요청</span><strong>{sum(bool(pr["review"]) for pr in prs)}</strong></div>'
               f'<div><span>중요도 높음</span><strong>{sum(priority_rank(pr) == 3 for pr in prs)}</strong></div>'
               '</section><section class="home-results" aria-labelledby="pr-list-title">'
               '<div class="home-list-heading"><h2 id="pr-list-title">검토할 PR</h2>'
               '<p>중요도 순, 같은 중요도에서는 리뷰 요청 우선, 미확인은 마지막</p></div>')
    # 검색어와 저장소는 한 폼으로 제출하고, 탭 선택은 숨은 값으로 유지한다.
    tab = query.get('tab', [''])[0] if query.get('tab', [''])[0] in {key for key, _, _ in TABS} else ''
    content += team_filter
    content += '<form class="filters pr-filters" method="get">' + ''.join(f'<input type="hidden" name="{key}" value="{esc(query[key][0])}">' for key in ('team_host', 'team') if query.get(key)) + (f'<input type="hidden" name="tab" value="{tab}">' if tab else '')
    content += ('<label class="pr-search"><span class="sr-only">PR 검색</span><input type="search" name="pr_q" value="' + esc(query.get('pr_q', [''])[0][:200])
                + '" maxlength="200" placeholder="제목, 저장소, 작성자로 검색"></label>')
    if len(repos) > 1:
        content += '<label><span class="sr-only">저장소</span><select name="repo"><option value="">모든 저장소</option>' + ''.join(
            f'<option value="{esc(r)}"' + (' selected' if query.get('repo', [''])[0] == r else '') + f'>{esc(r)}</option>' for r in repos) + '</select></label>'
    filtering = any(query.get(name, [''])[0] for name in ('pr_q', 'repo', 'importance', 'status', 'review', 'tab'))
    content += '<button class="btn secondary">검색</button>' + ('<a class="btn ghost" href="/">초기화</a>' if filtering else '') + '</form>'
    # 탭 수치는 검색·저장소 조건을 반영한 뒤 센다.
    term = query.get('pr_q', [''])[0][:200].casefold()
    base = [pr for pr in prs if (not query.get('repo', [''])[0] or repository_label(pr) == query['repo'][0])
            and (not term or term in ' '.join([repository_label(pr), str(pr['number']), pr['title'] or '', pr['author'] or '']).casefold())]
    keep = [(k, query[k][0]) for k in ('pr_q', 'repo', 'team_host', 'team') if query.get(k, [''])[0]]
    content += '<nav class="pr-tabs" aria-label="PR 보기">' + ''.join(
        f'<a href="/?{esc(urlencode(keep + ([("tab", key)] if key else [])))}"' + (' aria-current="page"' if key == tab else '')
        + f'><span>{title}</span><strong>{sum(map(test, base))}</strong></a>' for key, title, test in TABS) + '</nav>'
    prs = [pr for pr in prs if next(test for key, _, test in TABS if key == tab)(pr)]
    prs = [pr for pr in prs if all(
        not query.get(key, [''])[0] or query[key][0] == value
        or (key == 'status' and query[key][0] == 'stale' and pr['stale'])
        for key, value in dict(repo=repository_label(pr), importance=pr['importance'],
                               status=pr['status'], review=str(pr['review'])).items())]
    prs, controls, navigation = list_controls(prs, query, 'pr', 'PR', lambda p: ' '.join([repository_label(p), str(p['number']), p['title'] or '', p['author'] or '', (p['analysis']['intent'] or '') if p['analysis'] else '']), search_form=False)
    content += controls
    if has_prs and not prs:
        content += '<p class="empty">조건에 맞는 PR이 없습니다. <a href="/">전체 보기</a></p>'
    collected = {human_time(pr['snapshot']['collected_at']) if pr['snapshot'] else '대기' for pr in prs}
    shared_collection = len(collected) == 1
    analyzed = {human_time(pr['analysis']['created_at']) if pr['analysis'] else '대기' for pr in prs}
    shared_analysis = len(analyzed) == 1
    if shared_collection or shared_analysis:
        content += meta(*(['수집 ' + next(iter(collected))] if shared_collection else []),
                        *(['분석 ' + next(iter(analyzed))] if shared_analysis else []))
    content += '<div class="list">'
    for pr in prs:
        values = dict(repo=repository_label(pr), importance=pr['importance'], status=pr['status'], review=str(pr['review']))
        a, s = pr['analysis'], pr['snapshot']
        state = ''
        if not pr['allowed']:
            target = urlencode([('scope_host', pr.get('host', 'github.com')), ('tracked_q', pr['owner'] + '/' + pr['name'])])
            state = f'<p class="pr-state"><span>분석 허용 안 됨</span><a href="/repos?{esc(target)}#analysis-scope">분석 허용 범위 열기</a></p>'
        elif pr['status'] in ('failed', 'blocked', 'deferred', 'cancelled') or pr['stale']:
            state = '<p class="pr-state"><span>' + esc(label('stale' if pr['stale'] else pr['status'])) + f'</span><a href="/pr/{pr["id"]}">상세에서 다시 분석</a></p>'
        elif pr['status'] == 'unanalyzed':
            state = f'<p class="pr-state"><span>분석 대기</span><a href="/pr/{pr["id"]}">상세에서 분석 요청</a></p>'
        elif pr['status'] in ('completed', 'partial') and (not a or not a['intent']):
            state = f'<p class="pr-state"><span>변경 의도 미분석</span><a href="/pr/{pr["id"]}">상세에서 다시 분석</a></p>'
        if pr.get('previous_result'):
            state += '<p class="pr-state">직전 성공 분석 결과를 표시합니다.</p>'
        rank = priority_rank(pr)
        priority = label({3: 'high', 2: 'mid', 1: 'low'}.get(rank, 'unanalyzed')) if rank else '우선순위 미확인'
        content += ('<article class="issue-row"><div class="row-content">' + f'<h2><a href="/pr/{pr["id"]}">{esc(pr["title"])}</a></h2>'
                    + meta(values['repo'] + ' #' + str(pr['number']), pr['author'], *(['draft'] if pr['draft'] else []))
                    + '<p class="home-pr-badges"><span class="badge ' + ({3: 'high', 2: 'mid', 1: 'low'}.get(rank, 'unanalyzed')) + '">' + esc(('중요도 ' if rank else '') + priority) + '</span>'
                    + ('<span class="badge review">리뷰 요청 받음</span>' if pr['review'] else '')
                    + (f'<span class="badge">{esc(label(pr["status"]))}</span>' if pr['allowed'] and not pr['stale'] and pr['status'] in ('completed', 'partial') else '') + '</p>'
                    + f'<p class="issue-intent"><strong>변경 의도</strong> {esc(a["intent"] if a and a["intent"] else "분석 결과 없음")}</p>'
                    + state + file_card(pr)
                    + meta(*([] if shared_collection else ['수집 ' + (human_time(s['collected_at']) if s else '대기')]),
                           *([] if shared_analysis else ['분석 ' + (human_time(a['created_at']) if a else '대기')])) + '</div></article>')
    return content + '</div>' + navigation + ('<div class="empty"><p><strong>아직 수집된 열린 PR이 없습니다.</strong></p>'
                                              '<p>저장소를 연결한 뒤 수집하면 이곳에 표시됩니다.</p>'
                                              '<p><a class="btn primary" href="/repos">저장소 연결하기</a> <a class="btn secondary" href="/status">상태 확인</a></p></div>' if not has_prs else '') + '</section>'


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


# 변경 diff에 보이는 정의만 후보로 삼는다. 언어별 이름 해석이나 전체 저장소 색인은 하지 않는다.
SYMBOL_CALL = re.compile(r'\b([A-Za-z_][A-Za-z0-9_]*)\s*(?=\()')
SYMBOL_DEF = re.compile(r'^\s*(?:async\s+)?(?:def|function|func)\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(|^\s*(?:(?:public|private|protected|static|final|async|override|virtual|internal|protected|abstract|synchronized)\s+)*(?:[A-Za-z_][\w.<>,?\[\]]*\s+)+([A-Za-z_][A-Za-z0-9_]*)\s*\(')
SYMBOL_SKIP = frozenset(('if', 'for', 'while', 'switch', 'catch', 'return', 'new', 'throw', 'sizeof', 'print'))


def definition_name(text):
    if re.match(r'^\s*(?:return|await|throw|yield|raise|new|class|interface|enum|struct|with|assert|if|elif|else|for|while|try|except|finally|import|from|del|case|switch|catch|using)\b', text):
        return None
    match = SYMBOL_DEF.match(text)
    return (match.group(1) or match.group(2)) if match else None


# 같은 이름의 컬렉션·표준 라이브러리 메서드를 프로젝트 함수로 단정하지 않는다.
BUILTIN_METHODS = frozenset(('add', 'append', 'clear', 'contains', 'count', 'extend', 'filter', 'find',
                             'forEach', 'get', 'has', 'includes', 'indexOf', 'insert', 'join', 'keys',
                             'length', 'map', 'pop', 'push', 'reduce', 'remove', 'replace', 'set',
                             'size', 'sort', 'split', 'toString', 'values'))


def code_symbols(line):
    """한 줄에서 문자열·주석을 가리고 함수 이름의 위치를 찾는다."""
    masked = list(line)
    quote = None
    escaped = False
    for index, char in enumerate(line):
        if quote:
            masked[index] = ' '
            if escaped:
                escaped = False
            elif char == chr(92):
                escaped = True
            elif char == quote:
                quote = None
        elif char in (chr(34), chr(39), '`'):
            quote = char
            masked[index] = ' '
        elif char == '#' or line[index:index + 2] in ('//', '/*'):
            masked[index:] = ' ' * (len(line) - index)
            break
    visible = ''.join(masked)
    definition = definition_name(visible[1:] if visible[:1] in (' ', '+', '-') else visible)
    found = []
    definition_seen = False
    for match in SYMBOL_CALL.finditer(visible):
        name = match.group(1)
        if name in SYMBOL_SKIP:
            continue
        is_definition = name == definition and not definition_seen
        before = visible[:match.start(1)].rstrip()
        if not is_definition and before.endswith('.') and name in BUILTIN_METHODS:
            continue
        found.append((match.start(1), match.end(1), name, 'definition' if is_definition else 'usage'))
        definition_seen = definition_seen or is_definition
    return found


def symbol_candidates(units, file_href):
    definitions, occurrences = {}, []
    for unit in units:
        change_id = unit.get('change_id', '')
        if not re.fullmatch(r'h[0-9]+', change_id) or unit.get('excluded'):
            continue
        old_line = new_line = None
        for number, line in enumerate(unit.get('diff', '').splitlines()):
            if line.startswith('@@'):
                match = re.match(r'@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@', line)
                old_line, new_line = (int(match.group(1)), int(match.group(2))) if match else (None, None)
                continue
            if old_line is None or not line.startswith((' ', '+', '-')):
                continue
            side = 'old' if line[0] == '-' else 'new' if line[0] == '+' else 'both'
            line_no = old_line if side == 'old' else new_line
            if side != 'new': old_line += 1
            if side != 'old': new_line += 1
            target = file_href(unit['file_path']).split('#', 1)[0] + '&symbol_from=1#symbol-' + change_id + '-' + str(number)
            for _, _, name, kind in code_symbols(line):
                item = {'path': unit['file_path'], 'side': side, 'href': target, 'kind': kind,
                        'line': None if unit.get('redacted') else line_no, 'unit': change_id}
                occurrences.append((name, item))
                if kind == 'definition': definitions.setdefault(name, []).append(item)
    result = {name: [] for name in definitions}
    for name, item in occurrences:
        if name in result and item not in result[name]:
            result[name].append(item)
    return result


def clickable_code(line, known_symbols, side):
    known_symbols = known_symbols or {}
    chunks, previous = [], 0
    for start, end, name, kind in code_symbols(line):
        definitions = known_symbols.get(name, [])
        if not any(item['kind'] == 'definition' and (side == 'both' or item['side'] in ('both', side)) for item in definitions):
            continue
        chunks.append(esc(line[previous:start]))
        chunks.append(f'<button type="button" class="symbol-call" data-symbol="{esc(name)}" aria-label="{esc(name)} 정의와 사용 위치 보기">{esc(name)}</button>')
        previous = end
    return ''.join(chunks) + esc(line[previous:])


def render_diff(diff, numbered=False, line_numbers=True, symbol_unit=None, symbol_ids=True, known_symbols=()):
    lines = []
    in_hunk = False
    old_line = new_line = None
    for line_index, line in enumerate(diff.splitlines(keepends=True)):
        if line.startswith('diff --git '):
            in_hunk = False
        if line.startswith('@@'):
            kind = 'hunk'
            in_hunk = True
            match = re.match(r'@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@', line)
            old_line, new_line = (int(match.group(1)), int(match.group(2))) if match else (None, None)
        elif not in_hunk and line.startswith(('+++ ', '--- ')):
            kind = 'header'
        elif line.startswith('+'):
            kind = 'add'
        elif line.startswith('-'):
            kind = 'delete'
        else:
            kind = 'context'
        if not numbered:
            lines.append(f'<span class="diff-{kind}">{esc(line)}</span>')
            continue
        left = right = ''
        if line_numbers and in_hunk and kind != 'hunk' and not line.startswith('\\'):
            if kind != 'add' and old_line is not None:
                left, old_line = old_line, old_line + 1
            if kind != 'delete' and new_line is not None:
                right, new_line = new_line, new_line + 1
        text = line.rstrip(chr(10)).rstrip(chr(13))
        side = 'old' if kind == 'delete' else 'new' if kind == 'add' else 'both'
        target = f' id="symbol-{symbol_unit}-{line_index}"' if symbol_unit and symbol_ids and in_hunk and any(name in known_symbols for _, _, name, _ in code_symbols(text)) else ''
        code = clickable_code(text, known_symbols, side) if symbol_unit and in_hunk and kind in ('add', 'delete', 'context') else esc(text)
        lines.append(f'<span class="diff-{kind} diff-line"{target} data-symbol-side="{side}"><span class="diff-number">{left}</span>'
                     f'<span class="diff-number">{right}</span><span class="diff-code">{code}</span></span>')
    return '<pre class="diff' + (' numbered' if numbered else '') + '">' + ''.join(lines) + '</pre>'



def file_label(path):
    parent, _, name = path.rpartition('/')
    directory = '…/' + parent.rsplit('/', 1)[-1] if parent else ''
    return (f'<span class="file-label" title="{esc(path)}" aria-label="{esc(path)}">'
            f'<span class="file-basename" aria-hidden="true">{esc(name)}</span>'
            + (f'<span class="file-directory" aria-hidden="true">{esc(directory)}</span>' if directory else '') + '</span>')


def full_path_title(path):
    """선택한 파일은 같은 이름 파일을 구분할 수 있게 전체 경로를 보여준다."""
    parent, _, name = path.rpartition('/')
    return (f'<span class="file-path-full" title="{esc(path)}">' + (f'<span class="file-parent">{esc(parent)}/</span>' if parent else '')
            + f'<span class="file-basename">{esc(name)}</span></span>')


def file_nav(neighbors):
    """작업 공간에서 앞뒤 파일로 넘긴다. 링크라서 스크립트 없이도 동작한다."""
    previous, following = neighbors
    def link(target, text, rel):
        if not target:
            return f'<span class="btn ghost sm" aria-disabled="true">{text}</span>'
        href, path = target
        return (f'<a class="btn secondary sm" rel="{rel}" href="{esc(href)}" data-select-file="{file_anchor(path)}"'
                f' aria-label="{text}: {esc(path)}">{text}</a>')
    return ('<nav class="file-step" aria-label="파일 이동">' + link(previous, '← 이전 파일', 'prev')
            + '<span class="file-step-hint">단축키 j 다음, k 이전</span>' + link(following, '다음 파일 →', 'next') + '</nav>')


def file_panel(row, members, items, github_link, *, active=True, neighbors=None, inline=False, position=None, known_symbols=(), file_href=None):
    path = row['path']
    attributes = '' if inline else (f' id="{file_anchor(path)}" tabindex="-1" data-file-panel'
                                    + (f' data-file-position="{position}"' if position else '')
                                    + f' data-file-path="{esc(path)}"'
                                    + ('' if active else ' hidden'))
    content = f'<section class="detail-file"{attributes}><header class="detail-file-heading"><h3 class="file-title">{full_path_title(path)}</h3>{file_badges(row, '중요도 ')}<span class="file-count">변경 {len(members)}개</span></header>'
    if row['group_reason'] or row['focus_reason']:
        content += '<p class="file-focus-reason"><strong>중요한 이유</strong> ' + esc(row['group_reason'] or row['focus_reason']) + '</p>'
    # 연관 경로는 합성 결과의 변경 ID에서만 가져온다.
    for title, entries in (('연관 파일', row['related']), ('핵심 변경', [dict(path=p, relation=None) for p in row['supports']])):
        if entries and file_href:
            content += f'<p class="related-files"><strong>{title}</strong>' + ''.join(
                f'<a href="{esc(file_href(entry["path"]))}">{esc(entry["path"])}</a>'
                + (f'<span class="badge hint">{esc(RELATION_LABELS.get(entry["relation"], "기타"))}</span>' if title == '연관 파일' else '')
                for entry in entries) + '</p>'
    if 'logic_without_tests' in row['rule'] and (row['rank'] >= 2 or row['focus_rank'] is not None):
        content += ('<details class="test-change-note"><summary>관련 테스트 변경 미확인</summary>'
                    '<p>이번 PR의 diff에서 이 파일과 연결되는 테스트 변경을 확인하지 못했습니다. 기존 테스트의 유무나 실행 결과는 확인하지 않았습니다.</p></details>')
    if github_link:
        digest = hashlib.sha256(path.encode()).hexdigest()
        content += f'<p><a href="{esc(github_link)}/files#diff-{digest}" target="_blank" rel="noopener noreferrer">GitHub에서 이 파일 보기 ↗<span class="sr-only"> (새 탭)</span></a></p>'
    for unit in members:
        item = items.get(unit['change_id'])
        reason = item['reason'] if item else unit.get('exclusion_reason') or unit.get('omission_reason') or '분석되지 않은 변경입니다.'
        content += '<div class="file-change"><p class="change-reason">' + esc(reason) + '</p>'
        if unit.get('redacted'):
            content += '<p class="muted">일부 민감 정보를 가려 줄 번호를 표시하지 않습니다. GitHub 원문에서 위치를 확인하세요.</p>'
        if unit.get('diff'):
            content += render_diff(unit['diff'], numbered=True, line_numbers=not unit.get('redacted'),
                                   symbol_unit=unit['change_id'] if re.fullmatch(r'h[0-9]+', unit.get('change_id', '')) else None,
                                   symbol_ids=not inline, known_symbols=known_symbols)
        if 'location_hints' in unit or 'kind_signals' in unit:
            sources = [(title, unit.get(key) or []) for title, key in (
                ('경로상 확인', 'location_hints'), ('파일 종류 신호', 'kind_signals'), ('변경 내용 신호', 'content_hits'),
                ('안전 확인', 'hard_floors'), ('기계적 힌트', 'mechanical_hints'))]
        else:
            sources = [('규칙 후보', [flag for flag in unit.get('candidates', []) if flag != 'logic_without_tests'])]
        model_flags = item.get('risk_flags', []) if item else []
        if any(flags for _, flags in sources) or model_flags:
            content += '<details class="change-details"><summary>분석 근거 더보기</summary>'
            for title, flags in sources:
                if flags:
                    content += f'<p>{title}: ' + esc(', '.join(FLAG_LABELS.get(flag, flag) for flag in flags)) + '</p>'
            if model_flags:
                content += '<p>모델 판단: ' + esc(', '.join(FLAG_LABELS.get(flag, flag) for flag in model_flags)) + '</p>'
            content += '</details>'
        content += '</div>'
    if neighbors:
        content += file_nav(neighbors)
    return content + '</section>'

def detail(pr, query, config, offline=False, analysis_panel="", csrf=None):
    a, snapshot = pr['analysis'], pr['snapshot']
    latest = pr.get('latest_analysis') or a
    current = is_current(pr)
    shown = current_view(pr)
    rows = file_rows(shown)
    legacy = not rows or rows[0]['legacy']
    view = 'hints' if query.get('view') == ['hints'] else ''
    hint_paths = {row['path'] for row in rows if row['hint']}
    units_by_file = {}
    for unit in pr['units']:
        units_by_file.setdefault(unit['file_path'], []).append(unit)
    link = github(pr)
    def file_href(path, preserve_search=False):
        params = [('file', path)]
        order = query.get('order', [''])[0]
        if order in ('priority', 'original'):
            params.append(('order', order))
        # 필터 밖 파일로 가는 링크는 필터를 풀어야 목적 파일 패널이 열린다.
        if view and path in hint_paths:
            params.append(('view', view))
        if preserve_search and query.get('unit_q', [''])[0]:
            params.append(('unit_q', query['unit_q'][0][:200]))
        return '?' + urlencode(params) + '#' + file_anchor(path)
    candidates = symbol_candidates(pr['units'], file_href)
    content = '<div id="symbol-index" data-symbol-index="' + esc(json.dumps(candidates, ensure_ascii=False)) + '"></div>'
    content += ('<div class="symbol-chooser" id="symbol-chooser" hidden><div class="symbol-dialog" role="dialog" aria-modal="true" aria-labelledby="symbol-title" aria-describedby="symbol-description">'
                '<div class="symbol-dialog-heading"><div><span class="symbol-dialog-kicker">PR 변경 코드</span>'
                '<h2 id="symbol-title">함수 정의와 사용 위치</h2></div>'
                '<button type="button" class="symbol-close" data-symbol-close aria-label="함수 위치 닫기">×</button></div>'
                '<p class="symbol-dialog-description" id="symbol-description">이 PR에서 이름이 일치하는 위치입니다. 실제 연결 여부를 확인하세요.</p>'
                '<div class="symbol-groups" data-symbol-results></div>'
                '<p class="symbol-dialog-footer">이동 후 이전 위치로 돌아올 수 있습니다.</p></div></div>')
    content += f'<p><a href="/">← PR 목록</a></p><header class="detail-header"><h1>{esc(pr["title"])}</h1>'
    content += meta(repository_label(pr) + ' #' + str(pr['number']), pr['author'])
    if link:
        content += f'<p><a href="{esc(link)}" target="_blank" rel="noopener noreferrer">GitHub 원문 ↗<span class="sr-only"> (새 탭)</span></a></p>'
    session = None if offline else session_link(config, pr)
    if session:
        sid, url = session
        content += f'<p><a href="{esc(url)}" target="_blank" rel="noopener noreferrer">Sessio: {esc(sid)} ↗<span class="sr-only"> (새 탭)</span></a></p>'
    content += '</header>'
    if pr['stale']:
        content += '<aside class="detail-warning" role="status"><strong>이전 SHA 결과</strong><p>PR이 변경되어 기존 분석이 최신 변경과 다를 수 있습니다. 최신 변경을 수집한 뒤 다시 분석하세요.</p></aside>'
    elif not pr.get('allowed', True):
        content += '<aside class="detail-warning"><strong>분석 허용 필요</strong><p>이 저장소의 분석이 허용되지 않았습니다.</p></aside>'
    elif a and pr['status'] not in ('completed', 'partial'):
        content += '<aside class="detail-warning"><strong>' + esc(label(pr['status'])) + '</strong><p>' + esc(latest['error'] or '분석을 다시 요청하거나 상태를 확인하세요.') + '</p>'
        content += web_jobs.recovery_actions(csrf, offline, web_jobs.recovery_kind(latest['error'])) + '</aside>'
    if current and 'prompt_version' in a.keys():
        from .analyze.protocol import PROMPT_VERSION
        if a['prompt_version'] != PROMPT_VERSION:
            content += '<p class="notice">이 결과는 이전 중요도 기준으로 분석했습니다. 현재 기준을 적용하려면 다시 분석하세요.</p>'
    intent = a['intent'] if current and a['intent'] else '아직 최신 변경의 분석 결과가 없습니다.'
    content += '<section class="detail-overview" aria-label="PR 검토 요약"><article class="detail-insight"><p class="detail-kicker">PR 이해</p><h2>변경 의도</h2>'
    content += f'<p class="detail-intent">{esc(intent)}</p>'
    body = sanitize(pr.get('body') or '')
    if body.strip():
        content += '<details class="detail-description"><summary>PR 원래 설명 보기</summary><p>' + esc(body) + '</p></details>'
    if pr.get('previous_result'):
        content += '<p class="muted">직전 성공 분석 결과를 표시합니다.</p>'
    missing_tests = sum('logic_without_tests' in row['rule'] for row in rows)
    if missing_tests:
        content += (f'<p class="test-summary">관련 테스트 변경 미확인 파일 {missing_tests}개</p>'
                    '<p class="muted test-summary-help">이번 PR의 diff 기준이며 기존 테스트의 유무나 실행 결과는 확인하지 않았습니다.</p>')
    content += '</article></section>' + analysis_panel
    look = first_look(rows) if not legacy else None
    # 스키마 2 결과는 보존 기간 동안 예전 선정 그대로 보여 준다.
    top = ([] if not current else look['top'] if look else
           [row for row in rows if row['rank'] >= 2 or (row['rank'] and (priority_rules(row) or row['focus_rank'] is not None))][:3])
    content += '<section class="detail-focus"><div class="section-heading"><h2>먼저 볼 파일</h2><p>PR 목적과 중요도를 기준으로 확인할 변경입니다.</p></div>'
    if top:
        content += '<div class="focus-list">'
        for row in top:
            reason = row['group_reason'] or row['focus_reason'] or row['reason'] or '규칙에 따라 확인이 필요한 변경입니다.'
            summary = related_summary(row)
            content += (f'<details class="focus-entry"><summary class="focus-file">' + file_badges(row)
                        + f'<span class="focus-text">{file_label(row["path"])}<span class="focus-reason">{esc(reason)}</span>'
                        + (f'<span class="badge role">{esc(summary)}</span>' if summary else '') + '</span>'
                        '<span class="focus-toggle" aria-hidden="true">⌄</span></summary>'
                        '<div class="focus-inline">' + file_panel(row, units_by_file[row['path']], shown['items'], link, inline=True,
                                                                   known_symbols=candidates, file_href=file_href) + '</div></details>')
        content += '</div>'
    else:
        content += '<p class="muted">' + ('높음 또는 보통으로 분석된 파일이 없습니다.' if current else '분석 후 중요한 파일과 선정 이유를 확인할 수 있습니다.') + '</p>'
    if look:
        content += look_footer(look, file_href)
    content += '</section>'
    issues = [u for u in pr['units'] if u.get('excluded') or u.get('omission_reason') or (current and u['change_id'] not in shown['items'])]
    stats = json.loads(snapshot['coverage']).get('analysis', {}) if snapshot else {}
    extras = [(key, title) for key, title in (('redacted', '민감 정보 정제'), ('overview_missing', '개요 없이 분석한 변경'),
              ('synthesis_omitted', '결과 요약에서 빠진 변경'), ('synthesis_uncertainties_omitted', '결과 요약에서 빠진 불확실성')) if stats.get(key)]
    uncertainties = (pr.get('result') or {}).get('uncertainties', []) if current else []
    redacted_paths = list(dict.fromkeys(u['file_path'] for u in pr['units'] if u.get('redacted')))
    if issues or extras or uncertainties or redacted_paths or (current and pr['status'] == 'partial'):
        paths = list(dict.fromkeys(unit['file_path'] for unit in issues))
        valid_uncertainties = [u for u in uncertainties if isinstance(u, dict) and isinstance(u.get('reason'), str)]
        content += '<aside class="detail-caution"><div class="scope-heading"><strong>분석 범위 확인</strong><p>분석하지 못했거나 추가 확인이 필요한 부분입니다.</p></div><div class="scope-summary">'
        for title, count in [('제외 또는 미분석 파일', len(paths)), ('민감 정보 가림 파일', len(redacted_paths)), ('추가 확인', len(valid_uncertainties) + sum(stats[key] for key, _ in extras))]:
            if count:
                content += f'<span>{esc(title)} <strong>{count}</strong></span>'
        content += '</div>'
        if not (issues or extras or uncertainties or redacted_paths):
            content += '<p>일부 변경을 분석하지 못했습니다. <a href="/status">상태 확인</a></p>'
        if paths:
            content += '<details class="scope-details"><summary>제외 또는 미분석 파일 보기</summary><div class="caution-files">'
            for path in paths:
                reasons = list(dict.fromkeys(unit.get('exclusion_reason') or unit.get('omission_reason') or '분석 결과 없음'
                                             for unit in issues if unit['file_path'] == path))
                content += (f'<span><a href="{esc(file_href(path))}" data-select-file="{file_anchor(path)}">{esc(path)}</a>'
                            f' <span class="muted">{esc(", ".join(reasons))}</span></span>')
            content += '</div></details>'
        if redacted_paths:
            content += '<details class="scope-details"><summary>민감 정보를 가린 파일 보기</summary><div class="caution-files">'
            for path in redacted_paths:
                content += f'<a href="{esc(file_href(path))}" data-select-file="{file_anchor(path)}">{esc(path)}</a>'
            content += '</div></details>'
        if extras or valid_uncertainties:
            content += '<details class="scope-details"><summary>추가 확인 내용 보기</summary>'
            for key, title in extras:
                content += f'<p>{esc(title)} {esc(stats[key])} <a href="?order=priority#file-changes">전체 파일 확인</a></p>'
            paths_by_id = {unit['change_id']: unit['file_path'] for unit in pr['units']}
            for uncertainty in valid_uncertainties:
                path = paths_by_id.get(uncertainty.get('change_id'))
                if path:
                    content += f'<p><a href="{esc(file_href(path))}" data-select-file="{file_anchor(path)}">{esc(path)}</a>: {esc(uncertainty["reason"])}</p>'
            content += '</details>'
        content += '</aside>'
    if a or snapshot:
        content += '<details class="detail-diagnostics"><summary>수집 및 분석 상태 보기</summary>'
        if a:
            content += meta('상태: ' + label(pr['status']), '모델 ' + a['model'], 'Codex CLI ' + a['cli_version'])
        if snapshot:
            coverage = json.loads(snapshot['coverage'])
            content += meta('수집 상태: ' + coverage.get('status', '대기'), coverage.get('collection_error', ''))
            content += meta('분석 ' + str(stats.get('analyzed', 0)), '제외 ' + str(stats.get('excluded', 0)), '누락 ' + str(stats.get('omitted', 0)))
            if stats.get('overview_eligible'):
                content += meta('전체 맥락 발췌 ' + str(stats.get('overview_sampled', 0)) + '/' + str(stats['overview_eligible']))
            content += meta('수집 ' + human_time(snapshot['collected_at']), '분석 ' + (human_time(a['created_at']) if a else '대기'))
        content += '</details>'
    if query.get('symbol_from') == ['1']:
        content += '<button type="button" class="btn secondary sm symbol-return" data-symbol-return>← 이전 위치로 돌아가기</button>'
    content += '<section class="detail-changes" id="file-changes"><div class="section-heading"><h2>파일별 변경</h2><p>목록에서 파일을 선택하고 변경 내용과 검토 근거를 확인하세요.</p></div><nav class="toolbar segments" aria-label="변경 보기">'
    for href, title, selected in [('?order=original', '원래 파일 순서', query.get('order', [''])[0] == 'original' and query.get('diff') != ['1']),
                                  ('?order=priority', '검토 우선순위', query.get('order', [''])[0] != 'original' and query.get('diff') != ['1']),
                                  ('?diff=1', '전체 diff', query.get('diff', [''])[0] == '1')]:
        params = parse_qs(href[1:])
        for key in ('unit_q', 'file', 'view'):
            if query.get(key, [''])[0]:
                params[key] = query[key][:1]
        content += f'<a class="btn ghost sm" href="?{esc(urlencode(params, doseq=True))}#file-changes"' + (' aria-current="true"' if selected else '') + f'>{title}</a>'
    content += '</nav>'
    if query.get('diff') == ['1']:
        return content + '<h2 id="full-diff">전체 원본 diff</h2>' + render_diff(read_diff(config, snapshot) or 'diff 캐시가 없습니다.', numbered=True) + '</section>'
    if query.get('order', [''])[0] == 'original' and snapshot:
        files = json.loads(snapshot['coverage']).get('files', [])
        file_order = {entry['filename']: index for index, entry in enumerate(files)}
        rows.sort(key=lambda row: file_order.get(row['path'], len(files)))
    if view:
        rows = [row for row in rows if row['hint']]
        params = [(key, value) for key, values in query.items() if key in ('order', 'unit_q') for value in values]
        content += (f'<p class="file-summary hint-line">경로상 확인 파일 {len(rows)}개만 표시합니다. 경로만으로 붙은 힌트이며 중요도 판단은 아닙니다.'
                    f'<a href="?{esc(urlencode(params))}#file-changes">전체 파일 보기</a></p>')
    selected_file = query.get('file', [''])[0]
    term = query.get('unit_q', [''])[0][:200]
    def file_text(row):
        return row['path'] + ' ' + ' '.join(u.get('diff', '') for u in units_by_file[row['path']])
    filtered = [row for row in rows if term.casefold() in file_text(row).casefold()]
    # 선택 파일을 필터링하지 않고 파일이 포함된 페이지를 연다.
    if not selected_file and 'unit_page' not in query and any(row['path'] == term for row in filtered):
        selected_file = term
    page_query = {key: values for key, values in query.items() if key != 'file'}
    if selected_file:
        selected_index = next((i for i, row in enumerate(filtered) if row['path'] == selected_file), None)
        if selected_index is not None:
            page_query['unit_page'] = [str(selected_index // 20 + 1)]
    file_choices = rows
    rows, controls, navigation = list_controls(rows, page_query, 'unit', '파일', file_text, fragment='#file-changes')
    if rows and not any(row['path'] == selected_file for row in rows):
        selected_file = rows[0]['path']
    # 후보는 페이지나 현재 검색 결과가 아닌 PR 전체 파일에서 만든다.
    choices = ''
    for index, row in enumerate(file_choices):
        params = [(key, value) for key, values in query.items()
                  if key not in ('file', 'diff', 'unit_q', 'unit_page') for value in values]
        params.append(('file', row['path']))
        href = '?' + urlencode(params) + '#' + file_anchor(row['path'])
        choices += f'<li><a id="file-option-{index}" role="option" tabindex="-1" href="{esc(href)}">{esc(row["path"])}</a></li>'
    controls = controls.replace('<form class="filters"', '<form class="filters file-search-form"', 1)
    controls = controls.replace('파일 검색 <input type="search"', '파일 또는 변경 내용 검색 <input type="search" autocomplete="off" placeholder="파일명이나 경로, 변경 내용 검색" role="combobox" aria-autocomplete="list" aria-controls="file-search-options" aria-expanded="false"', 1)
    controls = controls.replace('</label><button', '</label><div class="file-search-suggestions" data-file-suggestions hidden><p class="file-search-caption">이 PR의 파일</p><ul id="file-search-options" role="listbox">' + choices + '</ul><p data-file-no-match hidden>일치하는 파일이 없습니다.</p></div><button', 1)
    content += '<div class="file-search-controls" data-file-search>' + controls + '</div>'
    if rows:
        positions = {row['path']: index + 1 for index, row in enumerate(filtered)}
        total = len(filtered)
        selected_position = positions[selected_file]
        content += ('<div class="detail-file-layout" data-file-workspace><nav class="detail-file-nav" aria-label="현재 페이지 파일 목록">'
                    f'<div class="file-nav-heading"><strong>파일 목록</strong><span>{total}개</span></div>')
        content += '<label class="mobile-file-select">파일 선택<select data-file-select>'
        for row in rows:
            active = row['path'] == selected_file
            content += f'<option value="{file_anchor(row["path"])}"' + (' selected' if active else '') + f'>{esc(row["path"])}</option>'
        content += '</select></label><div class="file-nav-links">'
        def nav_link(row):
            active = row['path'] == selected_file
            params = [(key, value) for key, values in page_query.items() if key not in ('file', 'diff') for value in values]
            params.append(('file', row['path']))
            href = '?' + urlencode(params) + '#' + file_anchor(row['path'])
            return (f'<a href="{esc(href)}" data-select-file="{file_anchor(row["path"])}"' + (' aria-current="true"' if active else '')
                    + f'>{file_label(row["path"])}<span class="file-nav-meta">{file_badges(row)}'
                    + f'<span>변경 단위 {len(units_by_file[row["path"]])}개</span></span></a>')
        # 검토 우선순위에서는 기계적 변경이 맨 뒤에 모인다. 그 묶음만 접는다.
        folded = [row for row in rows if not legacy and not view and query.get('order', [''])[0] != 'original' and foldable(row)]
        content += ''.join(nav_link(row) for row in rows if row not in folded)
        if folded:
            content += ('<details class="file-nav-fold"' + (' open' if any(row['path'] == selected_file for row in folded) else '')
                        + f'><summary>기계적 변경 {len(folded)}개</summary>' + ''.join(nav_link(row) for row in folded) + '</details>')
        content += ('</div>' + navigation + '</nav><div class="detail-file-list">'
                    f'<p class="file-position" data-file-position-label>파일 {selected_position} / {total}</p>'
                    '<p class="sr-only" role="status" aria-live="polite" data-selected-file>선택한 파일: ' + esc(selected_file) + '</p>')
    for row in rows:
        index = positions[row['path']] - 1
        neighbors = tuple((file_href(other['path'], preserve_search=True), other['path'])
                          if 0 <= index + step < len(filtered) and (other := filtered[index + step]) else None
                          for step in (-1, 1))
        content += file_panel(row, units_by_file[row['path']], shown['items'], link,
                              active=row['path'] == selected_file, neighbors=neighbors,
                              position=f'{index + 1} / {len(filtered)}', known_symbols=candidates, file_href=file_href)
    if rows:
        content += '</div></div>'
    elif not pr['units']:
        content += '<article>수집된 변경 파일이 없습니다.</article>'
    elif view and not term:
        content += '<p class="muted">경로상 확인 파일이 없습니다.</p>'
    return content + ('' if rows else navigation) + '</section>'


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
                    has_connected_prs = any(pr.get('tracked', 1) for pr in prs)
                    prs, team_filter = home_team_filter(self.server, conn, prs, query, csrf, offline)
                    body = listing(prs, query, csrf, web_jobs.collect_panel(conn, csrf, offline, compact=True), team_filter, has_connected_prs) + web_jobs.script()
                else:
                    match = re.fullmatch(r'/pr/([1-9][0-9]*)', url.path)
                    pr = next((p for p in prs if match and p['id'] == int(match[1])), None)
                    if pr is None:
                        return self.reply(404, 'PR이 없습니다')
                    title = pr['title']
                    body = (detail(pr, parse_qs(url.query), self.server.config, offline,
                                   web_jobs.analysis_panel(conn, pr, csrf, offline), csrf) + web_jobs.script())
        self.reply(200, page(title, body, '/' if url.path.startswith('/pr/') else url.path, detail_page=url.path.startswith('/pr/')))

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
                from .lifecycle import worker as cleanup_worker
                threads.append(threading.Thread(target=cleanup_worker, args=(config, stop), daemon=True))
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
