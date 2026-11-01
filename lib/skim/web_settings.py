"""한국어 설정 화면과 읽기 전용 저장소 조회 워커."""
from contextlib import closing
import json
import os
import re
import tempfile
import threading
import time

from .collect import Collector, GitHub
from .config import Config, valid_model
from .db import connect, analysis_allowed
from . import jobs
from .hosts import hostname, registered, authenticated
from .web_jobs import JOB_PATHS, job_box, script
from .repos import web_repository, add_repository, remove_repository

POST_PATHS = {'/hosts/add', '/hosts/remove', '/repos/add', '/repos/remove', '/repos/load', '/settings/analysis', '/settings/model'} | JOB_PATHS
WARNING = ('허용하면 PR 코드, diff와 제목, 설명이 Codex CLI를 통해 모델 서비스로 전송됩니다. '
           '민감 정보 제외는 완전하지 않으며 서비스의 전송, 보존 정책이 적용됩니다.')


class RepoLookup:
    """조회는 공통 작업 큐의 단일 워커가 실행하고 목록 결과는 메모리에 60초 동안 재사용한다."""
    def __init__(self, config, clock=time.monotonic):
        self.config, self.clock = config, clock
        self.lock = threading.Lock()
        self.items = []
        self.host = "github.com"
        self.expires = 0
        self.gh = GitHub()
        self._host_limits = {}

    def snapshot(self, with_host=False):
        with self.lock:
            items = list(self.items)
            return (self.host, items) if with_host else items

    def fresh(self, host="github.com"):
        with self.lock:
            return host == self.host and self.clock() < self.expires

    def run(self, parts, source, host="github.com"):
        """워커 전용. 성공 메시지를 반환하고 실패는 예외로 알린다(이전 목록은 보존)."""
        with closing(connect(self.config)) as conn:
            host = registered(conn, host)
        self.gh.host = host
        self.gh.not_before = self._host_limits.get(host, 0)
        try:
            return self._run(parts, source, host)
        finally:
            self._host_limits[host] = self.gh.not_before

    def _run(self, parts, source, host):
        if parts:
            payload = self.gh.get('repos/' + '/'.join(parts))
            actual = web_repository(payload['full_name'])
            if tuple(p.lower() for p in actual) != tuple(p.lower() for p in parts):
                raise ValueError('저장소 주소가 변경되었습니다. 실제 주소를 입력하세요')
            with closing(connect(self.config)) as conn, Collector(conn, self.config).lock(), conn:
                registered(conn, host)
                add_repository(conn, actual, source, host)
            return '저장소를 등록했습니다. 분석은 별도 허용이 필요합니다.'
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
            self.items = sorted(items, key=str.lower)
            self.expires = self.clock() + 60
        return ('최대 1,000개까지만 표시합니다. 직접 입력도 사용할 수 있습니다.' if truncated else
                '접근 가능한 저장소 조회 완료. 선택한 저장소만 등록합니다.')


def form(action, csrf, content):
    from .web import esc
    return (f'<form method="post" action="{action}"><input type="hidden" name="csrf" value="{esc(csrf)}">'
            + content + '</form>')


def source_field():
    return '<label>출처<select name="source"><option value="responsible">담당 (responsible)</option><option value="owned">소유 (owned)</option><option value="managed">관리 (managed)</option></select></label>'


def policy_form(csrf, scope, target, host="github.com"):
    from .web import esc
    return form('/settings/analysis', csrf,
                f'<input type="hidden" name="host" value="{esc(host)}"><input type="hidden" name="scope" value="{scope}"><label>대상<input name="target" value="{esc(target)}" required></label>'
                '<select name="policy"><option value="deny">거부</option><option value="reset">초기화 / 조직 설정 상속</option><option value="allow">허용</option></select>'
                '<label><input type="checkbox" name="confirm" value="yes">코드가 Codex(모델 서비스)로 전송됨을 이해하고 허용합니다</label><button>분석 정책 저장</button>')


