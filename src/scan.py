"""Rescan all paper RQ1 snapshots using the exact run3 detector (no network)."""
from pathlib import Path
import argparse
from concurrent.futures import ProcessPoolExecutor
import hashlib
import json
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from artifact import ROOT,rows,new_output,write,canonical

def worker(r):
    sys.path.insert(0,str(ROOT/'src/frozen'))
    from src.parsers.workflow_parser import WorkflowParser
    from src.detectors.smell.cache_smell_detector import CacheSmellDetector
    p=ROOT/'data/canonical/content'/(r['content_hash']+'.yml')
    expected=r['detectors']['cache_smell']
    if not p.is_file():
        if not expected.get('error'):raise FileNotFoundError(p)
        state=expected  # documented unavailable frozen content remains excluded
    else:
        raw=p.read_bytes()
        candidates=[raw,raw.replace(b'\r\n',b'\n'),p.read_text(encoding='utf-8').encode('utf-8')]
        if not any(hashlib.sha256(b).hexdigest()==r['content_hash'] for b in candidates):
            raise ValueError('Content hash mismatch: '+str(p))
        model=WorkflowParser().parse_content(raw.decode('utf-8-sig'))
        if model is None:state={'error':'YAML parse failure'}
        else: state=CacheSmellDetector().detect(model).to_dict()
    r['detectors']['cache_smell']=state
    r['yml_file']='data/canonical/content/'+r['content_hash']+'.yml'
    return canonical(r)

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--workers',type=int,default=4);p.add_argument('--limit',type=int)
    a=p.parse_args();out=new_output(a.output)
    import itertools
    records=rows(ROOT/'RQ1/data/rq1_scan_direct.jsonl')
    if a.limit:records=itertools.islice(records,a.limit)
    n=0
    with (out/'rq1_scan_direct.jsonl').open('w',encoding='utf-8') as stream,ProcessPoolExecutor(max_workers=a.workers) as pool:
        for n,r in enumerate(pool.map(worker,records,chunksize=30),1):
            stream.write(json.dumps(r,ensure_ascii=False)+'\n')
            if n%5000==0:print('Scanned',n,flush=True)
    write(out/'scan_run.json',{'scanned':n,'limit':a.limit,'network_requests':0,
        'detector':'frozen run3','missing_content_policy':'retain original excluded state only'})

if __name__=='__main__':main()
