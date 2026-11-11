"""Deterministic split scoring and corpus checks; no model calls."""
import ast
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
            manifest = dict(MANIFEST, cases=[case], confirmed_by=None, confirmed_sha256=None, confirmed_at=None)
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
        self.assertEqual(MANIFEST['confirmed_by'], 'sangwon')
        self.assertEqual(MANIFEST['confirmed_at'], '2026-11-12')
        self.assertEqual(MANIFEST['confirmed_sha256'], evaluation.confirmation_hash(MANIFEST, 'skim-ko-6'))
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
            config_file = tmp / 'config-file-home'
            (config_file / 'config').mkdir(parents=True)
            (config_file / 'config/config.json').symlink_to(ROOT / 'eval/skim-ko-5/manifest.json')
            missing_file = tmp / 'missing-file-home'
            (missing_file / 'config').mkdir(parents=True)
            (missing_file / 'config/config.json').symlink_to(ROOT / 'eval/skim-ko-5/nested.json')
            cache = tmp / 'cache-home'
            (cache / 'data').mkdir(parents=True)
            (cache / 'data/cache').symlink_to(ROOT / 'eval/skim-ko-5', target_is_directory=True)
            chained = tmp / 'chained-home'
            (tmp / 'elsewhere/cache').mkdir(parents=True)
            (tmp / 'elsewhere/cache/diffs').symlink_to(corpus, target_is_directory=True)
            chained.mkdir()
            (chained / 'data').symlink_to(tmp / 'elsewhere', target_is_directory=True)
            homes = [ROOT / 'eval', ROOT / 'eval/skim-ko-5', ROOT / 'eval/skim-ko-5/nested', linked, derived, corpus,
                     config_file, missing_file, cache, chained]
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
            self.assertFalse((ROOT / 'eval/skim-ko-5/nested.json').exists())

    def test_compare_cli_allows_home_outside_eval(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            corpus = tmp / 'corpus'
            corpus.mkdir()
            home = tmp / 'home'
            (tmp / 'elsewhere').mkdir()
            (home / 'config').mkdir(parents=True)
            (home / 'config/config.json').write_text('{}')
            (home / 'data/cache/diffs').mkdir(parents=True)
            (home / 'data/logs').symlink_to(tmp / 'elsewhere', target_is_directory=True)
            (home / 'data/loop').symlink_to(home / 'data', target_is_directory=True)
            for target in (home, tmp / 'new-home'):
                argv = ['eval_release.py', '--home', str(target), '--corpus', str(corpus), '--compare',
                        '--out', str(tmp / 'comparison.json')]
                with self.subTest(home=target), patch.object(sys, 'argv', argv), patch.dict(
                        'os.environ'), patch('eval_release.Config.load') as load, patch(
                        'eval_release.compare') as compare:
                    self.assertEqual(evaluation.main(), 0)
                    self.assertEqual(evaluation.os.environ['SKIM_HOME'], str(target.resolve()))
                load.assert_called_once_with()
                compare.assert_called_once_with(load.return_value, corpus.resolve(), (tmp / 'comparison.json').resolve())

    def test_auth_rename_fixture_preserves_behavior(self):
        """The low label holds only if the diff renames private and local names and nothing else."""
        before, after = [], []
        for line in (CORPUS / 'diffs/fx-auth-rename.diff').read_text().splitlines():
            if line.startswith(('diff ', 'index ', '--- ', '+++ ', '@@')):
                continue
            if line[:1] != '+':
                before.append(line[1:])
            if line[:1] != '-':
                after.append(line[1:])
        renames = dict(_calc='_expires_at', d='lifetime', sid='session_id', exp='expires_at', rec='record')

        class Rename(ast.NodeTransformer):
            def visit_Name(self, node):
                node.id = renames.get(node.id, node.id)
                return node

            def visit_FunctionDef(self, node):
                node.name = renames.get(node.name, node.name)
                return self.generic_visit(node)

        def shape(lines, rename=False):
            # The last hunk stops at a bare `def` context line; give it a body so the excerpt parses.
            tree = ast.parse('\n'.join(lines + ['    ...']))
            if rename:
                tree = Rename().visit(tree)
            imports = sorted(ast.dump(n) for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom)))
            rest = [ast.dump(n) for n in tree.body if not isinstance(n, (ast.Import, ast.ImportFrom))]
            public = [n.name for n in tree.body if isinstance(n, ast.FunctionDef) and not n.name.startswith('_')]
            signatures = [ast.dump(n.args) for n in tree.body if isinstance(n, ast.FunctionDef)]
            return imports, rest, public, signatures

        self.assertEqual(shape(before, rename=True), shape(after))
        self.assertEqual(shape(before)[2:], shape(after)[2:])
        self.assertEqual(shape(after)[2], ['open_session', 'is_live', 'close_session'])