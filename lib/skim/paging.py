"""웹 목록의 페이지 나누기. 필터 쿼리를 유지한 이전/다음 링크를 만든다."""
from urllib.parse import urlencode

PAGE_SIZE = 20


def page_number(query, key='page'):
    raw = query.get(key, [''])[0]
    # 긴 숫자열은 int 변환 한도(4300자리)에 걸리므로 길이부터 제한한다.
    return int(raw) if raw.isascii() and raw.isdigit() and len(raw) <= 9 and int(raw) > 0 else 1


def paginate(items, query, path, size=PAGE_SIZE, key='page'):
    """(현재 페이지 항목, 이동 링크 HTML)을 돌려준다. 범위 밖 페이지는 마지막 페이지로 맞춘다."""
    from .web import esc
    pages = max(1, -(-len(items) // size))
    current = min(page_number(query, key), pages)
    shown = items[(current - 1) * size:current * size]
    if pages == 1:
        return shown, ''
    keep = [(key_name, value) for key_name, values in query.items() if key_name not in (key, 'csrf') for value in values]

    def link(number, label):
        return f'<a href="{esc(path + "?" + urlencode(keep + [(key, number)]))}">{label}</a>'

    nav = '<nav class="pager">'
    nav += link(current - 1, '이전') if current > 1 else '<span class="muted">이전</span>'
    nav += f'<span>{current} / {pages} 페이지 ({len(items)}개)</span>'
    nav += link(current + 1, '다음') if current < pages else '<span class="muted">다음</span>'
    return shown, nav + '</nav>'
