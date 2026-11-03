"""모델 프로세스가 끝나기 전 부모가 종료돼도 분석 잠금과 실행 표식을 보유한다."""
import os
import select
from pathlib import Path
import signal
import subprocess
import sys
import time


def wait_for_exit(child, timeout):
    """종료된 자식의 PID를 회수하지 않고 종료 여부를 확인한다."""
    if hasattr(os, 'waitid'):
        deadline = time.monotonic() + timeout
        while os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is None:
            if time.monotonic() >= deadline:
                return False, False
            time.sleep(0.05)
        return True, False
    # macOS의 일부 Python 3.11 빌드에는 os.waitid가 없다.
    if not hasattr(select, 'kqueue'):
        raise OSError('프로세스 종료를 안전하게 감시할 수 없습니다.')
    watcher = select.kqueue()
    try:
        event = select.kevent(child.pid, filter=select.KQ_FILTER_PROC,
                              flags=select.KQ_EV_ADD | select.KQ_EV_ONESHOT,
                              fflags=select.KQ_NOTE_EXIT)
        try:
            return bool(watcher.control([event], 1, timeout)), False
        except ProcessLookupError:
            # 감시 등록 전 종료된 경우에는 회수 후 그룹이 없을 때만 표식을 지운다.
            child.wait(timeout=0)
            return True, True
    finally:
        watcher.close()


def main():
    marker = Path(sys.argv[1])
    timeout = float(sys.argv[2])
    command = sys.argv[3:]
    try:
        fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    except OSError:
        return 125
    os.close(fd)
    child = None
    try:
        child = subprocess.Popen(command, start_new_session=True)
        exited, reaped = wait_for_exit(child, timeout)
        timed_out = not exited
        if not reaped:
            # PID를 회수하기 전에 남은 같은 그룹을 종료해 PID 재사용 경합을 막는다.
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except PermissionError:
                # macOS는 종료된 그룹 리더에 대해 EPERM을 반환할 수 있다.
                if timed_out:
                    raise
        code = child.wait()
        if timed_out:
            code = 124
        deadline = time.monotonic() + 5
        while True:
            try:
                os.killpg(child.pid, 0)
            except ProcessLookupError:
                break
            if time.monotonic() >= deadline:
                return 125
            time.sleep(0.05)
        marker.unlink()
        return code if code >= 0 else 128 - code
    except OSError:
        if child is None:
            marker.unlink(missing_ok=True)
        return 125


if __name__ == '__main__':
    raise SystemExit(main())
