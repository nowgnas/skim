#!/usr/bin/env python3
"""Evaluate the release corpus, or rescore recorded evidence offline."""
import argparse
import copy
from dataclasses import replace
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'lib'))
from skim.analyze import Analyzer
from skim.analyze.codex import CodexAdapter
from skim.analyze.protocol import PROMPT_VERSION, SCHEMA_VERSION, SYSTEM, STAGE_INSTRUCTIONS, encode
from skim.analyze.units import SANITIZATION_POLICY
from skim.analyze.units import parse_diff, SECRET_VALUE, SECRET_ASSIGNMENT, sanitize
from skim.config import Config, private_dir, private_file
from skim.db import connect
from skim.web import file_rows, first_look, foldable, top_file_rows


def sha256(value):
    return hashlib.sha256(value).hexdigest()


def prompt_hash():
    return sha256(encode(dict(system=SYSTEM, stages=STAGE_INSTRUCTIONS)).encode('utf-8'))


# Corpora scored with split metrics: model importance, signal exposure and grouping are
# judged separately, and a rule candidate or floor never stands in for a model high.
SPLIT_SCORING = ('skim-ko-6',)
# Per-case lists that fail a split-scored case when non-empty.
SPLIT_FAILURES = ('false_high', 'location_hint_missing', 'location_hint_promoted', 'signal_hidden',
                  'unexpected_floor', 'mechanical_folded')


def confirmation_hash(manifest, version=None):
    """Bind human confirmation to the complete case definitions, including high_files.

    Split-scored corpora also bind the capture rule text, since the labels mean
    something different once that policy changes.
    """
    version = manifest.get('prompt_version') if version is None else version
    confirmed = (dict(cases=manifest['cases'], rule=manifest['rule'])
                 if version in SPLIT_SCORING else manifest['cases'])
    return sha256(json.dumps(confirmed, sort_keys=True, ensure_ascii=False,
                             separators=(',', ':')).encode('utf-8'))


def saved_requests(recorded):
    requests = recorded.get('requests')
    if not isinstance(requests, list) or not requests or any(
            not isinstance(r, str) or not r.strip() for r in requests):
        raise ValueError(f"{recorded['id']}: saved requests missing")
    return requests


def split_scores(case, rows, top):
    """Score one case under the split policy from the same rows the screen renders."""
    by_path = {row['path']: row for row in rows}
    top_paths = [row['path'] for row in top]
    safety_paths = [row['path'] for row in first_look(rows)['safety']]

    def is_high(path):
        return path in by_path and by_path[path]['rank'] == 3

    def exposure(path, key):
        where = 'top' if path in top_paths else 'safety' if path in safety_paths else None
        return dict(signal=bool(path in by_path and by_path[path][key]), where=where)

    captured = {path: is_high(path) for path in case['high_files']}
    hinted = case.get('location_hint_files', [])
    kinds = {path: exposure(path, 'kind_signals') for path in case.get('kind_signal_files', [])}
    floors = {path: exposure(path, 'hard_floors') for path in case.get('floor_files', [])}
    expected = set(case['high_files']) | set(case.get('core_files', []))
    top3 = top_paths[:3]
    matched = len(expected.intersection(top3))
    core_hit = {path: path in by_path and by_path[path]['role'] == 'core' for path in case.get('core_files', [])}
    related_hit = {core: {path: core in by_path and any(entry['path'] == path for entry in by_path[core]['related'])
                          for path in related}
                   for core, related in case.get('related_of', {}).items()}
    metrics = dict(
        model_high_files=sorted(path for path in by_path if is_high(path)),
        top3=top3, top3_precision=matched / len(top3) if top3 else None,
        top3_recall=matched / len(expected) if expected else None,
        # Precision and recall use three entries even when the UI exposes more highs.
        top3_recall_ceiling=min(1.0, 3 / len(expected)) if expected else None,
        kind_signal_exposure=kinds, floor_exposure=floors, core_hit=core_hit, related_hit=related_hit)
    return dict(
        metrics=metrics, captured=captured, misses=[path for path, ok in captured.items() if not ok],
        false_high=[path for path in case.get('not_high_files', []) if is_high(path)],
        location_hint_missing=[path for path in hinted
                               if not (path in by_path and by_path[path]['location_hints'])],
        location_hint_promoted=[path for path in hinted if path in top_paths
                                and not is_high(path) and by_path[path]['role'] != 'core'],
        signal_hidden=sorted({path for signals in (kinds, floors) for path, seen in signals.items()
                              if not (seen['signal'] and seen['where'])}),
        unexpected_floor=[path for path in case.get('no_floor_files', [])
                          if path in by_path and by_path[path]['hard_floors']],
        mechanical_folded=[path for path in case.get('not_mechanical_files', []) if path in by_path
                           and (by_path[path]['role'] == 'mechanical' or foldable(by_path[path]))])


