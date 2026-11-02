"""한국어 설정 화면과 읽기 전용 저장소 조회 워커."""
from contextlib import closing
from pathlib import Path
from datetime import datetime, timezone
import json
import os
import re
import tempfile
import threading
import time
from urllib.parse import parse_qsl, urlencode

from .collect import Collector, GitHub
from .config import Config, valid_model
from .db import SCOPES, analysis_allowed, analysis_scope, connect
from . import jobs
from .hosts import hostname, registered, authenticated
from .web_jobs import JOB_PATHS, job_box, script
from .repos import web_repository, add_repository, remove_repository

POST_PATHS = {'/hosts/check', '/hosts/add', '/hosts/remove', '/repos/add', '/repos/remove', '/repos/load', '/repos/scope', '/settings/model', '/home/team'} | JOB_PATHS
WARNING = ('허용하면 PR 코드, diff와 제목, 설명이 Codex CLI를 통해 모델 서비스로 전송됩니다. '
           '민감 정보 제외는 완전하지 않으며 서비스의 전송, 보존 정책이 적용됩니다.')


class RepoLookup:
    """조회는 공통 작업 큐의 단일 워커가 실행하고 목록 결과는 메모리에 60초 동안 재사용한다."""
    def __init__(self, config, clock=time.monotonic):
        self.config, self.clock = config, clock
        self.lock = threading.Lock()
        self.items = []
        self.teams = {}
        self.selected_team = ''
        self.host = "github.com"
        self.mode = "all"
        self.truncated = False
        self.expires = 0
        self.gh = GitHub()
        self._host_limits = {}

    def snapshot(self, with_host=False, with_mode=False, with_teams=False):
        with self.lock:
            items = list(self.items)
            if with_teams:
                return self.host, self.mode, self.truncated, self.expires > 0, items, list(self.teams), self.selected_team
            if with_mode:
                return self.host, self.mode, self.truncated, self.expires > 0, items
            return (self.host, items) if with_host else items

    def fresh(self, host="github.com", mode="all", team=''):
        with self.lock:
            return host == self.host and mode == self.mode and team == self.selected_team and self.clock() < self.expires

    def run(self, parts, source, host="github.com", mode="all", team=''):
        """워커 전용. 성공 메시지를 반환하고 실패는 예외로 알린다(이전 목록은 보존)."""
        with closing(connect(self.config)) as conn:
            host = registered(conn, host)
        self.gh.host = host
        self.gh.not_before = self._host_limits.get(host, 0)
        self.gh.require_complete_sso = mode == 'team' and parts is None
        try:
            return self._run(parts, source, host, mode, team)
        finally:
            self._host_limits[host] = self.gh.not_before
            self.gh.require_complete_sso = False

    def _run(self, parts, source, host, mode="all", team=''):
        if parts:
            payload = self.gh.get('repos/' + '/'.join(parts))
            actual = web_repository(payload['full_name'])
            if tuple(p.lower() for p in actual) != tuple(p.lower() for p in parts):
                raise ValueError('저장소 주소가 변경되었습니다. 실제 주소를 입력하세요')
            with closing(connect(self.config)) as conn, Collector(conn, self.config).lock(), conn:
                registered(conn, host)
                add_repository(conn, actual, source, host)
            return '저장소를 등록했습니다. 분석은 별도 허용이 필요합니다.'
        if mode == 'team':
            return self._team_repos(host, team)
        user = self.gh.get('user')
        if not isinstance(user, dict) or not isinstance(user.get('login'), str):
            raise ValueError('사용자 응답 형식 오류')
        items = set()
        truncated = False
        for page in range(1, 11):
            batch = self.gh.get('user/repos?affiliation=owner,organization_member,collaborator&per_page=100&page=' + str(page))
            if not isinstance(batch, list):
                raise ValueError('저장소 응답 형식 오류')
            for row in batch:
                try:
                    items.add('/'.join(web_repository(row['full_name'])))
                except (ValueError, KeyError, TypeError):
                    continue
            if len(batch) < 100:
                break
        else:
            truncated = True
        with self.lock:
            self.host = host
            self.mode = "all"
            self.truncated = truncated
            self.items = sorted(items, key=str.lower)
            self.teams = {}
            self.selected_team = ''
            self.expires = self.clock() + 60
        return ('최대 1,000개까지만 표시합니다. 직접 입력도 사용할 수 있습니다.' if truncated else
                '접근 가능한 저장소 조회 완료. 선택한 저장소만 등록합니다.')

    def _team_repos(self, host, selected_team=''):
        teams, items, team_items = [], {}, {}
        requests = 0
        truncated = False
        for page in range(1, 11):
            batch = self.gh.get(f'user/teams?per_page=100&page={page}')
            requests += 1
            if not isinstance(batch, list):
                raise ValueError('팀 응답 형식 오류')
            for row in batch:
                try:
                    org = row['organization']['login']
                    slug = row['slug']
                    web_repository(org + '/repo')
                    if not isinstance(slug, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]{0,99}', slug):
                        raise ValueError('팀 이름 형식 오류')
                    teams.append((org, slug))
                except (KeyError, TypeError, ValueError):
                    raise ValueError('팀 응답 형식 오류') from None
            if len(batch) < 100:
                break
        else:
            truncated = True
        teams_truncated = truncated
        truncated = False
        team_items = {org + '/' + slug: set() for org, slug in teams}
        if selected_team and selected_team not in team_items:
            raise ValueError('소속 팀을 확인할 수 없습니다')
        for org, slug in teams:
            team_name = org + '/' + slug
            if selected_team and team_name != selected_team:
                continue
            page = 1
            while True:
                if requests >= 50 or len(items) >= 1000:
                    truncated = True
                    break
                batch = self.gh.get(f'orgs/{org}/teams/{slug}/repos?per_page=100&page={page}')
                requests += 1
                if not isinstance(batch, list):
                    raise ValueError('팀 저장소 응답 형식 오류')
                for row in batch:
                    try:
                        name = '/'.join(web_repository(row['full_name']))
                        if name.split('/')[0].casefold() != org.casefold():
                            raise ValueError('팀 조직 불일치')
                        if name.casefold() not in items and len(items) >= 1000:
                            truncated = True
                            break
                        items.setdefault(name.casefold(), name)
                        team_items[team_name].add(name.casefold())
                    except (KeyError, TypeError, ValueError):
                        raise ValueError('팀 저장소 응답 형식 오류') from None
                if truncated or len(batch) < 100:
                    break
                page += 1
            if truncated:
                break
        truncated = truncated or teams_truncated
        with self.lock:
            self.host = host
            self.mode = 'team'
            self.truncated = truncated
            self.items = sorted(items.values(), key=str.lower)
            self.teams = team_items
            self.selected_team = selected_team
            self.expires = self.clock() + 60
        if truncated:
            return '내 팀 저장소 조회가 한도에 도달해 일부만 표시합니다.'
        return '내 팀 저장소 조회 완료. 선택한 저장소만 등록합니다.'


