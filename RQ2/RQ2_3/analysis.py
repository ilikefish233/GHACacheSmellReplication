"""Recompute adopted exploratory resource contrasts from bundled raw job metadata."""
from pathlib import Path
import argparse
import ast
from collections import Counter,defaultdict
import csv
import hashlib
import json
import math
import random
import statistics
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'src'))
from artifact import ROOT,load,rows,new_output,write,digest,canonical

def main():
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--output',type=Path,required=True)
    a=ap.parse_args()
    from data_setup import require_data
    require_data('rq2_3')
    out=new_output(a.output)
    source=ROOT/'RQ2/RQ2_3/expected'
    helper=load('resource_math',ROOT/'src/resources/recompute.py')
    jobs={(str(r['repository_id']),str(r['run_id']),int(r['run_attempt'])):r
          for r in rows(ROOT/'RQ2/RQ2_3/data/frozen_job_metadata.jsonl')}
    def dump(name,rs):
        with (out/name).open('w',encoding='utf-8') as f:
            for r in rs:f.write(json.dumps(r,ensure_ascii=False,allow_nan=False)+'\n')
    def csvout(name,rs):helper.csvout(out,name,list(rs))
    path=ROOT/'src/resources/run_relaxed.py';tree=ast.parse(path.read_text(encoding='utf-8-sig'))
    functions=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in {'calculate','necessary_checks'}]
    s=ast.unparse(ast.Module(body=functions,type_ignores=[]))
    s=s.replace("legacy = base.import_file('relaxed_resource_math', base.OLD / 'recompute.py')",'legacy = LEGACY')
    start=s.index('    for p in json.loads(')
    end=s.index('    byevent =',start)
    s=s[:start]+s[end:]  # original whole-archive hashes are retained as provenance; bundled records have artifact hashes
    start=s.index("        ref = r['job_archive']")
    end=s.index('        if str(meta[',start)
    s=s[:start]+"        meta = JOBS[(str(r['repository_id']), str(r['run_id']), int(r['run_attempt']))]\n"+s[end:]
    s=s.replace('    write_report(audit, summary)','')
    formal=('no_dependency_cache','no_fallback_cache','redundant_cache_update','cache_position_error',
        'conflicting_cache_sources','static_cache_key','overly_specific_cache_key','no_docker_layer_cache',
        'deprecated_cache_action','unreachable_cache_write','cache_as_artifact')
    scope={'HERE':source,'LEGACY':helper,'JOBS':jobs,'rows':rows,'sha':digest,'dump':dump,
        'jout':lambda n,v:write(out/n,v),'csvout':csvout,'json':json,'defaultdict':defaultdict,
        'Counter':Counter,'statistics':statistics,'random':random,'FORMAL':formal,'SMELLS':formal,
        'math':math}
    scope['SMELLS']=formal+('cache_path_pollution','incorrect_cache_hit_condition',
        'malformed_cache_path','non_portable_cache_path','overly_broad_cache_path')
    exec(compile(s,str(path),'exec'),scope);scope['calculate']()
    # Reuse original monthly-frequency implementation, changing only locations and the provenance lookup.
    monthly=ROOT/'src/resources/calculate_observed_monthly.py'
    s=monthly.read_text(encoding='utf-8-sig')
    s=s.replace("ROOT / 'data_26_6_9_canonical_20260609/repositories.jsonl'","ROOT / 'data/canonical/repositories.jsonl'")
    start=s.index("    expected = next(s['sha256']")
    end=s.index("    assert h.hexdigest() == expected",start)
    s=s[:start]+"    expected = digest(SOURCE / 'workflow_runs.jsonl')\n"+s[end:]
    t=ast.parse(s)
    # Keep immutable original module but override its location constants after initialization.
    mscope={'__name__':'resource_frequency','__file__':str(monthly),'digest':digest}
    exec(compile(t,str(monthly),'exec'),mscope)
    mscope.update(ROOT=ROOT,HERE=out,OUT=out/'observed_monthly_frequency',
        SOURCE=ROOT/'RQ2/RQ2_3/data/frequency')
    # Input provenance is historical and read-only; monthly calculation only reads it.
    import shutil
    shutil.copy2(source/'input_provenance.json',out/'input_provenance.json')
    mscope['main']()
    for p in list(out.glob('*.json'))+list((out/'observed_monthly_frequency').glob('*.json')):
        write(p,canonical(json.loads(p.read_text(encoding='utf-8'))))
    print('RQ2.3 contrasts and actual-frequency monthly equivalents recomputed:',out,flush=True)

if __name__=='__main__':main()