def assess(case, result, requests, diff, version=None):
    units = result.get('units', [])
    items = {i['change_id']: i for i in result.get('items', [])}
    files = []
    for file in result.get('files', []):
        flags = sorted({flag for u in units if u['file_path'] == file['file_path']
                        for flag in items.get(u['change_id'], {}).get('risk_flags', [])})
        files.append(dict(file, risk_flags=flags))
    captured = {path: any(f['file_path'] == path and
                          (f['importance'] == 'high' or f['candidates']) for f in files)
                for path in case['high_files']}
    visible_paths = {f['file_path'] for f in files}
    hidden = [u['change_id'] for u in units if
              u['file_path'] not in visible_paths or
              u.get('status') not in ('analyzed', 'excluded', 'omitted', 'unanalysed') or
              (u.get('status') == 'analyzed') != (u['change_id'] in items)]
    expectations = {}
    if case.get('expect_redacted'):
        secrets = [m.group() for pattern in (SECRET_VALUE, SECRET_ASSIGNMENT)
                   for m in pattern.finditer(diff)]
        # Assignment matches include names; also check each original assigned value.
        values = [s.split('=', 1)[-1].split(':', 1)[-1].strip().strip('\"\'') for s in secrets]
        sent = '\n'.join(requests)
        expectations['expect_redacted'] = dict(
            met=bool(values) and all(v not in sent for v in values if v) and
                any(u.get('redacted') for u in units),
            secret_absent=bool(values) and all(v not in sent for v in values if v),
            unit_marked_redacted=any(u.get('redacted') for u in units))
    if case.get('expect_unanalysed_visible'):
        source_units, _ = parse_diff(diff)
        relevant = [u for u in source_units if u['kind'] == 'synthetic']
        visible_paths = {f['file_path'] for f in files}
        expectations['expect_unanalysed_visible'] = dict(
            met=bool(relevant) and all(
                u['file_path'] in visible_paths and any(
                    shown['file_path'] == u['file_path'] and
                    shown.get('kind') == 'synthetic' and
                    (shown['change_id'] in items or shown.get('status') in
                     ('excluded', 'omitted', 'unanalysed'))
                    for shown in units) for u in relevant))
    if 'watch' in case:
        watch = case['watch']
        present = any(f['file_path'] == watch['file'] and
                      watch['model_flag_absent'] in f['risk_flags'] for f in files)
        expectations['watch'] = dict(watch, model_flag_present=present, met=not present)
    rows = file_rows(dict(units=units, items=items, result=result))
    top = top_file_rows(rows)
    top3 = [row['path'] for row in top[:3]]
    expected = set(case['high_files'])
    matched = len(expected.intersection(top3))
    metrics = dict(model_high_files=sorted(f['file_path'] for f in files if f.get('importance') == 'high'),
                   rule_candidate_files=sorted(f['file_path'] for f in files if f.get('candidates')),
                   top3=top3, top3_precision=matched / len(top3) if top3 else None,
                   top3_recall=matched / len(expected) if expected else None,
                   intent=result.get('intent'), intent_review='human_review_required')
    scores = dict(displayed=dict(result, files=files), metrics=metrics, captured=captured,
                  misses=[p for p, ok in captured.items() if not ok],
                  hidden_unanalysed=hidden, expectations=expectations)
    if version in SPLIT_SCORING:
        split = split_scores(case, rows, top)
        scores.update(split, metrics=dict(metrics, **split['metrics']))
    return scores


