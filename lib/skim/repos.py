"""CLI와 웹이 공유하는 로컬 추적 변경. GitHub 쓰기 작업은 하지 않는다."""
import re


def web_repository(value):
    if not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}', value) or '..' in value:
        raise ValueError('owner/repo 형식을 확인하세요 (.. 금지)')
    owner, name = value.split('/')
    if name == '.':
        raise ValueError('잘못된 저장소 이름입니다')
    return owner, name


def add_repository(conn, parts, source):
    conn.execute('INSERT INTO repositories(owner,name,source,tracked) VALUES(?,?,?,1) '
                 'ON CONFLICT(owner,name) DO UPDATE SET source=excluded.source,enabled=1,tracked=1',
                 (*parts, source))


def remove_repository(conn, parts):
    result = conn.execute('UPDATE repositories SET tracked=0 WHERE owner=? AND name=? AND tracked=1', parts)
    if not result.rowcount:
        raise ValueError('등록되지 않은 저장소입니다')
    # 리뷰 요청 출처·스냅샷·분석·분석 정책은 보존한다.
    conn.execute("DELETE FROM pr_sources WHERE source='repository' AND pr_id IN "
                 '(SELECT p.id FROM pull_requests p JOIN repositories r ON r.id=p.repo_id '
                 'WHERE r.owner=? AND r.name=?)', parts)
    conn.execute('DELETE FROM pull_requests WHERE repo_id IN '
                 '(SELECT id FROM repositories WHERE owner=? AND name=?) '
                 'AND NOT EXISTS (SELECT 1 FROM pr_sources WHERE pr_id=pull_requests.id)', parts)
