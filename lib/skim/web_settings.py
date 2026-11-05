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
from .timefmt import human_time
from .hosts import hostname, registered, authenticated
from .web_jobs import JOB_PATHS, job_box, script
from .repos import web_repository, add_repository, remove_repository

POST_PATHS = {'/home/teams', '/hosts/check', '/hosts/add', '/hosts/remove', '/repos/add', '/repos/remove', '/repos/load', '/repos/scope', '/settings/model'} | JOB_PATHS
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

    def team_snapshot(self, host):
        """메인 필터에는 유효한 호스트별 팀 결과만 전달한다."""
        with self.lock:
            valid = host == self.host and self.mode == 'team' and self.clock() < self.expires
            teams = {name: set(repos) for name, repos in self.teams.items()} if valid else {}
            if self.selected_team:
                teams = {name: repos for name, repos in teams.items() if name == self.selected_team}
            return valid, self.truncated if valid else False, teams

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
        except Exception:
            if mode == 'team' and parts is None:
                with self.lock:
                    if self.host == host and self.mode == 'team':
                        self.expires = 0
            raise
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
    """호스트의 연결한 저장소와(연결을 끊었어도) 분석에 선택된 저장소. 화면과 저장이 같은 목록을 쓴다."""
    from .web import list_controls
    rows = conn.execute("SELECT r.*, (SELECT count(*) FROM pull_requests p WHERE p.repo_id=r.id AND p.state='open') AS open_prs "
                        "FROM repositories r WHERE r.host=? AND (r.tracked=1 OR r.analysis_policy='allow') "
                        "ORDER BY r.tracked DESC, r.owner, r.name", (host,)).fetchall()
    # 예전 링크의 select_q도 같은 검색으로 받는다.
    if not query.get('tracked_q') and query.get('select_q'):
        query = dict(query, tracked_q=query['select_q'])
    return list_controls(rows, query, 'tracked', '저장소', lambda r: r['owner'] + '/' + r['name'])


