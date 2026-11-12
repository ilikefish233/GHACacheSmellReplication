"""Rescan all paper RQ1 snapshots using the exact run3 detector (no network)."""
from pathlib import Path
import argparse
from functools import partial
from concurrent.futures import ProcessPoolExecutor
import hashlib
import json
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from artifact import ROOT,rows,new_output,write,canonical,DataPaths,add_data_dir

def worker(r, content_dir):
    sys.path.insert(0,str(ROOT/'src/frozen'))
    from src.parsers.workflow_parser import WorkflowParser
    from src.detectors.smell.cache_smell_detector import CacheSmellDetector
    p=content_dir/(r['content_hash']+'.yml')
    expected=r['detectors']['cache_smell']
    if not p.is_file():
        if not expected.get('error'):raise FileNotFoundError(p)
        state=expected  # documented unavailable frozen content remains excluded
    else:
        raw=p.read_bytes()
        candidates=[raw,raw.replace(b'\r\n',b'\n'),p.read_text(encoding='utf-8').encode('utf-8')]
        if not any(hashlib.sha256(b).hexdigest()==r['content_hash'] for b in candidates):
            if not expected.get('error'):
                raise ValueError('Content hash mismatch: '+str(p))
            state=expected  # original run3 explicitly excluded this mismatched content
        else:
            try:
                model=WorkflowParser().parse_content(raw.decode('utf-8-sig'))
                if model is None:state={'error':'YAML parse failure'}
                else: state=CacheSmellDetector().detect(model).to_dict()
            except Exception as exc:
                # The original run3 scan retained detector exceptions as unusable
                # results, allowing the remaining workflows to be processed.
                state={'error':f'{type(exc).__name__}: {exc}'}
    r['detectors']['cache_smell']=state
    r['yml_file']='data/canonical/content/'+r['content_hash']+'.yml'
    return canonical(r)

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--workers',type=int,default=4);p.add_argument('--limit',type=int)
    p.add_argument('--input',type=Path,help='Prepared scan records to rescan instead of the archived scan input')
    add_data_dir(p)
    a=p.parse_args()
    paths=DataPaths(a.data_dir)
    from data_setup import require_data
    require_data('detector', paths)
    out=new_output(a.output)
    import itertools
    records=rows(a.input or paths.path('RQ1/data/rq1_scan_direct.jsonl'))
    scan_record=partial(worker, content_dir=paths.path('data/canonical/content'))
    if a.limit:records=itertools.islice(records,a.limit)
    n=0
    with (out/'rq1_scan_direct.jsonl').open('w',encoding='utf-8') as stream,ProcessPoolExecutor(max_workers=a.workers) as pool:
        for n,r in enumerate(pool.map(scan_record,records,chunksize=30),1):
            stream.write(json.dumps(r,ensure_ascii=False)+'\n')
            if n%5000==0:print('Scanned',n,flush=True)
    write(out/'scan_run.json',{'scanned':n,'limit':a.limit,'network_requests':0,
        'detector':'frozen run3','missing_content_policy':'retain original excluded state only'})

if __name__=='__main__':main()
