"""실제 Codex와 네트워크 없이 격리 계약을 검증한다."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
from skim.analyze.codex import CodexAdapter, IsolationError, FLAGS
from skim.config import Config
from skim.doctor import run
from skim.cli import main


class CodexTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.source = self.root / 'user-codex'
        self.source.mkdir()
        (self.source / 'auth.json').write_text('credential-never-read')
        env = patch.dict(os.environ, CODEX_HOME=str(self.source), SKIM_HOME=str(self.root / 'skim'))
        env.start()
        self.addCleanup(env.stop)
        self.adapter = CodexAdapter(self.runner, self.root / 'data')
        self.calls = []
        self.version = 'codex-cli 0.165.0'
        self.help = ' '.join(FLAGS) + ' --disable'
        self.features = 'apps stable true\nplugins under development false\nmulti_agent experimental true\nweb_search_cached stable true\nother stable true'
        self.answer = {'tool_ran': False, 'instructions_seen': ''}
        self.file_location = None
        self.events = [{'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'x'}}]
        self.stderr = ''
        self.exitcode = 0
        self.adapter.init_home()

    def runner(self, command, **kw):
        self.calls.append((command, kw))
        if command[0] == 'gh':
            return subprocess.CompletedProcess(command, 0, '', '')
        self.assertEqual(set(kw['env']), {'PATH', 'HOME', 'CODEX_HOME'})
        self.assertNotEqual(kw['env']['CODEX_HOME'], str(self.adapter.home))
        self.assertEqual(list(Path(kw['cwd']).iterdir()), [])
        args = command[1:]
        if args == ['--version']:
            output = self.version
        elif args in (['exec', '--help'], ['--help']):
            output = self.help
        elif args == ['features', 'list']:
            output = self.features
        else:
            self.assertEqual(args[0], 'exec')
            self.assertEqual(command[command.index('-c') + 1], 'web_search="disabled"')
            self.assertEqual([p.name for p in Path(kw['cwd']).parent.iterdir()], ['work'])
            self.assertEqual(command[command.index('--sandbox') + 1], 'read-only')
            for flag in FLAGS:
                self.assertIn(flag, command)
            for name in ('apps', 'plugins', 'multi_agent', 'web_search_cached'):
                self.assertIn(name, command)
            self.assertNotIn('other', command)
            self.assertEqual(list(Path(kw['env']['HOME']).iterdir()), [])
            if self.file_location:
                directory = kw['cwd'] if self.file_location == 'cwd' else kw['env']['HOME']
                (Path(directory) / 'probe.txt').touch()
            Path(command[command.index('--output-last-message') + 1]).write_text(json.dumps(self.answer))
            self.assertIn('--json', command)
            events = self.events + [{'type': 'turn.completed'}]
            stdout = '\n'.join(json.dumps(e) for e in events)
            return subprocess.CompletedProcess(command, self.exitcode, stdout, self.stderr)
        return subprocess.CompletedProcess(command, 0, output, '')

    def test_init_explicit_idempotent_and_no_credential_reads(self):
        with patch.object(Path, 'read_text', side_effect=AssertionError('인증 읽기 금지')):
            self.adapter.init_home()
            self.adapter.check_home()
        self.assertEqual(self.adapter.home.stat().st_mode & 0o777, 0o700)
        self.assertEqual(list(self.adapter.home.iterdir()), [self.adapter.home / 'auth.json'])
        self.assertEqual(os.readlink(self.adapter.home / 'auth.json'), str(self.source / 'auth.json'))
        self.assertFalse(self.calls)

    def test_home_contamination_and_permissions_block(self):
        for name in ('config.toml', 'AGENTS.md', 'skills', 'plugins', 'memories', 'hooks'):
            entry = self.adapter.home / name
            entry.touch()
            with self.assertRaises(IsolationError):
                self.adapter.verify()
            entry.unlink()
        self.assertFalse(self.calls)
        self.adapter.home.chmod(0o755)
        with self.assertRaises(IsolationError):
            self.adapter.verify()

    def test_regular_auth_and_arbitrary_link_block(self):
        link = self.adapter.home / 'auth.json'
        link.unlink()
        link.write_text('no')
        with self.assertRaises(IsolationError):
            self.adapter.check_home()
        link.unlink()
        other = self.root / 'other'
        other.touch()
        link.symlink_to(other)
        with self.assertRaises(IsolationError):
            self.adapter.check_home()

    def test_version_and_flag_gates(self):
        for version in ('codex-cli 0.164.9', 'unknown', 'codex-cli 0.165.0-beta', 'codex-cli 0.165'):
            self.version = version
            with self.assertRaises(IsolationError):
                self.adapter.verify()
        self.version = 'codex-cli 0.165.0'
        for flag in FLAGS:
            self.help = ' '.join(f for f in FLAGS if f != flag)
            with self.assertRaises(IsolationError):
                self.adapter.verify()
        self.assertFalse(any('--output-last-message' in c for c, _ in self.calls))

    def test_probe_required_cached_expired_and_version_keyed(self):
        with self.assertRaises(IsolationError):
            self.adapter.generate('repo-code', {})
        self.adapter.probe()
        self.assertEqual(self.adapter.cli_version, '0.165.0')
        self.assertEqual(self.adapter.model, 'unknown')
        self.adapter.verify()
        cache = self.adapter.probe_path
        value = json.loads(cache.read_text())
        value['checked_at'] = time.time() - 7 * 86400
        cache.write_text(json.dumps(value))
        with self.assertRaises(IsolationError):
            self.adapter.verify()
        self.adapter.probe()
        self.version = 'codex-cli 0.166.0'
        with self.assertRaises(IsolationError):
            self.adapter.verify()

    def test_probe_blocks_writes_bad_output_and_exit(self):
        self.adapter.probe()
        for location in ('cwd', 'home'):
            self.file_location = location
            with self.assertRaises(IsolationError):
                self.adapter.probe()
            self.assertFalse(self.adapter.probe_path.exists())
        self.file_location = None
        for answer in ({}, [],
                       {'tool_ran': False, 'instructions_seen': None},
                       {'tool_ran': False, 'instructions_seen': '', 'extra': True},
                       {'tool_ran': 0, 'instructions_seen': ''}):
            self.answer = answer
            with self.assertRaises(IsolationError):
                self.adapter.probe()
        self.answer = {'tool_ran': False, 'instructions_seen': ''}
        self.exitcode = 1
        with self.assertRaises(IsolationError):
            self.adapter.probe()

    def test_probe_judges_tool_calls_from_events_not_self_report(self):
        self.answer = {'tool_ran': True, 'instructions_seen': 'Other instructions'}
        self.adapter.probe()
        for item in ({'type': 'command_execution'}, {'type': 'mcp_tool_call'}, {'type': 'web_search'},
                     {'type': 'file_change'}, 'bad'):
            self.events = [{'type': 'item.started', 'item': item}]
            with self.assertRaises(IsolationError):
                self.adapter.probe()
            self.assertFalse(self.adapter.probe_path.exists())
        self.events = []
        self.adapter.probe()

    def test_generate_same_isolation_and_doctor(self):
        config = Config(self.root / 'absent', self.adapter.data_dir)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(run(config, self.runner, lambda _: '/fake', self.adapter, probe=True), 0)
        for label in ('codex-home', 'version', 'flags', 'probe'):
            self.assertIn('정상: ' + label, out.getvalue())
        self.assertIn('경고: 차단 목록 밖의 활성 Codex 기능: other', out.getvalue())
        self.answer = {'intent': '변경 요약'}
        self.assertEqual(json.loads(self.adapter.generate('정제된 자료', {})), self.answer)
        execs = [kw for c, kw in self.calls if '--output-last-message' in c]
        self.assertNotIn('정제된 자료', execs[0]['input'])
        self.assertEqual(execs[-1]['input'], '정제된 자료')
        self.assertEqual(execs[0]['env'].keys(), execs[-1]['env'].keys())

    def test_feature_format_and_flag_suffixes_block(self):
        for output in ('', 'FEATURE STAGE ENABLED', '{"shell_tool":true}',
                       'other stable true', 'apps stable maybe', 'apps stable true\nheader'):
            self.features = output
            with self.assertRaises(IsolationError):
                self.adapter.check_flags()
        self.features = 'shell_tool stable false'
        for flag in (*FLAGS, '--disable'):
            self.help = (' '.join(FLAGS) + ' --disable').replace(flag, flag + '-extra')
            with self.assertRaises(IsolationError):
                self.adapter.check_flags()

    def test_runtime_state_and_auth_rename_are_temporary(self):
        original = self.adapter.runner

        def runner(command, **kw):
            result = original(command, **kw)
            runtime = Path(kw['env']['CODEX_HOME'])
            runtime.mkdir(exist_ok=True)
            (runtime / 'log').touch()
            if '--output-last-message' in command:
                replacement = runtime / 'replacement'
                replacement.write_text('갱신 자격 증명')
                replacement.replace(runtime / 'auth.json')
            return result

        self.adapter.runner = runner
        self.adapter.probe()
        self.adapter.generate('자료', {})
        self.adapter.check_home()
        self.assertEqual((self.source / 'auth.json').read_text(), 'credential-never-read')
        for _, kw in self.calls:
            self.assertFalse(Path(kw['env']['CODEX_HOME']).exists())

    def test_reset_repairs_contamination_without_following_links(self):
        (self.adapter.home / 'log').touch()
        (self.adapter.home / 'linked-directory').symlink_to(self.source, target_is_directory=True)
        auth = self.adapter.home / 'auth.json'
        auth.unlink()
        auth.write_text('갱신된 사본')
        with self.assertRaises(IsolationError):
            self.adapter.init_home()
        self.adapter.init_home(reset=True)
        self.adapter.check_home()
        self.assertTrue((self.source / 'auth.json').is_file())

    def test_probe_model_must_match(self):
        self.adapter.probe()
        self.adapter.model = 'different'
        with self.assertRaises(IsolationError):
            self.adapter.check_probe()

    def test_reset_repairs_permissions_and_invalidates_probe(self):
        self.adapter.probe()
        cache = self.adapter.probe_path
        self.adapter.home.chmod(0o755)
        with self.assertRaises(IsolationError):
            self.adapter.init_home()
        self.adapter.init_home(reset=True)
        self.adapter.check_home()
        self.assertFalse(cache.exists())

    def test_reset_rejects_file_or_symlink_without_touching_target(self):
        (self.adapter.home / 'auth.json').unlink()
        self.adapter.home.rmdir()
        self.adapter.home.write_text('보존')
        with self.assertRaises(IsolationError):
            self.adapter.init_home(reset=True)
        self.assertEqual(self.adapter.home.read_text(), '보존')
        self.adapter.home.unlink()
        self.adapter.home.symlink_to(self.source, target_is_directory=True)
        with self.assertRaises(IsolationError):
            self.adapter.init_home(reset=True)
        self.assertEqual((self.source / 'auth.json').read_text(), 'credential-never-read')

    def test_analysis_execution_failures_retry_once_and_exhaust(self):
        from skim.analyze import Analyzer
        from skim.analyze.protocol import SYNTHESIS_SCHEMA
        self.adapter.probe()
        original = self.adapter.runner
        for failure in ('exit', 'timeout', 'missing'):
            for recover in (True, False):
                with self.subTest(failure=failure, recover=recover):
                    attempts = []

                    def runner(command, **kw):
                        if '--output-last-message' not in command:
                            return original(command, **kw)
                        attempts.append(command)
                        if recover and len(attempts) == 2:
                            self.answer = {'intent': '요약'}
                            return original(command, **kw)
                        if failure == 'timeout':
                            raise subprocess.TimeoutExpired(command, 120)
                        return subprocess.CompletedProcess(command, 1 if failure == 'exit' else 0, '', '')

                    self.adapter.runner = runner
                    analyzer = Analyzer(None, None, adapter=self.adapter)
                    with patch.object(analyzer, '_reserve') as reserve:
                        if recover:
                            self.assertEqual(analyzer._call('synthesis', {}, SYNTHESIS_SCHEMA), {'intent': '요약'})
                        else:
                            with self.assertRaises(ValueError) as caught:
                                analyzer._call('synthesis', {}, SYNTHESIS_SCHEMA)
                            self.assertNotIsInstance(caught.exception, IsolationError)
                        self.assertEqual(reserve.call_count, 2)
                    self.assertEqual(len(attempts), 2)

    def test_failed_execution_home_contamination_blocks_without_retry(self):
        from skim.analyze import Analyzer
        from skim.analyze.protocol import SYNTHESIS_SCHEMA
        self.adapter.probe()
        original = self.adapter.runner

        def runner(command, **kw):
            if '--output-last-message' in command:
                (self.adapter.home / 'config.toml').touch()
                raise subprocess.TimeoutExpired(command, 120)
            return original(command, **kw)

        self.adapter.runner = runner
        analyzer = Analyzer(None, None, adapter=self.adapter)
        with patch.object(analyzer, '_reserve') as reserve:
            with self.assertRaises(IsolationError):
                analyzer._call('synthesis', {}, SYNTHESIS_SCHEMA)
            self.assertEqual(reserve.call_count, 1)

    def test_probe_records_are_separate_per_model(self):
        self.adapter.probe()
        first = self.adapter.probe_path
        self.adapter.model = 'another/model'
        self.adapter.probe()
        self.assertNotEqual(first, self.adapter.probe_path)
        self.assertTrue(first.exists())
        self.adapter.model = 'unknown'
        self.adapter.check_probe()

    def test_model_banner_cached_without_extra_execution(self):
        self.stderr = 'OpenAI Codex\nmodel: gpt-5.4\nprovider: openai\n'
        self.adapter.probe()
        self.assertEqual(self.adapter.model, 'gpt-5.4')
        self.assertEqual(json.loads(self.adapter.probe_path.read_text())['model'], 'gpt-5.4')
        fresh = CodexAdapter(self.runner, self.adapter.data_dir)
        before = len(self.calls)
        fresh.verify()
        self.assertEqual(fresh.model, 'gpt-5.4')
        self.assertFalse(any('--output-last-message' in c for c, _ in self.calls[before:]))
        fresh.generate('자료', {})
        command = self.calls[-1][0]
        self.assertEqual(command[command.index('--model') + 1], 'gpt-5.4')

    def test_model_event_precedes_banner_and_unknown_fallback(self):
        self.stderr = 'model: banner-model\n'
        self.events = [{'type': 'thread.started', 'metadata': {'model': 'event-model'}}]
        self.adapter.probe()
        self.assertEqual(self.adapter.model, 'event-model')
        self.stderr = 'WARNING: proceeding, even though we could not update PATH alias\n'
        self.events = [{'type': 'thread.started', 'thread_id': 'test-thread'}]
        self.adapter.probe()
        self.assertEqual(self.adapter.model, 'unknown')
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            run(Config(self.root / 'config', self.adapter.data_dir), self.runner,
                lambda _: '/fake', self.adapter)
        self.assertIn('경고: model —', out.getvalue())
        self.assertIn('codex_model 설정', out.getvalue())
        self.assertIn('SKIM_CODEX_MODEL', out.getvalue())
        self.assertNotIn('정상: model', out.getvalue())
        self.assertNotIn('Codex 모델: unknown', out.getvalue())
        self.assertEqual(sum('model —' in line for line in out.getvalue().splitlines()), 1)

    def test_doctor_reports_one_known_model_after_checks(self):
        for configured, probe in ((True, True), (True, False), (False, True), (False, False)):
            with self.subTest(configured=configured, probe=probe):
                config = Config(self.root / 'config', self.adapter.data_dir,
                                codex_model='configured' if configured else None)
                self.stderr = 'model: detected\n'
                if not configured and not probe:
                    self.adapter.probe()
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    self.assertEqual(run(config, self.runner, lambda _: '/fake', probe=probe), 0)
                model = 'configured' if configured else 'detected'
                self.assertIn(f'정상: model — {model}\n', out.getvalue())
                self.assertEqual(sum('model —' in line for line in out.getvalue().splitlines()), 1)
                self.assertNotIn('Codex 모델:', out.getvalue())

    def test_configured_model_and_environment_override(self):
        config = Config.load()
        (config.config_dir / 'config.json').write_text(json.dumps({'codex_model': 'configured'}))
        self.assertEqual(Config.load().codex_model, 'configured')
        with patch.dict(os.environ, SKIM_CODEX_MODEL='override'):
            config = Config.load()
            self.assertEqual(config.codex_model, 'override')
            adapter = CodexAdapter(self.runner, self.adapter.data_dir,
                                   model=config.resolved_codex_model())
            self.stderr = 'model: ignored-banner\n'
            adapter.probe()
            adapter.verify()
            self.assertEqual(adapter.model, 'override')
            command = next(c for c, _ in reversed(self.calls) if '--output-last-message' in c)
            self.assertEqual(command[command.index('--model') + 1], 'override')

    def test_loaded_config_is_stable_after_file_and_environment_changes(self):
        from skim.analyze import Analyzer
        config = Config.load()
        path = config.config_dir / 'config.json'
        path.write_text(json.dumps({'codex_model': 'original'}))
        config = Config.load()
        for contents in ('[]', '{', '{"codex_model": "replacement"}'):
            path.write_text(contents)
            with patch.dict(os.environ, SKIM_CODEX_MODEL=''):
                analyzer = Analyzer(None, config)
                self.assertEqual(analyzer.adapter.model, 'original')
                self.assertEqual(config.resolved_codex_model(), 'original')
        explicit = Config(config.config_dir, config.data_dir, codex_model='explicit')
        self.assertEqual(Analyzer(None, explicit).adapter.model, 'explicit')

    def test_invalid_model_identifiers(self):
        config = Config.load()
        path = config.config_dir / 'config.json'
        for model in ('', 'unknown', '-model', 'two words', ' spaced ', 123):
            with self.subTest(model=model):
                path.write_text(json.dumps({'codex_model': model}))
                with self.assertRaises(ValueError):
                    Config.load()
                with self.assertRaises(ValueError):
                    CodexAdapter(self.runner, self.adapter.data_dir, model=model)
        for model in ('-model', 'two words', 'unknown'):
            self.assertIsNone(CodexAdapter._output_model(
                json.dumps({'metadata': {'model': model}}), ''))
        self.adapter.probe()
        path = self.adapter.probe_path
        value = json.loads(path.read_text())
        value['model'] = '-invalid'
        path.write_text(json.dumps(value))
        with self.assertRaises(IsolationError):
            self.adapter.check_probe()

    def test_doctor_reports_invalid_settings_and_continues(self):
        config = Config.load()
        path = config.config_dir / 'config.json'
        for contents, env in (('[]', {}), ('{', {}), ('{}', {'SKIM_CODEX_MODEL': ''})):
            path.write_text(contents)
            before = path.stat().st_mode
            out = io.StringIO()
            self.calls.clear()
            with patch.dict(os.environ, env), contextlib.redirect_stdout(out):
                self.assertEqual(run(config, self.runner, lambda _: '/fake', self.adapter), 1)
            self.assertIn('실패: model', out.getvalue())
            for label in ('codex-home', 'version', 'flags', 'probe', '로컬 권한'):
                self.assertIn(label, out.getvalue())
            self.assertNotIn('Traceback', out.getvalue())
            self.assertFalse(any('--output-last-message' in c for c, _ in self.calls))
            self.assertEqual(path.stat().st_mode, before)
        path.write_text('{}')
        path.chmod(0o644)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(run(config, self.runner, lambda _: '/fake', self.adapter), 1)
        self.assertIn('실패: model', out.getvalue())
        self.assertEqual(path.stat().st_mode & 0o777, 0o644)

    def test_generate_blocks_model_change_after_verification(self):
        self.adapter.probe()
        self.adapter.verify()
        path = self.adapter.probe_path
        value = json.loads(path.read_text())
        value['model'] = 'changed-model'
        path.write_text(json.dumps(value))
        before = len(self.calls)
        with self.assertRaises(IsolationError):
            self.adapter.generate('자료', {})
        self.assertFalse(any('--output-last-message' in c for c, _ in self.calls[before:]))

    def test_cli_init(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(['codex-home', 'init']), 0)
        self.assertTrue((self.root / 'skim/data/codex-home/auth.json').is_symlink())


class ProviderSchemaTests(unittest.TestCase):
    def test_strips_unique_items_recursively_without_mutation(self):
        from skim.analyze.codex import provider_schema
        schema = {'type': 'object', 'properties': {'a': {'type': 'array', 'uniqueItems': True, 'items': {'enum': ['x']}}}}
        self.assertEqual(provider_schema(schema), {'type': 'object', 'properties': {'a': {'type': 'array', 'items': {'enum': ['x']}}}})
        self.assertTrue(schema['properties']['a']['uniqueItems'])
        named = {'type': 'object', 'properties': {'uniqueItems': {'type': 'boolean'}}}
        self.assertEqual(provider_schema(named), named)
        listed = {'anyOf': [{'type': 'array', 'uniqueItems': True}]}
        self.assertEqual(provider_schema(listed), {'anyOf': [{'type': 'array'}]})