def add_panel(server, conn, csrf, query, host):
    """카드 안에서 펼치는 저장소 추가. 조회와 직접 연결 모두 현재 호스트로 한다."""
    from .web import esc, list_controls
    lookup = getattr(server, 'repo_lookup', None)
    lookup_host, lookup_mode, truncated, loaded, items, teams, selected_team = lookup.snapshot(with_teams=True) if lookup else ('github.com', 'all', False, False, [], [], '')
    mine = lookup_host == host
    running = jobs.active(conn, 'repos')
    # 이 호스트를 불러왔거나 조회 중이거나 검색 중이면 펼친 채로 보여준다.
    opened = (mine and (loaded or truncated)) or running is not None or bool(query.get('available_q')) or query.get('add', [''])[0] == '1'
    choices = '<option value="all"' + (' selected' if lookup_mode == 'all' or not mine else '') + '>접근 가능한 전체 저장소</option>'
    choices += '<option value="team"' + (' selected' if mine and lookup_mode == 'team' else '') + '>내 팀 저장소</option>'
    body = ('<details class="repo-add" id="add-repository"' + (' open' if opened else '') + '><summary class="btn secondary sm">+ 저장소 추가</summary>'
            '<div class="repo-add-body"><p class="muted">내 계정이 접근할 수 있는 저장소나 내 팀 저장소를 불러와 연결합니다.</p>')
    hidden_host = f'<input type="hidden" name="host" value="{esc(host)}">'
    body += form('/repos/load', csrf, hidden_host + '<label>조회 범위<select name="mode">' + choices + '</select></label><button>불러오기</button>')
    if getattr(server, 'offline', False):
        body += '<p>오프라인 모드에서는 GitHub 저장소를 불러오거나 접근을 확인하지 않습니다.</p>'
    if running is not None and not pending_row(conn, host):
        body += '<p>이미 저장소 조회 작업이 진행 중입니다. 완료 후 다시 요청하세요. 진행 중 요청은 새 작업을 만들지 않습니다.</p>'
    # 연결 확인 중인 저장소는 목록 행에서 진행을 보여주므로 여기서는 같은 작업을 반복하지 않는다.
    if not pending_row(conn, host):
        body += job_box(jobs.latest(conn, 'repos')) + script()
    if mine and (loaded or truncated or items):
        if truncated:
            body += '<p>조회 한도에 도달해 일부 결과만 표시합니다.</p>'
        elif loaded and not items:
            body += '<p>조회된 저장소가 없습니다. 팀 접근 권한과 조직 SSO를 확인하세요.</p>'
        if lookup_mode == 'team' and loaded:
            options = '<option value=""' + (' selected' if not selected_team else '') + '>모든 소속 팀</option>'
            options += ''.join(f'<option value="{esc(team)}"' + (' selected' if team == selected_team else '') + f'>{esc(team)}</option>' for team in sorted(teams, key=str.casefold))
            body += form('/repos/load', csrf, hidden_host + '<input type="hidden" name="mode" value="team"><label>소속 팀<select name="team">' + options + '</select></label><button>선택한 팀 조회</button>')
        tracked = {(row['owner'] + '/' + row['name']).casefold() for row in conn.execute('SELECT owner,name FROM repositories WHERE tracked=1 AND host=?', (host,))}
        items, controls, navigation = list_controls(items, query, 'available', '불러온 저장소', lambda name: name)
        body += controls + '<div class="list">'
        for name in items:
            body += '<article class="list-row"><span class="row-content">' + esc(name) + '</span>'
            if name.casefold() in tracked:
                body += '<span class="badge">연결됨</span>'
            else:
                body += form('/repos/add', csrf, hidden_host + f'<input type="hidden" name="repository" value="{esc(name)}">' + source_field(host + '/' + name) + f'<button aria-label="{esc(host + "/" + name)} 연결">연결</button>')
            body += '</article>'
        body += '</div>' + navigation
    body += ('<details class="direct-connect"><summary>목록에 없는 저장소를 주소로 연결</summary><div class="connect-toolbar">'
             + form('/repos/add', csrf, hidden_host + '<label>owner/repo<input name="repository" required maxlength="140"></label>' + source_field() + '<button>저장소 연결</button>')
             + '</div></details></div></details>')
    return body


def pending_row(conn, host):
    """연결을 누른 저장소는 확인이 끝나기 전에도 목록 맨 위에 '연결 확인 중'으로 보여준다."""
    from .web import esc
    row = jobs.active(conn, 'repos')
    params = json.loads(row['params'] or '{}') if row else {}
    if not row or not params.get('repository') or params.get('host', 'github.com') != host:
        return ''
    return ('<div class="repo-row repo-pending"><span class="pending-mark" aria-hidden="true">⟳</span><div class="repo-row-main">'
            f'<strong>{esc(params["repository"])}</strong><p class="meta"><span>연결 확인 중</span></p>'
            # 확인이 성공하면 페이지를 다시 읽어 일반 행으로 바꾼다. 실패하면 이 행에 이유가 남는다.
            + job_box(row).replace('<div class="job"', '<div class="job" data-job-reload="1"', 1) + '</div></div>')


