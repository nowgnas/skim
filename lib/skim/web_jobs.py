"""웹 작업 등록, 상태 화면. HTTP에서는 검증과 등록만 하고 실행은 워커가 맡는다."""
import re

from . import jobs
from .db import analysis_allowed, analysis_denied
from .repos import web_repository
from .hosts import registered

JOB_PATHS = {'/jobs/collect', '/jobs/analyze', '/jobs/doctor'}
RECOVERY = [
    ('gh', 'gh auth login --hostname <host>로 다시 인증하고, 조직 저장소는 gh 자격 증명의 SSO 승인을 확인하세요.'),
    ('codex-home', 'skim codex-home init으로 분석 전용 인증 링크를 만드세요. 본인 소유 경로 복구는 --reset을 사용합니다.'),
    ('version, flags', 'Codex 안정 버전 0.165.0 이상을 설치하고 PATH를 확인하세요.'),
    ('probe, 모델', '모델 변경 후나 probe 만료 시 터미널에서 skim doctor --probe를 실행하세요. 실제 모델을 호출하므로 웹에서는 자동 실행하지 않습니다.'),
    ('로컬 권한', 'SKIM_HOME 아래 디렉터리 0700, 파일 0600, 본인 소유, 심볼릭 링크 없음을 확인하세요.'),
]


class Rejected(ValueError):
    """사용자에게 그대로 보여도 안전한 거부 사유."""
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def summary_parts(row):
    d = jobs.describe(row)
    parts = [f'{d["kind_label"]} #{d["id"]}', d['status_label']]
    if d['stage'] and d['stage'] != d['status_label']:
        parts.append(d['stage'])
    if d['total'] is not None and d['processed'] is not None:
        parts.append(f'{d["processed"]}/{d["total"]}')
    for key, label in [('target', '대상'), ('model', '모델'), ('started_at', '시작'),
                       ('finished_at', '종료'), ('retry_at', '재시도 가능')]:
        if d[key] and (key != 'finished_at' or not d['active']):
            parts.append(label + ' ' + d[key])
    if d['followup']:
        parts.append('완료 후 1회 다시 수집 예정')
    return parts


def summary(row):
    return ', '.join(summary_parts(row))


def describe(row):
    return dict(jobs.describe(row), summary=summary(row), summary_parts=summary_parts(row))


def job_box(row, poll=True):
    from .web import esc
    if row is None:
        return '<p class="muted">최근 작업 없음</p>'
    d = jobs.describe(row)
    body = (f'<div class="job" data-job-id="{d["id"]}" data-job-active="{int(poll and d["active"])}">'
            '<p class="meta" data-job-field="summary">'
            + ''.join(f'<span>{esc(part)}</span>' for part in summary_parts(row)) + '</p>')
    if d['error']:
        body += f'<p class="error">{esc(d["error"])}</p>'
    if d['result']:
        body += f'<pre>{esc(d["result"])}</pre>'
    return body + '</div>'


def script():
    return '<script src="/static/jobs.js" defer></script>'


def collect_panel(conn, csrf, offline):
    from .web_settings import form
    body = '<article><h2>수집과 새로고침</h2>'
    if offline:
        body += '<p>오프라인 모드에서는 GitHub 수집을 실행하지 않습니다.</p>'
    else:
        body += ('<p class="muted">정기 수집과 같은 큐를 씁니다. 수집은 모델 전송 동의를 만들지 않으며 '
                 '수집 후 자동 분석도 저장소별 허용, 하루 예산, 격리 검사를 따릅니다.</p>')
        body += form('/jobs/collect', csrf, '<button>지금 수집</button>')
    return body + job_box(jobs.latest(conn, 'collect')) + '</article>'


def analysis_panel(conn, pr, csrf, offline):
    from .web import esc
    from .web_settings import form, consent_form, WARNING
    body = '<article><h2>PR 분석 요청</h2>'
    snapshot = pr['snapshot']
    if offline:
        body += '<p>오프라인 모드에서는 모델 분석을 실행하지 않습니다.</p>'
    elif analysis_denied(conn, pr['repo_id']):
        # 사용자가 명시적으로 거부한 저장소·조직에는 상세 화면에서 다시 허용을 권하지 않는다.
        body += '<p>이 저장소는 분석이 거부되어 있습니다. 바꾸려면 <a href="/settings">분석 허용</a>에서 정책을 변경하세요.</p>'
    elif not analysis_allowed(conn, pr['repo_id']):
        body += ('<p>이 저장소는 LLM 분석이 허용되지 않았습니다. 아래에서 이 저장소만 허용하거나 '
                 '<a href="/settings">분석 허용</a>에서 조직 단위로 켜세요. 허용해도 분석은 자동으로 시작하지 않습니다.</p>'
                 '<p><strong>' + WARNING + '</strong></p>'
                 + consent_form(csrf, pr.get('host', 'github.com'), pr['owner'] + '/' + pr['name'], f'/pr/{int(pr["id"])}'))
    elif not snapshot or snapshot['head_sha'] != pr['head_sha'] or snapshot['base_sha'] != pr['base_sha']:
        body += '<p>최신 SHA의 스냅샷이 없습니다. <a href="/status">먼저 수집</a>한 뒤 분석을 요청하세요.</p>'
    else:
        body += '<p><strong>' + WARNING + '</strong></p>'
        body += '<p class="muted">하루 20회 모델 호출 예산과 격리 검사는 수동 요청에도 적용됩니다.' + (' draft PR도 같은 동의, 예산을 따릅니다.' if pr['draft'] else '') + '</p>'
        body += form('/jobs/analyze', csrf,
                     f'<input type="hidden" name="host" value="{esc(pr.get("host", "github.com"))}"><input type="hidden" name="repository" value="{esc(pr["owner"] + "/" + pr["name"])}">'
                     f'<input type="hidden" name="number" value="{int(pr["number"])}"><button>이 PR 분석</button>')
    running = jobs.active(conn, 'analyze')
    if running is not None and running['target_id'] != pr['id']:
        body += ('<p>다른 분석 작업(' + esc(running['target'] or '자동 분석') + ')이 '
                 + esc(jobs.STATUS[running['status']]) + ' 상태입니다. 이 PR은 완료 후 다시 요청하세요.</p>')
        body += job_box(running)
    body += job_box(jobs.latest(conn, 'analyze', pr['id']))
    return body + '</article>'


