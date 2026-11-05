"""만료된 로컬 PR 데이터를 제한된 배치로 정리한다."""
import fcntl
import json
import os
import re
import sqlite3
import stat
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from .collect import Collector, GitHub, private_subdirectory
from .db import connect

BATCH_SIZE = 20


def usage(config):
    """심볼릭 링크를 따라가지 않고 Skim 데이터의 대략적인 파일 크기를 센다."""
    def size(path):
        try:
            info = path.lstat()
            return info.st_size if stat.S_ISREG(info.st_mode) else 0
        except OSError:
            return 0
    def tree(path):
        if path.is_symlink() or not path.is_dir():
            return 0
        return sum(size(entry) for entry in path.rglob('*'))
    data = config.data_dir
    return {'db': sum(size(data / ('skim.db' + suffix)) for suffix in ('', '-wal', '-shm')),
            'diff': tree(data / 'cache' / 'diffs'),
            'backups': tree(data / 'update-backups') + sum(size(path) for path in data.glob('skim.db.v*.bak')),
            'logs': tree(data / 'logs')}


def backups(config):
    result = []
    for directory, pattern in (('', r'skim\.db\.v[0-9]+\.[0-9]+\.bak'),
                               ('update-backups', r'[0-9a-f]{32}\.sqlite')):
        parts = (directory,) if directory else ()
        try:
            with private_subdirectory(config.data_dir, *parts) as fd:
                for name in os.listdir(fd):
                    if not re.fullmatch(pattern, name):
                        continue
                    info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                    if stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and info.st_nlink == 1:
                        result.append((directory + '/' + name if directory else name, info.st_size))
        except FileNotFoundError:
            continue
    return sorted(result)


