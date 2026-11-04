"""정식 릴리스 설치의 신뢰 경계를 임시 파일로 검증한다."""
import io
import hashlib
import json
import os
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, redirect_stdout, redirect_stderr
from types import SimpleNamespace
from unittest.mock import patch
import tarfile
import tempfile
from urllib.request import Request
from pathlib import Path
import unittest

from skim import maintenance, program_store, release_package, releases, update_backup


class UpdateCommandTests(unittest.TestCase):
    def test_no_release_does_not_touch_configuration(self):
        from skim import cli
        output = io.StringIO()
        with patch('skim.releases.fetch_latest_authenticated', return_value=None), \
                patch('skim.cli.Config.load', side_effect=AssertionError('설정 변경 금지')), redirect_stdout(output):
            self.assertEqual(cli.main(['update']), 0)
        self.assertIn('정식 릴리스가 없습니다', output.getvalue())

    def run_update(self, argv, tag):
        from skim import cli
        output, errors = io.StringIO(), io.StringIO()
        release = releases.parse_release(ReleaseTests().release(tag))
        with patch('skim.releases.fetch_latest_authenticated', return_value=release), \
                patch('skim.cli.Config.load', side_effect=AssertionError('설정 변경 금지')), \
                patch('skim.releases.download_private_asset', side_effect=AssertionError('다운로드 금지')), \
                redirect_stdout(output), redirect_stderr(errors):
            code = cli.main(argv)
        return code, output.getvalue(), errors.getvalue()

    def test_update_check_reports_newer_release(self):
        code, output, _ = self.run_update(['update', '--check'], 'v9.0.0')
        self.assertEqual(code, 0)
        self.assertIn('사용 가능한 정식 릴리스: v9.0.0', output)

    def test_update_same_version_is_current(self):
        for argv in (['update'], ['update', '--yes']):
            code, output, _ = self.run_update(argv, 'v1.0.0')
            self.assertEqual(code, 0)
            self.assertIn('최신 버전입니다: v1.0.0', output)

    def test_ownership_development_checkout_only_guides(self):
        code, output, errors = self.run_update(['update', '--yes'], 'v9.0.0')
        self.assertEqual(code, 1)
        self.assertIn('v9.0.0', output)
        self.assertIn('정식 설치본이 아니어서', errors)
        for text in (output, errors):
            self.assertNotIn('·', text)
            self.assertNotIn('—', text)

    def test_auth_failure_is_reported(self):
        from skim import cli
        errors = io.StringIO()
        with patch('skim.releases.fetch_latest_authenticated', side_effect=releases.ReleaseUnavailable('인증 확인')), \
                redirect_stderr(errors):
            self.assertEqual(cli.main(['update', '--check']), 1)
        self.assertIn('인증 확인', errors.getvalue())


class ReleaseTests(unittest.TestCase):
    def release(self, tag='v1.2.3'):
        names = ('skim-macos.tar.gz', 'SHA256SUMS', 'manifest.json', 'install.sh')
        return {'tag_name': tag, 'draft': False, 'prerelease': False,
                'assets': [{'id': i, 'name': n, 'browser_download_url': 'https://github.com/nowgnas/skim/releases/download/' + tag + '/' + n} for i, n in enumerate(names, 1)]}

    def archive(self, name, data=b'content', kind=tarfile.REGTYPE):
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode='w:gz') as tar:
            info = tarfile.TarInfo(name)
            info.type = kind
            info.size = len(data) if kind == tarfile.REGTYPE else 0
            tar.addfile(info, io.BytesIO(data) if info.size else None)
        return output.getvalue()

    def package_archive(self, manifest):
        output = io.BytesIO()
        files = {'skim/manifest.json': manifest, 'skim/bin/skim': b'#!/bin/sh\n',
                 'skim/lib/python.sh': b'# helper\n', 'skim/lib/skim/__init__.py': b'',
                 'skim/lib/skim/cli.py': b'', 'skim/lib/skim/web.py': b''}
        with tarfile.open(fileobj=output, mode='w:gz') as tar:
            for name, data in files.items():
                info = tarfile.TarInfo(name)
                info.mode = 0o755 if name == 'skim/bin/skim' else 0o644
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        return output.getvalue()

    def test_happy_path_release_metadata(self):
        selected = releases.parse_release(self.release())
        self.assertEqual(selected.version, (1, 2, 3))
        self.assertEqual(selected.tag, 'v1.2.3')

    def test_invalid_release_metadata(self):
        for update in ({'draft': True}, {'prerelease': True}, {'tag_name': 'v1.2.3-rc1'}, {'assets': []}):
            data = self.release()
            data.update(update)
            with self.subTest(update=update), self.assertRaises(ValueError):
                releases.parse_release(data)

    def test_invalid_release_archive(self):
        for name, kind in (('../outside', tarfile.REGTYPE), ('/absolute', tarfile.REGTYPE), ('link', tarfile.SYMTYPE)):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory, self.assertRaises(ValueError):
                releases.extract_archive(self.archive(name, kind=kind), Path(directory))

    def test_invalid_release_pax_decompression_limit(self):
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode='w:gz', format=tarfile.PAX_FORMAT) as archive:
            info = tarfile.TarInfo('skim/file')
            info.pax_headers = {'comment': 'x' * 131072}
            archive.addfile(info)
        with patch.object(releases, 'MAX_EXPANDED_BYTES', 32768):
            with tempfile.TemporaryDirectory() as directory, self.assertRaises(ValueError):
                releases.extract_archive(output.getvalue(), Path(directory))
            with tempfile.TemporaryDirectory() as directory:
                releases.extract_archive(self.archive('skim/file'), Path(directory))

    def test_happy_path_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            releases.extract_archive(self.archive('skim/bin/skim'), Path(directory))
            self.assertEqual((Path(directory) / 'skim/bin/skim').read_bytes(), b'content')

    def test_happy_path_checksums(self):
        metadata = self.release()
        selected = releases.parse_release(metadata)
        payloads = {'skim-macos.tar.gz': b'archive', 'manifest.json': json.dumps({'version': 'v1.2.3', 'archive': 'skim-macos.tar.gz'}).encode(), 'install.sh': b'install'}
        sums = ''.join(hashlib.sha256(value).hexdigest() + '  ' + name + '\n' for name, value in payloads.items()).encode()
        releases.verify_payload(selected, sums, payloads)

    def test_happy_path_staging(self):
        selected = releases.parse_release(self.release())
        manifest = json.dumps({'version': selected.tag, 'archive': 'skim-macos.tar.gz'}).encode()
        archive = self.package_archive(manifest)
        payloads = {'skim-macos.tar.gz': archive, 'manifest.json': manifest, 'install.sh': b'install'}
        sums = ''.join(hashlib.sha256(value).hexdigest() + '  ' + name + '\n' for name, value in payloads.items()).encode()
        payloads['SHA256SUMS'] = sums
        with tempfile.TemporaryDirectory() as directory:
            releases.stage_release(selected, Path(directory), lambda release, name, limit: payloads[name])
            self.assertEqual((Path(directory) / 'skim/manifest.json').read_bytes(), manifest)

    def test_invalid_release_missing_program(self):
        selected = releases.parse_release(self.release())
        manifest = json.dumps({'version': selected.tag, 'archive': 'skim-macos.tar.gz'}).encode()
        payloads = {'skim-macos.tar.gz': self.archive('skim/manifest.json', manifest),
                    'manifest.json': manifest, 'install.sh': b'install'}
        payloads['SHA256SUMS'] = ''.join(hashlib.sha256(value).hexdigest() + '  ' + name + '\n' for name, value in payloads.items()).encode()
        with tempfile.TemporaryDirectory() as directory, self.assertRaises(ValueError):
            releases.stage_release(selected, Path(directory), lambda release, name, limit: payloads[name])

    def test_invalid_release_checksums(self):
        selected = releases.parse_release(self.release())
        payloads = {'skim-macos.tar.gz': b'archive', 'manifest.json': b'{}', 'install.sh': b'install'}
        with self.assertRaises(ValueError):
            releases.verify_payload(selected, b'0' * 64 + b'  skim-macos.tar.gz\n', payloads)

    def test_invalid_release_redirect(self):
        for url in ('http://github.com/path', 'https://evil.example/path', 'https://github.com@evil.example/path'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                releases.check_download_url(url)

    def test_invalid_release_redirect_strips_credentials(self):
        original = Request('https://api.github.com/repos/nowgnas/skim/releases/assets/1', headers={'Authorization': 'Bearer private-token'})
        redirected = releases.SafeRedirect().redirect_request(original, None, 302, 'Found', {}, 'https://release-assets.githubusercontent.com/file')
        self.assertIsNone(redirected.get_header('Authorization'))

    def test_happy_path_private_asset(self):
        selected = releases.parse_release(self.release())
        class Response:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def read(self, limit):
                return b'data'
        class Opener:
            def open(self, req, timeout):
                self.assertions(req, timeout)
                return Response()
            def assertions(self, req, timeout):
                assert req.full_url == 'https://api.github.com/repos/nowgnas/skim/releases/assets/1'
                assert req.get_header('Authorization') == 'Bearer sample-token'
        runner = lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout='sample-token\n')
        self.assertEqual(releases.download_private_asset(selected, 'skim-macos.tar.gz', 10, runner, Opener()), b'data')

    def test_invalid_release_private_asset_error_hides_token(self):
        selected = releases.parse_release(self.release())
        def runner(*args, **kwargs):
            raise OSError('sample-secret')
        with self.assertRaises(releases.ReleaseUnavailable) as raised:
            releases.download_private_asset(selected, 'skim-macos.tar.gz', 10, runner)
        self.assertNotIn('sample-secret', str(raised.exception))

    def test_happy_path_latest_release(self):
        class FakeGitHub:
            def get(inner, endpoint):
                return [self.release()]
        version = releases.fetch_latest_authenticated(FakeGitHub())
        self.assertEqual(version.tag, 'v1.2.3')

    def test_release_gate_private_repository_no_release(self):
        class FakeGitHub:
            def get(self, endpoint):
                self_endpoint = 'repos/nowgnas/skim/releases?per_page=100&page=1'
                if endpoint != self_endpoint:
                    raise AssertionError(endpoint)
                return []
        self.assertIsNone(releases.fetch_latest_authenticated(FakeGitHub()))

    def test_invalid_release_private_repository_access(self):
        class FakeGitHub:
            def get(self, endpoint):
                raise OSError('token must not be shown')
        with self.assertRaises(releases.ReleaseUnavailable) as raised:
            releases.fetch_latest_authenticated(FakeGitHub())
        self.assertNotIn('token', str(raised.exception))

    def test_happy_path_version_comparison(self):
        self.assertEqual(releases.compare_versions(None, 'v1.2.3', None, 'a' * 64), 'install')
        self.assertEqual(releases.compare_versions('v1.2.2', 'v1.2.3', 'b' * 64, 'a' * 64), 'update')
        self.assertEqual(releases.compare_versions('v1.2.3', 'v1.2.3', 'a' * 64, 'a' * 64), 'current')

    def test_invalid_release_version_replacement(self):
        for current, latest, existing, new in (('v1.2.3', 'v1.2.3', 'a' * 64, 'b' * 64),
                                                ('v2.0.0', 'v1.9.9', 'a' * 64, 'b' * 64)):
            with self.subTest(current=current, latest=latest), self.assertRaises(ValueError):
                releases.compare_versions(current, latest, existing, new)