def form(action, csrf, content, variant='secondary'):
    from .web import esc
    icon = ''
    if action in ('/jobs/collect', '/hosts/check'):
        icon = ('<svg viewBox="0 0 16 16" fill="none" aria-hidden="true">'
                '<path d="M13 7a5 5 0 1 0-1 4M13 3v4H9" stroke="currentColor" '
                'stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>')
    size = ' sm' if action in ('/repos/remove', '/hosts/remove', '/hosts/check') else ''
    content = re.sub(r'<button([^>]*)>', lambda match: f'<button class="btn {variant}{size}"' + match[1] + '>' + icon, content)
    return (f'<form class="action-form" method="post" action="{action}"><input type="hidden" name="csrf" value="{esc(csrf)}">'
            + content + '</form>')


def confirm_action(title, effect, confirm_form):
    """되돌리기 어려운 작업은 펼쳐서 영향을 읽은 뒤에만 제출 버튼이 보이게 한다. 스크립트 없이 동작한다."""
    from .web import esc
    return (f'<details class="confirm-action"><summary class="btn danger sm">{esc(title)}</summary>'
            f'<p>{esc(effect)}</p>' + confirm_form + '</details>')


def source_field(target="저장소 연결"):
    from .web import esc
    return '<label>출처<select name="source" aria-label="' + esc(target) + ' 출처"><option value="responsible">담당 (responsible)</option><option value="owned">소유 (owned)</option><option value="managed">관리 (managed)</option></select></label>'


SCOPE_LABELS = {'none': '분석 안 함', 'all': '모든 저장소', 'selected': '선택한 저장소만'}


