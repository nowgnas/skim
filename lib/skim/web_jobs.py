"""웹 작업 등록, 상태 화면. HTTP에서는 검증과 등록만 하고 실행은 워커가 맡는다."""
import re
import json
from urllib.parse import urlencode

from . import jobs
from .timefmt import duration, human_time, kst_clock
from .db import analysis_allowed
from .repos import web_repository
from .hosts import registered

JOB_PATHS = {'/jobs/collect', '/jobs/analyze', '/jobs/doctor'}
RECOVERY = [
    ('gh', 'gh auth login --hostname <host>로 다시 인증하고, 조직 저장소는 gh 자격 증명의 SSO 승인을 확인하세요.'),
    ('codex-home', '분석 준비 초기화를 실행하세요. 터미널에서는 skim codex-home init을 사용합니다. 기존 파일이나 소유권 문제는 경로를 확인하세요.'),
    ('version, flags', 'Codex 안정 버전 0.165.0 이상을 설치하고 PATH를 확인하세요.'),
    ('probe, 모델', '모델 변경 후나 probe 만료 시 격리 검증을 실행하세요. 터미널에서는 skim doctor --probe를 사용합니다. 실제 모델 호출은 명시적 요청 때만 실행합니다.'),
    ('중단된 모델 실행', '이전 모델 실행 종료 여부를 확인할 수 없으면 분석을 차단합니다. Codex와 하위 프로세스가 모두 종료됐는지 직접 확인한 뒤 data 디렉터리의 analysis-inflight 표식을 제거하세요. 기본 경로는 ~/.local/share/skim, SKIM_HOME 사용 시 $SKIM_HOME/data입니다.'),
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
            parts.append(label + ' ' + (human_time(d[key]) if key.endswith('_at') else d[key]))
    if d['followup']:
        parts.append('완료 후 1회 다시 수집 예정')
    return parts


def summary(row):
    return ', '.join(summary_parts(row))


def collection_summary_parts(d):
    if d['status'] == 'pending':
        status = '수집 실패, 재시도 대기' if d['stage'] == '재시도 대기' else '수집 시작 대기'
    else:
        status = {'running': 'PR 정보 수집 중', 'completed': 'PR 정보 수집 완료',
                  'failed': 'PR 정보 수집 실패', 'deferred': 'PR 정보 수집 보류',
                  'blocked': 'PR 정보 수집 차단', 'cancelled': 'PR 정보 수집 취소'}.get(d['status'], 'PR 수집 상태 확인 필요')
    parts = [status]
    if d['status'] == 'running' and d['stage'] and d['stage'] not in ('수집 중', status):
        parts.append('진행 단계: ' + d['stage'])
    if d['total'] is not None and d['processed'] is not None:
        parts.append(f'처리한 항목: {d["processed"]}/{d["total"]}')
    if d['retry_at']:
        parts.append('다시 시도할 시각: ' + human_time(d['retry_at']))
    if d['finished_at'] and not d['active']:
        parts.append('수집 종료 시각: ' + human_time(d['finished_at']))
    if d['followup']:
        parts.append('현재 작업 후 한 번 더 수집 예정')
    return parts


def analysis_summary_parts(d):
    status = {'pending': '분석 대기 중', 'running': '분석 진행 중', 'completed': '분석 완료',
              'failed': '분석 실패', 'deferred': '분석 보류', 'blocked': '분석 차단',
              'cancelled': '분석 취소'}.get(d['status'], '분석 상태 확인 필요')
    parts = [status]
    if d['status'] == 'running' and d['stage'] and d['stage'] not in ('시작', '실행 중'):
        parts.append(d['stage'])
    if d['processed'] is not None and d['total'] is not None:
        parts.append(f'{d["processed"]}/{d["total"]}')
    return parts


RECORD_STATUS = {'pending': '대기 중', 'blocked': '차단'}
RECORD_TONE = {'running': 'running', 'completed': 'ok', 'failed': 'high', 'blocked': 'high'}


def first_line(text):
    return next(iter((text or '').strip().splitlines()), '')


def record_view(row, now=None):
    """One job-history card: badge, KST clock times, duration and a one-line outcome."""
    d = jobs.describe(row)
    running, failed = d['status'] == 'running', d['status'] in ('failed', 'blocked')

    def clock(label, value):
        relative = human_time(value, now)
        return [label, kst_clock(value, now), relative if relative == '방금' or relative.endswith('분 전') else '']

    facts = [clock('시작', d['started_at']) if d['started_at'] else clock('요청', d['created_at'])]
    if d['finished_at'] and not d['active']:
        facts.append(clock('종료', d['finished_at']))
    if d['started_at'] and (running or (d['finished_at'] and not d['active'])):
        facts.append(['경과' if running else '소요', duration(d['started_at'], None if running else d['finished_at'], now), ''])
    if d['retry_at']:
        facts.append(clock('재시도 가능', d['retry_at']))
    if d['model']:
        facts.append(['모델', d['model'], ''])
    notes = []
    if d['active']:
        stage = d['stage'] if d['stage'] not in (None, '', '시작', d['status_label']) else ''
        count = f'{d["processed"]}/{d["total"]}' if d['total'] is not None and d['processed'] is not None else ''
        notes.append(' '.join(filter(None, [stage, count])))
        if d['followup']:
            notes.append('완료 후 1회 다시 수집 예정')
    if not failed:
        checks = doctor_checks(row) if d['kind'] == 'doctor' and not d['active'] else []
        if checks:
            problems = sum(c['state'] != 'ok' for c in checks)
            notes.append(f'정상 {len(checks) - problems}개, 확인 필요 {problems}개')
        else:
            notes.append(first_line(d['error']) or first_line(d['result']))
    return dict(status_label=RECORD_STATUS.get(d['status'], d['status_label']), tone=RECORD_TONE.get(d['status'], ''),
                facts=facts, note=', '.join(filter(None, notes)), error_line=(first_line(d['error']) or first_line(d['result'])) if failed else '')


def describe(row):
    d = jobs.describe(row)
    result = dict(d, summary=summary(row), summary_parts=summary_parts(row), record=record_view(row))
    if d['kind'] == 'collect':
        result['collection_summary_parts'] = collection_summary_parts(d)
    elif d['kind'] == 'analyze':
        result['analysis_summary_parts'] = analysis_summary_parts(d)
    return result


def recovery_actions(csrf, offline, kind=None, dynamic=False):
    """문구는 실행하지 않고 미리 정한 상태 점검 작업만 제공한다."""
    if offline or not csrf:
        return ''
    from .web_settings import form
    probe = ('<p>격리 검증은 실제 모델을 호출하며 사용량 또는 비용이 발생할 수 있습니다.</p>'
             + form('/jobs/doctor', csrf, '<input type="hidden" name="action" value="probe">'
                    '<label><input type="checkbox" name="confirm" value="yes" required> 모델 호출에 동의합니다</label>'
                    '<button>격리 검증 실행</button>'))
    home = ('<p>전용 홈을 다시 만들고 기존 홈의 내용과 격리 검증 기록을 삭제합니다. 원본 인증 파일은 삭제하지 않습니다.</p>'
            + form('/jobs/doctor', csrf, '<input type="hidden" name="action" value="reset">'
                   '<label><input type="checkbox" name="confirm" value="yes" required> 삭제와 재설정에 동의합니다</label>'
                   '<button>전용 홈 재설정</button>'))
    actions = {'probe': probe, 'home': home,
               'status': '<a class="btn secondary sm" href="/status">상태 점검 및 복구 안내</a>'}
    return ''.join(f'<div class="recovery-action" data-recovery-kind="{name}"'
                   + ('' if kind == name else ' hidden') + '>' + content + '</div>'
                   for name, content in actions.items() if dynamic or kind == name)


def recovery_kind(error):
    if 'skim doctor --probe' in (error or ''):
        return 'probe'
    if 'skim codex-home init --reset' in (error or ''):
        return 'home'
    if ('Codex 분석 차단' in (error or '') or ('probe' in (error or '') and 'Codex' in (error or ''))
            or ('상태 점검' in (error or '') and ('격리' in error or '차단' in error))):
        return 'status'
    return None


def progress_bar(d):
    if not d['active']:
        return ''
    running = d['status'] == 'running'
    known = running and d['total'] is not None and d['total'] > 0 and d['processed'] is not None and 0 <= d['processed'] <= d['total']
    return ('<div class="job-progress" data-job-progress>'
            + '<span class="job-progress-label" data-job-progress-label>' + ('작업 중' if running else '대기 중') + '</span>'
            + '<progress aria-label="작업 진행"' + (f' max="{d["total"]}" value="{d["processed"]}"' if known else '')
            + ('' if running else ' hidden') + '></progress></div>')


def job_box(row, poll=True, compact=False, analysis_compact=False, csrf=None, offline=False):
    from .web import esc
    if row is None:
        if analysis_compact:
            return ''
        return '<p class="muted">아직 PR 수집 작업이 없습니다.</p>' if compact else '<p class="muted">최근 작업 없음</p>'
    d = jobs.describe(row)
    parts = collection_summary_parts(d) if compact else analysis_summary_parts(d) if analysis_compact else summary_parts(row)
    body = (f'<div class="job" data-job-id="{d["id"]}" data-job-active="{int(poll and d["active"])}"' + (' data-job-compact="1"' if compact else ' data-job-analysis="1"' if analysis_compact else '') + '>'
            '<p class="meta" aria-live="polite" data-job-field="summary">'
            + ''.join(f'<span>{esc(part)}</span>' for part in parts) + '</p>')
    body += progress_bar(d)
    result = '\n'.join(str(value) for value in (d['error'], d['result']) if value)
    if analysis_compact:
        if result == parts[0]:
            result = ''
        body += ('<div class="job-result-slot"><p class="analysis-result" data-job-result aria-live="polite"' + ('' if result else ' hidden')
                 + '><span data-job-result-text>' + esc(result) + '</span></p>'
                 + '<a class="btn secondary sm" data-job-refresh href="" hidden>결과 새로고침</a></div>')
    else:
        body += ('<div class="job-result-slot"><details data-job-result' + ('' if result else ' hidden')
                 + '><summary>' + ('수집 결과와 오류 보기' if compact else '결과 상세') + '</summary><pre data-job-result-text>' + esc(result) + '</pre></details>'
                 + '<a class="btn secondary sm" data-job-refresh href="" hidden>화면 새로고침</a></div>')
    if d['kind'] == 'analyze':
        body += recovery_actions(csrf, offline, recovery_kind(d['error']), dynamic=poll and d['active'])
    return body + '</div>'


def job_record(row, csrf=None, offline=False):
    """Status-page history card. jobs.js refreshes the same fields from the `record` payload."""
    from .web import esc
    d = jobs.describe(row)
    view = record_view(row)
    state = ' job-running' if d['status'] == 'running' else ' job-failed' if d['status'] in ('failed', 'blocked') else ''
    facts = ''.join('<span class="job-fact"><span class="job-fact-label">' + esc(label) + '</span><span>' + esc(value) + '</span>'
                    + (f'<span class="job-fact-hint">{esc(hint)}</span>' if hint else '') + '</span>'
                    for label, value, hint in view['facts'])
    result = '\n'.join(str(value) for value in (d['error'], d['result']) if value)
    body = ('<article class="job-row' + state + f'"><div class="job job-record" data-job-id="{d["id"]}" data-job-active="{int(d["active"])}" data-job-record="1">'
            '<div class="job-head"><span class="badge job-status' + (' ' + view['tone'] if view['tone'] else '') + '" aria-live="polite" data-job-status>'
            + esc(view['status_label']) + f'</span><h3 class="job-title">{esc(d["kind_label"])} #{d["id"]}</h3>'
            + (f'<span class="job-target">{esc(d["target"])}</span>' if d['target'] else '') + '</div>'
            '<p class="job-facts" data-job-facts>' + facts + '</p>' + progress_bar(d)
            + '<p class="job-note"' + ('' if view['note'] else ' hidden') + '>' + esc(view['note']) + '</p>'
            # The first error line stays visible without opening the details below.
            + '<p class="job-error"' + ('' if view['error_line'] else ' hidden') + '>' + esc(view['error_line']) + '</p>'
            + '<div class="job-result-slot"><details data-job-result' + ('' if result else ' hidden')
            + '><summary>결과 상세</summary><pre data-job-result-text>' + esc(result) + '</pre></details>'
            + '<a class="btn secondary sm" data-job-refresh href="" hidden>화면 새로고침</a></div>')
    if d['kind'] == 'analyze':
        body += recovery_actions(csrf, offline, recovery_kind(d['error']), dynamic=d['active'])
    return body + '</div></article>'


def script():
    return '<script src="/static/jobs.js" defer></script>'


def collect_panel(conn, csrf, offline, compact=False):
    from .web_settings import form
    if compact:
        action = '<p class="muted">오프라인 모드</p>' if offline else form('/jobs/collect', csrf, '<button>지금 수집</button>', variant='primary')
        note = '' if offline else '<p class="muted collect-note">분석이 허용된 저장소는 수집 후 자동 분석될 수 있습니다.</p>'
        return '<section class="heading-collect" aria-label="PR 수집 현황"><div class="collect-heading"><h2>PR 수집 현황</h2>' + action + '</div><p class="collect-description">GitHub에서 연결한 저장소와 내게 요청된 리뷰의 PR 정보를 가져옵니다.</p>' + job_box(jobs.latest(conn, 'collect'), compact=True) + note + '</section>'
    body = '<article class="collect-panel"><h2>수집과 새로고침</h2>'
    if offline:
        body += '<p>오프라인 모드에서는 GitHub 수집을 실행하지 않습니다.</p>'
    else:
        body += ('<p class="muted">정기 수집과 같은 큐를 씁니다. 수집은 모델 전송 동의를 만들지 않으며 '
                 '수집 후 자동 분석도 저장소별 허용, 하루 예산, 격리 검사를 따릅니다.</p>')
        body += form('/jobs/collect', csrf, '<button>지금 수집</button>')
    return body + job_box(jobs.latest(conn, 'collect')) + '</article>'


def analysis_panel(conn, pr, csrf, offline):
    from .web import esc, label
    from .web_settings import form
    body = '<article class="detail-action"><h2 class="sr-only">PR 분석 요청</h2><div class="analysis-controls">'
    snapshot = pr['snapshot']
    if offline:
        body += '<p class="analysis-note">오프라인 모드에서는 분석할 수 없습니다.</p>'
    elif not analysis_allowed(conn, pr['repo_id']):
        # 대상 저장소만 검색된 상태로 열고, 저장 후 이 PR로 돌아올 수 있게 번호를 넘긴다.
        target = urlencode([('scope_host', pr.get('host', 'github.com')), ('tracked_q', pr['owner'] + '/' + pr['name']),
                            ('from_pr', str(int(pr['id'])))])
        body += (f'<a class="btn primary" href="/repos?{esc(target)}#analysis-scope">분석 허용 범위 열기</a>'
                 '<p class="analysis-note">이 저장소는 분석이 허용되지 않았습니다.</p>')
    elif not snapshot or snapshot['head_sha'] != pr['head_sha'] or snapshot['base_sha'] != pr['base_sha']:
        body += '<p class="analysis-note">최신 변경을 먼저 <a href="/status">수집</a>해 주세요.</p>'
    else:
        current_analysis = pr['analysis'] if pr['analysis'] and pr['analysis']['snapshot_id'] == snapshot['id'] else None
        body += form('/jobs/analyze', csrf,
                     f'<input type="hidden" name="host" value="{esc(pr.get("host", "github.com"))}"><input type="hidden" name="repository" value="{esc(pr["owner"] + "/" + pr["name"])}">'
                     f'<input type="hidden" name="number" value="{int(pr["number"])}"><button>{"다시 분석" if current_analysis else "이 PR 분석"}</button>',
                     # 결과가 있으면 검토가 주된 일이고 재분석은 예산을 쓰는 보조 동작이다.
                     variant='secondary' if current_analysis else 'primary')
    if pr['analysis']:
        stats = json.loads(snapshot['coverage']).get('analysis', {}) if snapshot else {}
        body += ('<p class="analysis-summary"><span class="badge">' + esc(label(pr['status'])) + '</span>'
                 + f'<span>분석 {int(stats.get("analyzed", 0))}개</span><span>제외 {int(stats.get("excluded", 0))}개</span>'
                 + '<span>' + esc(human_time(pr['analysis']['created_at'])) + '</span></p>')
    body += '</div><div class="analysis-job-status">'
    running = jobs.active(conn, 'analyze')
    if running is not None and running['target_id'] != pr['id']:
        body += '<p class="analysis-note">다른 PR 분석: ' + esc(running['target'] or '자동 분석') + '</p>'
        body += job_box(running, analysis_compact=True, csrf=csrf, offline=offline)
    body += job_box(jobs.latest(conn, 'analyze', pr['id']), analysis_compact=True, csrf=csrf, offline=offline)
    return body + '</div></article>'


CHECK_LABELS = {'python': 'Python', 'codex-home': 'Codex 전용 홈', 'version': 'Codex 버전', 'flags': 'Codex 격리 옵션',
                'probe': '격리 검증', 'model': '분석 모델', '로컬 권한': '로컬 권한'}
CODEX_CHECKS = {'codex-home', 'version', 'flags', 'probe', 'model'}


def doctor_checks(row):
    """상태 점검 작업 결과의 '정상/실패/경고: 항목 — 내용' 줄을 항목별로 나눈다."""
    checks = []
    for line in '\n'.join(filter(None, [row['result'], row['last_error']])).splitlines() if row else []:
        match = re.match(r'(정상|실패|경고): (.+?) — (.*)', line)
        if not match:
            continue
        state, name, detail = match.groups()
        key = 'gh' if name.startswith('gh ') else name
        title = f'GitHub 인증 ({name[3:]})' if key == 'gh' else CHECK_LABELS.get(name, name)
        checks.append(dict(state={'정상': 'ok', '실패': 'bad', '경고': 'warn'}[state], key=key, title=title, detail=detail))
    return checks


def recovery_for(key):
    return next((text for name, text in RECOVERY if key in [part.strip() for part in name.split(',')]), '')


def codex_setup(csrf, offline, conn, opened):
    from .web_settings import form
    body = ('<details id="codex-setup" class="status-section codex-setup"' + (' open' if opened else '') + '><summary>Codex 분석 준비</summary>'
            '<p>초기화는 로컬 인증 링크를 준비합니다. 기존 파일은 삭제하거나 덮어쓰지 않습니다.</p>'
            '<p>격리 검증은 저장소 코드를 보내지 않으며 실제 모델 서비스를 호출합니다. 사용량 또는 비용이 발생할 수 있습니다.</p>')
    if offline:
        return body + '<p>오프라인 모드에서는 초기화와 격리 검증을 실행하지 않습니다.</p></details>'
    body += '<div class="toolbar">' + form('/jobs/doctor', csrf, '<input type="hidden" name="action" value="init"><button>분석 준비 초기화</button>')
    body += form('/jobs/doctor', csrf, '<input type="hidden" name="action" value="probe"><label><input type="checkbox" name="confirm" value="yes" required>실제 유료 모델 호출에 동의합니다</label><button>격리 검증 실행</button>') + '</div>'
    body += '<details><summary>전용 홈 재설정</summary>' + recovery_actions(csrf, offline, 'home') + '</details>'
    if jobs.active(conn, 'doctor'):
        body += '<p>상태 점검 또는 분석 준비 작업이 진행 중입니다. 완료 후 다른 작업을 요청하세요.</p>'
    return body + '</details>'


def status_page(conn, csrf, offline):
    """맨 위에 '지금 문제가 있는가'를, 그 아래에 작업 기록 한 목록을 둔다."""
    from .web import esc
    from .web_settings import form
    latest_doctor = jobs.latest(conn, 'doctor')
    finished = conn.execute("SELECT * FROM jobs WHERE type='doctor' AND status NOT IN ('pending','running') ORDER BY id DESC LIMIT 1").fetchone()
    checks = doctor_checks(finished)
    problems = [c for c in checks if c['state'] != 'ok']
    action = '' if offline else form('/jobs/doctor', csrf, '<button>상태 점검 실행</button>', variant='primary')
    body = ('<div class="heading"><div><h1>상태</h1><p class="muted">PR을 수집하고 분석할 준비가 되었는지, 최근 작업이 어떻게 끝났는지 확인합니다.</p></div>'
            + action + '</div>')
    if offline:
        body += '<p>오프라인 모드에서는 gh, Codex를 실행하는 상태 점검을 하지 않습니다.</p>'
    body += '<section class="status-section status-health"><div class="section-heading"><h2>점검 결과</h2><p class="muted">마지막으로 완료한 점검 결과입니다. 현재 상태가 달라졌다면 다시 점검하세요.</p>'
    if finished:
        body += ('<p class="meta"><span class="badge ' + ('high' if problems else 'ok') + '">' + (f'확인 필요 {len(problems)}개' if problems else '모두 정상')
                 + '</span><span>점검 ' + esc(human_time(finished['finished_at'])) + '</span></p>')
    body += '</div>'
    if latest_doctor and latest_doctor['status'] in ('pending', 'running'):
        body += job_box(latest_doctor)
    if not finished:
        body += '<p class="empty">아직 상태 점검을 하지 않았습니다. 위의 <strong>상태 점검 실행</strong>으로 gh 인증, Codex 준비, 로컬 권한을 확인하세요.</p>'
    elif not checks:
        body += job_box(finished)
    linked = False
    for check in problems:
        hint = recovery_for(check['key'])
        fix = ''
        if check['key'] == 'probe' and '선행 검사 실패' not in check['detail']:
            fix = recovery_actions(csrf, offline, 'probe')
        elif check['key'] == 'codex-home':
            fix = recovery_actions(csrf, offline, 'home')
        elif check['key'] == 'model':
            fix = '<a class="btn secondary sm" href="/settings">모델 설정</a>'
        elif check['key'] in CODEX_CHECKS and not linked:
            fix, linked = '<a class="btn secondary sm" href="#codex-setup">Codex 분석 준비 열기</a>', True
        body += (f'<div class="check-row"><span class="badge {"high" if check["state"] == "bad" else "mid"}">' + ('실패' if check['state'] == 'bad' else '경고') + '</span>'
                 f'<div class="check-main"><strong>{esc(check["title"])}</strong><p>{esc(check["detail"])}</p>'
                 + (f'<p class="check-hint">{esc(hint)}</p>' if hint else '') + '</div>' + fix + '</div>')
    healthy = [c for c in checks if c['state'] == 'ok']
    if healthy:
        body += (f'<details class="check-ok"><summary>정상 {len(healthy)}개</summary>' + ''.join(
            f'<div class="check-row"><span class="badge ok">정상</span><div class="check-main"><strong>{esc(c["title"])}</strong><p>{esc(c["detail"])}</p></div></div>'
            for c in healthy) + '</details>')
    if finished and checks:
        body += '<details class="check-raw"><summary>점검 원문과 작업 정보</summary>' + job_box(finished, poll=False) + '</details>'
    body += '</section>'
    body += codex_setup(csrf, offline, conn, any(c['key'] in CODEX_CHECKS for c in problems))
    # 진행 중인 작업을 먼저, 이어서 최근 작업을 한 목록으로 보여준다.
    active_rows = conn.execute("SELECT * FROM jobs WHERE status IN ('pending','running') ORDER BY id").fetchall()
    seen = {row['id'] for row in active_rows}
    rows = list(active_rows) + [row for row in jobs.recent(conn) if row['id'] not in seen]
    collect = '' if offline else form('/jobs/collect', csrf, '<button>지금 수집</button>')
    body += ('<section class="status-section"><div class="section-heading job-heading"><div><h2>작업 기록</h2>'
             '<p>수집, 분석, 저장소 조회, 상태 점검 작업입니다. 진행 중인 작업이 위에 옵니다. 시각은 한국 시간(KST)입니다.</p></div>' + collect + '</div>')
    body += '<div class="list job-history">' + (''.join(job_record(row, csrf, offline) for row in rows)
                                    or '<p class="muted">작업 기록이 없습니다.</p>') + '</div></section>'
    body += '<details class="status-recovery"><summary>복구 안내</summary><ul>' + ''.join(f'<li><strong>{esc(name)}</strong>: {esc(text)}</li>' for name, text in RECOVERY) + '</ul></details>'
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
        d = fields(data, (), ('action', 'confirm'))
        action = d.get('action', 'check')
        if action in ('probe', 'reset') and d.get('confirm') != 'yes':
            raise Rejected('격리 검증 또는 전용 홈 재설정 확인이 필요합니다', 400)
        labels = {'check': '로컬 환경', 'init': '분석 준비 초기화', 'reset': '전용 홈 재설정', 'probe': '격리 검증'}
        if action not in labels:
            raise Rejected('지원하지 않는 상태 점검 작업입니다', 400)
        row, created = jobs.enqueue(conn, 'doctor', 0, labels[action], params={'action': action})
        if not created:
            existing = json.loads(row['params'] or '{}').get('action', 'check')
            if existing != action:
                raise Rejected('다른 상태 점검 또는 분석 준비 작업이 진행 중입니다. 완료 후 다시 요청하세요.', 409)
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
