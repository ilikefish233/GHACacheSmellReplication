"""Verify every compressed stage input against its original byte hash."""
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import argparse
import ast
import gzip
import hashlib
import json
import re

HERE=Path(__file__).resolve().parent
PACKAGE=HERE.parent

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--workers',type=int,default=3)
    p.add_argument('--source-only',action='store_true',help='Check source hashes and syntax without rereading large gzip inputs')
    a=p.parse_args()
    records=json.loads((HERE/'data_manifest.json').read_text())
    def verify(r):
        h=hashlib.sha256(); size=0
        with gzip.open(PACKAGE/r['archive_path'],'rb') as f:
            for block in iter(lambda:f.read(4*1024*1024),b''):
                h.update(block);size+=len(block)
        assert h.hexdigest()==r['sha256'],r['original_path']
        assert size==r['bytes'],r['original_path']
        return size
    if a.source_only:
        sizes=[]
    else:
        print('Verifying all',len(records),'gzip inputs',flush=True)
        with ThreadPoolExecutor(max_workers=a.workers) as pool:sizes=list(pool.map(verify,records))
    sources=json.loads((HERE/'source_manifest.json').read_text())
    for r in sources:
        p=PACKAGE/'src/collection'/r['path']
        assert hashlib.sha256(p.read_bytes()).hexdigest()==r['sha256'],r['path']
    code=list(HERE.rglob('*.py'))
    for p in code:
        text=p.read_text(encoding='utf-8-sig')
        ast.parse(text,filename=str(p))
        assert not re.search(r'(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})',text),'Credential pattern in '+str(p)
    assert not list(HERE.rglob('.env'))
    result={'passed':True,'compressed_inputs_verified':len(sizes),'uncompressed_bytes_verified':sum(sizes),
            'source_files_verified':len(sources),'source_modules_verified':sum(r['path'].endswith('.py') for r in sources),'python_files_parsed':len(code),
            'credential_files_included':False,'network_requests':0,'rq3_executed':False}
    a.output.parent.mkdir(parents=True,exist_ok=True)
    a.output.write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(result,indent=2),flush=True)

if __name__=='__main__':main()