def scope_candidates(conn, host, query):
    """선택 목록 후보: 선택됨·추적 중·열린 PR이 있는 저장소. 화면과 저장이 같은 페이지를 계산한다."""
    from .web import list_controls
    rows = conn.execute("SELECT r.*, EXISTS(SELECT 1 FROM pull_requests p WHERE p.repo_id=r.id AND p.state='open') AS has_open "
                        "FROM repositories r WHERE r.host=? AND (r.tracked=1 OR r.analysis_policy='allow' OR has_open) "
                        "ORDER BY r.analysis_policy='allow' DESC, r.tracked DESC, r.owner, r.name", (host,)).fetchall()
    return list_controls(rows, query, 'select', '저장소', lambda r: r['owner'] + '/' + r['name'])


def scope_section(conn, csrf, query):
    from .web import esc
    hosts = [r[0] for r in conn.execute('SELECT host FROM github_hosts ORDER BY host')]
    wanted = query.get('scope_host', [''])[0]
    host = wanted if wanted in hosts else 'github.com'
    scope = analysis_scope(conn, host)
    body = ('<section id="analysis-scope" class="repo-section"><h2>분석 허용 범위</h2>'
            '<p class="muted">GitHub 앱 설치처럼 호스트마다 코드를 분석에 보낼 범위를 정합니다. 기본은 분석 안 함입니다.</p>'
            f'<p>{esc(WARNING)}</p>')
    if len(hosts) > 1:
        keep = ''.join(f'<input type="hidden" name="{k}" value="{esc(query[k][0])}">' for k in ('from_pr',) if query.get(k))
        body += ('<form class="filters" method="get" action="/repos#analysis-scope">' + keep + '<label>호스트<select name="scope_host">'
                 + ''.join(f'<option{" selected" if h == host else ""}>{esc(h)}</option>' for h in hosts)
                 + '</select></label><button class="btn secondary">호스트 보기</button></form>')
    rows, controls, navigation = scope_candidates(conn, host, query)
    radios = ''.join(f'<label class="scope-option"><input type="radio" name="scope" value="{value}"' + (' checked' if value == scope else '')
                     + f'><span><strong>{label}</strong><small>{hint}</small></span></label>'
                     for value, label, hint in [('none', SCOPE_LABELS['none'], '이 호스트의 코드는 모델 서비스로 보내지 않습니다.'),
                                                ('all', SCOPE_LABELS['all'], '앞으로 연결하거나 리뷰 요청을 받는 저장소도 포함합니다.'),
                                                ('selected', SCOPE_LABELS['selected'], '아래에서 체크한 저장소만 허용합니다.')])
    checks = ''.join(f'<label class="scope-repo"><input type="checkbox" name="repository" value="{esc(r["owner"] + "/" + r["name"])}"'
                     + (' checked' if r['analysis_policy'] == 'allow' else '') + f'><span>{esc(r["owner"] + "/" + r["name"])}</span>'
                     + ('<small>추적 중</small>' if r['tracked'] else '') + ('<small>열린 PR</small>' if r['has_open'] else '') + '</label>'
                     for r in rows)
    # 저장은 이 화면에 실제로 보여준 저장소만 바꾼다. 페이지를 다시 계산하면 그 사이 순서가 바뀌어 보지 못한 선택이 풀릴 수 있다.
    paging = ''.join(f'<input type="hidden" name="shown" value="{int(r["id"])}">' for r in rows)
    body += (f'<p class="meta"><span class="badge">{esc(host)}</span><span>현재: {SCOPE_LABELS[scope]}</span></p>'
             + controls
             + form('/repos/scope', csrf, f'<input type="hidden" name="host" value="{esc(host)}">' + paging
                    + '<fieldset class="scope-choices"><legend class="sr-only">분석 범위</legend>' + radios + '</fieldset>'
                    + '<fieldset class="scope-repos"><legend>선택할 저장소</legend>'
                    + (checks or '<p class="muted">연결했거나 열린 PR이 있는 저장소가 없습니다. 먼저 저장소를 연결하세요.</p>') + '</fieldset>'
                    + '<label class="consent"><input type="checkbox" name="confirm" value="yes">허용 범위의 코드가 Codex(모델 서비스)로 전송됨을 이해하고 허용합니다</label>'
                    + f'<button aria-label="{esc(host)} 분석 허용 범위 저장">범위 저장</button>')
             + navigation + '</section>')
    return body


