"""Deterministic split scoring and corpus checks; no model calls."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import eval_release as evaluation
import check_release
from skim.analyze.units import parse_diff

CORPUS = ROOT / 'eval/skim-ko-6'
MANIFEST = json.loads((CORPUS / 'manifest.json').read_text())


def evidence(case, importance='low', behavior='changed'):
    diff = (CORPUS / case['diff']).read_text()
    units, _ = parse_diff(diff)
    for unit in units:
        unit['status'] = 'analyzed'
    return dict(status='completed', units=units,
                items=[dict(change_id=u['change_id'], importance=importance, behavior=behavior,
                            reason='테스트 근거', risk_flags=[]) for u in units],
                files=[dict(file_path=p, importance=importance, candidates=[])
                       for p in dict.fromkeys(u['file_path'] for u in units)])


def case_named(name):
    return copy.deepcopy(next(c for c in MANIFEST['cases'] if c['id'] == name))


def score(case, result):
    return evaluation.assess(case, result, ['sanitized request'], '', 'skim-ko-6')


class SplitScoringTests(unittest.TestCase):
    def test_auth_low_and_high(self):
        case = case_named('fx-auth-rename')
        for importance in ('low', 'high'):
            scored = score(case, evidence(case, importance))
            paths = case['not_high_files'] if importance == 'high' else []
            self.assertEqual(scored['false_high'], paths)
            self.assertEqual(scored['metrics']['top3'], paths)
            self.assertEqual(scored['location_hint_promoted'], [])
            report = dict(prompt_version='skim-ko-6', cases=[dict(id=case['id'], **scored)])
            self.assertEqual(evaluation.summarize(report), [case['id']] if paths else [])

    def test_floor_and_test_gap_do_not_capture_high(self):
        for name in ('fx-migration-drop', 'fx-auth-outside'):
            case = case_named(name)
            result = evidence(case)
            scored = score(case, result)
            self.assertEqual(scored['misses'], case['high_files'])
            if name == 'fx-migration-drop':
                self.assertEqual(scored['signal_hidden'], [])
            else:
                self.assertTrue(all(u['test_gap'] for u in result['units']))
                self.assertTrue(all(not u['hard_floors'] for u in result['units']))
                self.assertEqual(scored['metrics']['top3'], [])

    def test_kind_and_floor_exposure_are_independent(self):
        case = case_named('fx-filekind')
        result = evidence(case)
        scored = score(case, result)
        self.assertEqual(scored['signal_hidden'], [])
        self.assertTrue(all(v == dict(signal=True, where='safety')
                            for v in scored['metrics']['kind_signal_exposure'].values()))
        path = case['kind_signal_files'][0]
        case['floor_files'] = [path]
        self.assertEqual(score(case, result)['signal_hidden'], [path])
        for u in result['units']:
            if u['file_path'] == path:
                u['kind_signals'] = []
                u['hard_floors'] = ['secrets']
        self.assertEqual(score(case, result)['signal_hidden'], [path])

    def test_location_hint_missing_and_promoted(self):
        case = case_named('fx-auth-rename')
        result = evidence(case, 'mid')
        self.assertEqual(score(case, result)['location_hint_promoted'], case['location_hint_files'])
        for unit in result['units']:
            unit['location_hints'] = []
        self.assertEqual(score(case, result)['location_hint_missing'], case['location_hint_files'])

    def test_annotations_and_mechanical_failures(self):
        case = case_named('fx-token-annotation')
        result = evidence(case)
        self.assertEqual(score(case, result)['unexpected_floor'], [])
        result['units'][0]['hard_floors'] = ['secrets']
        self.assertEqual(score(case, result)['unexpected_floor'], case['no_floor_files'])
        case = case_named('fx-format-behavior')
        self.assertEqual(score(case, evidence(case))['mechanical_folded'], [])
        self.assertCountEqual(score(case, evidence(case, behavior='unchanged'))['mechanical_folded'],
                              case['not_mechanical_files'])

    def test_grouping_is_reported_without_gating(self):
        case = case_named('fx-auth-related')
        result = evidence(case)
        for item, unit in zip(result['items'], result['units']):
            if unit['file_path'] in case['high_files']:
                item['importance'] = 'high'
        scored = score(case, result)
        self.assertEqual(scored['metrics']['core_hit'], {'app/checkout.py': False})
        self.assertFalse(any(scored['metrics']['related_hit']['app/checkout.py'].values()))
        report = dict(prompt_version='skim-ko-6', cases=[dict(id=case['id'], **scored)])
        self.assertEqual(evaluation.summarize(report), [])
        ids = {u['file_path']: u['change_id'] for u in result['units']}
        result['groups'] = [dict(core_change_id=ids['app/checkout.py'], reason='주문', related=[
            dict(change_id=ids[p], relation='other') for p in case['related_of']['app/checkout.py']])]
        scored = score(case, result)
        self.assertTrue(scored['metrics']['core_hit']['app/checkout.py'])
        self.assertTrue(all(scored['metrics']['related_hit']['app/checkout.py'].values()))

    def test_top3_stays_three_while_exposure_uses_full_top(self):
        case = case_named('fx-auth-related')
        result = evidence(case, 'high')
        paths = [f['file_path'] for f in result['files']]
        case = dict(high_files=paths, kind_signal_files=[paths[-1]])
        for u in result['units']:
            u['kind_signals'] = ['dependency']
        scored = score(case, result)
        self.assertEqual(len(scored['metrics']['top3']), 3)
        self.assertEqual(scored['metrics']['top3_precision'], 1)
        self.assertEqual(scored['metrics']['top3_recall'], .75)
        self.assertEqual(scored['metrics']['top3_recall_ceiling'], .75)
        self.assertEqual(scored['signal_hidden'], [])

    def test_confirmation_hash_and_old_policy(self):
        for version in ('skim-ko-3', 'skim-ko-4', 'skim-ko-5', 'skim-ko-6'):
            manifest = dict(prompt_version=version, cases=[dict(high_files=['x'])], rule='before')
            before = evaluation.confirmation_hash(manifest)
            manifest['rule'] = 'after'
            self.assertEqual(before == evaluation.confirmation_hash(manifest), version != 'skim-ko-6')
        check_release.check(ROOT, version='skim-ko-5')

    def test_old_versions_keep_candidate_capture(self):
        case = case_named('fx-migration-drop')
        result = evidence(case)
        result['files'][0]['candidates'] = ['db_schema_migration']
        for version in (None, 'skim-ko-5'):
            scored = evaluation.assess(case, result, ['sanitized request'], '', version)
            self.assertEqual(scored['misses'], [])
            self.assertFalse(set(evaluation.SPLIT_FAILURES) & set(scored))
            self.assertNotIn('top3_recall_ceiling', scored['metrics'])
        self.assertEqual(score(case, result)['misses'], case['high_files'])

    def test_every_new_failure_reaches_summary(self):
        case = case_named('fx-auth-rename')
        clean = score(case, evidence(case))
        for key in evaluation.SPLIT_FAILURES:
            scored = copy.deepcopy(clean)
            scored[key] = ['missing.py']
            report = dict(prompt_version='skim-ko-6', cases=[dict(id='test', **scored)])
            self.assertEqual(evaluation.summarize(report), ['test'])
            self.assertEqual(report['summary'][key], 1)

    def test_gate_recomputes_false_high_and_requires_confirmation_review(self):
        case = case_named('fx-auth-rename')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            corpus = root / 'eval/skim-ko-6'
            (corpus / 'diffs').mkdir(parents=True)
            data = (CORPUS / case['diff']).read_bytes()
            (corpus / case['diff']).write_bytes(data)
            manifest = dict(MANIFEST, cases=[case])
            report = dict(prompt_version='skim-ko-6', prompt_sha256=evaluation.prompt_hash(),
                          schema_version=evaluation.SCHEMA_VERSION, sanitization_policy=evaluation.SANITIZATION_POLICY,
                          model='fake', cli_version='fake', run_timestamp='2026-11-11T12:00:00+09:00', reviewed_by='test')
            def save(importance='low'):
                scored = score(case, evidence(case, importance))
                report['cases'] = [dict(id=case['id'], diff_sha256=evaluation.sha256(data), model='fake',
                    cli_version='fake', requests=['sanitized request'],
                    raw_outputs=[dict(stage='chunk', raw='{}'), dict(stage='synthesis', raw='{}')], **scored)]
                evaluation.summarize(report)
                (corpus / 'manifest.json').write_text(json.dumps(manifest))
                (corpus / 'results.json').write_text(json.dumps(report))
            save()
            with self.assertRaisesRegex(ValueError, 'confirmed_by'):
                check_release.check(root)
            manifest.update(confirmed_by='test', confirmed_sha256=evaluation.confirmation_hash(manifest))
            save()
            check_release.check(root)
            report['reviewed_by'] = None
            save()
            with self.assertRaisesRegex(ValueError, 'reviewed_by'):
                check_release.check(root)
            report['reviewed_by'] = 'test'
            save('high')
            with self.assertRaisesRegex(ValueError, 'false_high'):
                check_release.check(root)


class CorpusTests(unittest.TestCase):
    def test_labels_parse_and_copies_match(self):
        old = ROOT / 'eval/skim-ko-5'
        previous = {c['id']: c for c in json.loads((old / 'manifest.json').read_text())['cases']}
        self.assertEqual(len(MANIFEST['cases']), 18)
        for case in MANIFEST['cases']:
            with self.subTest(case=case['id']):
                data = (CORPUS / case['diff']).read_bytes()
                units, _ = parse_diff(data.decode())
                paths = {u['file_path'] for u in units}
                self.assertTrue(paths)
                labels = {p for key, values in case.items() if key.endswith('_files') for p in values}
                labels.update(case.get('related_of', {}))
                labels.update(p for values in case.get('related_of', {}).values() for p in values)
                self.assertLessEqual(labels, paths)
                if case['id'] in previous:
                    self.assertEqual(case, previous[case['id']])
                    self.assertEqual(data, (old / case['diff']).read_bytes())
                else:
                    self.assertLess(len(data.splitlines()), 150)
                    subprocess.run(['git', 'apply', '--numstat', str(CORPUS / case['diff'])],
                                   check=True, capture_output=True)
        for key in ('confirmed_by', 'confirmed_sha256', 'confirmed_at'):
            self.assertIsNone(MANIFEST[key])
        self.assertFalse((CORPUS / 'results.json').exists())

    def test_historical_corpora_unchanged(self):
        # The recorded evidence pins every old input: the confirmation hash covers the
        # cases and each result row carries the hash of the diff it was scored on.
        for version in ('skim-ko-3', 'skim-ko-4', 'skim-ko-5'):
            corpus = ROOT / 'eval' / version
            manifest = json.loads((corpus / 'manifest.json').read_text())
            if manifest.get('confirmed_sha256'):
                self.assertEqual(manifest['confirmed_sha256'], evaluation.confirmation_hash(manifest), version)
            if not (corpus / 'results.json').exists():
                continue
            recorded = {c['id']: c['diff_sha256'] for c in json.loads((corpus / 'results.json').read_text())['cases']}
            for case in manifest['cases']:
                self.assertEqual(recorded[case['id']], evaluation.sha256((corpus / case['diff']).read_bytes()),
                                 f"{version} {case['id']}")

    def test_compare_cli_writes_ungated_report_outside_corpus(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'comparison.json'
            corpus = Path(tmp) / 'corpus'
            corpus.mkdir()
            case = case_named('fx-auth-rename')
            (corpus / 'manifest.json').write_text(json.dumps(dict(MANIFEST, cases=[case])))
            fake = dict(id=case['id'], **score(case, evidence(case, 'high')))
            argv = ['eval_release.py', '--home', tmp, '--corpus', str(corpus), '--compare', '--out', str(out)]
            with patch.object(sys, 'argv', argv), patch('eval_release.Config.load'), patch(
                    'eval_release.run_case', return_value=(fake, SimpleNamespace(model='fake', cli_version='fake'))):
                self.assertEqual(evaluation.main(), 0)
            report = json.loads(out.read_text())
            self.assertFalse(report['comparison']['gating'])
            self.assertEqual(report['summary']['failures'], [case['id']])
            self.assertFalse((corpus / 'results.json').exists())
            for target in (corpus / 'results.json', ROOT / 'eval/skim-ko-5/results.json'):
                with patch.object(sys, 'argv', argv[:-1] + [str(target)]), self.assertRaises(SystemExit) as raised:
                    evaluation.main()
                self.assertEqual(raised.exception.code, 2)

    def test_compare_cli_rejects_home_that_writes_under_eval(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            corpus = tmp / 'corpus'
            corpus.mkdir()
            linked = tmp / 'linked-home'
            linked.symlink_to(ROOT / 'eval/skim-ko-5', target_is_directory=True)
            derived = tmp / 'derived-home'
            derived.mkdir()
            (derived / 'config').symlink_to(ROOT / 'eval/skim-ko-5', target_is_directory=True)
            homes = [ROOT / 'eval', ROOT / 'eval/skim-ko-5', ROOT / 'eval/skim-ko-5/nested', linked, derived, corpus]
            for home in homes:
                argv = ['eval_release.py', '--home', str(home), '--corpus', str(corpus), '--compare',
                        '--out', str(tmp / 'comparison.json')]
                with self.subTest(home=home), patch.object(sys, 'argv', argv), patch(
                        'eval_release.Config.load') as load, patch('eval_release.compare') as compare, patch(
                        'sys.stderr'), self.assertRaises(SystemExit) as raised:
                    evaluation.main()
                self.assertEqual(raised.exception.code, 2)
                load.assert_not_called()
                compare.assert_not_called()
            self.assertFalse((ROOT / 'eval/skim-ko-5/config').exists())
            self.assertFalse((ROOT / 'eval/skim-ko-5/nested').exists())
            self.assertFalse((corpus / 'config').exists())
