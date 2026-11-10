#!/usr/bin/env python3
"""Validate the recorded real-model release evaluation for the approved prompt."""
from datetime import date, datetime
import json
from pathlib import Path
import re
import sys

from eval_release import ROOT, PROMPT_VERSION, SCHEMA_VERSION, prompt_hash, sha256, assess, confirmation_hash, saved_requests, summarize, validate_binding
from skim.analyze.units import SANITIZATION_POLICY, parse_diff


def accepted_misses(manifest):
    """사람이 승인한 미스 허용 목록을 검증하고 case별 허용 파일 집합을 돌려준다."""
    entries = manifest.get('accepted_misses', [])
    if not isinstance(entries, list):
        raise ValueError('accepted_misses 형식 오류')
    high_files = {c['id']: c['high_files'] for c in manifest['cases']}
    accepted = {}
    for entry in entries:
        if not isinstance(entry, dict) or any(
                not isinstance(entry.get(key), str) or not entry[key].strip()
                for key in ('case', 'file', 'reason', 'approved_by', 'date')):
            raise ValueError('accepted_misses 필드 누락')
        # fromisoformat은 다른 형식도 받으므로 YYYY-MM-DD 형태를 먼저 확인한다.
        try:
            if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', entry['date']):
                raise ValueError
            date.fromisoformat(entry['date'])
        except ValueError:
            raise ValueError(f"accepted_misses 날짜 형식 오류: {entry['date']}") from None
        if entry['file'] not in high_files.get(entry['case'], ()):
            raise ValueError(f"accepted_misses 대상 아님: {entry['case']} {entry['file']}")
        files = accepted.setdefault(entry['case'], set())
        if entry['file'] in files:
            raise ValueError(f"accepted_misses 중복: {entry['case']} {entry['file']}")
        files.add(entry['file'])
    return accepted


def check(root=ROOT, version=PROMPT_VERSION):
    if version not in ('skim-ko-3', 'skim-ko-4', 'skim-ko-5', 'skim-ko-6'):
        raise ValueError('지원하지 않는 프롬프트 버전')
    corpus = root / 'eval' / version
    manifest = json.loads((corpus / 'manifest.json').read_text())
    if not isinstance(manifest.get('confirmed_by'), str) or not manifest['confirmed_by'].strip():
        raise ValueError('confirmed_by 누락: high_files 사람 확인 필요')
    if manifest.get('confirmed_sha256') != confirmation_hash(manifest):
        raise ValueError('confirmed_sha256 불일치: high_files 재확인 필요')
    accepted = accepted_misses(manifest)
    report = json.loads((corpus / 'results.json').read_text())
    if not isinstance(report.get('reviewed_by'), str) or not report['reviewed_by'].strip():
        raise ValueError('reviewed_by 누락: 결과와 원본 출력 사람 검토 필요')
    validate_binding(report, manifest, version)
    if version == PROMPT_VERSION and report.get('sanitization_policy') != SANITIZATION_POLICY:
        raise ValueError('민감 정보 정제 정책 평가 불일치: 실제 모델 재평가 필요')
    for key in ('model', 'cli_version', 'run_timestamp'):
        if not report.get(key) or report[key] == 'unknown':
            raise ValueError(f'{key} 평가 근거 누락')
    if datetime.fromisoformat(report['run_timestamp']).utcoffset() is None:
        raise ValueError('실행 일시 시간대 누락')
    cases = {c['id']: c for c in report['cases']}
    if len(cases) != len(report['cases']) or set(cases) != {c['id'] for c in manifest['cases']}:
        raise ValueError('평가 사례 누락 또는 중복')
    for expected in manifest['cases']:
        case = cases[expected['id']]
        if case['diff_sha256'] != sha256((corpus / expected['diff']).read_bytes()):
            raise ValueError(f"{case['id']}: diff 해시 불일치")
        diff = (corpus / expected['diff']).read_text(encoding='utf-8')
        outputs = case.get('raw_outputs')
        if not isinstance(outputs, list) or not outputs or not {'chunk', 'synthesis'} <= {o.get('stage') for o in outputs}:
            raise ValueError(f"{case['id']}: raw_outputs stages missing")
        requests = saved_requests(case)
        source, _ = parse_diff(diff)
        units = case['displayed']['units']
        source_ids = {u['change_id']: u['file_path'] for u in source}
        shown_ids = {u['change_id']: u['file_path'] for u in units}
        if len(shown_ids) != len(units) or shown_ids != source_ids:
            raise ValueError(f"{case['id']}: unit set mismatch")
        scores = assess(expected, case['displayed'], requests, diff)
        if any(case.get(key) != value for key, value in scores.items()):
            raise ValueError(f"{case['id']}: stored scores mismatch")
        if case['displayed']['status'] not in ('completed', 'partial'):
            raise ValueError(f"{case['id']}: 분석 실패")
        if case['hidden_unanalysed']:
            raise ValueError(f"{case['id']}: high 누락 또는 미분석 숨김")
        # 미스는 허용 목록과 정확히 일치해야 한다. 허용 외 미스와 실제로 미스가 아닌 허용 항목 모두 차단.
        allowed = accepted.get(case['id'], set())
        if len(case['misses']) != len(set(case['misses'])) or set(case['misses']) != allowed:
            raise ValueError(f"{case['id']}: high 누락 또는 허용 목록 불일치")
        if (case['model'], case['cli_version']) != (report['model'], report['cli_version']):
            raise ValueError(f"{case['id']}: 모델 또는 CLI 버전 불일치")
        files = case['displayed']['files']
        if any(not case['captured'].get(path) or not any(f['file_path'] == path and
                   (f['importance'] == 'high' or f['candidates']) for f in files)
               for path in expected['high_files'] if path not in allowed):
            raise ValueError(f"{case['id']}: 지정 high 미포착")
        for key in ('expect_redacted', 'expect_unanalysed_visible', 'watch'):
            if expected.get(key) and case['expectations'].get(key, {}).get('met') is not True:
                raise ValueError(f"{case['id']}: {key} 기대 불충족")
        if any(e.get('met') is not True for e in case['expectations'].values()):
            raise ValueError(f"{case['id']}: 기대 불충족")
    recomputed = dict(cases=report['cases'])
    summarize(recomputed)
    if recomputed['summary'] != report['summary']:
        raise ValueError('평가 요약 불일치')
    summary = report['summary']
    # 다른 검사를 모두 통과했으므로 실패 case는 허용 미스만으로 실패한 case여야 한다.
    allowed_failures = [c['id'] for c in report['cases'] if c['id'] in accepted]
    if (summary['total_misses'] != sum(len(f) for f in accepted.values()) or summary['hidden_count'] != 0
            or summary['failures'] != allowed_failures):
        raise ValueError('평가 요약 실패')


def main():
    try:
        check()
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        print(f'출시 차단: 실제 모델 회귀 평가 미검증: {exc}', file=sys.stderr)
        return 1
    print(f'출시 평가 통과: {PROMPT_VERSION}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