RETURN_FIELDS = {
    '/repos': {'tracked_q', 'tracked_page', 'available_q', 'available_page', 'available_team', 'from_pr',
               'scope_host', 'select_q', 'select_page'},
    '/settings': set(),
}


def numeric(key):
    return key.endswith('_page') or key == 'from_pr'


def positive(value):
    return value.isascii() and value.isdigit() and len(value) <= 9 and int(value) > 0


def validate_return_query(action, query):
    destination = '/repos' if action.startswith(('/repos/', '/hosts/')) else '/settings' if action.startswith('/settings/') else ''
    if len(query) > 6000 or not destination:
        raise ValueError('잘못된 목록 상태')
    pairs = parse_qsl(query, keep_blank_values=True, strict_parsing=True, max_num_fields=5)
    if (len({key for key, _ in pairs}) != len(pairs)
            or any(key not in RETURN_FIELDS[destination] or len(value) > 200
                   or (numeric(key) and not positive(value))
                   for key, value in pairs)):
        raise ValueError('잘못된 목록 상태')
    return urlencode(pairs)


def preserve_list_state(body, path, query):
    from .web import esc
    pairs = [(key, values[0]) for key, values in query.items()
             if key in RETURN_FIELDS[path] and values and len(values[0]) <= 200
             and (not numeric(key) or positive(values[0]))]
    if not pairs:
        return body
    field = f'<input type="hidden" name="return_query" value="{esc(urlencode(pairs))}">'
    return re.sub(r'(<form class="action-form"[^>]*>)', lambda m: m[1] + field, body)


def model_choices():
    """Codex가 저장한 표시 가능 모델만 선택지로 사용한다."""
    root = Path(os.environ.get('CODEX_HOME', '~/.codex')).expanduser()
    path = root / 'models_cache.json'
    try:
        if path.stat().st_size > 2 * 1024 * 1024:
            return []
        data = json.loads(path.read_text(encoding='utf-8'))
        models = data.get('models') if isinstance(data, dict) else None
        if not isinstance(models, list):
            return []
        choices = []
        for row in models[:500]:
            if not isinstance(row, dict):
                continue
            name = row.get('slug')
            if row.get('visibility') == 'list' and valid_model(name) and len(name) <= 64 and name not in choices:
                choices.append(name)
        return choices
    except (OSError, ValueError, UnicodeError):
        return []


