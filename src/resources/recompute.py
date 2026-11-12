"""Job resource accounting and repository bootstrap estimates for RQ2.3."""
from collections import defaultdict
from datetime import datetime
import csv
import json
import math
import statistics

B=2000

def dt(t):return datetime.fromisoformat(t.replace('Z','+00:00'))
def dump(out,name,rs):
    with (out/name).open('w',encoding='utf-8') as f:
        for r in rs:f.write(json.dumps(r,ensure_ascii=False,allow_nan=False)+'\n')
def csvout(out,name,rows):
    fields=list(dict.fromkeys(k for r in rows for k in r)) or ['empty']
    with (out/name).open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=fields);w.writeheader()
        for r in rows:w.writerow({k:json.dumps(v,ensure_ascii=False,sort_keys=True) if isinstance(v,(dict,list)) else v for k,v in r.items()})
def mean(values):return statistics.fmean(values) if values else None
def quantile(x,q):
    x=sorted(x);p=(len(x)-1)*q;lo=int(p);hi=min(lo+1,len(x)-1)
    return x[lo]+(x[hi]-x[lo])*(p-lo)
def runner_id(x):
    if isinstance(x,bool):return None
    try:y=float(x);return y if math.isfinite(y) else None
    except (TypeError,ValueError):return None
def cost(jobs):
    reasons=[];witness=[];ids=set();vm=0.;usd=0.;allocated=0
    if not jobs:return {'complete':False,'reasons':['empty_or_unavailable_jobs'],'vm_seconds':None,'reference_cost_usd':None,'job_math':[]}
    for j in jobs:
        if j.get('id') in ids:reasons.append('duplicate_job_id')
        ids.add(j.get('id'));rid=runner_id(j.get('runner_id'))
        if rid is None and not j.get('runner_name') and j.get('conclusion') in {'skipped','cancelled'} and not any(s.get('name')=='Set up job' and s.get('conclusion')=='success' for s in (j.get('steps') or [])):
            # GitHub represents skipped / pre-allocation cancelled jobs with
            # null runner_id, sometimes retaining configured OS labels and
            # synthetic or reversed timestamps. These are not allocated time.
            rid=0
        if rid is None:
            reasons.append('runner_allocation_unknown');continue
        if rid<=0:
            if j.get('runner_name'):reasons.append('contradictory_runnerless_job')
            witness.append({'job_id':j.get('id'),'runner_allocated':False,'vm_seconds':0.,'reference_cost_usd':0.});continue
        allocated+=1
        if j.get('status') not in (None,'completed'):reasons.append('allocated_job_not_completed')
        try:seconds=(dt(j['completed_at'])-dt(j['started_at'])).total_seconds()
        except (TypeError,KeyError,ValueError):reasons.append('missing_or_invalid_job_time');continue
        if seconds<0:reasons.append('negative_job_duration');continue
        labels={str(x).lower() for x in (j.get('labels') or [])};text=' '.join(labels)
        os='self-hosted' if 'self-hosted' in labels else 'Linux' if 'ubuntu' in text or 'linux' in text else 'Windows' if 'windows' in text else 'macOS' if any(x in text for x in ('macos','mac-os','osx')) else 'unknown'
        factor={'Linux':1,'Windows':2,'macOS':10}.get(os)
        if factor is None:reasons.append('self_hosted_runner' if os=='self-hosted' else 'unknown_pricing_category');continue
        minutes=math.ceil(seconds/60*factor);amount=.008*minutes;vm+=seconds;usd+=amount
        witness.append({'job_id':j.get('id'),'runner_allocated':True,'os':os,'vm_seconds':seconds,'multiplier':factor,'rounded_standard_minutes':minutes,'reference_cost_usd':amount})
    return {'complete':not reasons,'reasons':sorted(set(reasons)),'vm_seconds':vm if not reasons else None,'reference_cost_usd':usd if not reasons else None,'allocated_jobs':allocated,'job_math':witness}
def aggregate(rows,rng):
    groups=defaultdict(list)
    for r in rows:groups[r['repository_id']].append(r)
    repos=[(rid,mean([r['delta_vm_seconds'] for r in es]),mean([r['delta_reference_cost_usd'] for r in es])) for rid,es in sorted(groups.items())]
    ivm=iusd=None
    if len(repos)>=2:
        bv=[];bc=[]
        for _ in range(B):
            picks=rng.choices(repos,k=len(repos));bv.append(mean([r[1] for r in picks]));bc.append(mean([r[2] for r in picks]))
        ivm=[quantile(bv,.025),quantile(bv,.975)];iusd=[quantile(bc,.025),quantile(bc,.975)]
    return {'events':len(rows),'repositories':len(repos),'delta_vm_seconds':mean([r[1] for r in repos]),'delta_reference_cost_usd':mean([r[2] for r in repos]),'vm_interval_lower_seconds':ivm[0] if ivm else None,'vm_interval_upper_seconds':ivm[1] if ivm else None,'cost_interval_lower_usd':iusd[0] if iusd else None,'cost_interval_upper_usd':iusd[1] if iusd else None,'repositories_with_positive_vm_delta':sum(r[1]>0 for r in repos),'repositories_with_positive_cost_delta':sum(r[2]>0 for r in repos),'interval_limitation':'not_estimable' if not repos else 'single_repository_no_population_interval' if len(repos)==1 else 'few_repositories_unstable_interval' if len(repos)<10 else 'pointwise_observational_interval'}
