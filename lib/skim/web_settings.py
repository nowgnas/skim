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
from .db import connect, analysis_allowed
from . import jobs
from .hosts import hostname, registered, authenticated
from .web_jobs import JOB_PATHS, job_box, script
from .repos import web_repository, add_repository, remove_repository

POST_PATHS = {'/hosts/check', '/hosts/add', '/hosts/remove', '/repos/add', '/repos/remove', '/repos/load', '/repos/policy', '/settings/analysis', '/settings/model', '/home/team'} | JOB_PATHS
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


def source_field(target="저장소 연결"):
    from .web import esc
    return '<label>출처<select name="source" aria-label="' + esc(target) + ' 출처"><option value="responsible">담당 (responsible)</option><option value="owned">소유 (owned)</option><option value="managed">관리 (managed)</option></select></label>'


def policy_form(csrf, scope, target, host="github.com", policy="deny", action='/settings/analysis'):
    from .web import esc
    current = 'reset' if policy == 'inherit' else policy
    identity = host + '/' + (target or '새 조직')
    target_field = (f'<input type="hidden" name="target" value="{esc(target)}">' if target else
                    f'<label>조직 이름<input name="target" aria-label="{esc(identity)} 이름" required></label>')
    options = ''.join(f'<option value="{value}"' + (' selected' if current == value else '') + f'>{label}</option>'
                      for value, label in [('deny', '거부'), ('reset', '초기화 / 조직 설정 상속'), ('allow', '허용')])
    return form(action, csrf,
                f'<input type="hidden" name="host" value="{esc(host)}"><input type="hidden" name="scope" value="{scope}">' + target_field
                + f'<label class="policy-select"><span class="sr-only">{esc(identity)} 분석 정책</span><select name="policy" aria-label="{esc(identity)} 분석 정책">{options}</select></label>'
                + f'<button aria-label="{esc(identity)} 분석 정책 저장">저장</button><label class="consent"><input type="checkbox" name="confirm" value="yes" aria-label="{esc(identity)} 모델 서비스 전송 동의">코드가 Codex(모델 서비스)로 전송됨을 이해하고 허용합니다</label>')


def policy_row(csrf, scope, target, host, policy, effective=None):
    from .web import esc
    applied = effective if effective is not None else policy == 'allow'
    return ('<article class="list-row policy-row"><div class="row-content"><h3>' + esc(host + '/' + target)
            + '</h3><span class="badge">실제 적용: ' + ('허용' if applied else '거부') + '</span></div>'
            + policy_form(csrf, scope, target, host, policy) + '</article>')


RETURN_FIELDS = {
    '/repos': {'tracked_q', 'tracked_page', 'available_q', 'available_page', 'available_team'},
    '/settings': {'policy_q', 'policy_page', 'org_q', 'org_page'},
}


def validate_return_query(action, query):
    destination = '/repos' if action.startswith(('/repos/', '/hosts/')) else '/settings' if action.startswith('/settings/') else ''
    if len(query) > 6000 or not destination:
        raise ValueError('잘못된 목록 상태')
    pairs = parse_qsl(query, keep_blank_values=True, strict_parsing=True, max_num_fields=5)
    if (len({key for key, _ in pairs}) != len(pairs)
            or any(key not in RETURN_FIELDS[destination] or len(value) > 200
                   or (key.endswith('_page') and (not value.isascii() or not value.isdigit() or len(value) > 9))
                   for key, value in pairs)):
        raise ValueError('잘못된 목록 상태')
    return urlencode(pairs)


