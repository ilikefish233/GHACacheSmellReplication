"""Check rebuilt pre-refresh queue against the adopted October identities/order."""
from pathlib import Path
from itertools import zip_longest
import argparse
import json

PACKAGE=Path(__file__).resolve().parents[1]

def rows(p):
    with p.open(encoding='utf-8') as f:
        for line in f:
            if line.strip():yield json.loads(line)

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--work-dir',type=Path,required=True)
    a=p.parse_args()
    rebuilt=a.work_dir.resolve()/'rebuilt/model_input'
    checks={}
    fields={
        'version_records.jsonl':['version_id','repository_id','workflow_path','content_hash','last_change_at','effective_end_at','sequence','completed_count','success_count','technical_total_count'],
        'run_records.jsonl':['version_id','repository_id','workflow_id','workflow_path','run_id','run_attempt','created_at','duration_seconds','conclusion','event','head_branch','run_order_in_version','is_first_run_in_version'],
    }
    for name,keys in fields.items():
        n=0
        for n,(before,after) in enumerate(zip_longest(rows(PACKAGE/'RQ2/data'/name),rows(rebuilt/name)),1):
            if before is None or after is None:raise AssertionError(('length',name,n))
            for key in keys:
                if before.get(key)!=after.get(key):raise AssertionError((name,n,key,before.get(key),after.get(key)))
        checks[name]={'rows':n,'compared_fields':keys,'matched':True}
    result={'passed':True,'checks':checks,'detector_labels':'September queue labels intentionally excluded; October refresh is separate.'}
    out=a.work_dir/'handoff_verification.json'
    out.write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(result,indent=2))

if __name__=='__main__':main()
