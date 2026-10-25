"""Offline curation replay and explicit online acquisition entry points."""
from pathlib import Path
import argparse
import gzip
import hashlib
import json
import os
import shutil
import subprocess
import sys

HERE = Path(__file__).resolve().parent
PACKAGE = HERE.parent
CAN = 'data_26_6_9_canonical_20260609'
PLAN = 'output_rq2_unified_acquisition_plan_phase2_0_20260919'
EVIDENCE = 'output_canonical_rq2_query_coverage_phase1_2_1_20260919/rq2_query_coverage_evidence.jsonl'
CONSOLIDATION = 'output_rq2_unified_acquisition_consolidated_20260920/consolidation_manifest.json'

def link(source, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        return
    try:
        os.link(source, target)
    except OSError:
        shutil.copy2(source, target)

def setup(work):
    if work == PACKAGE or PACKAGE in work.parents:
        raise ValueError('Use a workspace outside the distributed package.')
    work.mkdir(parents=True, exist_ok=True)
    for p in (PACKAGE/'src/collection').rglob('*'):
        if p.is_file() and p.suffix in {'.py','.json'}:
            link(p, work/p.relative_to(PACKAGE/'src/collection'))
    for p in (PACKAGE/'data/canonical').glob('*.json*'):
        link(p, work/CAN/p.name)
    # Shared content stays in the package; canonical replay uses a hash index.
    return {r['original_path']:r for r in json.loads((HERE/'data_manifest.json').read_text())}

def materialize(work, entries, relative):
    target = work/relative
    if target.is_file():
        return target
    entry = entries[relative]
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(target.name+'.partial')
    h = hashlib.sha256()
    with gzip.open(PACKAGE/entry['archive_path'],'rb') as src, temp.open('wb') as dst:
        for block in iter(lambda:src.read(4*1024*1024),b''):
            h.update(block); dst.write(block)
    if h.hexdigest() != entry['sha256']:
        raise ValueError('Decompressed input hash mismatch: '+relative)
    temp.replace(target)
    return target

def command(work, stage, args, online=False):
    log = work/'logs'/f'{stage}.log'
    log.parent.mkdir(exist_ok=True)
    boot = HERE/'invoke.py'
    call = [sys.executable,str(boot),'--work',str(work)]
    if online:
        call.append('--online')
    call += ['--',*map(str,args)]
    with log.open('w',encoding='utf-8') as out:
        result = subprocess.run(call,cwd=work,stdout=out,stderr=subprocess.STDOUT,
                                env=dict(os.environ,PYTHONUTF8='1',PYTHONIOENCODING='utf-8'))
    if result.returncode:
        print(log.read_text(encoding='utf-8',errors='replace')[-10000:])
        raise SystemExit(f'{stage} failed; see {log}')
    print(f'{stage}: passed; log {log}',flush=True)

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('stage', choices=['filter','canonical','consolidate','coverage','queue','model-input','offline-all','collect-base','collect-runs','materialize'])
    p.add_argument('--work-dir',type=Path,required=True)
    p.add_argument('--execute',action='store_true',help='Enable the selected online acquisition stage')
    p.add_argument('--input-path',help='Original relative path to decompress with materialize')
    a = p.parse_args()
    work = a.work_dir.resolve()
    entries = setup(work)
    get = lambda path:materialize(work,entries,path)
    stages = ['filter','canonical','consolidate','coverage','queue','model-input'] if a.stage=='offline-all' else [a.stage]
    for stage in stages:
        if stage=='filter':
            command(work,stage,['scripts/filter_repositories.py','--data-dir',PACKAGE/'data/upstream/ghalogs','--output-dir',work/'rebuilt/filter'])
        elif stage=='canonical':
            meta=json.loads((PACKAGE/'data/canonical/dataset_manifest.json').read_text())
            for role,item in meta['sources'].items():
                if role not in {'history_state_source','detector_source'}:get(item['path'])
            command(work,stage,[str(HERE/'canonical_rq1_rq2.py'),'--package',PACKAGE,'--output-dir',work/'rebuilt/canonical'])
        elif stage=='consolidate':
            manifest=json.loads(gzip.open(PACKAGE/entries[CONSOLIDATION]['archive_path'],'rt',encoding='utf-8').read())
            for item in manifest['input_files']:get(item['path'])
            command(work,stage,['scripts/consolidate_rq2_full_acquisition.py','--output-dir',work/'rebuilt/consolidated'])
        elif stage=='coverage':
            get(EVIDENCE)
            command(work,stage,['scripts/build_rq2_final_coverage_evidence.py','--base-evidence',work/EVIDENCE,'--latest-status',work/'rebuilt/consolidated/workflow_acquisition_status.jsonl','--output-dir',work/'rebuilt/coverage'])
        elif stage=='queue':
            command(work,stage,['scripts/audit_rq2_canonical_coverage.py','--runs',work/'rebuilt/consolidated/workflow_runs.jsonl','--coverage-evidence',work/'rebuilt/coverage/rq2_final_coverage_evidence.jsonl','--status-file',work/'no_additional_repository_status.jsonl','--output-dir',work/'rebuilt/queue'])
        elif stage=='model-input':
            get(CAN+'/detector_states.jsonl')
            command(work,stage,['scripts/prepare_rq2_final_model_input.py','--workflows',work/'rebuilt/queue/rq2_final_analysis_workflows.jsonl','--runs',work/'rebuilt/queue/rq2_final_analysis_runs.jsonl','--output-dir',work/'rebuilt/model_input'])
        elif stage=='materialize':
            if not a.input_path:p.error('--input-path is required')
            print(get(a.input_path))
        elif stage in {'collect-base','collect-runs'}:
            if not a.execute:p.error('Online stages require --execute; no request was made.')
            if stage=='collect-base':
                (work/'new_collection/base').mkdir(parents=True,exist_ok=True)
                command(work,stage,['scripts/data_update/fetch_all.py','--input',work/'rebuilt/filter/filtered_repositories.json','--output-dir',work/'new_collection/base'],online=True)
            else:
                for name in ['rq2_acquisition_plan_manifest.json','rq2_acquisition_plan_repositories.jsonl','rq2_acquisition_plan_workflows.jsonl']:get(PLAN+'/'+name)
                command(work,stage,['scripts/execute_rq2_unified_acquisition.py','--execute','--plan-dir',work/PLAN,'--output-dir',work/'new_collection/runs'],online=True)

if __name__=='__main__':main()