def repository_card(server, conn, csrf, query, hosts):
    """호스트 하나의 연결 저장소와 분석 허용 범위를 한 목록에서 관리한다(GitHub 앱 설치 화면과 같은 구성)."""
    from .web import esc
    wanted = query.get('scope_host', [''])[0]
    host = wanted if wanted in hosts else 'github.com'
    scope = analysis_scope(conn, host)
    body = '<section id="analysis-scope" class="repo-section repo-card"><span id="connected-repositories" class="anchor"></span>'
    if len(hosts) > 1:
        keep = [(k, query[k][0]) for k in ('from_pr',) if query.get(k)]
        body += '<nav class="pr-tabs host-tabs" aria-label="호스트">' + ''.join(
            f'<a href="/repos?{esc(urlencode(keep + [("scope_host", h)]))}#analysis-scope"' + (' aria-current="page"' if h == host else '')
            + f'><span>{esc(h)}</span></a>' for h in hosts) + '</nav>'
    body += (f'<div class="section-heading repo-card-heading"><div><h2>{esc(host)} 저장소</h2>'
             '<p class="muted">연결은 PR을 수집하고, 분석 허용은 코드를 모델로 보낼 범위를 정합니다. 기본은 분석 안 함입니다.</p></div></div>'
             + add_panel(server, conn, csrf, query, host))
    rows, controls, navigation = scope_candidates(conn, host, query)
    radios = ''.join(f'<label class="scope-option"><input type="radio" name="scope" value="{value}"' + (' checked' if value == scope else '')
                     + f'><span><strong>{label}</strong><small>{hint}</small></span></label>'
                     for value, label, hint in [('none', SCOPE_LABELS['none'], '코드를 모델 서비스로 보내지 않습니다.'),
                                                ('all', SCOPE_LABELS['all'], '앞으로 연결하는 저장소도 포함합니다.'),
                                                ('selected', SCOPE_LABELS['selected'], '아래에서 체크한 저장소만 분석합니다.')])
    items = ''
    removals = ''
    for r in rows:
        name = r['owner'] + '/' + r['name']
        allowed = scope == 'all' or (scope == 'selected' and r['analysis_policy'] == 'allow')
        items += ('<div class="repo-row"><label class="scope-repo"><input type="checkbox" name="repository" value="' + esc(name) + '"'
                  + (' checked' if r['analysis_policy'] == 'allow' else '') + f' aria-label="{esc(name)} 분석 선택"></label>'
                  + f'<div class="repo-row-main"><strong>{esc(name)}</strong><p class="meta"><span>{esc(r["source"])}</span>'
                  + (f'<span>열린 PR {int(r["open_prs"])}개</span>' if r['open_prs'] else '')
                  + ('' if r['tracked'] else '<span>연결 해제됨</span>') + '</p></div>'
                  + f'<span class="badge {"ok" if allowed else ""}">' + ('분석 허용' if allowed else '분석 안 함') + '</span>')
        if r['tracked']:
            form_id = f'remove-{int(r["id"])}'
            # 연결 해제는 범위 저장 폼 안에 둘 수 없으므로 별도 폼을 form 속성으로 연결한다.
            items += ('<details class="confirm-action"><summary class="btn danger sm">연결 해제</summary>'
                      '<p>추적과 수집을 멈춥니다. 분석 선택은 그대로 남습니다.</p>'
                      f'<button class="btn danger sm" form="{form_id}" aria-label="{esc(host + "/" + name)} 연결 해제 확인">연결 해제 확인</button></details>')
            removals += (f'<form class="action-form" id="{form_id}" method="post" action="/repos/remove" hidden>'
                         f'<input type="hidden" name="csrf" value="{esc(csrf)}"><input type="hidden" name="host" value="{esc(host)}">'
                         f'<input type="hidden" name="repository" value="{esc(name)}"></form>')
        items += '</div>'
    # 저장은 이 화면에 실제로 보여준 저장소만 바꾼다. 페이지를 다시 계산하면 그 사이 순서가 바뀌어 보지 못한 선택이 풀릴 수 있다.
    shown = ''.join(f'<input type="hidden" name="shown" value="{int(r["id"])}">' for r in rows)
    empty = '<p class="empty">연결한 저장소가 없습니다. 위의 <strong>+ 저장소 추가</strong>에서 연결하세요.</p>'
    # 저장소가 한 페이지에 다 보이면 검색창은 오히려 첫 행동(범위 선택)을 가린다.
    searching = 'href' in navigation or query.get('tracked_q') or query.get('select_q')
    body += ((controls if searching else '')
             # form()은 안의 모든 버튼에 클래스를 덧씌우므로 연결 해제 버튼이 섞인 이 폼은 직접 만든다.
             + '<form class="action-form scope-form" method="post" action="/repos/scope">'
             + f'<input type="hidden" name="csrf" value="{esc(csrf)}"><input type="hidden" name="host" value="{esc(host)}">' + shown
             + '<fieldset class="scope-choices"><legend>분석 허용 범위</legend>' + radios + '</fieldset>'
             + '<fieldset class="scope-repos repo-list"><legend class="sr-only">저장소</legend>' + pending_row(conn, host) + (items or empty) + '</fieldset>'
             + f'<p class="consent-note">{esc(WARNING)}</p>'
             + '<label class="consent"><input type="checkbox" name="confirm" value="yes">허용 범위의 코드가 Codex(모델 서비스)로 전송됨을 이해하고 허용합니다</label>'
             + f'<button class="btn secondary" aria-label="{esc(host)} 분석 허용 범위 저장">저장</button></form>'
             + removals + navigation + '</section>')
    return body


