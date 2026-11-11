"""Synthetic evidence tests the gate only; it never substitutes for model eval."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import check_release
from eval_release import prompt_hash, sha256, SCHEMA_VERSION, SANITIZATION_POLICY, confirmation_hash, assess, summarize, SPLIT_SCORING
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
        # 기본 합성 결과에는 미스가 없으므로 허용 목록은 각 테스트에서 명시적으로 넣는다.
        self.manifest.pop('accepted_misses', None)
        self.manifest['confirmed_by'] = 'test reviewer'
        self.manifest['confirmed_sha256'] = confirmation_hash(self.manifest)
        (self.corpus / 'manifest.json').write_text(json.dumps(self.manifest))
        self.report = dict(prompt_version='skim-ko-5', prompt_sha256=prompt_hash(), schema_version=SCHEMA_VERSION,
                           sanitization_policy=SANITIZATION_POLICY,
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

    def check(self, version='skim-ko-5'):
        (self.corpus / 'results.json').write_text(json.dumps(self.report))
        check_release.check(self.root, version=version)

    def test_block_synthetic_results(self):
        with self.assertRaises(ValueError):
            self.check()

    def realistic(self, mid=None, case_id='fx-multichunk', status='partial', hide=False, version=None):
        """mid({case: 파일 집합})의 파일은 모델이 mid로 판단한 것처럼 만들어 미스를 재현한다.
        status·hide는 case_id에만 적용한다."""
        mid = mid or {}
        for expected, recorded in zip(self.manifest['cases'], self.report['cases']):
            diff = (self.corpus / expected['diff']).read_text()
            units, _ = parse_diff(diff)
            for unit in units:
                unit['status'] = 'unanalysed'
            target = expected['id'] == case_id
            if target and hide:
                units[0].pop('status')
            result = dict(status=status if target else 'partial', units=units, items=[], files=[
                dict(file_path=p, importance='mid' if p in mid.get(expected['id'], ()) else 'high', candidates=[])
                for p in dict.fromkeys(u['file_path'] for u in units)])
            recorded.update(raw_outputs=[dict(stage='chunk', raw='{}'), dict(stage='synthesis', raw='{"intent":"offline fixture"}')],
                            requests=[prompt('chunk', dict(units=units, metadata={})),
                                      prompt('synthesis', dict(metadata={}, items=[], uncertainties=[]))])
            if version in SPLIT_SCORING:
                # 분리 채점은 규칙 후보가 아니라 단위별 모델 판단만 포착으로 센다.
                for unit in units:
                    unit['status'] = 'analyzed'
                result['items'] = [dict(change_id=u['change_id'], importance='high', behavior='changed',
                                        reason='합성 근거', risk_flags=[]) for u in units]
            recorded.update(assess(expected, result, recorded['requests'], diff, version))
        summarize(self.report, version)

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

    def test_historical_binding_keeps_recorded_metadata(self):
        self.realistic()
        self.report.update(prompt_sha256='old-recorded-hash', schema_version='2')
        self.report.pop('sanitization_policy')
        self.check()
        for key in ('prompt_sha256', 'schema_version'):
            original = self.report.pop(key)
            with self.assertRaises(ValueError):
                self.check()
            self.report[key] = original

    def test_hash_mismatch(self):
        version = check_release.PROMPT_VERSION
        self.realistic(version=version)
        target = self.root / 'eval' / version
        self.corpus.rename(target)
        self.corpus = target
        self.manifest['prompt_version'] = version
        self.manifest['confirmed_sha256'] = confirmation_hash(self.manifest)
        (target / 'manifest.json').write_text(json.dumps(self.manifest))
        self.report['prompt_version'] = version
        self.check(version)
        for key, message in (('prompt_sha256', '해시'), ('schema_version', '스키마'),
                             ('sanitization_policy', '정제')):
            original = self.report[key]
            self.report[key] = 'wrong'
            with self.assertRaisesRegex(ValueError, message):
                self.check(version)
            self.report[key] = original

    def test_miss(self):
        self.report['cases'][0]['misses'] = ['lib/skim/db.py']
        with self.assertRaises(ValueError):
            self.check()

    def test_missing_case(self):
        self.report['cases'].pop()
        with self.assertRaises(ValueError):
            self.check()

    def test_supported_versions_reach_corpus_check(self):
        for version in ('skim-ko-3', 'skim-ko-4', 'skim-ko-6'):
            with self.subTest(version=version), self.assertRaises(FileNotFoundError):
                check_release.check(self.root, version=version)
        with self.assertRaises(FileNotFoundError):
            check_release.check(self.root)

    def test_prompt_hash_includes_stage_instructions(self):
        original = prompt_hash()
        with patch.dict('eval_release.STAGE_INSTRUCTIONS', chunk='다른 청크 지시'):
            self.assertNotEqual(prompt_hash(), original)

    def test_other_version(self):
        with self.assertRaises(ValueError):
            check_release.check(self.root, 'unsupported')

    def test_missing_results(self):
        with self.assertRaises(OSError):
            check_release.check(self.root, version='skim-ko-5')

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

    ACCEPTED = dict(case='fx-multichunk', file='app/order_routes.py', reason='알려진 한계',
                    approved_by='nowgnas', date='2026-11-04')

    def accept(self, *entries):
        self.manifest['accepted_misses'] = list(entries)
        (self.corpus / 'manifest.json').write_text(json.dumps(self.manifest))

    ROUTES = {'fx-multichunk': {'app/order_routes.py'}}

    def test_accepted_miss_passes(self):
        self.accept(self.ACCEPTED)
        self.realistic(mid=self.ROUTES)
        self.assertEqual(self.report['summary'], dict(total_misses=1, hidden_count=0, failures=['fx-multichunk']))
        self.check()

    def test_unaccepted_or_extra_miss_blocks(self):
        self.accept(self.ACCEPTED)
        for files in ({'app/order_core.py'}, {'app/order_routes.py', 'app/order_core.py'}):
            with self.subTest(files=files):
                self.realistic(mid={'fx-multichunk': files})
                with self.assertRaisesRegex(ValueError, 'fx-multichunk: high 누락 또는 허용 목록 불일치'):
                    self.check()

    def test_miss_in_other_case_blocks(self):
        # 허용된 미스는 그대로 두고, 같은 파일 경로라도 다른 case의 미스는 허용하지 않는다.
        self.accept(self.ACCEPTED)
        self.realistic(mid=dict(self.ROUTES, **{'fx-pr-purpose': {'infra/deployment.yaml'}}))
        with self.assertRaisesRegex(ValueError, 'fx-pr-purpose: high 누락'):
            self.check()

    def test_stale_accepted_miss_blocks(self):
        self.accept(self.ACCEPTED)
        self.realistic()
        with self.assertRaisesRegex(ValueError, 'fx-multichunk: high 누락 또는 허용 목록 불일치'):
            self.check()

    def test_invalid_accepted_entries_block(self):
        self.realistic(mid=self.ROUTES)
        invalid = [dict(self.ACCEPTED, **{key: value}) for key in self.ACCEPTED for value in (None, '', ' ', 1)]
        invalid += [{k: v for k, v in self.ACCEPTED.items() if k != key} for key in self.ACCEPTED]
        invalid += [dict(self.ACCEPTED, date=d) for d in ('2026-13-01', '2026-11-4', '20261104', '2026-11-04T00:00')]
        invalid += [dict(self.ACCEPTED, file='app/other.py'), dict(self.ACCEPTED, case='nope'),
                    dict(self.ACCEPTED, file='lib/skim/db.py')]
        for entry in invalid:
            with self.subTest(entry=entry):
                self.accept(entry)
                with self.assertRaisesRegex(ValueError, 'accepted_misses'):
                    self.check()
        self.accept(self.ACCEPTED, self.ACCEPTED)
        with self.assertRaisesRegex(ValueError, 'accepted_misses 중복'):
            self.check()
        self.manifest['accepted_misses'] = self.ACCEPTED
        (self.corpus / 'manifest.json').write_text(json.dumps(self.manifest))
        with self.assertRaisesRegex(ValueError, 'accepted_misses'):
            self.check()

    def test_accepted_case_still_requires_status_and_visibility(self):
        self.accept(self.ACCEPTED)
        for kwargs, error in ((dict(status='failed'), '분석 실패'), (dict(hide=True), '미분석 숨김')):
            with self.subTest(**kwargs):
                self.realistic(mid=self.ROUTES, **kwargs)
                with self.assertRaisesRegex(ValueError, f'fx-multichunk: .*{error}'):
                    self.check()

    def test_miss_without_accepted_list_blocks(self):
        for entries in (None, []):
            with self.subTest(entries=entries):
                if entries is None:
                    self.manifest.pop('accepted_misses', None)
                    (self.corpus / 'manifest.json').write_text(json.dumps(self.manifest))
                else:
                    self.accept(*entries)
                self.realistic(mid=self.ROUTES)
                with self.assertRaisesRegex(ValueError, 'high 누락'):
                    self.check()

    def test_accepted_miss_does_not_allow_forged_summary(self):
        self.accept(self.ACCEPTED)
        self.realistic(mid=self.ROUTES)
        original = copy.deepcopy(self.report)
        for change in (dict(failures=[]), dict(total_misses=0), dict(total_misses=0, failures=[])):
            with self.subTest(change=change):
                self.report = copy.deepcopy(original)
                self.report['summary'].update(change)
                with self.assertRaisesRegex(ValueError, '평가 요약 불일치'):
                    self.check()

    def test_accepted_miss_does_not_mask_other_case_failures(self):
        self.accept(self.ACCEPTED)
        for case_id, error in (('pr2', '분석 실패'), ('fx-secret', 'expect_redacted 기대 불충족')):
            with self.subTest(case=case_id):
                self.realistic(mid=self.ROUTES)
                expected = next(c for c in self.manifest['cases'] if c['id'] == case_id)
                case = next(c for c in self.report['cases'] if c['id'] == case_id)
                diff = (self.corpus / expected['diff']).read_text()
                if case_id == 'pr2':
                    case['displayed']['status'] = 'failed'
                else:
                    case['requests'] = [diff]
                # 점수와 요약을 다시 계산해 위조가 아닌 실제 실패로 만든다.
                case.update(assess(expected, case['displayed'], case['requests'], diff))
                summarize(self.report)
                with self.assertRaisesRegex(ValueError, f'{case_id}: {error}'):
                    self.check()
