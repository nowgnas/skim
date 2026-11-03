#!/usr/bin/env python3
"""Validate the recorded real-model release evaluation for the approved prompt."""
from datetime import datetime
import json
from pathlib import Path
import sys

from eval_release import ROOT, PROMPT_VERSION, SCHEMA_VERSION, prompt_hash, sha256, assess, confirmation_hash, saved_requests, summarize
from skim.analyze.units import parse_diff


def check(root=ROOT, version=PROMPT_VERSION):
    if version != 'skim-ko-5':
        raise ValueError('지원하지 않는 프롬프트 버전')
    corpus = root / 'eval' / version
    manifest = json.loads((corpus / 'manifest.json').read_text())
    if not isinstance(manifest.get('confirmed_by'), str) or not manifest['confirmed_by'].strip():
        raise ValueError('confirmed_by 누락: high_files 사람 확인 필요')
    if manifest.get('confirmed_sha256') != confirmation_hash(manifest):
        raise ValueError('confirmed_sha256 불일치: high_files 재확인 필요')
    report = json.loads((corpus / 'results.json').read_text())
    if not isinstance(report.get('reviewed_by'), str) or not report['reviewed_by'].strip():
        raise ValueError('reviewed_by 누락: 결과와 원본 출력 사람 검토 필요')
    if manifest['prompt_version'] != version or report['prompt_version'] != version:
        raise ValueError('프롬프트 버전 불일치')
    if report['prompt_sha256'] != prompt_hash():
        raise ValueError('프롬프트 해시 불일치')
    if report['schema_version'] != SCHEMA_VERSION:
        raise ValueError('스키마 버전 불일치')
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
        if case['misses'] or case['hidden_unanalysed']:
            raise ValueError(f"{case['id']}: high 누락 또는 미분석 숨김")
        if (case['model'], case['cli_version']) != (report['model'], report['cli_version']):
            raise ValueError(f"{case['id']}: 모델 또는 CLI 버전 불일치")
        files = case['displayed']['files']
        if any(not case['captured'].get(path) or not any(f['file_path'] == path and
                   (f['importance'] == 'high' or f['candidates']) for f in files)
               for path in expected['high_files']):
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
    if summary['total_misses'] != 0 or summary['hidden_count'] != 0 or summary['failures']:
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