def run_case(config, case, corpus, version=None):
    diff = (corpus / case['diff']).read_text(encoding='utf-8')
    digest = sha256(diff.encode())
    adapter = CodexAdapter(data_dir=config.data_dir, model=config.resolved_codex_model())
    outputs, requests = [], []
    generate = adapter.generate

    def record(request, schema):
        requests.append(request)
        entry = dict(stage=None, raw=None)
        outputs.append(entry)
        try:
            entry['stage'] = json.loads(request.split('\n', 1)[1])['stage']
            entry['raw'] = generate(request, schema)
            return entry['raw']
        except Exception as exc:
            entry['error'] = type(exc).__name__
            raise

    adapter.generate = record
    with tempfile.TemporaryDirectory(prefix='release-eval-', dir=config.config_dir.parent) as temporary:
        db_config = replace(config, data_dir=Path(temporary))
        conn = connect(db_config)
        try:
            units, _ = parse_diff(diff)
            files = [dict(filename=p, status='modified') for p in dict.fromkeys(u['file_path'] for u in units)]
            title, body = case.get('title', case['id']), case.get('body', '')
            material = dict(title=title, body=body, files=files, diff=diff, policy='raw-v1')
            raw_hash = sha256(json.dumps(material, sort_keys=True, ensure_ascii=False).encode())
            cache = private_dir(config.data_dir / 'cache' / 'diffs')
            path = cache / (digest + '.diff')
            private_file(path)
            path.write_text(diff, encoding='utf-8')
            coverage = dict(files=files, total_files=len(files), status='collected', policy='raw-v1')
            with conn:
                conn.execute("INSERT INTO repositories(owner,name,analysis_policy) VALUES('release','eval','allow')")
                conn.execute("UPDATE github_hosts SET analysis_scope='selected' WHERE host='github.com'")
                conn.execute("INSERT INTO pull_requests(repo_id,number,title,body,base_sha,head_sha) VALUES(1,1,?,?,'base','head')", (title, body))
                conn.execute('INSERT INTO snapshots(pr_id,base_sha,head_sha,diff_hash,input_hash,diff_path,coverage) VALUES(1,?,?,?,?,?,?)',
                             ('base', 'head', digest, raw_hash, str(path), json.dumps(coverage)))
            row = dict(conn.execute('SELECT s.*,p.title,p.body FROM snapshots s JOIN pull_requests p ON p.id=s.pr_id').fetchone())
            analyzer = Analyzer(conn, config, adapter)
            try:
                result = analyzer._analyze(row)
            except Exception as exc:
                result = dict(status='failed', error=type(exc).__name__, units=[], items=[], files=[])
        finally:
            conn.close()
    return dict(id=case['id'], diff_sha256=digest, raw_outputs=outputs, requests=requests,
                model=adapter.model, cli_version=adapter.cli_version,
                **assess(case, result, requests, diff, version)), adapter


def summarize(report, version=None):
    version = report.get('prompt_version') if version is None else version
    split = SPLIT_FAILURES if version in SPLIT_SCORING else ()
    failures = [c['id'] for c in report['cases'] if c['displayed']['status'] not in ('completed', 'partial')
                or c['misses'] or c['hidden_unanalysed'] or any(not e['met'] for e in c['expectations'].values())
                or any(c[key] for key in split)]
    report['summary'] = dict(total_misses=sum(len(c['misses']) for c in report['cases']),
                             hidden_count=sum(len(c['hidden_unanalysed']) for c in report['cases']), failures=failures,
                             **{key: sum(len(c[key]) for c in report['cases']) for key in split})
    return failures


SUPPORTED_VERSIONS = ('skim-ko-3', 'skim-ko-4', 'skim-ko-5', 'skim-ko-6')


def validate_binding(report, manifest, version):
    if version not in SUPPORTED_VERSIONS:
        raise ValueError('지원하지 않는 프롬프트 버전')
    if manifest.get('prompt_version') != version or report.get('prompt_version') != version:
        raise ValueError('프롬프트 버전 불일치')
    for key in ('prompt_sha256', 'schema_version'):
        if not isinstance(report.get(key), str) or not report[key].strip():
            raise ValueError(f'{key} 평가 근거 누락')
    if version == PROMPT_VERSION:
        if report['prompt_sha256'] != prompt_hash():
            raise ValueError('프롬프트 해시 불일치')
        if report['schema_version'] != SCHEMA_VERSION:
            raise ValueError('스키마 버전 불일치')


def rescore(report, manifest, corpus, version=None):
    """Recompute scores without invoking an adapter or changing recorded evidence."""
    version = version if version is not None else report.get('prompt_version')
    validate_binding(report, manifest, version)
    cases = {c['id']: c for c in report['cases']}
    expected_ids = {c['id'] for c in manifest['cases']}
    if len(cases) != len(report['cases']) or len(expected_ids) != len(manifest['cases']) or set(cases) != expected_ids:
        raise ValueError('evaluation cases missing or duplicated')
    # Validate every input before updating any scores.
    diffs = {}
    for expected in manifest['cases']:
        data = (corpus / expected['diff']).read_bytes()
        if cases[expected['id']]['diff_sha256'] != sha256(data):
            raise ValueError(f"{expected['id']}: diff sha256 mismatch")
        diffs[expected['id']] = data.decode('utf-8')
    updated = copy.deepcopy(report)
    for recorded in updated['cases']:
        expected = next(c for c in manifest['cases'] if c['id'] == recorded['id'])
        diff = diffs[recorded['id']]
        requests = saved_requests(recorded)
        scores = assess(expected, recorded['displayed'], requests, diff, version)
        recorded.update({k: v for k, v in scores.items() if k != 'displayed'})
    updated['reviewed_by'] = None
    summarize(updated, version)
    return updated


