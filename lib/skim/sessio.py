"""Sessio JSONL 인덱스를 읽기 전용으로 조회한다."""
import json
import re
from urllib.parse import quote, urlsplit

from .collect import GitHub, CollectionError


def valid_base_url(value):
    # 사용자 정보·토큰·외부 호스트·임의 경로를 링크에 섞지 않는다.
    if not isinstance(value, str) or not re.fullmatch(r'http://(?:127\.0\.0\.1|localhost):[0-9]+/', value):
        return False
    try:
        return 1 <= urlsplit(value).port <= 65535
    except ValueError:
        return False


def session_link(config, pr):
    """후보가 하나이고 현재 gh 사용자 본인의 PR일 때만 (ID, URL)을 반환한다."""
    if not pr.get('author') or not pr.get('head_branch') or not config.sessio_index_path:
        return None
    if not valid_base_url(config.sessio_web_url):
        return None
    matches = []
    try:
        with config.sessio_index_path.open(encoding='utf-8') as stream:
            for line in stream:
                if not line.strip():
                    continue
                row = json.loads(line)
                if not isinstance(row, dict):
                    return None
                # Sessio의 repo는 remote_name()으로 만든 저장소 이름이다.
                repo = row.get('repo')
                if isinstance(repo, str) and repo.lower() in (pr['name'].lower(), f"{pr['owner']}/{pr['name']}".lower()) and row.get('branch') == pr['head_branch']:
                    matches.append(row)
                    if len(matches) > 1:
                        return None
    except (OSError, UnicodeError, ValueError):
        # 손상된 인덱스에서 모호한 후보를 임의로 선택하지 않는다.
        return None
    if len(matches) != 1:
        return None
    row = matches[0]
    sid, harness = row.get('session_id'), row.get('harness')
    if harness not in ('codex', 'claude') or not isinstance(sid, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,199}', sid):
        return None
    try:
        login = GitHub().get('user').get('login')
    except (CollectionError, OSError, ValueError, TypeError, AttributeError):
        return None
    if not isinstance(login, str) or not login or login.lower() != pr['author'].lower():
        return None
    return sid, config.sessio_web_url + '#/s/' + quote(harness + ':' + sid, safe='')
