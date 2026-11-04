"""PATH의 가짜 도구와 격리된 터미널만 사용한다."""
import hashlib
import os
from pathlib import Path
import pty
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import zipfile


SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/install.sh'


class InstallScriptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.bin = self.home / 'bin'
        self.bin.mkdir()
        self.log = self.home / 'commands'
        self.env = {**os.environ, 'HOME': str(self.home), 'PATH': str(self.bin), 'SKIM_CODEX_BIN': 'codex'}
        self.tool('uname', 'echo Darwin')
        self.tool('python3', 'exec ' + shlex.quote(sys.executable) + ' "$@"')
        for name in ('mktemp', 'rm'):
            # macOS는 rm이 /bin, mktemp가 /usr/bin에 있어 경로를 고정하지 않는다.
            (self.bin / name).symlink_to(shutil.which(name, path='/usr/bin:/bin'))
        self.tool('codex', '''if [[ "$1" == --version ]]; then echo 'codex 0.165.0'; else
echo '--skip-git-repo-check --ephemeral --ignore-user-config --ignore-rules --sandbox --output-schema --output-last-message --disable'
fi''')
        self.installer = self.home / 'installer'
        with zipfile.ZipFile(self.installer, 'w') as archive:
            archive.writestr('__main__.py', "print('가짜 bootstrap 완료')")
        self.sums = self.home / 'sums'
        self.sums.write_text(hashlib.sha256(self.installer.read_bytes()).hexdigest() + '  install.sh\n')

    def tool(self, name, contents):
        path = self.bin / name
        path.write_text('#!/bin/bash\n' + contents + '\n')
        path.chmod(0o700)

    def gh(self):
        self.tool('gh', f'''echo "gh $*" >> '{self.log}'
if [[ "$1" == help ]]; then echo '  token:  인증 토큰'; exit 0; fi
if [[ "$1" == auth ]]; then exit 0; fi
if [[ "$1" == api ]]; then
  echo '[{{"tag_name":"v1.0.0"}},{{"tag_name":"v2.0.0","prerelease":true}},{{"tag_name":"v0.9.0"}}]'
  exit 0
fi
while [[ $# -gt 0 ]]; do
  if [[ "$1" == --dir ]]; then
    /bin/cp '{self.installer}' "$2/install.sh"
    /bin/cp '{self.sums}' "$2/SHA256SUMS"
    exit 0
  fi
  shift
done
exit 1''')

    @staticmethod
    def open_tty():
        """pty를 열고 답 'n'을 넣는다. master를 계속 비운다.

        macOS는 pty 출력(입력 에코)이 master에서 읽히기 전까지 자식이 종료 단계에서
        대기하고 SIGKILL로도 끝나지 않는다. 읽는 스레드로 이를 막는다.
        """
        master, slave = pty.openpty()

        def drain():
            while True:
                try:
                    if not os.read(master, 1024):
                        return
                except OSError:
                    return

        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
        os.write(master, b'n\n')
        return master, slave, reader

    @staticmethod
    def close_tty(master, slave, reader):
        os.close(slave)
        reader.join(timeout=5)
        os.close(master)

    def run_script(self, args=(), tty=False):
        if tty:
            master, slave, reader = self.open_tty()
            try:
                return subprocess.run(['/bin/bash', str(SCRIPT), *args], env=self.env, stdin=slave,
                                      capture_output=True, text=True, timeout=10, start_new_session=True)
            finally:
                self.close_tty(master, slave, reader)
        return subprocess.run(['/bin/bash', str(SCRIPT), *args], env=self.env, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=10, start_new_session=True)

    def test_missing_detection_without_brew(self):
        (self.bin / 'python3').unlink()
        (self.bin / 'codex').unlink()
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        for value in ('python3', 'gh', 'https://brew.sh/'):
            self.assertIn(value, result.stdout)
        self.assertFalse(self.log.exists())

    def test_decline_shows_manual_commands(self):
        self.tool('brew', f'echo brew >> "{self.log}"')
        result = self.run_script(tty=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('brew install gh', result.stdout)
        self.assertIn('수동', result.stdout)
        self.assertFalse(self.log.exists())

    def test_no_tty_never_installs(self):
        self.tool('brew', f'echo brew >> "{self.log}"')
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('터미널이 없어', result.stderr)
        self.assertFalse(self.log.exists())

    def test_pipe_input_uses_controlling_tty(self):
        import fcntl
        import termios
        self.tool('brew', f'echo brew >> "{self.log}"')
        master, slave, reader = self.open_tty()
        def attach_tty():
            os.setsid()
            fcntl.ioctl(slave, termios.TIOCSCTTY, 0)
        try:
            result = subprocess.run(['/bin/bash'], env=self.env,
                                    input=SCRIPT.read_text(), capture_output=True, text=True,
                                    pass_fds=(slave,), preexec_fn=attach_tty, timeout=10)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('수동', result.stdout)
            self.assertNotIn('터미널이 없어', result.stderr)
            self.assertFalse(self.log.exists())
        finally:
            self.close_tty(master, slave, reader)

    def test_yes_installs_without_tty(self):
        self.gh()
        ready = self.bin / 'gh-ready'
        (self.bin / 'gh').rename(ready)
        self.tool('brew', f'echo brew >> "{self.log}"\n/bin/cp "{ready}" "{self.bin / "gh"}"')
        result = self.run_script(['--yes'])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('가짜 bootstrap 완료', result.stdout)
        self.assertIn('brew', self.log.read_text())

    def test_sudo_displayed_before_decline(self):
        self.tool('uname', 'echo Linux')
        self.tool('apt-get', f'echo apt >> "{self.log}"')
        result = self.run_script(tty=True)
        self.assertIn('수동 설치', result.stdout)
        self.assertIn('sudo apt-get update', result.stdout)
        self.assertFalse(self.log.exists())

    def test_existing_tools_and_corrupt_bootstrap(self):
        self.gh()
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.installer.write_bytes(b'broken')
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('SHA256SUMS', result.stderr)

    def test_codex_official_install_command(self):
        self.gh()
        (self.bin / 'codex').unlink()
        self.tool('dnf', 'exit 99')
        self.tool('npm', 'exit 99')
        for system in ('Linux', 'Darwin'):
            with self.subTest(system=system):
                self.tool('uname', 'echo ' + system)
                result = self.run_script()
                self.assertIn('0.165.0', result.stderr)
                self.assertIn('developers.openai.com/codex/cli/', result.stderr)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('가짜 bootstrap 완료', result.stdout)

    def test_versioned_python_after_brew_install(self):
        self.gh()
        self.tool('python3', 'exit 1')
        ready = self.home / 'python-ready'
        ready.write_text('#!/bin/bash\nexec ' + shlex.quote(sys.executable) + ' "$@"\n')
        ready.chmod(0o700)
        self.tool('brew', f'/bin/cp "{ready}" "{self.bin / "python3.11"}"')
        result = self.run_script(['--yes'])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('가짜 bootstrap 완료', result.stdout)

    def test_linux_installs_same_bootstrap(self):
        self.gh()
        self.tool('uname', 'echo Linux')
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('가짜 bootstrap 완료', result.stdout)
        self.assertIn('release download v1.0.0 ', self.log.read_text())

    def test_auth_required_before_download(self):
        self.tool('gh', f'echo "$*" >> "{self.log}"\nif [[ "$1" == help ]]; then echo "  token:  인증 토큰"; exit 0; fi\nexit 1')
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('gh auth login', result.stderr)
        self.assertNotIn('download', self.log.read_text())

    def test_old_codex_warns_and_continues(self):
        self.gh()
        self.tool('codex', "echo 'codex 0.164.0'")
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('0.165.0', result.stderr)
        self.assertIn('download', self.log.read_text())

    def test_truncated_pipe_never_runs_body(self):
        self.tool('brew', f'echo unexpected >> "{self.log}"')
        source = SCRIPT.read_text()
        for marker in ('os="$(uname -s)"', 'for tool in "${missing[@]}";', 'gh release download'):
            result = subprocess.run(['/bin/bash'], input=source[:source.index(marker)],
                                    env=self.env, capture_output=True, text=True, timeout=10)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(self.log.exists())

    def test_old_gh_and_missing_isolation_flags(self):
        self.tool('gh', 'if [[ "$1" == help ]]; then echo "  login  로그인"; fi\nexit 0')
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('auth token', result.stderr)
        self.gh()
        self.tool('codex', "echo 'codex 0.165.0'")
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('격리 플래그', result.stderr)
        self.assertIn('download', self.log.read_text())

    def test_linux_old_python_manual_guide_without_sudo(self):
        self.tool('uname', 'echo Linux')
        self.tool('python3', 'exit 1')
        self.tool('sudo', f'echo sudo >> "{self.log}"')
        result = self.run_script(['--yes'])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('python3.11', result.stdout)
        self.assertIn('SKIM_PYTHON', result.stdout)
        self.assertFalse(self.log.exists())

    def test_release_api_argument_does_not_expand_glob(self):
        self.gh()
        with tempfile.TemporaryDirectory() as directory:
            collision = Path(directory) / 'repos/nowgnas/skim/releasesXper_page=100'
            collision.parent.mkdir(parents=True)
            collision.touch()
            result = subprocess.run(['/bin/bash', str(SCRIPT)], env=self.env,
                                    cwd=directory, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('releases?per_page=100', self.log.read_text())