class JournalTests(unittest.TestCase):
    def test_recovery_committed_request_retry_does_not_reopen_maintenance(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = releases.UpdateJournal(Path(directory))
            record = journal.start('b' * 32, 'v1.2.3', 'a' * 64)
            for state in ('quiesced', 'backup', 'switched', 'ready', 'committed'):
                journal.advance(record['id'], state)
            again = journal.start('b' * 32, 'v1.2.3', 'a' * 64)
            self.assertEqual(again['id'], record['id'])
            self.assertEqual(again['state'], 'committed')
            with maintenance.guard(Path(directory)):
                pass

    installation = 'b' * 32

    def test_happy_path_journal(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = releases.UpdateJournal(Path(directory))
            record = journal.start(self.installation, 'v1.2.3', 'a' * 64, {'tag': 'v1.2.2', 'sha256': 'c' * 64})
            for state in ('quiesced', 'backup', 'switched', 'ready', 'committed'):
                record = journal.advance(record['id'], state)
            self.assertEqual(record['state'], 'committed')

    def test_concurrent_requests_share_same_update(self):
        with tempfile.TemporaryDirectory() as directory:
            first = releases.UpdateJournal(Path(directory))
            second = releases.UpdateJournal(Path(directory))
            self.assertEqual(first.start(self.installation, 'v1.2.3', 'a' * 64)['id'], second.start(self.installation, 'v1.2.3', 'a' * 64)['id'])
            with self.assertRaises(ValueError):
                second.start(self.installation, 'v1.2.4', 'b' * 64)

    def test_concurrent_journal_start(self):
        with tempfile.TemporaryDirectory() as directory:
            def start(_):
                return releases.UpdateJournal(Path(directory)).start(self.installation, 'v1.2.3', 'a' * 64)['id']
            with ThreadPoolExecutor(max_workers=8) as executor:
                identities = list(executor.map(start, range(16)))
            self.assertEqual(len(set(identities)), 1)

    def test_recovery_repeated_phase(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = releases.UpdateJournal(Path(directory))
            record = journal.start(self.installation, 'v1.2.3', 'a' * 64)
            first = journal.advance(record['id'], 'quiesced')
            self.assertEqual(first, journal.advance(record['id'], 'quiesced'))
            with self.assertRaises(ValueError):
                journal.advance(record['id'], 'switched')

    def test_readiness_blocks_writes_during_update(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            journal = releases.UpdateJournal(path)
            record = journal.start(self.installation, 'v1.2.3', 'a' * 64)
            with self.assertRaises(maintenance.UpdateInProgress):
                with maintenance.guard(path):
                    pass
            with maintenance.guard(path, exclusive=True, update_id=record['id']):
                pass

    def test_concurrent_guard_waits_for_existing_work(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            journal = releases.UpdateJournal(path)
            with maintenance.guard(path):
                record = journal.start(self.installation, 'v1.2.3', 'a' * 64)
                with self.assertRaises(maintenance.UpdateInProgress):
                    with maintenance.guard(path, exclusive=True, update_id=record['id']):
                        pass
            with maintenance.guard(path, exclusive=True, update_id=record['id']):
                pass
            with self.assertRaises(maintenance.UpdateInProgress):
                with maintenance.guard(path):
                    pass

    def test_invalid_release_update_id_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            releases.UpdateJournal(path).start(self.installation, 'v1.2.3', 'a' * 64)
            with self.assertRaises(maintenance.UpdateInProgress):
                with maintenance.guard(path, exclusive=True, update_id='0' * 32):
                    pass


class PackagingTests(unittest.TestCase):
    def test_release_gate_blocks_packaging(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            destination = root / 'release-output'
            with self.assertRaises(ValueError):
                release_package.build(root, destination, 'v1.2.3', release_gate=lambda root: False)
            self.assertFalse(destination.exists())


class ProgramStoreTests(unittest.TestCase):
    def test_recovery_cancel_before_switch_reopens_access(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            program, data = root / 'program', root / 'data'
            program.mkdir(mode=0o700)
            data.mkdir(mode=0o700)
            store = program_store.ProgramStore.initialize(program)
            journal = releases.UpdateJournal(data)
            record = journal.start(store.installation_id, 'v1.2.3', 'a' * 64)
            journal.advance(record['id'], 'quiesced')
            store.cancel(data, record['id'])
            self.assertEqual(journal.read()['state'], 'rolled_back')
            store.cancel(data, record['id'])
            with maintenance.guard(data):
                pass

    def test_recovery_cancel_does_not_open_barrier_after_switch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            program, data = root / 'program', root / 'data'
            program.mkdir(mode=0o700)
            data.mkdir(mode=0o700)
            store = program_store.ProgramStore.initialize(program)
            journal = releases.UpdateJournal(data)
            record = journal.start(store.installation_id, 'v1.2.3', 'a' * 64)
            (program / 'versions' / 'v1.2.3').mkdir(mode=0o700)
            (program / 'current').symlink_to('versions/v1.2.3')
            with self.assertRaises(ValueError):
                store.cancel(data, record['id'])
            self.assertEqual(journal.read()['state'], 'prepared')
            with self.assertRaises(maintenance.UpdateInProgress), maintenance.guard(data):
                pass

    def test_recovery_cancel_preserves_existing_database(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            program, data = root / 'program', root / 'data'
            program.mkdir(mode=0o700)
            data.mkdir(mode=0o700)
            store = program_store.ProgramStore.initialize(program)
            (program / 'versions' / 'v1.2.2').mkdir(mode=0o700)
            (program / 'current').symlink_to('versions/v1.2.2')
            with closing(sqlite3.connect(data / 'skim.db')) as conn, conn:
                conn.execute('CREATE TABLE item (value TEXT)')
                conn.execute("INSERT INTO item VALUES ('preserved')")
            journal = releases.UpdateJournal(data)
            record = journal.start(store.installation_id, 'v1.2.3', 'a' * 64,
                                   {'tag': 'v1.2.2', 'sha256': 'b' * 64})
            store.cancel(data, record['id'])
            self.assertEqual(store.current(), 'v1.2.2')
            with closing(sqlite3.connect(data / 'skim.db')) as conn:
                self.assertEqual(conn.execute('SELECT value FROM item').fetchone(), ('preserved',))

    def test_recovery_cancel_retries_after_running_work_finishes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            program, data = root / 'program', root / 'data'
            program.mkdir(mode=0o700)
            data.mkdir(mode=0o700)
            store = program_store.ProgramStore.initialize(program)
            with maintenance.guard(data):
                record = releases.UpdateJournal(data).start(store.installation_id, 'v1.2.3', 'a' * 64)
                with self.assertRaises(maintenance.UpdateInProgress):
                    store.cancel(data, record['id'])
            store.cancel(data, record['id'])
            self.assertEqual(releases.UpdateJournal(data).read()['state'], 'rolled_back')

    def test_ownership_rejects_foreign_or_unmanaged_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'foreign').write_text('preserve')
            with self.assertRaises(ValueError):
                program_store.ProgramStore.initialize(root)
            self.assertEqual((root / 'foreign').read_text(), 'preserve')

    def test_happy_path_store_identity_is_stable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'program'
            root.mkdir(mode=0o700)
            created = program_store.ProgramStore.initialize(root)
            self.assertEqual(created.installation_id, program_store.ProgramStore.open(root).installation_id)

    def test_invalid_release_current_link_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'program'
            root.mkdir(mode=0o700)
            store = program_store.ProgramStore.initialize(root)
            (root / 'current').symlink_to('/tmp/outside')
            with self.assertRaises(ValueError):
                store.current()

    def test_happy_path_stage_version(self):
        fixture = ReleaseTests()
        release = releases.parse_release(fixture.release())
        manifest = json.dumps({'version': release.tag, 'archive': 'skim-macos.tar.gz'}).encode()
        payloads = {'skim-macos.tar.gz': fixture.package_archive(manifest),
                    'manifest.json': manifest, 'install.sh': b'install'}
        payloads['SHA256SUMS'] = ''.join(hashlib.sha256(value).hexdigest() + '  ' + name + '\n' for name, value in payloads.items()).encode()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'program'
            root.mkdir(mode=0o700)
            store = program_store.ProgramStore.initialize(root)
            sha256 = store.stage(release, lambda selected, name, limit: payloads[name])
            self.assertEqual(sha256, hashlib.sha256(payloads['skim-macos.tar.gz']).hexdigest())
            self.assertTrue((root / 'versions' / release.tag / 'manifest.json').is_file())
            self.assertIsNone(store.current())
            self.assertEqual(store.stage(release, lambda selected, name, limit: payloads[name]), sha256)
            (root / 'versions' / release.tag / 'manifest.json').write_text('changed')
            with self.assertRaises(ValueError):
                store.stage(release, lambda selected, name, limit: payloads[name])

    def test_invalid_release_stage_enforces_recovery_tree_limit(self):
        fixture = ReleaseTests()
        release = releases.parse_release(fixture.release())
        manifest = json.dumps({'version': release.tag, 'archive': 'skim-macos.tar.gz'}).encode()
        payloads = {'skim-macos.tar.gz': fixture.package_archive(manifest), 'manifest.json': manifest, 'install.sh': b'install'}
        payloads['SHA256SUMS'] = ''.join(hashlib.sha256(value).hexdigest() + '  ' + name + '\n' for name, value in payloads.items()).encode()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'program'
            root.mkdir(mode=0o700)
            store = program_store.ProgramStore.initialize(root)
            with patch.object(program_store, 'MAX_FILES', 2), self.assertRaises(ValueError):
                store.stage(release, lambda selected, name, limit: payloads[name])
            self.assertFalse((root / 'versions' / release.tag).exists())

    def test_recovery_switch_is_idempotent(self):
        fixture = ReleaseTests()
        release = releases.parse_release(fixture.release())
        manifest = json.dumps({'version': release.tag, 'archive': 'skim-macos.tar.gz'}).encode()
        payloads = {'skim-macos.tar.gz': fixture.package_archive(manifest), 'manifest.json': manifest, 'install.sh': b'install'}
        payloads['SHA256SUMS'] = ''.join(hashlib.sha256(value).hexdigest() + '  ' + name + '\n' for name, value in payloads.items()).encode()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            program, data = root / 'program', root / 'data'
            program.mkdir(mode=0o700)
            data.mkdir(mode=0o700)
            store = program_store.ProgramStore.initialize(program)
            sha256 = store.stage(release, lambda selected, name, limit: payloads[name])
            journal = releases.UpdateJournal(data)
            record = journal.start(store.installation_id, release.tag, sha256)
            journal.advance(record['id'], 'quiesced')
            journal.advance(record['id'], 'backup')
            store.activate(data, record['id'])
            self.assertEqual(store.current(), release.tag)
            store.activate(data, record['id'])
            self.assertEqual(journal.read()['state'], 'switched')

    def test_concurrent_store_rejects_another_data_directory(self):
        fixture = ReleaseTests()
        release = releases.parse_release(fixture.release())
        manifest = json.dumps({'version': release.tag, 'archive': 'skim-macos.tar.gz'}).encode()
        payloads = {'skim-macos.tar.gz': fixture.package_archive(manifest), 'manifest.json': manifest, 'install.sh': b'install'}
        payloads['SHA256SUMS'] = ''.join(hashlib.sha256(value).hexdigest() + '  ' + name + '\n' for name, value in payloads.items()).encode()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            program, first, second = root / 'program', root / 'first', root / 'second'
            for path in (program, first, second):
                path.mkdir(mode=0o700)
            store = program_store.ProgramStore.initialize(program)
            sha256 = store.stage(release, lambda selected, name, limit: payloads[name])
            records = []
            for data in (first, second):
                journal = releases.UpdateJournal(data)
                record = journal.start(store.installation_id, release.tag, sha256)
                journal.advance(record['id'], 'quiesced')
                journal.advance(record['id'], 'backup')
                records.append(record)
            store.activate(first, records[0]['id'])
            with self.assertRaises(ValueError):
                store.activate(second, records[1]['id'])
            self.assertEqual(releases.UpdateJournal(second).read()['state'], 'backup')

    def test_recovery_rollback_version(self):
        fixture = ReleaseTests()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            program, data = root / 'program', root / 'data'
            program.mkdir(mode=0o700)
            data.mkdir(mode=0o700)
            store = program_store.ProgramStore.initialize(program)
            journal = releases.UpdateJournal(data)
            previous = None
            for tag in ('v1.2.2', 'v1.2.3'):
                release = releases.parse_release(fixture.release(tag))
                manifest = json.dumps({'version': tag, 'archive': 'skim-macos.tar.gz'}).encode()
                payloads = {'skim-macos.tar.gz': fixture.package_archive(manifest), 'manifest.json': manifest, 'install.sh': b'install'}
                payloads['SHA256SUMS'] = ''.join(hashlib.sha256(value).hexdigest() + '  ' + name + '\n' for name, value in payloads.items()).encode()
                sha256 = store.stage(release, lambda selected, name, limit: payloads[name])
                record = journal.start(store.installation_id, tag, sha256, previous)
                journal.advance(record['id'], 'quiesced')
                update_backup.backup_database(data, record['id'])
                store.activate(data, record['id'])
                if previous is None:
                    journal.advance(record['id'], 'ready')
                    journal.advance(record['id'], 'committed')
                previous = {'tag': tag, 'sha256': sha256}
            journal.begin_rollback(record['id'])
            update_backup.restore_database(data, record['id'])
            store.rollback(data, record['id'])
            self.assertEqual(store.current(), 'v1.2.2')
            self.assertEqual(journal.read()['state'], 'rolled_back')
            with maintenance.guard(data):
                pass


class BackupTests(unittest.TestCase):
    def test_recovery_backup_is_consistent_and_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            with closing(sqlite3.connect(data / 'skim.db')) as connection, connection:
                connection.execute('CREATE TABLE example(value TEXT)')
                connection.execute("INSERT INTO example VALUES('saved')")
            (data / 'skim.db').chmod(0o600)
            journal = releases.UpdateJournal(data)
            record = journal.start('b' * 32, 'v1.2.3', 'a' * 64)
            journal.advance(record['id'], 'quiesced')
            backup = update_backup.backup_database(data, record['id'])
            self.assertEqual(backup, update_backup.backup_database(data, record['id']))
            with closing(sqlite3.connect(backup)) as connection:
                self.assertEqual(connection.execute('SELECT value FROM example').fetchone()[0], 'saved')
            self.assertEqual(journal.read()['state'], 'backup')

    def test_recovery_interrupted_backup_link(self):
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            with closing(sqlite3.connect(data / 'skim.db')) as connection, connection:
                connection.execute('CREATE TABLE example(value TEXT)')
            (data / 'skim.db').chmod(0o600)
            journal = releases.UpdateJournal(data)
            record = journal.start('b' * 32, 'v1.2.3', 'a' * 64)
            journal.advance(record['id'], 'quiesced')
            backup = update_backup.backup_database(data, record['id'])
            leftover = backup.parent / '.backup-interrupted'
            os.link(backup, leftover)
            self.assertEqual(update_backup.backup_database(data, record['id']), backup)
            self.assertFalse(leftover.exists())

    def test_recovery_restore_keeps_maintenance_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            with closing(sqlite3.connect(data / 'skim.db')) as connection, connection:
                connection.execute('CREATE TABLE example(value TEXT)')
                connection.execute("INSERT INTO example VALUES('saved')")
            (data / 'skim.db').chmod(0o600)
            journal = releases.UpdateJournal(data)
            record = journal.start('b' * 32, 'v1.2.3', 'a' * 64)
            journal.advance(record['id'], 'quiesced')
            update_backup.backup_database(data, record['id'])
            journal.advance(record['id'], 'switched')
            with closing(sqlite3.connect(data / 'skim.db')) as connection, connection:
                connection.execute("INSERT INTO example VALUES('migration')")
            journal.begin_rollback(record['id'])
            update_backup.restore_database(data, record['id'])
            update_backup.restore_database(data, record['id'])
            with closing(sqlite3.connect(data / 'skim.db')) as connection:
                self.assertEqual(connection.execute('SELECT value FROM example').fetchall(), [('saved',)])
            with self.assertRaises(maintenance.UpdateInProgress):
                with maintenance.guard(data):
                    pass


class BootstrapTests(unittest.TestCase):
    def payloads(self, tag='v1.0.0', version=None):
        fixture = ReleaseTests()
        release = releases.parse_release(fixture.release(tag))
        manifest = json.dumps({'version': release.tag, 'archive': 'skim-macos.tar.gz'}).encode()
        output = io.BytesIO()
        repo = Path(__file__).resolve().parents[1]
        files = {'skim/manifest.json': manifest, 'skim/bin/skim': (repo / 'bin/skim').read_bytes(),
                 'skim/lib/python.sh': (repo / 'lib/python.sh').read_bytes()}
        for path in (repo / 'lib/skim').rglob('*.py'):
            files['skim/lib/skim/' + str(path.relative_to(repo / 'lib/skim'))] = path.read_bytes()
        if version:
            files['skim/lib/skim/__init__.py'] = files['skim/lib/skim/__init__.py'].replace(
                b'__version__ = "1.0.0"', b'__version__ = "' + version.encode() + b'"')
        with tarfile.open(fileobj=output, mode='w:gz') as archive:
            for name, data in files.items():
                info = tarfile.TarInfo(name)
                info.size = len(data)
                info.mode = 0o755 if name == 'skim/bin/skim' else 0o644
                archive.addfile(info, io.BytesIO(data))
        payloads = {'skim-macos.tar.gz': output.getvalue(), 'manifest.json': manifest, 'install.sh': b'bootstrap'}
        payloads['SHA256SUMS'] = ''.join(hashlib.sha256(value).hexdigest() + '  ' + name + '\n'
                                        for name, value in payloads.items()).encode()
        return release, payloads

    def test_happy_path_install_and_update_first_install(self):
        from skim import bootstrap
        import subprocess
        release, payloads = self.payloads()
        with tempfile.TemporaryDirectory(prefix='skim-test-', dir=Path(__file__).resolve().parents[1]) as temporary, patch.dict(os.environ, {}, clear=False):
            home = Path(temporary)
            with patch('skim.releases.fetch_latest_authenticated', return_value=release):
                store = bootstrap.install(home, download=lambda r, n, limit: payloads[n])
            self.assertEqual(store.current(), 'v1.0.0')
            launcher = home / '.local/bin/skim'
            environment = {**os.environ, 'HOME': str(home), 'SKIM_HOME': str(home / 'userdata')}
            result = subprocess.run([str(launcher), '--help'], env=environment, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(list(store.root.rglob('__pycache__')))
            self.assertFalse((home / 'userdata').exists())
            self.assertFalse((home / '.config/skim').exists())
            environment['SKIM_HOME'] = str(store.root)
            result = subprocess.run([str(launcher), '--help'], env=environment, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('분리', result.stderr)

    def test_ownership_preserves_existing_install(self):
        from skim import bootstrap
        for kind in ('file', 'checkout_link', 'broken_link', 'program', 'app', 'agent'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory(prefix='skim-test-', dir=Path(__file__).resolve().parents[1]) as temporary:
                home = Path(temporary)
                launcher = home / '.local/bin/skim'
                launcher.parent.mkdir(parents=True)
                if kind == 'file':
                    launcher.write_text('사용자 파일')
                elif kind in ('checkout_link', 'broken_link'):
                    target = home / 'checkout/bin/skim'
                    if kind == 'checkout_link':
                        target.parent.mkdir(parents=True)
                        target.write_text('개발 코드')
                    launcher.symlink_to(target)
                elif kind == 'program':
                    root = home / '.local/share/skim-program'
                    root.mkdir(parents=True)
                    (root / 'keep').write_text('유지')
                elif kind == 'app':
                    (home / 'Applications/skim PR.app').mkdir(parents=True)
                else:
                    agent = home / 'Library/LaunchAgents/com.skim.web.plist'
                    agent.parent.mkdir(parents=True)
                    import plistlib
                    agent.write_bytes(plistlib.dumps({'Label': 'other'}))
                    agent.chmod(0o600)
                before = {str(p): (os.readlink(p) if p.is_symlink() else p.read_bytes())
                          for p in home.rglob('*') if p.is_symlink() or p.is_file()}
                with patch('skim.releases.fetch_latest_authenticated', side_effect=AssertionError('조회 금지')):
                    with self.assertRaises(ValueError):
                        bootstrap.install(home)
                after = {str(p): (os.readlink(p) if p.is_symlink() else p.read_bytes())
                         for p in home.rglob('*') if p.is_symlink() or p.is_file()}
                self.assertEqual(before, after)

    def test_bootstrap_invalid_release_preserves_installed(self):
        from skim import bootstrap
        release, payloads = self.payloads()
        for selected, broken in ((None, False), (release, True)):
            with tempfile.TemporaryDirectory(prefix='skim-test-', dir=Path(__file__).resolve().parents[1]) as temporary:
                home = Path(temporary)
                data = home / 'data'
                data.write_text('보존')
                if broken:
                    payloads['skim-macos.tar.gz'] += b'broken'
                with patch('skim.releases.fetch_latest_authenticated', return_value=selected), self.assertRaises(ValueError):
                    bootstrap.install(home, download=lambda r, n, limit: payloads[n])
                self.assertEqual(data.read_text(), '보존')
                self.assertFalse((home / '.local').exists())

    def test_bootstrap_data_overlap_and_symlink_parent(self):
        from skim import bootstrap
        release, payloads = self.payloads()
        with tempfile.TemporaryDirectory(prefix='skim-test-', dir=Path(__file__).resolve().parents[1]) as temporary:
            home = Path(temporary)
            with patch.dict(os.environ, {'SKIM_HOME': str(home / '.local')}), self.assertRaises(ValueError):
                bootstrap.install(home)
            outside = home / 'outside'
            outside.mkdir()
            (home / '.local').symlink_to(outside)
            with patch('skim.releases.fetch_latest_authenticated', return_value=release), self.assertRaises(ValueError):
                bootstrap.install(home, download=lambda r, n, limit: payloads[n])
            self.assertEqual(list(outside.iterdir()), [])

    def test_bootstrap_packaged_zipapp_entrypoint(self):
        import gzip
        import subprocess
        import zipfile
        release, payloads = self.payloads()
        archived = gzip.decompress(payloads['skim-macos.tar.gz'])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            destination = root / 'release'
            results = [SimpleNamespace(stdout=b''), SimpleNamespace(stdout=archived)]
            with patch('skim.release_package.subprocess.run', side_effect=results):
                release_package.build(root, destination, release.tag, release_gate=lambda root: True)
            with zipfile.ZipFile(destination / 'install.sh') as archive:
                self.assertIn('skim/bootstrap.py', archive.namelist())
                self.assertIn('from skim.bootstrap import main', archive.read('__main__.py').decode())
            result = subprocess.run([sys.executable, '-I', str(destination / 'install.sh'), '--help'],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_bootstrap_lookup_failure_and_unsupported_platform(self):
        from skim import bootstrap
        with tempfile.TemporaryDirectory(prefix='skim-test-', dir=Path(__file__).resolve().parents[1]) as temporary, patch('skim.releases.fetch_latest_authenticated',
                side_effect=releases.ReleaseUnavailable('네트워크 확인')):
            with self.assertRaises(releases.ReleaseUnavailable):
                bootstrap.install(Path(temporary))
            self.assertEqual(list(Path(temporary).iterdir()), [])
        with patch('skim.bootstrap.sys.platform', 'win32'), redirect_stderr(io.StringIO()):
            self.assertEqual(bootstrap.main([]), 1)

    def test_linux_bootstrap_main(self):
        from skim import bootstrap
        with patch('skim.bootstrap.sys.platform', 'linux'), patch('skim.bootstrap.install') as install:
            self.assertEqual(bootstrap.main([]), 0)
            install.assert_called_once_with()

    def test_failed_install_can_retry_without_touching_competing_launcher(self):
        from skim import bootstrap
        release, payloads = self.payloads()
        for failure in (OSError('stage 실패'), KeyboardInterrupt()):
            with self.subTest(failure=type(failure)), tempfile.TemporaryDirectory(prefix='skim-test-', dir=Path(__file__).resolve().parents[1]) as temporary:
                home = Path(temporary)
                root = home / '.local/share/skim-program'
                with patch('skim.releases.fetch_latest_authenticated', return_value=release):
                    with patch.object(program_store.ProgramStore, 'stage', side_effect=failure):
                        with self.assertRaises(type(failure)):
                            bootstrap.install(home, download=lambda r, n, limit: payloads[n])
                    self.assertFalse(root.exists())
                    bootstrap.install(home, download=lambda r, n, limit: payloads[n])
        with tempfile.TemporaryDirectory(prefix='skim-test-', dir=Path(__file__).resolve().parents[1]) as temporary:
            home = Path(temporary)
            launcher = home / '.local/bin/skim'
            def competing_link(*args):
                launcher.write_text('다른 설치')
                raise FileExistsError('경합')
            with patch('skim.releases.fetch_latest_authenticated', return_value=release), patch('skim.bootstrap.os.link', side_effect=competing_link):
                with self.assertRaises(FileExistsError):
                    bootstrap.install(home, download=lambda r, n, limit: payloads[n])
            self.assertEqual(launcher.read_text(), '다른 설치')
            self.assertFalse((home / '.local/share/skim-program').exists())

    def test_launcher_codex_override_and_preimport_permissions(self):
        from skim import bootstrap
        import subprocess
        release, payloads = self.payloads()
        with tempfile.TemporaryDirectory(prefix='skim home ', dir=Path(__file__).resolve().parents[1]) as temporary:
            home = Path(temporary)
            with patch('skim.releases.fetch_latest_authenticated', return_value=release):
                store = bootstrap.install(home, download=lambda r, n, limit: payloads[n])
            payload = store.root / 'versions/v1.0.0'
            module = payload / 'lib/skim/cli.py'
            module.write_text("import os\ndef main():\n print(os.environ.get('SKIM_CODEX_BIN'))\n return 0\n")
            codex = home / '.local/share/skim-tools/node_modules/.bin/codex'
            codex.parent.mkdir(parents=True)
            codex.write_text('사용하지 않는 가짜 도구')
            codex.chmod(0o700)
            environment = {**os.environ, 'HOME': str(home), 'SKIM_HOME': str(home / 'userdata')}
            environment.pop('SKIM_CODEX_BIN', None)
            # 설치 시 Python 경로에 고정하지 않고 공백이 있는 명시 경로도 지원한다.
            python = home / 'python path'
            python.symlink_to(sys.executable)
            environment['SKIM_PYTHON'] = str(python)
            launcher = home / '.local/bin/skim'
            result = subprocess.run([str(launcher)], env=environment, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), str(codex))
            environment['SKIM_CODEX_BIN'] = '선택한 도구'
            result = subprocess.run([str(launcher)], env=environment, capture_output=True, text=True)
            self.assertEqual(result.stdout.strip(), '선택한 도구')
            sentinel = home / 'imported'
            (payload / 'lib/skim/__init__.py').write_text('from pathlib import Path\nPath(' + repr(str(sentinel)) + ').touch()\n')
            module.chmod(0o666)
            result = subprocess.run([str(launcher)], env=environment, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn('선택한 도구', result.stdout)
            self.assertFalse(sentinel.exists())
            self.assertFalse(list(payload.rglob('__pycache__')))

    def test_installed_app_and_uninstall_never_modify_files(self):
        from skim import bootstrap, macos_bundle
        release, payloads = self.payloads()
        with tempfile.TemporaryDirectory(prefix='skim-test-', dir=Path(__file__).resolve().parents[1]) as temporary:
            home = Path(temporary)
            with patch('skim.releases.fetch_latest_authenticated', return_value=release):
                store = bootstrap.install(home, download=lambda r, n, limit: payloads[n])
            module = store.root / 'versions/v1.0.0/lib/skim/macos_bundle.py'
            with patch.object(macos_bundle, '__file__', str(module)), patch.object(macos_bundle.sys, 'platform', 'darwin'), patch.object(macos_bundle.subprocess, 'run', side_effect=AssertionError('프로세스 변경 금지')), redirect_stderr(io.StringIO()):
                self.assertEqual(macos_bundle.install_app(None), 1)
                self.assertEqual(macos_bundle.uninstall_app(None), 1)
            self.assertFalse((home / 'Applications').exists())
            self.assertFalse((home / 'Library').exists())


class Crash(BaseException):
    """프로세스 강제 종료를 흉내 낸다. 일반 예외 처리로 복구되지 않는다."""


class UpdateApplyTests(unittest.TestCase):
    """정식 설치본 v1.0.0에서 v1.0.1로의 적용을 임시 홈과 가짜 릴리스로 검증한다."""

    def setUp(self):
        from skim import bootstrap
        # launcher는 sticky 공유 임시 경로를 거부하므로 저장소 아래 임시 경로를 쓴다.
        temporary = tempfile.TemporaryDirectory(prefix='skim-test-', dir=Path(__file__).resolve().parents[1])
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        environment = patch.dict(os.environ, {'HOME': str(self.home), 'SKIM_HOME': str(self.home / 'userdata')})
        environment.start()
        self.addCleanup(environment.stop)
        fixture = BootstrapTests()
        installed, payloads = fixture.payloads()
        with patch('skim.releases.fetch_latest_authenticated', return_value=installed), redirect_stdout(io.StringIO()):
            self.store = bootstrap.install(self.home, download=lambda r, n, limit: payloads[n])
        self.release, self.payloads = fixture.payloads('v1.0.1', '1.0.1')
        self.data = self.home / 'userdata/data'
        self.data.mkdir(parents=True, mode=0o700)
        with closing(sqlite3.connect(self.data / 'skim.db')) as connection, connection:
            connection.execute('CREATE TABLE item(value TEXT)')
            connection.execute("INSERT INTO item VALUES('saved')")
        (self.data / 'skim.db').chmod(0o600)
        self.module = self.store.root / 'versions/v1.0.0/lib/skim/update_apply.py'

    def update(self, release=None, **options):
        from skim import update_apply
        options.setdefault('download', lambda r, n, limit: self.payloads[n])
        options.setdefault('sleep', lambda seconds: None)
        options.setdefault('assume_yes', True)
        output, errors = io.StringIO(), io.StringIO()
        with patch('skim.releases.fetch_latest_authenticated', return_value=release or self.release), \
                redirect_stdout(output), redirect_stderr(errors):
            code = update_apply.run(home=self.home, module=self.module, **options)
        return code, output.getvalue(), errors.getvalue()

    def items(self):
        with closing(sqlite3.connect(self.data / 'skim.db')) as connection:
            return connection.execute('SELECT value FROM item').fetchall()

    def journal(self):
        return releases.UpdateJournal(self.data).read()

    def launcher_version(self):
        import subprocess
        result = subprocess.run([str(self.home / '.local/bin/skim'), '--version'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def assert_preserved(self):
        self.assertEqual(self.store.current(), 'v1.0.0')
        self.assertEqual(self.items(), [('saved',)])
        self.assertEqual(self.launcher_version(), 'skim 1.0.0')
        with maintenance.guard(self.data):
            pass

    def test_happy_path_install_and_update_applies_release(self):
        code, output, errors = self.update()
        self.assertEqual(code, 0, errors)
        self.assertIn('v1.0.1 업데이트를 마쳤습니다', output)
        self.assertEqual(self.store.current(), 'v1.0.1')
        record = self.journal()
        self.assertEqual((record['state'], record['tag'], record['previous']['tag']), ('committed', 'v1.0.1', 'v1.0.0'))
        self.assertTrue((self.data / 'update-backups' / (record['id'] + '.sqlite')).is_file())
        self.assertEqual(self.items(), [('saved',)])
        self.assertEqual(self.launcher_version(), 'skim 1.0.1')
        self.assertTrue((self.store.root / 'versions/v1.0.0').is_dir())
        with maintenance.guard(self.data):
            pass

    def test_update_confirmation_declined_changes_nothing(self):
        refuse = lambda r, n, limit: self.fail('다운로드 금지')
        for answer, expected in (('n', 0), ('', 0), (EOFError(), 1)):
            def ask(prompt):
                self.assertIn('v1.0.0에서 v1.0.1로', prompt)
                if isinstance(answer, BaseException):
                    raise answer
                return answer
            with self.subTest(answer=answer):
                code, _, errors = self.update(assume_yes=False, ask=ask, download=refuse)
                self.assertEqual(code, expected)
                if expected:
                    self.assertIn('--yes', errors)
                self.assertIsNone(self.journal())
                self.assert_preserved()

    def test_invalid_release_preserves_installed_on_update(self):
        _, wrong_manifest = BootstrapTests().payloads('v1.0.0', '1.0.1')
        corrupted = dict(self.payloads, **{'skim-macos.tar.gz': self.payloads['skim-macos.tar.gz'] + b'broken'})
        for name, payloads in (('checksum', corrupted), ('manifest', wrong_manifest)):
            with self.subTest(name=name):
                code, _, errors = self.update(download=lambda r, n, limit: payloads[n])
                self.assertEqual(code, 1)
                self.assertIn('오류', errors)
                self.assertIsNone(self.journal())
                self.assertFalse((self.store.root / 'versions/v1.0.1').exists())
                self.assert_preserved()
        def unavailable(release, name, limit):
            raise releases.ReleaseUnavailable('릴리스 파일을 받지 못했습니다. gh 로그인·권한을 확인하세요.')
        code, _, errors = self.update(download=unavailable)
        self.assertEqual(code, 1)
        self.assertNotIn('·', errors)
        self.assert_preserved()

    def test_readiness_blocks_writes_on_actual_server(self):
        from skim import cli, update_apply
        import subprocess
        # 태그는 v1.0.1이지만 코드가 1.0.0을 보고하는 배포물은 실제 launcher 실행 확인에서 거부된다.
        _, mismatched = BootstrapTests().payloads('v1.0.1')
        observed = []
        def runner(command, **kwargs):
            observed.append(self.journal()['state'])
            with self.assertRaises(maintenance.UpdateInProgress), maintenance.guard(self.data):
                pass
            with patch('skim.cli.connect', side_effect=AssertionError('DB 접근 금지')), redirect_stderr(io.StringIO()):
                self.assertEqual(cli.main(['repo', 'list']), 1)
            # 새 버전이 시작하며 DB를 바꾼 상황을 흉내 낸다.
            with closing(sqlite3.connect(self.data / 'skim.db')) as connection, connection:
                connection.execute("INSERT INTO item VALUES('migration')")
            return subprocess.run(command, **kwargs)
        code, _, errors = self.update(download=lambda r, n, limit: mismatched[n], runner=runner)
        self.assertEqual(code, 1)
        self.assertEqual(observed, ['switched'])
        self.assertIn('이전 버전 v1.0.0과 데이터를 복구했습니다', errors)
        self.assertEqual(self.journal()['state'], 'rolled_back')
        self.assert_preserved()

    def test_readiness_failure_never_commits(self):
        from skim import update_apply
        with patch.object(update_apply, 'check_ready', side_effect=update_apply.UpdateFailed('준비 안 됨')):
            self.assertEqual(self.update()[0], 1)
        self.assertEqual(self.journal()['state'], 'rolled_back')
        self.assert_preserved()

    def test_recovery_after_process_failure_rolls_back_switch(self):
        from skim import update_apply
        def migrate_then_crash(*args):
            with closing(sqlite3.connect(self.data / 'skim.db')) as connection, connection:
                connection.execute("INSERT INTO item VALUES('migration')")
            raise Crash()
        with patch.object(update_apply, 'check_ready', side_effect=migrate_then_crash), \
                patch.object(update_apply, 'restore', side_effect=Crash()), self.assertRaises(Crash):
            self.update()
        self.assertEqual(self.journal()['state'], 'switched')
        self.assertEqual(self.store.current(), 'v1.0.1')
        with self.assertRaises(maintenance.UpdateInProgress), maintenance.guard(self.data):
            pass
        code, output, _ = self.update(assume_yes=False, ask=lambda prompt: 'n')
        self.assertEqual(code, 0)
        self.assertIn('되돌려 이전 버전과 데이터를 복구했습니다', output)
        self.assertEqual(self.journal()['state'], 'rolled_back')
        self.assert_preserved()
        code, _, errors = self.update()
        self.assertEqual(code, 0, errors)
        self.assertEqual(self.store.current(), 'v1.0.1')

    def test_recovery_after_process_failure_commits_ready(self):
        original = releases.UpdateJournal.advance
        def advance(journal, update_id, state):
            if state == 'committed':
                raise Crash()
            return original(journal, update_id, state)
        with patch.object(releases.UpdateJournal, 'advance', advance), self.assertRaises(Crash):
            self.update()
        self.assertEqual(self.journal()['state'], 'ready')
        code, output, errors = self.update()
        self.assertEqual(code, 1)
        self.assertIn('완료로 기록했습니다', output)
        self.assertIn('다시 실행하세요', errors)
        self.assertEqual(self.journal()['state'], 'committed')
        self.assertEqual(self.launcher_version(), 'skim 1.0.1')
        with maintenance.guard(self.data):
            pass

    def test_concurrent_update_drains_jobs(self):
        from contextlib import ExitStack
        running = ExitStack()
        running.enter_context(maintenance.guard(self.data))
        waits = []
        def sleep(seconds):
            waits.append(seconds)
            self.assertEqual(self.items(), [('saved',)])
            if len(waits) == 2:
                running.close()
        code, output, errors = self.update(sleep=sleep)
        self.assertEqual(code, 0, errors)
        self.assertEqual(len(waits), 2)
        self.assertIn('끝나기를 기다립니다', output)
        self.assertEqual(self.store.current(), 'v1.0.1')

    def test_concurrent_update_drains_jobs_without_killing_them(self):
        from contextlib import ExitStack
        with maintenance.guard(self.data):
            code, _, errors = self.update(seconds=3)
            self.assertEqual(code, 1)
            self.assertIn('종료한 뒤 다시 실행하세요', errors)
            self.assertIsNone(self.journal())
        self.assert_preserved()
        # 확인 직후 시작한 작업은 중단하지 않고, 이후의 새 작업은 업데이트 기록이 막는다.
        running = ExitStack()
        original = releases.UpdateJournal.start
        def start(journal, *args):
            running.enter_context(maintenance.guard(self.data))
            return original(journal, *args)
        with patch.object(releases.UpdateJournal, 'start', start):
            code, _, errors = self.update(seconds=3)
        self.assertEqual(code, 1)
        self.assertIn('skim update를 다시 실행하면', errors)
        self.assertEqual(self.journal()['state'], 'prepared')
        with self.assertRaises(maintenance.UpdateInProgress), maintenance.guard(self.data):
            pass
        running.close()
        code, output, _ = self.update(assume_yes=False, ask=lambda prompt: 'n')
        self.assertIn('되돌려', output)
        self.assertEqual(self.journal()['state'], 'rolled_back')
        self.assert_preserved()

    def test_concurrent_update_and_uninstall_do_not_touch_running_update(self):
        from skim import uninstall
        import subprocess
        observed = []
        def runner(command, **kwargs):
            # 첫 업데이트가 전환을 마친 시점에 두 번째 업데이트와 데이터까지 지우는 제거가 들어온다.
            code, _, errors = self.update()
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as removal:
                removed = uninstall.run(yes=True, purge_data=True, interactive=False)
            observed.append((code, errors, removed, removal.getvalue(), self.journal()['state'], self.store.current()))
            return subprocess.run(command, **kwargs)
        code, _, errors = self.update(runner=runner)
        self.assertEqual(code, 0, errors)
        [(second, second_errors, removed, removal, state, current)] = observed
        self.assertEqual((second, removed, state, current), (1, 1, 'switched', 'v1.0.1'))
        for text in (second_errors, removal):
            self.assertIn('다른 skim 업데이트 또는 제거가 진행 중', text)
        self.assertEqual(self.journal()['state'], 'committed')
        self.assertEqual(self.launcher_version(), 'skim 1.0.1')
        self.assertTrue((self.data / 'update-backups' / (self.journal()['id'] + '.sqlite')).is_file())
        self.assertEqual(self.items(), [('saved',)])

    def test_uninstall_blocked_by_unfinished_update(self):
        from skim import uninstall
        from contextlib import ExitStack
        original = releases.UpdateJournal.advance
        def advance(journal, update_id, state):
            if state == 'ready':
                raise Crash()
            return original(journal, update_id, state)
        with patch.object(releases.UpdateJournal, 'advance', advance), \
                patch('skim.update_apply.restore', side_effect=Crash()), self.assertRaises(Crash):
            self.update()
        record = self.journal()
        self.assertEqual(record['state'], 'switched')
        with redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(uninstall.run(yes=True, purge_data=True, interactive=False), 1)
        self.assertEqual(output.getvalue(), '')
        self.assertIn('skim update를 실행해 복구', errors.getvalue())
        self.assertEqual(self.journal(), record)
        self.assertTrue((self.data / 'update-backups' / (record['id'] + '.sqlite')).is_file())
        self.assertTrue((self.home / '.local/bin/skim').is_file())
        self.assertTrue((self.store.root / 'versions/v1.0.0').is_dir())
        # 제거 확인 질문을 기다리는 동안에도 업데이트는 설치를 바꾸지 않는다.
        held = ExitStack()
        held.enter_context(self.store.exclusive())
        code, _, errors = self.update()
        held.close()
        self.assertEqual(code, 1)
        self.assertIn('다른 skim 업데이트 또는 제거가 진행 중', errors)
        self.assertEqual(self.journal(), record)
        code, output, _ = self.update(assume_yes=False, ask=lambda prompt: 'n')
        self.assertIn('되돌려', output)
        self.assert_preserved()

    def test_uninstall_checks_bound_data_from_other_home(self):
        from skim import uninstall
        original = releases.UpdateJournal.advance
        def advance(journal, update_id, state):
            if state == 'ready':
                raise Crash()
            return original(journal, update_id, state)
        with patch.object(releases.UpdateJournal, 'advance', advance), \
                patch('skim.update_apply.restore', side_effect=Crash()), self.assertRaises(Crash):
            self.update()
        record = self.journal()
        self.assertEqual(record['state'], 'switched')
        # 현재 SKIM_HOME에는 기록이 없지만 설치에 연결된 데이터에는 미완료 기록이 있다.
        with patch.dict(os.environ, {'SKIM_HOME': str(self.home / 'otherdata')}), \
                redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(uninstall.run(yes=True, purge_data=True, interactive=False), 1)
        self.assertEqual(output.getvalue(), '')
        self.assertIn('skim update를 실행해 복구', errors.getvalue())
        self.assertEqual(self.journal(), record)
        self.assertTrue((self.home / '.local/bin/skim').is_file())
        self.assertTrue((self.store.root / 'versions/v1.0.0').is_dir())
        # 연결 기록을 읽을 수 없어도 복구 가능성을 지키기 위해 제거하지 않는다.
        binding = self.store.root / '.data-binding.json'
        binding.unlink()
        binding.write_text('{', encoding='utf-8')
        with patch.dict(os.environ, {'SKIM_HOME': str(self.home / 'otherdata')}), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(uninstall.run(yes=True, purge_data=True, interactive=False), 1)
        self.assertIn('skim update를 실행해 복구', errors.getvalue())
        self.assertTrue((self.store.root / 'versions/v1.0.0').is_dir())

    def test_entrypoints_report_same_result(self):
        from skim import cli
        record = releases.UpdateJournal(self.data).start(self.store.installation_id, 'v1.0.1', 'a' * 64,
                                                         self.store.payload('v1.0.0'))
        reported = set()
        with patch('skim.cli.connect', side_effect=AssertionError('DB 접근 금지')), \
                patch('skim.web.serve', side_effect=AssertionError('서버 시작 금지')):
            for argv in (['collect'], ['analyze'], ['repo', 'list'], ['web', '--no-browser', '--offline'], []):
                errors = io.StringIO()
                with self.subTest(argv=argv), redirect_stderr(errors):
                    self.assertEqual(cli.main(argv), 1)
                reported.add(errors.getvalue())
        self.assertEqual(len(reported), 1)
        self.assertIn('완료되지 않은 업데이트', reported.pop())
        # 업데이트 명령은 같은 기록을 복구한 뒤 결과를 보고한다.
        code, output, _ = self.update(assume_yes=False, ask=lambda prompt: 'n')
        self.assertIn('v1.0.1 업데이트를 되돌려', output)
        self.assertEqual(self.journal()['id'], record['id'])
        code, _, errors = self.update()
        self.assertEqual(code, 0, errors)
        # 자동 시작 앱과 터미널이 쓰는 영구 launcher가 새 버전을 실행한다.
        self.assertEqual(self.launcher_version(), 'skim 1.0.1')


if __name__ == '__main__':
    unittest.main()
