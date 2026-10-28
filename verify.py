"""Verify artifact integrity and independently regenerated scientific results."""
from pathlib import Path
import argparse
import csv
import json
import math
import sys
sys.path.insert(0,str(Path(__file__).resolve().parent/'src'))
from artifact import ROOT,canonical,digest,write

def compare(a,b,path=''):
    if isinstance(a,dict):
        for k,v in a.items():
            if k not in b:raise AssertionError('Missing '+path+'/'+k)
            compare(v,b[k],path+'/'+k)
    elif isinstance(a,list):
        assert len(a)==len(b),(path,len(a),len(b))
        for i,(x,y) in enumerate(zip(a,b)):compare(x,y,path+'/'+str(i))
    elif isinstance(a,(int,float)) and not isinstance(a,bool):
        assert isinstance(b,(int,float)) and math.isclose(a,b,rel_tol=1e-8,abs_tol=1e-10),(path,a,b)
    else: assert a==b,(path,a,b)

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--results',type=Path,default=ROOT/'results')
    p.add_argument('--checksums',action='store_true')
    p.add_argument('--integrity-only',action='store_true')
    p.add_argument('--validation-only',action='store_true',help='Check only the bundled 383-workflow validation; no RQ data required')
    a=p.parse_args();checks={}
    if a.validation_only and a.integrity_only:
        p.error('--validation-only cannot be combined with --integrity-only')
    if a.checksums:
        if not (ROOT/'checksums.sha256').is_file():
            raise SystemExit('Full-package checksums are not bundled in this GitHub edition. Use the complete offline artifact for its integrity-only check.')
        n=0
        for line in (ROOT/'checksums.sha256').read_text(encoding='utf-8').splitlines():
            h,name=line.split('  ',1)
            assert digest(ROOT/name)==h,'SHA256 mismatch: '+name
            n+=1
        checks['verified_file_checksums']=n
    if not a.integrity_only:
        def read(p):return canonical(json.loads(p.read_text(encoding='utf-8-sig')))
        expected=read(ROOT/'Validation/expected/evaluation.json');actual=read(a.results/'validation/evaluation.json')
        provenance=read(ROOT/'docs/replication_manifest.json')['validation']
        for name,h in provenance['input_sha256'].items():
            assert digest(ROOT/name)==h,'Validation input SHA256 mismatch: '+name
        assert digest(ROOT/'Validation/expected/evaluation.json')==provenance['expected_result_sha256'],'Validation expected result SHA256 mismatch'
        for k in ['source_sha256','physical_cases','excluded_adoption_zero','excluded_ids',
                  'parsed_and_detected_cases','failures','policy','by_smell_type','cases']:
            compare(expected[k],actual[k],'validation/'+k)
        checks['validation_383_workflows_and_all_type_metrics']=True
        if a.validation_only:
            print(json.dumps(checks,indent=2))
            write(a.results/'verification.json',{'passed':True,'scope':'validation only','checks':checks,
                'relative_tolerance':1e-8,'absolute_tolerance':1e-10})
            return
        for name in ['all_completed_duration','successful_duration']:
            e=read(ROOT/'RQ2/expected/duration'/(name+'.json'));r=read(a.results/'rq2'/(name+'.json'))
            for k in ['runs','workflow_versions','repositories','design_groups','coefficients']:
                compare(e[k],r[k],name+'/'+k)
            checks[name]=True
        e=read(ROOT/'RQ2/expected/success/reliability_lpm.json');r=read(a.results/'rq2/reliability_lpm.json')
        for k in ['runs','workflow_versions','repositories','design_groups','coefficients','predictions']:
            compare(e[k],r[k],'success/'+k)
        checks['success_lpm']=True
        for name in ['multiplicity.csv','cooccurrence.csv','ecosystem_preserving_paper_groups.csv',
            'roles_preserving_paper_groups.csv','triggers_preserving_paper_groups.csv',
            'repository_concentration.csv','repository_distribution_tests.csv','prevalence_comparison.csv',
            'ndc_exclusive_metadata_opportunities.csv']:
            def table(path):
                with path.open(encoding='utf-8-sig') as f:
                    return sorted((canonical(dict(row)) for row in csv.DictReader(f)),key=lambda row:json.dumps(row,sort_keys=True))
            e=table(ROOT/'RQ1/expected/paper_tables'/name);r=table(a.results/'rq1'/name)
            assert len(e)==len(r),(name,len(e),len(r))
            # Row ordering and float text are incidental; compare keyed numerical values.
            for x,y in zip(e,r):
                for k,v in x.items():
                    try:compare(float(v),float(y[k]),name+'/'+k)
                    except ValueError:compare(v,y[k],name+'/'+k)
            checks['rq1/'+name]=True
        for name in ['smell_resource_summary.json','result_audit.json','observed_monthly_frequency/smell_observed_monthly_summary.json']:
            e=read(ROOT/'RQ2/RQ2_3/expected'/name);r=read(a.results/'rq2_3'/name)
            compare(e,r,'resource/'+name);checks['rq2_3/'+name]=True
    print(json.dumps(checks,indent=2))
    if not a.integrity_only:
        write(a.results/'verification.json',{'passed':True,'checks':checks,'relative_tolerance':1e-8,'absolute_tolerance':1e-10})

if __name__=='__main__':main()