def render(server, conn, csrf, path, query=None):
    from .web import esc, meta, list_controls
    query = query or {}
    if path == '/repos':
        body = '<div class="page-intro"><h1>저장소</h1><p>GitHub 호스트와 조회 범위를 선택하고 리뷰할 저장소를 연결하세요.</p><p class="muted">GitHub에는 읽기 전용으로 접근합니다. 분석 실행은 아래 호스트별 분석 허용 범위를 따릅니다.</p></div>'
        from_pr = query.get('from_pr', [''])[0]
        if positive(from_pr):
            body += (f'<p class="notice">PR에서 이동했습니다. <a href="#analysis-scope">분석 허용 범위</a>에서 이 저장소를 허용한 뒤 '
                     f'<a href="/pr/{int(from_pr)}">PR로 돌아가 분석을 요청하세요</a>.</p>')
        lookup = getattr(server, 'repo_lookup', None)
        lookup_host, lookup_mode, truncated, loaded, items, teams, selected_team = lookup.snapshot(with_teams=True) if lookup else ('github.com', 'all', False, False, [], [], '')
        choices = '<option value="all"' + (' selected' if lookup_mode == 'all' else '') + '>접근 가능한 전체 저장소</option>'
        choices += '<option value="team"' + (' selected' if lookup_mode == 'team' else '') + '>내 팀 저장소</option>'
        body += '<section class="repo-section repo-discovery"><h2>저장소 찾기</h2><p class="muted">팀 저장소만 보려면 조회 범위에서 내 팀 저장소를 선택하세요.</p>'
        body += form('/repos/load', csrf, host_field(conn, '저장소 불러오기', lookup_host) + '<label>조회 범위<select name="mode">' + choices + '</select></label><button>저장소 불러오기</button>', variant='primary')
        if getattr(server, 'offline', False):
            body += '<p>오프라인 모드에서는 GitHub 저장소를 불러오거나 접근을 확인하지 않습니다.</p>'
        running = jobs.active(conn, 'repos')
        if running is not None:
            body += '<p>이미 저장소 조회 작업이 진행 중입니다. 완료 후 다시 요청하세요. 진행 중 요청은 새 작업을 만들지 않습니다.</p>'
        body += job_box(jobs.latest(conn, 'repos')) + script()
        body += '<h2>조회 결과: ' + esc(lookup_host) + ' / ' + (esc(selected_team) if selected_team else '내 팀 저장소' if lookup_mode == 'team' else '전체 저장소') + '</h2>'
        if truncated:
            body += '<p>조회 한도에 도달해 일부 결과만 표시합니다.</p>'
        elif loaded and not items:
            body += '<p>조회된 저장소가 없습니다. 팀 접근 권한과 조직 SSO를 확인하세요.</p>'
        if lookup_mode == 'team' and loaded:
            options = '<option value=""' + (' selected' if not selected_team else '') + '>모든 소속 팀</option>'
            options += ''.join(f'<option value="{esc(team)}"' + (' selected' if team == selected_team else '') + f'>{esc(team)}</option>' for team in sorted(teams, key=str.casefold))
            body += form('/repos/load', csrf, '<input type="hidden" name="host" value="' + esc(lookup_host) + '"><input type="hidden" name="mode" value="team"><label>소속 팀<select name="team">' + options + '</select></label><button>선택한 팀 조회</button>')
        elif not loaded:
            body += '<p class="muted">팀을 선택하려면 조회 범위에서 내 팀 저장소를 선택하고 불러오세요.</p>'
        tracked = {(row['host'].casefold(), (row['owner'] + '/' + row['name']).casefold()): row for row in conn.execute('SELECT * FROM repositories WHERE tracked=1')}
        items, controls, navigation = list_controls(items, query, 'available', '조회된 저장소', lambda name: lookup_host + '/' + name)
        body += controls + '<div class="list">'
        for name in items:
            row = tracked.get((lookup_host.casefold(), name.casefold()))
            body += '<article class="list-row"><span class="row-content">' + esc(lookup_host + '/' + name) + '</span>'
            if row:
                params = [(key, value) for key, values in query.items() if key in RETURN_FIELDS['/repos'] and key not in ('tracked_q', 'tracked_page') for value in values[:1]]
                params.append(('tracked_q', lookup_host + '/' + name))
                body += '<span class="badge">연결됨 · 분석 ' + ('허용' if analysis_allowed(conn, row['id']) else '안 함') + '</span><a class="btn secondary sm" href="?' + esc(urlencode(params)) + '#connected-repositories">관리</a>'
            else:
                body += form('/repos/add', csrf, f'<input type="hidden" name="host" value="{esc(lookup_host)}"><input type="hidden" name="repository" value="{esc(name)}">' + source_field(lookup_host + '/' + name) + f'<button aria-label="{esc(lookup_host + "/" + name)} 선택하여 연결">선택하여 연결</button>')
            body += '</article>'
        body += '</div>' + navigation + '</section>'
        body += '<section class="repo-section"><h2>직접 연결</h2><p class="muted">목록에서 찾지 못한 저장소는 주소를 입력해 연결할 수 있습니다.</p>'
        body += '<div class="connect-toolbar">' + form('/repos/add', csrf, host_field(conn, selected=lookup_host) + '<label>owner/repo<input name="repository" required maxlength="140"></label>' + source_field() + '<button>저장소 연결</button>') + '</div></section>'
        rows = list(conn.execute('SELECT * FROM repositories ORDER BY tracked DESC,owner,name'))
        rows, controls, navigation = list_controls(rows, query, 'tracked', '연결된 저장소', lambda r: r['host'] + '/' + r['owner'] + '/' + r['name'])
        body += '<section id="connected-repositories" class="repo-section"><h2>연결된 저장소</h2><p class="muted">이전에 연결한 저장소도 표시합니다. 연결 해제는 로컬 추적만 중단하며 분석 허용 범위는 그대로입니다.</p>' + controls + '<div class="list">'
        for row in rows:
            name = row['owner'] + '/' + row['name']
            body += '<article class="list-row"><div class="row-content"><h3>' + esc(name) + '</h3>' + meta(row['host'], row['source'], '추적 중' if row['tracked'] else '연결 해제됨') + '</div><div class="toolbar row-actions">'
            if row['tracked']:
                body += confirm_action('연결 해제', '추적과 수집을 멈춥니다. 분석 허용 범위는 그대로 남아 동의가 철회되지 않으며, 리뷰 요청으로만 남은 PR이 아니면 수집 기록이 정리될 수 있습니다.',
                                       form('/repos/remove', csrf, f'<input type="hidden" name="host" value="{esc(row["host"])}"><input type="hidden" name="repository" value="{esc(name)}"><button aria-label="{esc(row["host"] + "/" + name)} 연결 해제 확인">연결 해제 확인</button>', variant='danger'))
            body += '<span class="badge">분석 ' + ('허용' if analysis_allowed(conn, row['id']) else '안 함') + '</span></div></article>'
        body += '</div>' + navigation + '</section>' + scope_section(conn, csrf, query) + '<details class="repo-section host-management"><summary>GitHub 호스트 관리</summary>' + host_settings(server, conn, csrf) + '</details>'
        return preserve_list_state(body, path, query)
    settings = json.loads((server.config.config_dir / 'config.json').read_text())
    model = settings.get('codex_model', '')
    body = '<h1>설정</h1><p>분석 허용 범위(호스트별 전체 또는 선택한 저장소)는 <a href="/repos#analysis-scope">저장소 화면</a>에서 정합니다. 기본은 분석 안 함입니다.</p>'
    body += '<h2>모델 설정</h2>' + meta('파일 값: ' + (model or '미설정'), '실제 적용: ' + (Config.parse_codex_model(settings) or '자동 감지'))
    if 'SKIM_CODEX_MODEL' in os.environ:
        body += '<p>SKIM_CODEX_MODEL 환경변수가 파일 설정보다 우선합니다.</p>'
    body += ('<p>다음 분석부터 적용됩니다. 모델 변경 후 격리 검증이 일치하지 않으면 분석이 차단됩니다. '
             '<a href="/status#codex-setup">상태 화면의 격리 검증</a>이나 <code>skim doctor --probe</code>로 재검증하세요.</p>')
    choices = model_choices()
    if not choices:
        body += '<p class="muted">모델 목록을 불러올 수 없습니다. Codex 로그인과 모델 목록을 확인하세요.</p>'
    else:
        body += '<p class="muted">Codex에 저장된 모델 목록입니다. 계정과 목록의 최신 상태에 따라 사용 가능 여부가 다를 수 있습니다.</p>'
    if model and valid_model(model) and model not in choices:
        choices.append(model)
    options = '<option value=""' + (' selected' if not model else '') + '>자동 선택</option>'
    options += ''.join(f'<option value="{esc(name)}"' + (' selected' if name == model else '') + f'>{esc(name)}</option>' for name in choices)
    body += form('/settings/model', csrf, '<label>분석 모델<select name="model">' + options + '</select></label><button>모델 저장</button>', variant='primary')
    return body


