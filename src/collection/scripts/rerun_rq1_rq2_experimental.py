"""Isolated detector rerun on frozen RQ1 snapshots and final RQ2 run cohort."""
from __future__ import annotations

import sys as _smell_sys
from pathlib import Path as _SmellPath
_SMELL_PROJECT_ROOT = str(_SmellPath(__file__).resolve().parents[1])
if _SMELL_PROJECT_ROOT not in _smell_sys.path:
    _smell_sys.path.insert(0, _SMELL_PROJECT_ROOT)
from src.smell_names import loads_smell_json


import argparse
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import traceback

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.prepare_canonical_experiment_inputs import make_scan_record, build_repo_info


def rows(path):
    with path.open(encoding='utf-8') as f:
        for line in f:
            if line.strip():
                yield loads_smell_json(line)


def dump(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


def write_row(f, row):
    f.write(json.dumps(row, ensure_ascii=False) + '\n')


def hash_matches(path, expected):
    data = path.read_bytes()
    # Collection on Windows translated LF once, including API text already in CRLF.
    variants = (data, data.replace(b'\r\n', b'\n'), path.read_text(encoding='utf-8').encode('utf-8'))
    return any(hashlib.sha256(value).hexdigest() == expected for value in variants)


def _scan(task):
    from src.detectors.smell.cache_smell_detector import CacheSmellDetector
    key, content, workflow_path = task
    if not content:
        return key, dict(has_smell=False, smells=[], smells_by_type={}, metadata={}, error='Frozen content unavailable; excluded, never treated as smell-free')
    if not hash_matches(Path(content), key[0]):
        return key, dict(has_smell=False, smells=[], smells_by_type={}, metadata={}, error=f'Content hash mismatch: {content}')
    result = CacheSmellDetector().detect_from_file(Path(content), workflow_path=workflow_path)
    value = result.to_dict()
    value['smells_by_type'] = dict(Counter(s['type'] for s in value.get('smells', [])))
    return key, value


def scan(task):
    try:
        return _scan(task)
    except Exception:
        return task[0], dict(has_smell=False, smells=[], smells_by_type={}, metadata={}, error=traceback.format_exc())


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output-dir', type=Path, required=True)
    p.add_argument('--workers', type=int, default=6)
    p.add_argument('--bootstrap', type=int, default=1000)
    p.add_argument('--resume-scan', action='store_true', help='Resume this experimental directory after an interrupted scan')
    args = p.parse_args()
    out = args.output_dir.resolve()
    if out.exists() and not args.resume_scan:
        raise FileExistsError(f'Refusing to overwrite {out}')
    if args.resume_scan and (out / 'analysis_inputs').exists():
        raise ValueError('Scan resume is only allowed before analysis inputs have been written')
    out.mkdir(parents=True, exist_ok=args.resume_scan)
    canonical = ROOT / 'data_26_6_9_canonical_20260609'
    frozen = ROOT / 'output_canonical_rq2_phase4_final_model_input_20260920'
    source_hashes = {}
    for folder in ('src', 'scripts'):
        for path in (ROOT / folder).rglob('*.py'):
            relative = path.relative_to(ROOT)
            source_hashes[str(relative)] = hashlib.sha256(path.read_bytes()).hexdigest()
            target = out / 'source_snapshot' / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
    manifest = dict(status='running', experimental=True, started_at=datetime.now(timezone.utc).isoformat(),
                    canonical_input=str(canonical), rq2_frozen_input=str(frozen),
                    cutoff='2026-06-09T23:59:59Z', window_days=30,
                    bootstrap=args.bootstrap, seed=42, weighting='version',
                    source_sha256=source_hashes, stages=[], rq3_executed=False,
                    note='Original canonical cohorts and run intervals retained; all detector states recomputed. Additional candidate smell types excluded by existing 11-smell analysis taxonomy.')
    dump(out / 'experiment_manifest.json', manifest)
    try:
        snapshots = {r['workflow_key']: r for r in rows(canonical / 'workflow_snapshots.jsonl')}
        cohort = list(rows(canonical / 'rq1_cohort.jsonl'))
        model_scans = list(rows(frozen / 'version_scan_results.jsonl'))
        def content_path(s):
            direct = canonical / (s.get('content_file') or ('content/' + s['content_hash'] + '.yml'))
            if direct.is_file():
                return str(direct)
            # Recover only byte-equivalent frozen content from local collection files.
            repo = s.get('canonical_name') or s.get('repository_name')
            folder = ROOT / 'data_26_6_9_complete_20260609' / 'workflows' / repo
            stem = Path(s['workflow_path']).stem
            for candidate in folder.glob(stem + '*'):
                if candidate.is_file() and hash_matches(candidate, s['content_hash']):
                    return str(candidate)
            return None
        tasks = {}
        for r in cohort:
            s = snapshots[r['workflow_key']]
            assert s['content_hash'] == r['content_hash']
            tasks[(s['content_hash'], s['workflow_path'])] = content_path(s)
        for r in model_scans:
            tasks[(r['content_hash'], r['workflow_path'])] = content_path(r)
        print(f'Scanning {len(tasks)} unique content/path pairs for {len(cohort)} RQ1 workflows', flush=True)
        states = {}
        state_file = out / 'detector_states.jsonl'
        if args.resume_scan and state_file.exists():
            for r in rows(state_file):
                states[(r['content_hash'], r['workflow_path'])] = r['detector']
        with state_file.open('a' if args.resume_scan else 'w', encoding='utf-8') as f, ProcessPoolExecutor(max_workers=args.workers) as pool:
            work = ((key, content, key[1]) for key, content in tasks.items() if key not in states)
            for n, (key, state) in enumerate(pool.map(scan, work, chunksize=30), 1):
                states[key] = state
                write_row(f, dict(content_hash=key[0], workflow_path=key[1], detector=state))
                if n % 2000 == 0:
                    print(f'Scanned {n}/{len(tasks)}', flush=True)
        errors = [dict(content_hash=k[0], workflow_path=k[1], error=s['error']) for k,s in states.items() if s.get('error')]
        dump(out / 'detector_errors.json', errors)
        manifest['scan_count'] = len(states)
        manifest['detector_error_count'] = len(errors)
        views = out / 'analysis_inputs'
        views.mkdir()
        repositories = {r['repository_id']: r for r in rows(canonical / 'repositories.jsonl')}
        with (views / 'repo_info.jsonl').open('w', encoding='utf-8') as f:
            for r in build_repo_info(canonical, repositories):
                write_row(f, r)
        with (views / 'rq1_scan_direct.jsonl').open('w', encoding='utf-8') as direct, (views / 'smell_scan_results.jsonl').open('w', encoding='utf-8') as wrapped:
            for r in cohort:
                s = snapshots[r['workflow_key']]
                record = make_scan_record(canonical, s, states[(s['content_hash'], s['workflow_path'])])
                record['yml_file'] = tasks[(s['content_hash'], s['workflow_path'])] or ''
                write_row(direct, record)
                write_row(wrapped, {r['workflow_key']: record})
        model = out / 'rq2_model_input'
        model.mkdir()
        print('Copying frozen RQ2 run and version records into isolated output', flush=True)
        for name in ('run_records.jsonl', 'version_records.jsonl', 'matching_metadata.json'):
            shutil.copy2(frozen / name, model / name)
        detector_version = 'experimental-source-sha256:' + hashlib.sha256(json.dumps(source_hashes, sort_keys=True).encode()).hexdigest()
        with (model / 'version_scan_results.jsonl').open('w', encoding='utf-8') as f:
            for r in model_scans:
                r['detector'] = states[(r['content_hash'], r['workflow_path'])]
                r['detector_version'] = detector_version
                r['detector_state_key'] = f"{r['content_hash']}|{r['workflow_path']}|{detector_version}"
                write_row(f, r)
        # Update exposure provenance in the copied version records, retaining every run and boundary.
        with (model / 'version_records.jsonl').open('w', encoding='utf-8') as f:
            for r in rows(frozen / 'version_records.jsonl'):
                r['detector_version'] = detector_version
                r['detector_state_key'] = f"{r['content_hash']}|{r['workflow_path']}|{detector_version}"
                write_row(f, r)
        metadata = loads_smell_json((model / 'matching_metadata.json').read_text(encoding='utf-8'))
        metadata.update(experimental=True, detector_version=detector_version, source_frozen_input=str(frozen))
        metadata['detector_versions'] = [detector_version]
        dump(model / 'matching_metadata.json', metadata)
        stages = [
            ('rq1_prevalence', ['scripts/smell_prevalence_new.py', '--data-dir', str(views), '--smell-scan-dir', str(views), '--output-dir', str(out / 'rq1')]),
            ('rq1_context', ['scripts/analyze_rq1_context_distributions.py', '--data-dir', str(views), '--scan-path', str(views / 'rq1_scan_direct.jsonl'), '--output-dir', str(out / 'rq1')]),
            ('rq2_regression', ['scripts/analyze_rq2_run_level.py', '--input-dir', str(model), '--output-dir', str(out / 'rq2'), '--bootstrap', str(args.bootstrap), '--seed', '42', '--weighting', 'version']),
        ]
        for name, command in stages:
            print(f'Starting {name}', flush=True)
            with (out / (name + '.log')).open('w', encoding='utf-8') as log:
                subprocess.run([sys.executable, '-u', *command], cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, check=True)
            manifest['stages'].append(name)
            dump(out / 'experiment_manifest.json', manifest)
            print(f'Completed {name}', flush=True)
        changed = [name for name, digest in source_hashes.items() if hashlib.sha256((ROOT / name).read_bytes()).hexdigest() != digest]
        manifest.update(status='complete', completed_at=datetime.now(timezone.utc).isoformat(), source_changes_during_run=changed)
        dump(out / 'experiment_manifest.json', manifest)
    except BaseException:
        manifest.update(status='failed', error=traceback.format_exc())
        dump(out / 'experiment_manifest.json', manifest)
        raise


if __name__ == '__main__':
    main()
