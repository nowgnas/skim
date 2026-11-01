"""검증된 GitHub 호스트와 자격 증명 상태 (원문 출력은 노출하지 않는다)."""
import re
import subprocess


def hostname(value):
    value = value.lower()
    if len(value) > 253 or not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*', value, flags=re.ASCII):
        raise ValueError('호스트 이름 형식을 확인하세요')
    return value


def registered(conn, value):
    value = hostname(value)
    if not conn.execute('SELECT 1 FROM github_hosts WHERE host=?', (value,)).fetchone():
        raise ValueError('등록되지 않은 호스트입니다')
    return value


def authenticated(host, runner=None):
    try:
        return (runner or subprocess.run)(['gh', 'auth', 'status', '--hostname', hostname(host)],
                                         capture_output=True, text=True, timeout=15).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False
