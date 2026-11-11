"""저장된 UTC 시각을 화면용 현지 시각 문장으로 바꾼다."""
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))


def parse(value):
    """Return an aware datetime, or None when the stored value is not a timestamp."""
    if not value or not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value.strip().replace('Z', '+00:00'))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def human_time(value, now=None):
    """'방금', 'N분 전', '오늘 15:09', '어제 15:09', '11월 3일 15:09', '2025년 11월 3일'.

    SQLite의 CURRENT_TIMESTAMP처럼 시간대가 없는 값은 UTC로 본다. 시각으로 읽을 수 없는 값은 그대로 돌려준다.
    """
    moment = parse(value)
    if moment is None:
        return value or ''
    now = (now or datetime.now(timezone.utc)).astimezone()
    local = moment.astimezone(now.tzinfo)
    seconds = (now - local).total_seconds()
    if 0 <= seconds < 60:
        return '방금'
    if 0 <= seconds < 3600:
        return f'{int(seconds // 60)}분 전'
    clock = local.strftime('%H:%M')
    days = (now.date() - local.date()).days
    if days == 0:
        return '오늘 ' + clock
    if days == 1:
        return '어제 ' + clock
    if local.year == now.year:
        return f'{local.month}월 {local.day}일 {clock}'
    return f'{local.year}년 {local.month}월 {local.day}일'


def kst_clock(value, now=None):
    """'오늘 21:37 KST', '어제 23:20 KST', '11월 11일 21:40 KST', '2025년 11월 3일 21:40 KST'.

    Always Asia/Seoul wall-clock time, whatever timezone the server runs in.
    """
    moment = parse(value)
    if moment is None:
        return value or ''
    now = (now or datetime.now(timezone.utc)).astimezone(KST)
    local = moment.astimezone(KST)
    clock = local.strftime('%H:%M') + ' KST'
    days = (now.date() - local.date()).days
    if days == 0:
        return '오늘 ' + clock
    if days == 1:
        return '어제 ' + clock
    if local.year == now.year:
        return f'{local.month}월 {local.day}일 {clock}'
    return f'{local.year}년 {local.month}월 {local.day}일 {clock}'


def duration(start, end=None, now=None):
    """'42초', '1분', '2분 4초', '1시간 5분'. An open interval runs until now."""
    begin = parse(start)
    finish = parse(end) if end else (now or datetime.now(timezone.utc))
    if begin is None or finish is None:
        return ''
    seconds = max(0, int((finish - begin).total_seconds()))
    hours, minutes, seconds = seconds // 3600, seconds % 3600 // 60, seconds % 60
    if hours:
        return f'{hours}시간' + (f' {minutes}분' if minutes else '')
    if minutes:
        return f'{minutes}분' + (f' {seconds}초' if seconds else '')
    return f'{seconds}초'
