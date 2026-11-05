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
from skim.analyze.protocol import PROMPT_VERSION, SCHEMA_VERSION, SYSTEM
from skim.analyze.units import SANITIZATION_POLICY
from skim.analyze.units import parse_diff, SECRET_VALUE, SECRET_ASSIGNMENT, sanitize
from skim.config import Config, private_dir, private_file
from skim.db import connect
from skim.web import file_rows, top_file_rows


def sha256(value):
    return hashlib.sha256(value).hexdigest()


def prompt_hash():
    return sha256(SYSTEM.encode('utf-8'))


def confirmation_hash(manifest):
    """Bind human confirmation to the complete case definitions, including high_files."""
    return sha256(json.dumps(manifest['cases'], sort_keys=True, ensure_ascii=False,
                             separators=(',', ':')).encode('utf-8'))


def saved_requests(recorded):
    requests = recorded.get('requests')
    if not isinstance(requests, list) or not requests or any(
            not isinstance(r, str) or not r.strip() for r in requests):
        raise ValueError(f"{recorded['id']}: saved requests missing")
    return requests


def assess(case, result, requests, diff):
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
    top3 = [row['path'] for row in top_file_rows(rows)]
    expected = set(case['high_files'])
    matched = len(expected.intersection(top3))
    metrics = dict(model_high_files=sorted(f['file_path'] for f in files if f.get('importance') == 'high'),
                   rule_candidate_files=sorted(f['file_path'] for f in files if f.get('candidates')),
                   top3=top3, top3_precision=matched / len(top3) if top3 else None,
                   top3_recall=matched / len(expected) if expected else None,
                   intent=result.get('intent'), intent_review='human_review_required')
    return dict(displayed=dict(result, files=files), metrics=metrics, captured=captured,
                misses=[p for p, ok in captured.items() if not ok],
                hidden_unanalysed=hidden, expectations=expectations)


def run_case(config, case, corpus):
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
                **assess(case, result, requests, diff)), adapter


def summarize(report):
    failures = [c['id'] for c in report['cases'] if c['displayed']['status'] not in ('completed', 'partial')
                or c['misses'] or c['hidden_unanalysed'] or any(not e['met'] for e in c['expectations'].values())]
    report['summary'] = dict(total_misses=sum(len(c['misses']) for c in report['cases']),
                             hidden_count=sum(len(c['hidden_unanalysed']) for c in report['cases']), failures=failures)
    return failures


def rescore(report, manifest, corpus):
    """Recompute scores without invoking an adapter or changing recorded evidence."""
    if report['prompt_sha256'] != prompt_hash() or report['schema_version'] != SCHEMA_VERSION or report['prompt_version'] != PROMPT_VERSION:
        raise ValueError('prompt or schema mismatch')
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
        scores = assess(expected, recorded['displayed'], requests, diff)
        recorded.update({k: v for k, v in scores.items() if k != 'displayed'})
    updated['reviewed_by'] = None
    summarize(updated)
    return updated


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', type=Path)
    parser.add_argument('--out', type=Path, default=ROOT / 'eval' / PROMPT_VERSION / 'results.json')
    parser.add_argument('--rescore', action='store_true', help='rescore existing results without model calls')
    parser.add_argument('--review', metavar='NAME', help='stamp human review without model calls')
    args = parser.parse_args()
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
    corpus = ROOT / 'eval' / PROMPT_VERSION
    manifest = json.loads((corpus / 'manifest.json').read_text())
    if PROMPT_VERSION not in ('skim-ko-3', 'skim-ko-4', 'skim-ko-5') or manifest['prompt_version'] != PROMPT_VERSION:
        parser.error('unsupported release prompt version')
    if args.rescore:
        try:
            report = rescore(json.loads(args.out.read_text()), manifest, corpus)
        except (OSError, ValueError, KeyError) as exc:
            parser.error(str(exc))
        args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
        return int(bool(report['summary']['failures']))
    config = Config.load()
    report = dict(prompt_version=PROMPT_VERSION, prompt_sha256=prompt_hash(),
                  schema_version=SCHEMA_VERSION, sanitization_policy=SANITIZATION_POLICY, cli_version='unknown', model='unknown',
                  run_timestamp=datetime.now().astimezone().isoformat(), reviewed_by=None, cases=[])
    for case in manifest['cases']:
        print(f"Evaluating {case['id']}", file=sys.stderr, flush=True)
        result, adapter = run_case(config, case, corpus)
        report['cases'].append(result)
        report.update(cli_version=adapter.cli_version, model=adapter.model)
    failures = summarize(report)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    private_file(args.out)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    return int(bool(failures))


if __name__ == '__main__':
    raise SystemExit(main())