RETURN_FIELDS = {
    '/repos': {'tracked_q', 'tracked_page', 'available_q', 'available_page', 'available_team', 'from_pr', 'add',
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
        hosts = [r[0] for r in conn.execute('SELECT host FROM github_hosts ORDER BY host')]
        body = ('<div class="page-intro"><h1>저장소</h1><p>리뷰할 저장소를 연결하고, 호스트마다 분석에 보낼 저장소를 고릅니다.</p>'
                '<p class="muted">GitHub에는 읽기 전용으로 접근합니다.</p></div>')
        from_pr = query.get('from_pr', [''])[0]
        if positive(from_pr):
            body += (f'<p class="notice">PR에서 이동했습니다. 아래 <a href="#analysis-scope">분석 허용 범위</a>에서 이 저장소를 허용한 뒤 '
                     f'<a href="/pr/{int(from_pr)}">PR로 돌아가 분석을 요청하세요</a>.</p>')
        body += repository_card(server, conn, csrf, query, hosts)
        body += '<details class="repo-section host-management"><summary>GitHub 호스트 관리</summary>' + host_settings(server, conn, csrf) + '</details>'
        return preserve_list_state(body, path, query)
    settings = json.loads((server.config.config_dir / 'config.json').read_text())
    model = settings.get('codex_model', '')
    body = '<h1>설정</h1><p>분석 허용 범위(호스트별 전체 또는 선택한 저장소)는 <a href="/repos#analysis-scope">저장소 화면</a>에서 정합니다. 기본은 분석 안 함입니다.</p>'
    body += '<p class="notice">보관 기간 자동 정리는 아직 지원하지 않습니다. 설정 파일의 retention_days 값은 현재 적용되지 않으며 수집된 diff와 분석 기록은 자동 삭제되지 않습니다.</p>'
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


def mutate(server, path, data):
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
    if path == '/home/teams':
        d = fields(data, ('host',), ('team', 'pr_q', 'repo', 'tab'))
        if any(len(value) > 200 for value in d.values()):
            raise ValueError('필터 입력이 너무 깁니다')
        result = {key: d[key] for key in ('team', 'pr_q', 'repo', 'tab') if d.get(key)}
        with closing(connect(server.config)) as conn:
            host = registered(conn, d['host'])
            result['team_host'] = host
            if not getattr(server, 'offline', False):
                existing, created = jobs.enqueue(conn, 'repos', 0, host + ' / 내 팀 저장소',
                                                params={'host': host, 'mode': 'team', 'home_teams': True})
                existing_params = json.loads(existing['params'] or '{}')
                if not created and (existing_params.get('host') != host or not existing_params.get('home_teams')):
                    result['team_busy'] = '1'
        return '/?' + urlencode(result)
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
            jobs.enqueue(conn, 'repos', 0, target, params=params)
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
        body += '<article class="list-row"><div class="row-content"><h3>' + esc(host) + '</h3><p class="meta"><span class="badge">' + esc(status) + '</span><span>마지막 확인: ' + esc(human_time(cached[1]) if cached else '없음') + '</span></p></div><div class="toolbar row-actions">'
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