def render(server, conn, csrf, path):
    from .web import esc, meta
    if path == '/repos':
        body = '<h1>저장소</h1><p>조직 저장소를 포함하여 GitHub에는 항상 읽기 전용으로 접근합니다.</p>'
        body += '<p>연결 해제는 로컬 추적만 해제합니다. 리뷰 요청 결과와 분석 정책은 보존되며 분석 동의 철회가 아닙니다. 분석 허용 화면에서 별도로 끄세요.</p>'
        body += host_settings(server, conn, csrf)
        for row in conn.execute('SELECT * FROM repositories WHERE tracked=1 ORDER BY owner,name'):
            name = row['owner'] + '/' + row['name']
            body += '<article>' + meta(row['host'], name, row['source'])
            body += form('/repos/remove', csrf, f'<input type="hidden" name="host" value="{esc(row["host"])}"><input type="hidden" name="repository" value="{esc(name)}"><button>연결 해제</button>') + '</article>'
        body += '<h2>직접 연결</h2>' + form('/repos/add', csrf, host_field(conn) + '<label>owner/repo<input name="repository" required maxlength="140"></label>' + source_field() + '<button>저장소 연결</button>')
        body += form('/repos/load', csrf, host_field(conn) + '<button>접근 가능한 저장소 불러오기</button>')
        lookup = getattr(server, 'repo_lookup', None)
        lookup_host, items = lookup.snapshot(with_host=True) if lookup else ('github.com', [])
        if getattr(server, 'offline', False):
            body += '<p>오프라인 모드에서는 GitHub 저장소를 불러오거나 접근을 확인하지 않습니다.</p>'
        running = jobs.active(conn, 'repos')
        if running is not None:
            body += '<p>이미 저장소 조회 작업이 진행 중입니다. 완료 후 다시 요청하세요. 진행 중 요청은 새 작업을 만들지 않습니다.</p>'
        body += job_box(jobs.latest(conn, 'repos')) + script()
        for name in items:
            body += '<article>' + esc(lookup_host + '/' + name) + form('/repos/add', csrf, f'<input type="hidden" name="host" value="{esc(lookup_host)}"><input type="hidden" name="repository" value="{esc(name)}">' + source_field() + '<button>선택하여 연결</button>') + '</article>'
        return body
    settings = json.loads((server.config.config_dir / 'config.json').read_text())
    model = settings.get('codex_model', '')
    body = '<h1>설정과 분석 허용</h1><p>' + WARNING + '</p><p>기본 꺼짐. 저장소의 명시적 허용, 거부는 조직 정책보다 우선합니다. 조직 허용은 해당 조직의 모든 저장소에 적용됩니다.</p>'
    for row in conn.execute('SELECT * FROM repositories ORDER BY owner,name'):
        name = row['owner'] + '/' + row['name']
        body += '<article><h2>' + esc(row['host'] + '/' + name) + '</h2>' + meta('정책: ' + row['analysis_policy'], '실제 적용: ' + ('허용' if analysis_allowed(conn, row['id']) else '거부'))
        body += policy_form(csrf, 'repo', name, row['host']) + '</article>'
    body += '<h2>조직 정책</h2>'
    for row in conn.execute('SELECT owner,policy FROM organization_policies ORDER BY owner'):
        body += '<article><p>' + esc(row['owner']) + ': ' + esc(row['policy']) + '</p>'
        body += policy_form(csrf, 'org', row['owner']) + '</article>'
    body += policy_form(csrf, 'org', '') + host_field_note(conn, csrf)
    body += '<h2>모델 설정</h2>' + meta('파일 값: ' + (model or '미설정'), '실제 적용: ' + (Config.parse_codex_model(settings) or '자동 감지'))
    if 'SKIM_CODEX_MODEL' in os.environ:
        body += '<p>SKIM_CODEX_MODEL 환경변수가 파일 설정보다 우선합니다.</p>'
    body += '<p>다음 분석부터 적용됩니다. 모델 변경 후 격리 검증이 일치하지 않으면 분석이 차단됩니다. skim doctor --probe로 재검증하세요.</p>'
    body += form('/settings/model', csrf, '<label>codex_model<input name="model" maxlength="64" value="' + esc(model) + '" required></label><button>모델 저장</button>')
    return body