def preserve_list_state(body, path, query):
    from .web import esc
    pairs = [(key, values[0]) for key, values in query.items()
             if key in RETURN_FIELDS[path] and values and len(values[0]) <= 200
             and (not key.endswith('_page') or (values[0].isascii() and values[0].isdigit() and len(values[0]) <= 9))]
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
        body = '<div class="page-intro"><h1>저장소</h1><p>GitHub 호스트와 조회 범위를 선택하고 리뷰할 저장소를 연결하세요.</p><p class="muted">GitHub에는 읽기 전용으로 접근합니다. 분석 실행은 저장소와 조직의 분석 허용 설정을 따릅니다.</p></div>'
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
                body += '<span class="badge">연결됨 · 분석 ' + ('허용' if analysis_allowed(conn, row['id']) else '거부') + '</span><a class="btn secondary sm" href="?' + esc(urlencode(params)) + '#connected-repositories">관리</a>'
            else:
                body += form('/repos/add', csrf, f'<input type="hidden" name="host" value="{esc(lookup_host)}"><input type="hidden" name="repository" value="{esc(name)}">' + source_field(lookup_host + '/' + name) + f'<button aria-label="{esc(lookup_host + "/" + name)} 선택하여 연결">선택하여 연결</button>')
            body += '</article>'
        body += '</div>' + navigation + '</section>'
        body += '<section class="repo-section"><h2>직접 연결</h2><p class="muted">목록에서 찾지 못한 저장소는 주소를 입력해 연결할 수 있습니다.</p>'
        body += '<div class="connect-toolbar">' + form('/repos/add', csrf, host_field(conn, selected=lookup_host) + '<label>owner/repo<input name="repository" required maxlength="140"></label>' + source_field() + '<button>저장소 연결</button>') + '</div></section>'
        rows = list(conn.execute('SELECT * FROM repositories ORDER BY tracked DESC,owner,name'))
        rows, controls, navigation = list_controls(rows, query, 'tracked', '연결된 저장소', lambda r: r['host'] + '/' + r['owner'] + '/' + r['name'])
        body += '<section id="connected-repositories" class="repo-section"><h2>저장소 연결과 분석 설정</h2><p class="muted">이전에 연결한 저장소도 표시합니다. 연결 해제는 로컬 추적만 중단하며 정책을 유지합니다.</p><p>' + esc(WARNING) + '</p>' + controls + '<div class="list">'
        for row in rows:
            name = row['owner'] + '/' + row['name']
            body += '<article class="list-row"><div class="row-content"><h3>' + esc(name) + '</h3>' + meta(row['host'], row['source'], '추적 중' if row['tracked'] else '연결 해제됨') + '</div><div class="toolbar row-actions">'
            if row['tracked']:
                body += form('/repos/remove', csrf, f'<input type="hidden" name="host" value="{esc(row["host"])}"><input type="hidden" name="repository" value="{esc(name)}"><button aria-label="{esc(row["host"] + "/" + name)} 연결 해제">연결 해제</button>', variant='danger')
            body += '</div>'
            body += '<div class="repository-policy"><span class="badge">실제 분석: ' + ('허용' if analysis_allowed(conn, row['id']) else '거부') + '</span>' + policy_form(csrf, 'repo', name, row['host'], row['analysis_policy'], '/repos/policy') + '</div></article>'
        body += '</div>' + navigation + '</section><details class="repo-section host-management"><summary>GitHub 호스트 관리</summary>' + host_settings(server, conn, csrf) + '</details>'
        return preserve_list_state(body, path, query)
    settings = json.loads((server.config.config_dir / 'config.json').read_text())
    model = settings.get('codex_model', '')
    body = '<h1>설정과 분석 허용</h1><p>' + WARNING + '</p><p>기본 꺼짐. 저장소의 명시적 허용, 거부는 조직 정책보다 우선합니다. 조직 허용은 해당 조직의 모든 저장소에 적용됩니다.</p>'
    body += '<h2>모델 설정</h2>' + meta('파일 값: ' + (model or '미설정'), '실제 적용: ' + (Config.parse_codex_model(settings) or '자동 감지'))
    if 'SKIM_CODEX_MODEL' in os.environ:
        body += '<p>SKIM_CODEX_MODEL 환경변수가 파일 설정보다 우선합니다.</p>'
    body += '<p>다음 분석부터 적용됩니다. 모델 변경 후 격리 검증이 일치하지 않으면 분석이 차단됩니다. skim doctor --probe로 재검증하세요.</p>'
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
    body += '<p>저장소별 분석 정책은 <a href="/repos#connected-repositories">저장소 관리</a>에서 변경하세요.</p>'
    rows = list(conn.execute('SELECT owner,policy FROM organization_policies ORDER BY owner'))
    rows, org_controls, org_navigation = list_controls(rows, query, 'org', '조직 정책', lambda r: r['owner'])
    body += '<h2>조직 정책</h2>' + org_controls + '<div class="list">'
    for row in rows:
        body += policy_row(csrf, 'org', row['owner'], 'github.com', row['policy'])
    body += '</div>' + org_navigation + '<section class="policy-add"><h3>github.com 조직 추가</h3>' + policy_form(csrf, 'org', '') + '</section>' + host_field_note(conn, csrf)
    return preserve_list_state(body, path, query)


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
                conn.execute('INSERT OR IGNORE INTO github_hosts VALUES(?)', (host,))
            else:
                if host == 'github.com':
                    raise ValueError('기본 호스트는 삭제할 수 없습니다')
                conn.execute("UPDATE repositories SET tracked=0,enabled=0,analysis_policy='deny' WHERE host=?", (host,))
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
                d = fields(data, ('scope', 'target', 'policy'), ('confirm', 'host'))
                host = registered(conn, d.get('host', 'github.com'))
                if d['policy'] not in ('allow', 'deny', 'reset') or d['scope'] not in ('repo', 'org'):
                    raise ValueError('분석 정책 형식이 잘못되었습니다')
                if path == '/repos/policy' and d['scope'] != 'repo':
                    raise ValueError('저장소 정책만 변경할 수 있습니다')
                if d['policy'] == 'allow' and d.get('confirm') != 'yes':
                    raise ValueError('모델 서비스 전송 확인 체크가 필요합니다')
                if d['scope'] == 'repo':
                    parts = web_repository(d['target'])
                    policy = 'inherit' if d['policy'] == 'reset' else d['policy']
                    if not conn.execute('UPDATE repositories SET analysis_policy=? WHERE host=? AND owner=? AND name=?', (policy, host, *parts)).rowcount:
                        raise ValueError('등록되지 않은 저장소입니다')
                else:
                    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]{0,38}', d['target']):
                        raise ValueError('조직 이름 형식을 확인하세요')
                    if host != 'github.com':
                        conn.execute('DELETE FROM host_organization_policies WHERE host=? AND owner=?', (host, d['target']))
                        if d['policy'] != 'reset':
                            conn.execute('INSERT INTO host_organization_policies VALUES(?,?,?)', (host, d['target'], d['policy']))
                    elif d['policy'] == 'reset':
                        conn.execute('DELETE FROM organization_policies WHERE owner=?', (d['target'],))
                    else:
                        conn.execute('INSERT INTO organization_policies VALUES(?,?) ON CONFLICT(owner) DO UPDATE SET policy=excluded.policy', (d['target'], d['policy']))
        if path == '/repos/remove':
            collector.clean_cache()
    return '/repos' if path.startswith('/repos/') else '/settings'