def fields(data, required, optional=()):
    if not set(required) <= set(data) or set(data) - set(required) - set(optional) or any(len(v) != 1 for v in data.values()):
        raise ValueError('허용 필드와 입력 개수를 확인하세요')
    return {key: value[0] for key, value in data.items()}


def mutate(server, path, data, strict_lookup=False):
    if path == '/home/team':
        filter_keys = ('repo', 'importance', 'status', 'review', 'pr_q')
        d = fields(data, ('host',), ('team',) + filter_keys)
        if any(len(d.get(key, '')) > 200 for key in filter_keys):
            raise ValueError('필터를 확인하세요')
        team = d.get('team', '')
        request = {'host': [d['host']], 'mode': ['team'], 'team': [team]}
        mutate(server, '/repos/load', request, strict_lookup=True)
        params = [(key, d[key]) for key in filter_keys if d.get(key)]
        if team:
            params.append(('team', d['host'] + '|' + team))
        return '/?' + urlencode(params) if params else '/'
    if path == '/hosts/check':
        d = fields(data, ('host',))
        with closing(connect(server.config)) as conn:
            host = registered(conn, d['host'])
        check_auth(server, host)
        return '/repos'
    if path in ('/hosts/add', '/hosts/remove'):
        d = fields(data, ('host',))
        host = hostname(d['host'])
        with closing(connect(server.config)) as conn, Collector(conn, server.config).lock(), conn:
            if path == '/hosts/add':
                conn.execute('INSERT OR IGNORE INTO github_hosts(host) VALUES(?)', (host,))
            else:
                if host == 'github.com':
                    raise ValueError('기본 호스트는 삭제할 수 없습니다')
                conn.execute("UPDATE repositories SET tracked=0,enabled=0,analysis_policy='inherit' WHERE host=?", (host,))
                conn.execute('DELETE FROM host_organization_policies WHERE host=?', (host,))
                conn.execute('DELETE FROM github_hosts WHERE host=?', (host,))
        if path == '/hosts/add':
            check_auth(server, host)
        else:
            getattr(server, 'host_auth', {}).pop(host, None)
        return '/repos'
    if path in ('/repos/load', '/repos/add'):
        d = fields(data, () if path.endswith('load') else ('repository', 'source'), ('host', 'mode', 'team') if path.endswith('load') else ('host',))
        mode = d.get('mode', 'all')
        if mode not in ('all', 'team'):
            raise ValueError('조회 범위를 확인하세요')
        team = d.get('team', '')
        if team and (mode != 'team' or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9][A-Za-z0-9-]{0,99}', team)):
            raise ValueError('팀 이름을 확인하세요')
        with closing(connect(server.config)) as conn:
            host = registered(conn, d.get('host', 'github.com'))
        parts = None
        if path == '/repos/add':
            parts = web_repository(d['repository'])
            if d['source'] not in ('owned', 'managed', 'responsible'):
                raise ValueError('잘못된 저장소 출처입니다')
        if getattr(server, 'offline', False):
            return '/repos'
        if parts is None and server.repo_lookup.fresh(host, mode, team):
            return '/repos'
        params = dict(repository='/'.join(parts), source=d['source']) if parts else {}
        params['host'] = host
        if parts is None:
            params['mode'] = mode
            if team:
                params['team'] = team
        with closing(connect(server.config)) as conn:
            target = host + '/' + params['repository'] if parts else host + ' / ' + (team or ('내 팀 저장소' if mode == 'team' else '접근 가능한 저장소 목록'))
            job, created = jobs.enqueue(conn, 'repos', 0, target, params=params)
            if strict_lookup and not created and job['target'] != target:
                from .web_jobs import Rejected
                raise Rejected('다른 저장소 조회가 진행 중입니다. 완료 후 다시 요청하세요.', 409)
        return '/repos'
    if path == '/settings/model':
        d = fields(data, ('model',))
        model = d['model']
        target = server.config.config_dir / 'config.json'
        settings = json.loads(target.read_text())
        previous = settings.get('codex_model')
        if model and (not valid_model(model) or (model != previous and (len(model) > 64 or model not in model_choices()))):
            raise ValueError('목록에서 모델을 선택하세요')
        if model:
            settings['codex_model'] = model
        else:
            settings.pop('codex_model', None)
        fd, temporary = tempfile.mkstemp(dir=target.parent, prefix='.config-')
        try:
            with os.fdopen(fd, 'w') as output:
                json.dump(settings, output, ensure_ascii=False, indent=2)
                output.write('\n')
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return '/settings'
    with closing(connect(server.config)) as conn:
        collector = Collector(conn, server.config)
        with collector.lock(), conn:
            if path == '/repos/remove':
                d = fields(data, ('repository',), ('host',))
                remove_repository(conn, web_repository(d['repository']), registered(conn, d.get('host', 'github.com')))
            else:
                save_scope(conn, data)
        if path == '/repos/remove':
            collector.clean_cache()
    return '/repos' if path.startswith('/repos/') else '/settings'


