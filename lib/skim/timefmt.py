"""저장된 UTC 시각을 화면용 현지 시각 문장으로 바꾼다."""
from datetime import datetime, timezone


def human_time(value, now=None):
    """'방금', 'N분 전', '오늘 15:09', '어제 15:09', '11월 3일 15:09', '2025년 11월 3일'.

    SQLite의 CURRENT_TIMESTAMP처럼 시간대가 없는 값은 UTC로 본다. 시각으로 읽을 수 없는 값은 그대로 돌려준다.
    """
    if not value or not isinstance(value, str):
        return value or ''
    try:
        moment = datetime.fromisoformat(value.strip().replace('Z', '+00:00'))
    except ValueError:
        return value
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
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