def host_field(conn, purpose="저장소 연결", selected="github.com"):
    from .web import esc
    return '<label>호스트<select name="host" aria-label="' + esc(purpose) + ' 호스트">' + ''.join(
        f'<option{" selected" if r[0] == selected else ""}>{esc(r[0])}</option>' for r in conn.execute('SELECT host FROM github_hosts ORDER BY host')) + '</select></label>'


def host_settings(server, conn, csrf):
    from .web import esc
    body = '<h2>GitHub 호스트</h2><p>호스트 삭제 시 기록은 보존하고 대시보드에서 숨깁니다. 저장소 추적과 수집과 분석 허용을 끄고 조직 정책을 삭제합니다.</p>'
    body += '<div class="list">'
    for row in conn.execute('SELECT host FROM github_hosts ORDER BY host'):
        host = row[0]
        cached = getattr(server, 'host_auth', {}).get(host)
        status = ('로그인됨' if cached[0] else '로그인 안 됨') if cached else '미확인'
        body += '<article class="list-row"><div class="row-content"><h3>' + esc(host) + '</h3><p class="meta"><span class="badge">' + esc(status) + '</span><span>마지막 확인: ' + esc(cached[1] if cached else '없음') + '</span></p></div><div class="toolbar row-actions">'
        body += form('/hosts/check', csrf, f'<input type="hidden" name="host" value="{esc(host)}"><button aria-label="{esc(host)} 인증 다시 확인">인증 다시 확인</button>')
        if host != 'github.com':
            body += form('/hosts/remove', csrf, f'<input type="hidden" name="host" value="{esc(host)}"><button aria-label="{esc(host)} 호스트 삭제">호스트 삭제</button>', variant='danger')
        body += '</div></article>'
    return body + '</div>' + form('/hosts/add', csrf, '<label>호스트<input name="host" aria-label="새 호스트 추가" required maxlength="253" placeholder="github.corp.example"></label><button>호스트 추가</button>')


def host_field_note(conn, csrf):
    from .web import esc
    body = ''
    for row in conn.execute("SELECT host FROM github_hosts WHERE host!='github.com'"):
        body += '<h3>' + esc(row[0]) + ' 조직 추가</h3><section class="policy-add">' + policy_form(csrf, 'org', '', row[0]) + '</section>'
    for row in conn.execute('SELECT * FROM host_organization_policies ORDER BY host,owner'):
        body += '<div class="list">' + policy_row(csrf, 'org', row['owner'], row['host'], row['policy']) + '</div>'
    return body


def check_auth(server, host):
    if getattr(server, 'offline', False):
        return
    if not hasattr(server, 'host_auth'):
        server.host_auth = {}
    server.host_auth[host] = (authenticated(host), datetime.now(timezone.utc).isoformat(timespec='seconds'))