def fields(data, required, optional=()):
    if not set(required) <= set(data) or set(data) - set(required) - set(optional) or any(len(v) != 1 for v in data.values()):
        raise ValueError('허용 필드와 입력 개수를 확인하세요')
    return {key: value[0] for key, value in data.items()}


def mutate(server, path, data):
    if path in ('/hosts/add', '/hosts/remove'):
        d = fields(data, ('host',))
        host = hostname(d['host'])
        with closing(connect(server.config)) as conn, Collector(conn, server.config).lock(), conn:
            if path == '/hosts/add':
                conn.execute('INSERT OR IGNORE INTO github_hosts VALUES(?)', (host,))
            else:
                if host == 'github.com':
                    raise ValueError('기본 호스트는 삭제할 수 없습니다')
                conn.execute('UPDATE repositories SET tracked=0,enabled=0 WHERE host=?', (host,))
                conn.execute('DELETE FROM host_organization_policies WHERE host=?', (host,))
                conn.execute('DELETE FROM github_hosts WHERE host=?', (host,))
        return '/repos'
    if path in ('/repos/load', '/repos/add'):
        d = fields(data, () if path.endswith('load') else ('repository', 'source'), ('host',))
        with closing(connect(server.config)) as conn:
            host = registered(conn, d.get('host', 'github.com'))
        parts = None
        if path == '/repos/add':
            parts = web_repository(d['repository'])
            if d['source'] not in ('owned', 'managed', 'responsible'):
                raise ValueError('잘못된 저장소 출처입니다')
        if getattr(server, 'offline', False):
            return '/repos'
        if parts is None and server.repo_lookup.fresh(host):
            return '/repos'
        params = dict(repository='/'.join(parts), source=d['source']) if parts else {}
        params['host'] = host
        with closing(connect(server.config)) as conn:
            jobs.enqueue(conn, 'repos', 0, params.get('repository', '접근 가능한 저장소 목록'), params=params)
        return '/repos'
    if path == '/settings/model':
        d = fields(data, ('model',))
        model = d['model']
        if not re.fullmatch(r'[A-Za-z0-9._:-]{1,64}', model) or not valid_model(model):
            raise ValueError('모델 이름 형식을 확인하세요 (최대 64자)')
        target = server.config.config_dir / 'config.json'
        settings = json.loads(target.read_text())
        settings['codex_model'] = model
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


def host_field(conn):
    from .web import esc
    return '<label>호스트<select name="host">' + ''.join(
        f'<option{" selected" if r[0] == "github.com" else ""}>{esc(r[0])}</option>' for r in conn.execute('SELECT host FROM github_hosts ORDER BY host')) + '</select></label>'


def host_settings(server, conn, csrf):
    from .web import esc
    body = '<h2>GitHub 호스트</h2><p>호스트 삭제 시 저장된 기록은 보존하며 해당 호스트의 추적과 수집을 중지합니다.</p>'
    for row in conn.execute('SELECT host FROM github_hosts ORDER BY host'):
        host = row[0]
        status = '미확인 (오프라인)' if getattr(server, 'offline', False) else ('로그인됨' if authenticated(host) else '로그인 안 됨')
        body += '<article>' + esc(host) + ': ' + status
        if host != 'github.com':
            body += form('/hosts/remove', csrf, f'<input type="hidden" name="host" value="{esc(host)}"><button>호스트 삭제</button>')
        body += '</article>'
    return body + form('/hosts/add', csrf, '<label>호스트<input name="host" required maxlength="253" placeholder="github.corp.example"></label><button>호스트 추가</button>')


def host_field_note(conn, csrf):
    from .web import esc
    body = ''
    for row in conn.execute("SELECT host FROM github_hosts WHERE host!='github.com'"):
        body += '<h3>' + esc(row[0]) + ' 조직 정책</h3>' + policy_form(csrf, 'org', '', row[0])
    for row in conn.execute('SELECT * FROM host_organization_policies ORDER BY host,owner'):
        body += esc(row['host'] + '/' + row['owner'] + ': ' + row['policy']) + policy_form(csrf, 'org', row['owner'], row['host'])
    return body
