"""Synthetic evidence tests the gate only; it never substitutes for model eval."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import check_release
from eval_release import prompt_hash, sha256, SCHEMA_VERSION, confirmation_hash, assess, summarize
from skim.analyze.units import parse_diff
from skim.analyze.protocol import prompt


class ReleaseGateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.corpus = self.root / 'eval/skim-ko-5'
        self.corpus.mkdir(parents=True)
        self.manifest = json.loads((ROOT / 'eval/skim-ko-5/manifest.json').read_text())
        self.manifest['confirmed_by'] = 'test reviewer'
        self.manifest['confirmed_sha256'] = confirmation_hash(self.manifest)
        (self.corpus / 'manifest.json').write_text(json.dumps(self.manifest))
        self.report = dict(prompt_version='skim-ko-5', prompt_sha256=prompt_hash(), schema_version=SCHEMA_VERSION,
                           cli_version='0.165.0', model='synthetic-gate-test', run_timestamp='2026-10-31T12:00:00+09:00',
                           reviewed_by='test reviewer', cases=[], summary=dict(total_misses=0, hidden_count=0, failures=[]))
        for case in self.manifest['cases']:
            path = self.corpus / case['diff']
            path.parent.mkdir(exist_ok=True)
            path.write_bytes((ROOT / 'eval/skim-ko-5' / case['diff']).read_bytes())
            self.report['cases'].append(dict(id=case['id'], diff_sha256=sha256(path.read_bytes()),
                model=self.report['model'], cli_version=self.report['cli_version'],
                displayed=dict(status='completed', files=[dict(file_path=p, importance='high', candidates=[]) for p in case['high_files']], items=[], units=[]),
                captured={p: True for p in case['high_files']}, misses=[], hidden_unanalysed=[],
                expectations={k: dict(met=True) for k in ('expect_redacted', 'expect_unanalysed_visible', 'watch') if case.get(k)}))

    def check(self):
        (self.corpus / 'results.json').write_text(json.dumps(self.report))
        check_release.check(self.root)

    def test_block_synthetic_results(self):
        with self.assertRaises(ValueError):
            self.check()

    def realistic(self):
        for expected, recorded in zip(self.manifest['cases'], self.report['cases']):
            diff = (self.corpus / expected['diff']).read_text()
            units, _ = parse_diff(diff)
            for unit in units:
                unit['status'] = 'unanalysed'
            result = dict(status='partial', units=units, items=[], files=[
                dict(file_path=p, importance='high', candidates=[])
                for p in dict.fromkeys(u['file_path'] for u in units)])
            recorded.update(raw_outputs=[dict(stage='chunk', raw='{}'), dict(stage='synthesis', raw='{"intent":"offline fixture"}')],
                            requests=[prompt('chunk', dict(units=units, metadata={})),
                                      prompt('synthesis', dict(metadata={}, items=[], uncertainties=[]))])
            recorded.update(assess(expected, result, recorded['requests'], diff))
        summarize(self.report)

    def test_pass_realistic_structure(self):
        self.realistic()
        self.check()

    def test_recomputed_evidence_and_visibility(self):
        self.realistic()
        original = copy.deepcopy(self.report)
        mutations = [
            lambda c: c.update(raw_outputs=[]),
            lambda c: c.update(raw_outputs=[dict(stage='chunk', raw='{}')]),
            lambda c: c.update(requests=[]),
            lambda c: c['displayed']['units'].pop(),
            lambda c: c['displayed']['files'].pop(),
            lambda c: c['displayed']['units'][0].pop('status'),
            lambda c: c.update(captured={}),
        ]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                self.report = copy.deepcopy(original)
                mutate(self.report['cases'][0])
                with self.assertRaises(ValueError):
                    self.check()

    def test_review_required(self):
        self.realistic()
        for value in (None, '', ' ', False, 0):
            self.report['reviewed_by'] = value
            with self.assertRaisesRegex(ValueError, 'reviewed_by'):
                self.check()

    def test_hash_mismatch(self):
        self.report['prompt_sha256'] = 'wrong'
        with self.assertRaisesRegex(ValueError, '해시'):
            self.check()

    def test_miss(self):
        self.report['cases'][0]['misses'] = ['lib/skim/db.py']
        with self.assertRaises(ValueError):
            self.check()

    def test_missing_case(self):
        self.report['cases'].pop()
        with self.assertRaises(ValueError):
            self.check()

    def test_other_version(self):
        with self.assertRaises(ValueError):
            check_release.check(self.root, 'skim-ko-3')

    def test_missing_results(self):
        with self.assertRaises(OSError):
            check_release.check(self.root)

    def test_hidden_and_expectation_failures(self):
        original = copy.deepcopy(self.report)
        self.report['cases'][0]['hidden_unanalysed'] = ['h1']
        with self.assertRaises(ValueError):
            self.check()
        self.report = original
        self.report['cases'][-1]['expectations']['expect_unanalysed_visible']['met'] = False
        with self.assertRaises(ValueError):
            self.check()

    def test_high_files_require_human_confirmation(self):
        for value in (None, '', ' ', False, 0, 'missing'):
            if value != 'missing':
                self.manifest['confirmed_by'] = value
            else:
                self.manifest.pop('confirmed_by', None)
            (self.corpus / 'manifest.json').write_text(json.dumps(self.manifest))
            with self.assertRaisesRegex(ValueError, 'confirmed_by.*high_files'):
                self.check()

    def test_stale_confirmation(self):
        self.manifest['cases'][0]['high_files'] = []
        (self.corpus / 'manifest.json').write_text(json.dumps(self.manifest))
        with self.assertRaisesRegex(ValueError, 'confirmed_sha256'):
            self.check()

    def test_forged_expectations_are_recomputed(self):
        self.realistic()
        secret = next(c for c in self.report['cases'] if c['id'] == 'fx-secret')
        expected = next(c for c in self.manifest['cases'] if c['id'] == 'fx-secret')
        secret['requests'] = [(self.corpus / expected['diff']).read_text()]
        with self.assertRaisesRegex(ValueError, 'stored scores mismatch'):
            self.check()

    def test_missing_file_or_status_blocks_even_with_updated_scores(self):
        for missing in ('file', 'status'):
            self.realistic()
            expected = self.manifest['cases'][0]
            case = self.report['cases'][0]
            if missing == 'file':
                case['displayed']['files'].pop()
            else:
                case['displayed']['units'][0].pop('status')
            case.update(assess(expected, case['displayed'], case['requests'],
                              (self.corpus / expected['diff']).read_text()))
            summarize(self.report)
            with self.assertRaises(ValueError):
                self.check()
