"""Verify reconstructed canonical identities, fields and normalized run bytes."""
from pathlib import Path
import argparse
import hashlib
import json

def rows(path):
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            if line.strip():yield json.loads(line)

def verify(package,out):
    expected=package/'data/canonical'
    checks={}
    scientific_fields={
        'workflow_snapshots':['repository_id','canonical_name','workflow_path','workflow_id','content_hash','snapshot_status'],
        'workflow_boundaries':['repository_id','workflow_path','last_change_at','boundary_status','boundary_commit_sha'],
        'rq1_cohort':['repository_id','workflow_path','content_hash','detector_state_key','eligible'],
        'rq2_cohort':['repository_id','workflow_path','workflow_id','content_hash','last_change_at','boundary_status','qualifying_run_count','window_start','eligible'],
    }
    for name,key in [('workflow_snapshots','workflow_key'),('workflow_boundaries','workflow_key'),('rq1_cohort','workflow_key'),('rq2_cohort','workflow_key')]:
        before={r[key] for r in rows(expected/(name+'.jsonl'))}
        after={r[key] for r in rows(out/(name+'.jsonl'))}
        checks[name]={'expected':len(before),'rebuilt':len(after),'identities_equal':before==after}
        if before!=after:raise AssertionError((name,len(before),len(after)))
        reference={r[key]:r for r in rows(expected/(name+'.jsonl'))}
        for r in rows(out/(name+'.jsonl')):
            for field in scientific_fields[name]:
                if r.get(field)!=reference[r[key]].get(field):
                    raise AssertionError((name,r[key],field,r.get(field),reference[r[key]].get(field)))
        checks[name]['scientific_fields_matched']=scientific_fields[name]
    expected_run_hash=next(r['sha256'] for r in json.loads((Path(__file__).parent/'data_manifest.json').read_text()) if r['original_path']=='data_26_6_9_canonical_20260609/workflow_runs.jsonl')
    digest=hashlib.sha256()
    with (out/'workflow_runs.jsonl').open('rb') as stream:
        for block in iter(lambda:stream.read(4*1024*1024),b''):digest.update(block)
    checks['normalized_runs']={'sha256':digest.hexdigest(),'byte_identical_to_frozen_canonical':digest.hexdigest()==expected_run_hash}
    if digest.hexdigest()!=expected_run_hash:raise AssertionError('Canonical run normalization changed')
    report={'rq3_executed':False,'network_requests':0,'content_relocation':'frozen hash with original availability exclusions',
        'checks':checks,'note':'Retired historical detector cache is deliberately omitted; current-cutoff RQ1/RQ2 only.'}
    (out/'replay_report.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(report,indent=2))

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--work-dir',type=Path,required=True)
    a=p.parse_args()
    verify(Path(__file__).resolve().parents[1],a.work_dir.resolve()/'rebuilt/canonical')

if __name__=='__main__':main()