def status_page(conn, csrf, offline):
    from .web import esc
    from .web_settings import form
    body = '<h1>상태와 작업</h1><article><h2>상태 점검 (doctor)</h2>'
    body += '<p class="muted">gh 인증, Codex 설치, 전용 홈, 캐시된 격리 probe, 모델, 로컬 권한을 점검합니다. 실제 모델 호출(--probe)과 인증, 권한 수정은 하지 않습니다.</p>'
    if offline:
        body += '<p>오프라인 모드에서는 gh, Codex를 실행하는 상태 점검을 하지 않습니다.</p>'
    else:
        body += form('/jobs/doctor', csrf, '<button>상태 점검 실행</button>')
    body += job_box(jobs.latest(conn, 'doctor'))
    body += '<h2>복구 안내</h2><ul>' + ''.join(f'<li><strong>{esc(name)}</strong>: {esc(text)}</li>' for name, text in RECOVERY) + '</ul></article>'
    body += collect_panel(conn, csrf, offline)
    body += '<h2>활성 작업</h2>'
    active_rows = conn.execute("SELECT * FROM jobs WHERE status IN ('pending','running') ORDER BY id").fetchall()
    body += ''.join('<article>' + job_box(row) + '</article>' for row in active_rows) or '<p class="muted">활성 작업이 없습니다.</p>'
    body += '<h2>최근 작업</h2>'
    rows = jobs.recent(conn)
    body += ''.join('<article>' + job_box(row) + '</article>' for row in rows) or '<p class="muted">작업 기록이 없습니다.</p>'
    return body + script()


def mutate(server, conn, path, data):
    from .web_settings import fields
    if getattr(server, 'offline', False):
        raise Rejected('오프라인 모드에서는 수집, 분석, 상태 점검을 실행하지 않습니다. 저장된 결과만 볼 수 있습니다.', 409)
    if path == '/jobs/collect':
        fields(data, ())
        jobs.request_collect(conn)
        return '/status'
    if path == '/jobs/doctor':
        fields(data, ())
        jobs.enqueue(conn, 'doctor', 0, '로컬 환경')
        return '/status'
    d = fields(data, ('repository', 'number'), ('host',))
    host = registered(conn, d.get('host', 'github.com'))
    owner, name = web_repository(d['repository'])
    if not re.fullmatch(r'[1-9][0-9]{0,9}', d['number']):
        raise Rejected('PR 번호 형식을 확인하세요')
    pr = conn.execute('SELECT p.id,p.repo_id,p.state,p.number,p.head_sha,p.base_sha,r.owner,r.name FROM pull_requests p '
                      'JOIN repositories r ON r.id=p.repo_id WHERE r.host=? AND r.owner=? AND r.name=? AND p.number=?',
                      (host, owner, name, int(d['number']))).fetchone()
    if pr is None or pr['state'] != 'open':
        raise Rejected('수집된 열린 PR이 아닙니다', 404)
    location = f'/pr/{pr["id"]}'
    if not analysis_allowed(conn, pr['repo_id']):
        raise Rejected('이 저장소는 LLM 분석이 허용되지 않았습니다. 분석 허용 화면에서 먼저 켜세요.', 403)
    snapshot = conn.execute('SELECT id,head_sha,base_sha FROM snapshots WHERE pr_id=? ORDER BY id DESC LIMIT 1', (pr['id'],)).fetchone()
    if snapshot is None or (snapshot['head_sha'], snapshot['base_sha']) != (pr['head_sha'], pr['base_sha']):
        raise Rejected('최신 SHA의 스냅샷이 없습니다. 먼저 수집하세요.', 409)
    # 다른 PR 분석이 활성이면 대상을 바꾸지 않고 기존 작업을 그대로 둔다. 화면이 상태와 재요청 안내를 보여 준다.
    jobs.enqueue(conn, 'analyze', pr['id'], f'{pr["owner"]}/{pr["name"]}#{pr["number"]}',
                 snapshot['head_sha'], {'snapshot_id': snapshot['id']})
    return location