def save_scope(conn, data):
    """호스트 분석 범위와 화면에 보여준 저장소의 선택을 저장한다. 새로 허용되는 코드가 있으면 전송 확인을 요구한다."""
    if set(data) - {'host', 'scope', 'confirm', 'repository', 'shown'} or any(
            len(data.get(key, [])) > 1 for key in ('host', 'scope', 'confirm')):
        raise ValueError('허용 필드와 입력 개수를 확인하세요')
    host = registered(conn, data.get('host', ['github.com'])[0])
    scope = data.get('scope', [''])[0]
    if scope not in SCOPES:
        raise ValueError('분석 범위를 선택하세요')
    previous = analysis_scope(conn, host)
    if scope != 'selected':
        # 숨겨진 체크박스 값은 무시한다. 철회(안 함)는 목록이 바뀌었어도 항상 성공해야 한다.
        if scope == 'all' and previous != 'all' and data.get('confirm', [''])[0] != 'yes':
            raise ValueError('모델 서비스 전송 확인 체크가 필요합니다')
        conn.execute('UPDATE github_hosts SET analysis_scope=? WHERE host=?', (scope, host))
        # 남겨 두면 나중에 '선택' 모드로 돌아올 때 확인 없이 다시 허용되므로 비운다.
        conn.execute("UPDATE repositories SET analysis_policy='inherit' WHERE host=?", (host,))
        return
    ids = data.get('shown', [])
    if len(ids) > 200 or not all(value.isascii() and value.isdigit() and len(value) <= 18 for value in ids):
        raise ValueError('저장소 목록을 확인하세요')
    shown = {(r['owner'] + '/' + r['name']).casefold(): r for r in conn.execute(
        'SELECT id, owner, name, analysis_policy FROM repositories WHERE host=? AND id IN (%s)' % ','.join('?' * len(ids)),
        (host, *map(int, ids)))} if ids else {}
    checked = {'/'.join(web_repository(value)).casefold() for value in data.get('repository', [])}
    if checked - set(shown):
        raise ValueError('저장소 목록이 바뀌었습니다. 화면을 새로고침한 뒤 다시 선택하세요')
    added = any(shown[name]['analysis_policy'] != 'allow' for name in checked)
    if (added or previous == 'none' and checked) and data.get('confirm', [''])[0] != 'yes':
        raise ValueError('모델 서비스 전송 확인 체크가 필요합니다')
    conn.execute('UPDATE github_hosts SET analysis_scope=? WHERE host=?', (scope, host))
    for name, row in shown.items():
        conn.execute('UPDATE repositories SET analysis_policy=? WHERE id=?', ('allow' if name in checked else 'inherit', row['id']))