def remove_backup(config, name):
    from .maintenance import guard
    with guard(config.data_dir):
        available = {item[0] for item in backups(config)}
        if name not in available:
            raise ValueError('삭제 가능한 백업을 찾을 수 없습니다')
        parent, _, filename = name.rpartition('/')
        with private_subdirectory(config.data_dir, *((parent,) if parent else ())) as fd:
            info = os.stat(filename, dir_fd=fd, follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
                raise ValueError('백업 파일의 소유자와 종류를 확인하세요')
            os.unlink(filename, dir_fd=fd)


def policy(config):
    settings = json.loads((config.config_dir / 'config.json').read_text())
    if not isinstance(settings, dict):
        raise ValueError('설정은 JSON 객체여야 합니다')
    days = settings.get('retention_days', config.retention_days)
    hours = settings.get('cleanup_interval_hours', config.cleanup_interval_hours)
    enabled = settings.get('retention_enabled', config.retention_enabled)
    if type(days) is not int or not 1 <= days <= 3650:
        raise ValueError('보관 기간은 1~3650일이어야 합니다')
    if type(hours) is not int or not 1 <= hours <= 720:
        raise ValueError('정리 주기는 1~720시간이어야 합니다')
    if type(enabled) is not bool:
        raise ValueError('정리 설정을 확인하세요')
    return days, hours, enabled


def auxiliary_policy(config):
    settings = json.loads((config.config_dir / 'config.json').read_text())
    jobs = settings.get('job_retention_days', config.job_retention_days)
    logs = settings.get('log_retention_days', config.log_retention_days)
    if any(type(value) is not int or not 1 <= value <= 3650 for value in (jobs, logs)):
        raise ValueError('작업과 로그 보관 기간은 1~3650일이어야 합니다')
    return jobs, logs


def preview(config, conn, days, now=None):
    now = time.time() if now is None else now
    rows = conn.execute("SELECT id,closed_at FROM pull_requests WHERE state IN ('closed','merged')").fetchall()
    ids = {row['id'] for row in rows if row['closed_at'] is None or
           (timestamp(row['closed_at']) is not None and timestamp(row['closed_at']) <= now - days * 86400)}
    paths = {}
    for row in conn.execute('SELECT pr_id,diff_path FROM snapshots WHERE diff_path IS NOT NULL'):
        paths.setdefault(row['diff_path'], set()).add(row['pr_id'])
    total = 0
    cache = config.data_dir / 'cache' / 'diffs'
    try:
        with private_subdirectory(config.data_dir, 'cache', 'diffs') as fd:
            for path, owners in paths.items():
                item = Path(path)
                if not owners or not owners <= ids or item.parent != cache or not re.fullmatch(r'[0-9a-f]{64}\.diff', item.name):
                    continue
                info = os.stat(item.name, dir_fd=fd, follow_symlinks=False)
                if stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and info.st_nlink == 1:
                    total += info.st_size
    except (OSError, ValueError):
        return len(ids), None
    return len(ids), total


def timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return result.timestamp() if result.utcoffset() is not None else None
    except (ValueError, OverflowError, OSError):
        return None


def cleanup(config, conn, gh=None, now=None, cursor=0):
    """GitHub 재확인에 실패하거나 분석 작업이 있으면 해당 PR을 보존한다."""
    days, _, enabled = policy(config)
    job_days, log_days = auxiliary_policy(config)
    if not enabled:
        return {'removed': 0, 'deferred': 0}
    now = time.time() if now is None else now
    gh = gh or GitHub()
    candidates = conn.execute(
        "SELECT p.id,p.number,p.closed_at,p.state,r.host,r.owner,r.name "
        "FROM pull_requests p JOIN repositories r ON r.id=p.repo_id "
        "WHERE p.state IN ('closed','merged') ORDER BY p.id").fetchall()
    eligible = [row for row in candidates if row['closed_at'] is None or
                (timestamp(row['closed_at']) is not None and timestamp(row['closed_at']) <= now - days * 86400)]
    confirmed = []
    deferred = 0
    eligible.sort(key=lambda row: (row['id'] <= cursor, row['id']))
    batch = eligible[:BATCH_SIZE]
    for row in batch:
        try:
            gh.host = row['host']
            response = gh.get(f"repos/{row['owner']}/{row['name']}/pulls/{row['number']}")
            if response.get('state') != 'closed':
                deferred += 1
                continue
            state = 'merged' if response.get('merged_at') else 'closed'
            closed = response.get('merged_at') or response.get('closed_at')
            closed_time = timestamp(closed)
            if state not in ('closed', 'merged') or closed_time is None:
                deferred += 1
                continue
            confirmed.append((dict(row), state, closed, closed_time))
        except Exception:
            deferred += 1
    lock_path = config.data_dir / 'analysis.lock'
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {'removed': 0, 'deferred': len(eligible), 'busy': True, 'cursor': cursor, 'processed': 0}
        collector = Collector(conn, config)
        removed = 0
        with collector.lock():
            try:
                with private_subdirectory(config.data_dir, 'cache', 'diffs'):
                    pass
            except FileNotFoundError:
                pass
            with conn:
                conn.execute('BEGIN IMMEDIATE')
                for row, state, closed, closed_time in confirmed:
                    current = conn.execute('SELECT state,closed_at FROM pull_requests WHERE id=? AND repo_id=(SELECT id FROM repositories WHERE host=? AND owner=? AND name=?) AND number=?',
                                           (row['id'], row['host'], row['owner'], row['name'], row['number'])).fetchone()
                    if not current or current['state'] not in ('closed', 'merged') or current['closed_at'] != row['closed_at']:
                        deferred += 1
                        continue
                    conn.execute('UPDATE pull_requests SET state=?,closed_at=? WHERE id=?', (state, closed, row['id']))
                    if closed_time > now - days * 86400:
                        continue
                    active = conn.execute("SELECT 1 FROM jobs WHERE type='analyze' AND target_id=? AND status IN ('pending','running')", (row['id'],)).fetchone()
                    if active:
                        deferred += 1
                        continue
                    conn.execute("UPDATE jobs SET target_id=-id WHERE type='analyze' AND target_id=?", (row['id'],))
                    conn.execute('DELETE FROM pull_requests WHERE id=?', (row['id'],))
                    removed += 1
                conn.execute("DELETE FROM jobs WHERE status IN ('completed','failed','blocked','deferred','cancelled') AND finished_at IS NOT NULL AND julianday(finished_at) <= julianday(?, 'unixepoch', ?)", (now, f'-{job_days} days'))
            collector._clean_cache()
            try:
                with private_subdirectory(config.data_dir, 'logs') as directory:
                    info = os.stat('analysis.log', dir_fd=directory, follow_symlinks=False)
                    if (stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and info.st_nlink == 1
                            and now - info.st_mtime >= log_days * 86400):
                        os.unlink('analysis.log', dir_fd=directory)
            except FileNotFoundError:
                pass
        remaining = max(0, len(eligible) - BATCH_SIZE)
        return {'removed': removed, 'deferred': deferred + remaining, 'remaining': remaining,
                'processed': len(batch), 'busy': False, 'cursor': batch[-1]['id'] if batch else 0}
    finally:
        os.close(fd)


def worker(config, stop):
    """웹 서버가 켜져 있는 동안 저장된 마지막 점검 시각을 기준으로 실행한다."""
    while not stop.is_set():
        try:
            with closing(connect(config)) as conn:
                _, hours, enabled = policy(config)
                row = conn.execute('SELECT last_run,last_result,requested,cursor FROM cleanup_state WHERE id=1').fetchone()
                now = time.time()
                if enabled and (not row or row['requested'] or row['last_run'] is None or now < row['last_run'] or now - row['last_run'] >= hours * 3600):
                    result = cleanup(config, conn, now=now, cursor=row['cursor'] if row else 0)
                    previous = json.loads(row['last_result']) if row and row['requested'] and row['last_result'] else {}
                    if not result.get('busy') and previous.get('remaining', 0):
                        result['remaining'] = max(0, min(result.get('remaining', 0), previous['remaining'] - result.get('processed', 0)))
                    with conn:
                        conn.execute('INSERT INTO cleanup_state(id,last_run,last_result,requested,cursor) VALUES(1,?,?,?,?) ON CONFLICT(id) DO UPDATE SET last_run=excluded.last_run,last_result=excluded.last_result,requested=excluded.requested,cursor=excluded.cursor',
                                     (now, json.dumps(result), int(bool(result.get('busy') or result.get('remaining'))), result.get('cursor', 0)))
        except (OSError, ValueError, sqlite3.Error):
            pass
        stop.wait(60)
