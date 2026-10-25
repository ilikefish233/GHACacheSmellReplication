"""Refit unified duration regressions and the adopted success LPM offline."""
from pathlib import Path
import argparse
import json
import sys
import time
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from artifact import ROOT,load,rows,new_output,write,digest

def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--output',type=Path,required=True)
    ap.add_argument('--rebuild-groups',action='store_true',help='Reaggregate every run, rather than reuse verified sufficient statistics')
    ap.add_argument('--input',type=Path,default=ROOT/'RQ2/data')
    ap.add_argument('--bootstrap',type=int,default=0,help='Duration sensitivity only; adopted primary analyses use zero')
    args=ap.parse_args()
    from data_setup import require_data
    require_data('rq2')
    out=new_output(args.output)
    slim=ROOT/'src/models/rq2_unified_slim_20261018'
    sys.path.insert(0,str(slim))
    old=load('artifact_unified',slim/'run.py')
    lpm=load('artifact_lpm',ROOT/'src/models/rq2_unified_lpm_20261019/run.py')
    old.ROOT=ROOT
    lpm.previous.ROOT=ROOT
    prepared=ROOT/'RQ2/prepared'
    manifest=json.loads((prepared/'manifest.json').read_text(encoding='utf-8'))
    for n,h in manifest['code_sha256'].items():
        if digest(slim/n)!=h: raise ValueError('Frozen model code mismatch: '+n)
    for n,h in manifest['input_sha256'].items():
        if digest(args.input/n)!=h: raise ValueError('Frozen run/label input mismatch: '+n)
    if args.rebuild_groups:
        # Same version features are reused; run grouping and all outcomes are rebuilt.
        # The cache is separately content-addressed in checksums.sha256.
        versions,keys,values,success_n=old.prepare(args.input,out,1,{},prepared)
    else:
        versions={r['version_id']:r for r in rows(prepared/'workflow_features.jsonl')}
        keys=[tuple(r) for r in rows(prepared/'group_keys.jsonl')]
        with np.load(prepared/'grouped_outcomes.npz') as data:
            values=data['values'];success_n=data['success_n']
    for index,endpoint in enumerate(['all_completed_duration','successful_duration']):
        old.analyze_endpoint(endpoint,versions,keys,values,success_n,out,args.bootstrap,42+index)
    result=lpm.analyze(versions,keys,values)
    write(out/'reliability_lpm.json',result)
    write(out/'replication_run.json',{'completed':True,'regressions_refitted':True,
        'run_groups_rebuilt':args.rebuild_groups,'duration_bootstrap':args.bootstrap,
        'success_model':'weighted_linear_probability_model','input_sha256':manifest['input_sha256']})
    print('Duration models and success LPM refitted:',out,flush=True)

if __name__=='__main__':main()