def host_field(conn, purpose="저장소 연결", selected="github.com"):
    from .web import esc
    return '<label>호스트<select name="host" aria-label="' + esc(purpose) + ' 호스트">' + ''.join(
        f'<option{" selected" if r[0] == selected else ""}>{esc(r[0])}</option>' for r in conn.execute('SELECT host FROM github_hosts ORDER BY host')) + '</select></label>'


def host_settings(server, conn, csrf):
    from .web import esc
    body = '<h2>GitHub 호스트</h2><p>호스트 삭제 시 기록은 보존하고 대시보드에서 숨깁니다. 저장소 추적과 수집, 분석 허용을 끕니다.</p>'
    body += '<div class="list">'
    for row in conn.execute('SELECT host FROM github_hosts ORDER BY host'):
        host = row[0]
        cached = getattr(server, 'host_auth', {}).get(host)
        status = ('로그인됨' if cached[0] else '로그인 안 됨') if cached else '미확인'
        body += '<article class="list-row"><div class="row-content"><h3>' + esc(host) + '</h3><p class="meta"><span class="badge">' + esc(status) + '</span><span>마지막 확인: ' + esc(cached[1] if cached else '없음') + '</span></p></div><div class="toolbar row-actions">'
        body += form('/hosts/check', csrf, f'<input type="hidden" name="host" value="{esc(host)}"><button aria-label="{esc(host)} 인증 다시 확인">인증 다시 확인</button>')
        if host != 'github.com':
            body += confirm_action('호스트 삭제', '이 호스트의 저장소 추적과 분석 허용을 끕니다. 수집 기록은 보존하지만 대시보드에서 숨겨집니다.',
                               form('/hosts/remove', csrf, f'<input type="hidden" name="host" value="{esc(host)}"><button aria-label="{esc(host)} 호스트 삭제 확인">호스트 삭제 확인</button>', variant='danger'))
        body += '</div></article>'
    return body + '</div>' + form('/hosts/add', csrf, '<label>호스트<input name="host" aria-label="새 호스트 추가" required maxlength="253" placeholder="github.corp.example"></label><button>호스트 추가</button>')


def check_auth(server, host):
    if getattr(server, 'offline', False):
        return
    if not hasattr(server, 'host_auth'):
        server.host_auth = {}
    server.host_auth[host] = (authenticated(host), datetime.now(timezone.utc).isoformat(timespec='seconds'))