def compare(config, corpus, out):
    """Run the current analyzer over another corpus and write an ungated side-by-side report."""
    manifest = json.loads((corpus / 'manifest.json').read_text())
    version = manifest['prompt_version']
    recorded = corpus / 'results.json'
    baseline = {c['id']: c for c in json.loads(recorded.read_text())['cases']} if recorded.exists() else {}
    report = dict(prompt_version=PROMPT_VERSION, prompt_sha256=prompt_hash(), schema_version=SCHEMA_VERSION,
                  sanitization_policy=SANITIZATION_POLICY, cli_version='unknown', model='unknown',
                  run_timestamp=datetime.now().astimezone().isoformat(),
                  comparison=dict(corpus=corpus.name, corpus_prompt_version=version, gating=False), cases=[])
    for case in manifest['cases']:
        print(f"Comparing {case['id']}", file=sys.stderr, flush=True)
        result, adapter = run_case(config, case, corpus, version)
        # Old corpora accept a rule candidate as a capture, so also show what the model alone caught.
        high = result['metrics']['model_high_files']
        result['model_only_misses'] = [path for path in case['high_files'] if path not in high]
        before = baseline.get(case['id'])
        if before:
            result['baseline'] = dict(misses=before['misses'], **{
                key: before.get('metrics', {}).get(key) for key in ('model_high_files', 'top3')})
        report['cases'].append(result)
        report.update(cli_version=adapter.cli_version, model=adapter.model)
    summarize(report, version)
    out.parent.mkdir(parents=True, exist_ok=True)
    private_file(out)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')


def writes_into_corpus(home, source):
    """Config.load() creates config/ and data/ under home, so check where those really land."""
    protected = ((ROOT / 'eval').resolve(), source)
    targets = [home] + [(home / name).resolve() for name in ('config', 'data')]
    return any(target == root or root in target.parents for target in targets for root in protected)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', type=Path)
    parser.add_argument('--version', default=PROMPT_VERSION, choices=SUPPORTED_VERSIONS)
    parser.add_argument('--out', type=Path)
    parser.add_argument('--rescore', action='store_true', help='rescore existing results without model calls')
    parser.add_argument('--review', metavar='NAME', help='stamp human review without model calls')
    parser.add_argument('--corpus', type=Path, help='corpus directory to run with --compare, e.g. eval/skim-ko-5')
    parser.add_argument('--compare', action='store_true',
                        help='run the current analyzer on --corpus and write an ungated report to --out')
    args = parser.parse_args()
    if args.compare or args.corpus:
        if not (args.compare and args.corpus and args.out and args.home) or args.rescore or args.review is not None:
            parser.error('--compare needs --corpus, --out and --home, and cannot combine with --rescore or --review')
        source, out = args.corpus.resolve(), args.out.resolve()
        if (ROOT / 'eval').resolve() in out.parents or source in out.parents or out == source:
            parser.error('--compare never writes inside an evaluation corpus; choose another --out')
        home = args.home.expanduser().resolve()
        if writes_into_corpus(home, source):
            parser.error('--compare never writes inside an evaluation corpus; choose another --home')
        os.environ['SKIM_HOME'] = str(home)
        compare(Config.load(), source, out)
        return 0
    corpus = ROOT / 'eval' / args.version
    if args.out is None:
        args.out = corpus / 'results.json'
    if args.review is not None:
        if not args.review.strip() or args.rescore:
            parser.error('--review requires a non-empty name and cannot combine with --rescore')
        report = json.loads(args.out.read_text())
        report['reviewed_by'] = args.review.strip()
        args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
        return 0
    if args.home:
        os.environ['SKIM_HOME'] = str(args.home.expanduser().resolve())
    elif not args.rescore:
        parser.error('--home is required for model evaluation')
    manifest = json.loads((corpus / 'manifest.json').read_text())
    if manifest['prompt_version'] != args.version:
        parser.error('unsupported release prompt version')
    if args.rescore:
        try:
            report = rescore(json.loads(args.out.read_text()), manifest, corpus, args.version)
        except (OSError, ValueError, KeyError) as exc:
            parser.error(str(exc))
        args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
        return int(bool(report['summary']['failures']))
    if args.version != PROMPT_VERSION:
        parser.error('historical versions require --rescore or --review')
    config = Config.load()
    report = dict(prompt_version=PROMPT_VERSION, prompt_sha256=prompt_hash(),
                  schema_version=SCHEMA_VERSION, sanitization_policy=SANITIZATION_POLICY, cli_version='unknown', model='unknown',
                  run_timestamp=datetime.now().astimezone().isoformat(), reviewed_by=None, cases=[])
    for case in manifest['cases']:
        print(f"Evaluating {case['id']}", file=sys.stderr, flush=True)
        result, adapter = run_case(config, case, corpus, args.version)
        report['cases'].append(result)
        report.update(cli_version=adapter.cli_version, model=adapter.model)
    failures = summarize(report, args.version)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    private_file(args.out)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    return int(bool(failures))


if __name__ == '__main__':
    raise SystemExit(main())
