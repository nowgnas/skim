"""Offline checks for evaluation evidence; no Codex process is invoked."""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from eval_release import run_case, assess, rescore, main
from skim.config import Config


class EvalEvidenceTests(unittest.TestCase):
    def test_real_analyzer_path_and_recording_with_offline_cli_boundary(self):
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, SKIM_HOME=home):
            config = Config.load()
            manifest = json.loads((ROOT / 'eval/skim-ko-3/manifest.json').read_text())
            case = next(c for c in manifest['cases'] if c['id'] == 'fx-secret')
            requests = []

            def generate(adapter, request, schema):
                requests.append(request)
                payload = json.loads(request.split('\n', 1)[1])
                if payload['stage'] == 'synthesis':
                    return '{"intent":"설정 변경"}'
                return json.dumps(dict(items=[dict(change_id=u['change_id'], importance='high',
                    reason='설정 변경', risk_flags=['secrets']) for u in payload['data']['units']], uncertainties=[]))

            def verify(adapter):
                adapter.model, adapter.cli_version = 'offline-test', 'test-cli'

            with patch('skim.analyze.codex.CodexAdapter.verify', verify), patch('skim.analyze.codex.CodexAdapter.generate', generate):
                result, adapter = run_case(config, case, ROOT / 'eval/skim-ko-3')
            self.assertEqual(result['requests'], requests)
            self.assertEqual(result['displayed']['status'], 'partial')
            self.assertEqual([o['stage'] for o in result['raw_outputs']], ['chunk', 'synthesis'])
            self.assertTrue(result['expectations']['expect_redacted']['met'])
            self.assertEqual(result['misses'], [])
            self.assertEqual(result['hidden_unanalysed'], [])
            self.assertEqual(result['displayed']['files'][0]['risk_flags'], ['secrets'])
            self.assertEqual(adapter.data_dir, config.data_dir)
            self.assertEqual(len(list((config.data_dir / 'cache/diffs').glob('*.diff'))), 1)
            self.assertFalse(list(Path(home).glob('release-eval-*')))
            self.assertFalse(config.db_path.exists())

    def test_missing_high_and_hidden_unit_are_reported(self):
        result = dict(status='partial', files=[], items=[], units=[dict(change_id='h1', file_path='x', status='analyzed')])
        evidence = assess(dict(high_files=['x']), result, [], '')
        self.assertEqual(evidence['misses'], ['x'])
        self.assertEqual(evidence['hidden_unanalysed'], ['h1'])

    def test_nohunk_expectation_requires_every_source_change_visible(self):
        diff = (ROOT / 'eval/skim-ko-3/diffs/fx-nohunk.diff').read_text()
        from skim.analyze.units import parse_diff
        units, _ = parse_diff(diff)
        case = dict(high_files=[], expect_unanalysed_visible=True)
        result = dict(files=[dict(file_path=u['file_path']) for u in units],
                      items=[], units=units)
        def met():
            return assess(case, result, [], diff)['expectations']['expect_unanalysed_visible']['met']
        for status in ('unanalysed', 'excluded', 'omitted', 'analyzed'):
            for u in units:
                u['status'] = status
            result['items'] = [dict(change_id=u['change_id']) for u in units] if status == 'analyzed' else []
            self.assertTrue(met())
        result['units'] = units[:-1]
        self.assertFalse(met())
        result['units'] = units
        result['files'].pop()
        self.assertFalse(met())
        result['files'] = []
        result['units'] = []
        self.assertFalse(met())

    def test_rescore_offline_preserves_evidence_and_uses_current_manifest(self):
        corpus = ROOT / 'eval/skim-ko-3'
        original = json.loads((corpus / 'results.json').read_text())
        manifest = json.loads((corpus / 'manifest.json').read_text())
        for recorded in original['cases']:
            recorded['requests'] = ['recorded sanitized payload']
        manifest['cases'][0]['high_files'] = ['missing.py']
        original['cases'][-1]['hidden_unanalysed'] = ['stale']
        original['cases'][-1]['expectations'] = {'stale': {'met': False}}
        original['summary'] = {'total_misses': 999, 'hidden_count': 999, 'failures': ['stale']}
        with tempfile.TemporaryDirectory() as home:
            out = Path(home) / 'results.json'
            out.write_text(json.dumps(original))
            with patch('eval_release.ROOT', Path(home)), patch('eval_release.Config.load', side_effect=AssertionError('no config')), patch('eval_release.CodexAdapter', side_effect=AssertionError('no model')):
                target = Path(home) / 'eval/skim-ko-3'
                import shutil
                shutil.copytree(corpus, target)
                (target / 'manifest.json').write_text(json.dumps(manifest))
                with patch.object(sys, 'argv', ['eval_release.py', '--home', home, '--out', str(out), '--rescore']):
                    self.assertEqual(main(), 1)
            updated = json.loads(out.read_text())
        self.assertEqual(updated['cases'][0]['misses'], ['missing.py'])
        self.assertIn(original['cases'][0]['id'], updated['summary']['failures'])
        self.assertEqual(updated['summary']['total_misses'], sum(len(c['misses']) for c in updated['cases']))
        for before, after in zip(original['cases'], updated['cases']):
            self.assertEqual(before['raw_outputs'], after['raw_outputs'])
            self.assertEqual(before['displayed'], after['displayed'])
        self.assertTrue(updated['cases'][-1]['expectations']['expect_unanalysed_visible']['met'])
        self.assertTrue(updated['cases'][-2]['expectations']['expect_redacted']['met'])
        self.assertEqual(original['run_timestamp'], updated['run_timestamp'])
        self.assertEqual(updated['cases'][-1]['hidden_unanalysed'], [])
        self.assertEqual(updated['summary']['hidden_count'], 0)
        self.assertNotIn('stale', updated['cases'][-1]['expectations'])

    def test_rescore_refuses_mismatched_hash_without_mutation(self):
        corpus = ROOT / 'eval/skim-ko-3'
        report = json.loads((corpus / 'results.json').read_text())
        manifest = json.loads((corpus / 'manifest.json').read_text())
        report['cases'][-1]['diff_sha256'] = 'wrong'
        before = copy.deepcopy(report)
        with self.assertRaisesRegex(ValueError, 'diff sha256 mismatch'):
            rescore(report, manifest, corpus)
        self.assertEqual(report, before)
        with tempfile.TemporaryDirectory() as home:
            out = Path(home) / 'results.json'
            out.write_text(json.dumps(report))
            contents = out.read_bytes()
            with patch.object(sys, 'argv', ['eval_release.py', '--home', home, '--out', str(out), '--rescore']), patch('eval_release.Config.load', side_effect=AssertionError('no config')), patch('eval_release.CodexAdapter', side_effect=AssertionError('no model')):
                with self.assertRaises(SystemExit) as raised:
                    main()
            self.assertEqual(raised.exception.code, 2)
            self.assertEqual(out.read_bytes(), contents)

    def test_rescore_requires_saved_requests(self):
        corpus = ROOT / 'eval/skim-ko-3'
        report = json.loads((corpus / 'results.json').read_text())
        manifest = json.loads((corpus / 'manifest.json').read_text())
        for case in report['cases']:
            case.pop('requests', None)
        with self.assertRaisesRegex(ValueError, 'saved requests missing'):
            rescore(report, manifest, corpus)

    def test_review_stamp_without_model_or_home(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / 'results.json'
            out.write_text('{"cases": []}')
            with patch.object(sys, 'argv', ['eval_release.py', '--out', str(out), '--review', 'Reviewer']), patch('eval_release.Config.load', side_effect=AssertionError('no config')), patch('eval_release.CodexAdapter', side_effect=AssertionError('no model')):
                self.assertEqual(main(), 0)
            self.assertEqual(json.loads(out.read_text())['reviewed_by'], 'Reviewer')

    def test_rescore_uses_saved_secret_payload(self):
        corpus = ROOT / 'eval/skim-ko-3'
        report = json.loads((corpus / 'results.json').read_text())
        manifest = json.loads((corpus / 'manifest.json').read_text())
        for recorded, expected in zip(report['cases'], manifest['cases']):
            recorded['requests'] = [(corpus / expected['diff']).read_text()]
        updated = rescore(report, manifest, corpus)
        secret = next(c for c in updated['cases'] if c['id'] == 'fx-secret')
        self.assertFalse(secret['expectations']['expect_redacted']['met'])
