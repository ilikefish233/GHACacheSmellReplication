"""Standalone offline RQ2.3 recomputation; standard library only.

python E:\\gha_log_analysis\\output_rq23_historical_before_after_20261010\\recompute.py --output-dir E:\\gha_log_analysis\\output_rq23_historical_before_after_20261010\\recomputed
"""
from pathlib import Path
from collections import defaultdict,Counter
from datetime import datetime
import argparse,csv,hashlib,json,math,random,statistics,platform
HERE=Path(__file__).resolve().parent
ROOT=HERE.parent
SMELLS=('no_dependency_cache','no_restore_keys','redundant_cache_update','cache_position_error','conflicting_cache_sources','static_cache_key','overly_specific_cache_key','no_docker_layer_cache','deprecated_cache_action','unreachable_cache_write','cache_as_artifact')
SEED=20261006;B=2000
def load(p):
    with p.open(encoding='utf-8-sig') as f:
        for line in f:
            if line.strip():yield json.loads(line)
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def dt(t):return datetime.fromisoformat(t.replace('Z','+00:00'))
def key(r):return str(r['repository_id']),int(r['run_id']),int(r.get('run_attempt') or 1)
def jout(out,name,x):(out/name).write_text(json.dumps(x,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')
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

def main(out):
    if out!=HERE:
        if out.exists() and any(out.iterdir()):raise RuntimeError('Output must be new/empty')
        out.mkdir(parents=True,exist_ok=True)
    freeze=json.loads((HERE/'freeze_audit.json').read_text(encoding='utf-8'))
    for n,d in freeze['sha256'].items():
        if sha(HERE/n)!=d:raise ValueError('Frozen file changed: '+n)
    manifest={key(r):r for r in load(HERE/'frozen_run_manifest.jsonl')}
    metadata={key(r):r for r in load(HERE/'frozen_job_metadata.jsonl')}
    attribution={key(r):r for r in load(HERE/'run_attribution.jsonl')}
    events=list(load(HERE/'frozen_events.jsonl'));review=list(load(HERE/'candidate_review.jsonl'))
    config={r['event_id']:r for r in load(HERE/'final_configuration_verification.jsonl')}
    runresults=[];byevent=defaultdict(lambda:defaultdict(list));exclusion=Counter();membership_missing=Counter();jobmath=[];zero=[];witness_os=set()
    for k,r in sorted(manifest.items()):
        m=metadata.get(k,{});a=attribution.get(k,{});rs=[]
        if not a.get('verified'):rs.append(a.get('reason') or 'unverified_execution_content')
        if m.get('matched_attempt')!='exact':rs.append(m.get('error') or 'not_exact_attempt_jobs')
        if m.get('local_job_list_conflict'):rs.append('conflicting_local_job_lists')
        if m.get('error'):rs.append(m['error'])
        js=m.get('jobs') or []
        if any(int(j.get('run_attempt') or 1)!=r['run_attempt'] for j in js):rs.append('job_attempt_mismatch')
        cr=cost(js);rs+=cr['reasons'];rs=sorted(set(rs));complete=not rs
        result={**r,'resource_complete':complete,'exclusion_reasons':rs,'vm_seconds':cr['vm_seconds'] if complete else None,'reference_cost_usd':cr['reference_cost_usd'] if complete else None,'job_count':len(js),'allocated_job_count':cr.get('allocated_jobs'),'jobs_acquisition':m.get('acquisition'),'content_evidence':a.get('evidence'),'actual_content_sha256':a.get('actual_content_sha256'),'cache_hit_evidence':'not_available_in_job_metadata; no hit-based selection'}
        # Job step names do not establish a cache hit. Mark only explicit evidence
        # if present; no logs are downloaded for this task.
        runresults.append(result)
        if rs:exclusion.update(rs)
        if complete:
            if cr['vm_seconds']==0:zero.append(k)
            observed_os={j.get('os') for j in cr['job_math'] if j.get('os')}
            if len(jobmath)<12 or observed_os-witness_os or (cr['vm_seconds']==0 and not any(x['vm_seconds']==0 for x in jobmath)):
                jobmath.append({'run_key':list(k),'vm_seconds':cr['vm_seconds'],'reference_cost_usd':cr['reference_cost_usd'],'job_math':cr['job_math']})
                witness_os.update(observed_os)
        for member in r['memberships']:
            event_id=member['event_id'];s=member['side']
            if complete and a.get('actual_content_sha256')==member['expected_content_sha256']:
                byevent[event_id][s].append({**result,**member})
            else:membership_missing[(event_id,s)]+=1
    outputs=[];valid=[];two=[];event_status=[]
    for e in events:
        sides={s:sorted(byevent[e['event_id']][s],key=lambda r:r['rank']) for s in ('before','after')}
        configuration_verified=config[e['event_id']]['configuration_verified']
        both=all(sides.values()) and configuration_verified;base={k:e[k] for k in ('event_id','repository_id','repository_name','workflow_path','workflow_id','research_branch','selected_event','after_commit_date','before_content_sha256','after_content_sha256')}
        for s,rs in sides.items():
            base[s+'_frozen_runs']=e['selected_counts'][s];base[s+'_complete_runs']=len(rs)
            base[s+'_mean_vm_seconds']=mean([r['vm_seconds'] for r in rs]);base[s+'_mean_reference_cost_usd']=mean([r['reference_cost_usd'] for r in rs])
            base[s+'_selected_min_distance_days']=min((m['distance_days'] for r in manifest.values() for m in r['memberships'] if m['event_id']==e['event_id'] and m['side']==s),default=None)
            base[s+'_selected_max_distance_days']=max((m['distance_days'] for r in manifest.values() for m in r['memberships'] if m['event_id']==e['event_id'] and m['side']==s),default=None)
            base[s+'_complete_min_distance_days']=min((r['distance_days'] for r in rs),default=None);base[s+'_complete_max_distance_days']=max((r['distance_days'] for r in rs),default=None)
            base[s+'_complete_run_keys']=[list(key(r)) for r in rs]
            base[s+'_conclusions']=dict(Counter(r['conclusion'] for r in rs))
        base.update(status='included' if both else 'excluded_configuration_verification' if not configuration_verified else 'excluded_missing_resource_side',single_observation_side=any(len(rs)==1 for rs in sides.values()),other_configuration_differences=e['configuration_changes'],opaque_workload_references=e['opaque_workload_references'],internal_scripts_and_dependencies_not_fully_observed=bool(e['opaque_workload_references']),**{k:v for k,v in config[e['event_id']].items() if k not in ('event_id','repository_name','workflow_path')})
        event_status.append({**base,'smells':[m['smell'] for m in e['members']]})
        for m in e['members']:
            present='after' if m['direction']=='introduced' else 'before';absent='before' if present=='after' else 'after'
            row={**base,**m,'present_side':present,'absent_side':absent,'delta_vm_seconds':base[present+'_mean_vm_seconds']-base[absent+'_mean_vm_seconds'] if both else None,'delta_reference_cost_usd':base[present+'_mean_reference_cost_usd']-base[absent+'_mean_reference_cost_usd'] if both else None}
            outputs.append(row)
            if both:valid.append(row)
            # Diagnostic only: exact same frozen event, event type, and verified
            # observation universe; limit to rank<=2, never fill a missing rank.
            p2=[r for r in sides[present] if r['rank']<=2];a2=[r for r in sides[absent] if r['rank']<=2]
            if p2 and a2 and both:two.append({**row,'delta_vm_seconds':mean([r['vm_seconds'] for r in p2])-mean([r['vm_seconds'] for r in a2]),'delta_reference_cost_usd':mean([r['reference_cost_usd'] for r in p2])-mean([r['reference_cost_usd'] for r in a2])})
    summary=[];rng=random.Random(SEED)
    for s in SMELLS:
        es=[r for r in valid if r['smell']==s]
        def normalize(ts):return {'overly_specific_cache_key' if t=='disposable_cache_key' else t for t in ts}
        candidates=[r for r in review if s in r.get('changed_formal_smells',[]) or ('changed_formal_smells' not in r and any(s in (normalize(q['old_before'])^normalize(q['old_after'])) for q in r.get('sources',[]) if q.get('old_before') is not None))]
        members=[r for r in review if any(m['smell']==s for m in r.get('included_members',[]))]
        row={'smell':s,**aggregate(es,rng),'introduced_events':sum(r['direction']=='introduced' for r in es),'removed_events':sum(r['direction']=='removed' for r in es),'single_observation_events':sum(r['single_observation_side'] for r in es),'other_smell_change_events':sum(bool(r['other_smells_changed']) for r in es),'opaque_task_reference_events':sum(bool(r['opaque_workload_references']) for r in es),'candidate_events':len(candidates),'eligible_absent_member_events':len(members),'frozen_events':sum(m['smell']==s for e in events for m in e['members']),'candidate_exclusion_reasons':dict(Counter(z for r in candidates for z in r.get('exclusion_reasons',[]))),'absent_member_exclusion_reasons':dict(Counter(m['reason'] for r in candidates for m in r.get('excluded_members',[]) if m['smell']==s))}
        row.update(tool_or_action_version_change_events=sum(r['action_or_tool_version_changed'] for r in es),matrix_values_change_events=sum(r['same_size_matrix_values_changed'] for r in es),runner_label_change_events=sum(r['runner_labels_changed_same_category'] for r in es),cache_conditional_execution_change_events=sum(bool(r['configuration_notes']) for r in es))
        summary.append(row)
    # Comparison with old analysis: old all-events point estimate, old results
    # restricted to final valid events, and observation expansion at fixed rules.
    old_dir=ROOT/'output_rq23_within_workflow_cost_20261006'
    old_summary={r['smell']:r for r in json.loads((old_dir/'smell_cost_summary.json').read_text(encoding='utf-8'))}
    old_events={(r['source'],r['transition_index'],r['smell']):r for r in load(old_dir/'event_cost_differences.jsonl')}
    eventmap={e['event_id']:e for e in events};comparison=[]
    for s in SMELLS:
        matched=[];expanded_matched=[]
        for r in valid:
            if r['smell']!=s:continue
            e=eventmap[r['event_id']];matches=[old_events.get((q['source'],q['index'],s)) for q in e['sources']];matches=[q for q in matches if q]
            if not matches:continue
            old=matches[0];matched.append({**r,'delta_reference_cost_usd':old['delta_usd_per_run']});expanded_matched.append(r)
        a=aggregate(matched,random.Random(SEED));b=aggregate(expanded_matched,random.Random(SEED));t=aggregate([r for r in two if r['smell']==s],random.Random(SEED))
        row={'smell':s,'old_all_events':old_summary[s]['transitions'],'old_all_repositories':old_summary[s]['repositories'],'old_all_delta_usd':old_summary[s]['mean_repo_delta_usd_per_run'],'old_same_final_events':a['events'],'old_same_final_delta_usd':a['delta_reference_cost_usd'],'new_same_old_events_delta_usd':b['delta_reference_cost_usd'],'nearest_two_events':t['events'],'nearest_two_delta_vm_seconds':t['delta_vm_seconds'],'nearest_two_delta_usd':t['delta_reference_cost_usd'],'new_all_events':next(x['events'] for x in summary if x['smell']==s),'new_all_delta_usd':next(x['delta_reference_cost_usd'] for x in summary if x['smell']==s)}
        # Isolate pure supplementation on the subset valid under rank<=2 too.
        ids={r['event_id'] for r in two if r['smell']==s}
        same=aggregate([r for r in valid if r['smell']==s and r['event_id'] in ids],random.Random(SEED))
        row['nearest_two_common_expanded_delta_vm_seconds']=same['delta_vm_seconds'];row['nearest_two_common_expanded_delta_usd']=same['delta_reference_cost_usd']
        comparison.append(row)
    flow=[]
    def flowrow(stage,es,rs=None):
        flow.append({'stage':stage,'unique_events':len(es),'repositories':len({e['repository_id'] for e in es}),'workflow_identities':len({(e['repository_id'],e['workflow_path'],str(e.get('workflow_id') or '')) for e in es}),'content_versions':len({(e['repository_id'],e['workflow_path'],e.get(s+'_content_sha256')) for e in es for s in ('before','after')}),'unique_runs':len(rs) if rs is not None else None})
    flowrow('all_existing_unique_candidates',review)
    flowrow('configuration_and_eligible_absent_member_pass',[r for r in review if r['initial_status']=='configuration_pass'])
    flowrow('frozen_common_identity_event_runs',events,manifest)
    config_pass=[e for e in events if config[e['event_id']]['configuration_verified']];flowrow('frozen_events_final_configuration_verified',config_pass)
    verified={k:r for k,r in manifest.items() if attribution.get(k,{}).get('verified')}
    def covered_events(run_keys,both=False):
        cover=defaultdict(set)
        for k in run_keys:
            for m in manifest[k]['memberships']:cover[m['event_id']].add(m['side'])
        return [e for e in events if len(cover[e['event_id']])>=(2 if both else 1)]
    flowrow('attribution_verified_any_side',covered_events(verified),verified)
    flowrow('attribution_verified_both_sides',covered_events(verified,True))
    complete={key(r):r for r in runresults if r['resource_complete']};flowrow('resource_complete_runs_any_event',covered_events(complete),complete)
    inc=[e for e in event_status if e['status']=='included'];used={tuple(k) for e in inc for s in ('before','after') for k in e[s+'_complete_run_keys']};flowrow('both_sides_resource_complete_events',inc,used)
    flowrow('no_common_identity_or_event_excluded',[r for r in review if r['initial_status']=='configuration_pass' and r['final_pre_resource_status']=='excluded'])
    flowrow('one_or_both_resource_sides_missing',[e for e in event_status if e['status']=='excluded_missing_resource_side'])
    flowrow('final_configuration_verification_excluded',[e for e in event_status if e['status']=='excluded_configuration_verification'])
    dump(out,'resource_run_records.jsonl',runresults);dump(out,'event_resource_differences.jsonl',outputs);csvout(out,'event_resource_differences.csv',outputs)
    dump(out,'event_status.jsonl',event_status);jout(out,'smell_resource_summary.json',summary);csvout(out,'smell_resource_summary.csv',summary)
    csvout(out,'old_new_estimate_comparison.csv',comparison);jout(out,'old_new_estimate_comparison.json',comparison);csvout(out,'sample_flow.csv',flow)
    jout(out,'job_recalculation_witnesses.json',jobmath)
    final_reviews=[];statusmap={r['event_id']:r for r in event_status}
    for r in review:
        final={**r};event=statusmap.get(r['event_id'])
        final['final_status']=event['status'] if event else 'excluded_before_run_freeze'
        final['final_configuration_exclusion_reasons']=event.get('configuration_exclusion_reasons',[]) if event else []
        final['before_resource_complete_runs']=event.get('before_complete_runs') if event else None
        final['after_resource_complete_runs']=event.get('after_complete_runs') if event else None
        final_reviews.append(final)
    csvout(out,'candidate_review_final.csv',final_reviews)
    # Necessary numerical/identity checks, not an additional code-quality suite.
    memberships=sum(len(r['memberships']) for r in manifest.values())
    for e in inc:
        assert e['before_complete_runs']<=e['before_frozen_runs']<=10 and e['after_complete_runs']<=e['after_frozen_runs']<=10
    for r in valid:
        sign=1 if r['direction']=='introduced' else -1
        assert math.isclose(r['delta_vm_seconds'],sign*(r['after_mean_vm_seconds']-r['before_mean_vm_seconds']),abs_tol=1e-8)
        assert math.isclose(r['delta_reference_cost_usd'],sign*(r['after_mean_reference_cost_usd']-r['before_mean_reference_cost_usd']),abs_tol=1e-10)
    for witness in jobmath:
        assert math.isclose(witness['vm_seconds'],sum(j['vm_seconds'] for j in witness['job_math']),abs_tol=1e-8)
        assert math.isclose(witness['reference_cost_usd'],sum(j['reference_cost_usd'] for j in witness['job_math']),abs_tol=1e-10)
    for r in runresults:assert (r['vm_seconds'] is not None)==(r['reference_cost_usd'] is not None)==r['resource_complete']
    checks={'unique_frozen_runs':len(manifest),'frozen_run_memberships':memberships,'unique_valid_events':len(inc),'valid_smell_memberships':len(valid),'valid_repositories':len({e['repository_id'] for e in inc}),'resource_complete_unique_runs':len(complete),'used_unique_runs':len(used),'valid_zero_runs':len(zero),'single_observation_events':sum(e['single_observation_side'] for e in inc),'run_exclusion_reason_counts':dict(exclusion),'same_runs_for_vm_and_cost':True,'present_minus_absent_verified':True,'max_ten_original_runs_no_replacements_verified':True,'job_recalculation_witness_count':len(jobmath),'job_recalculation_witness_os':sorted(witness_os),'seed':SEED,'bootstrap_reps':B,'pointwise_percentile_ci':'linear interpolated 0.025 and 0.975 quantiles of repository resamples','python_version':platform.python_version(),'input_sha256':{n:sha(HERE/n) for n in ('analysis_plan.md','candidate_review.csv','frozen_events.jsonl','frozen_run_manifest.jsonl','run_attribution.jsonl','frozen_job_metadata.jsonl','final_configuration_verification.jsonl')},'recomputation_script_sha256':sha(HERE/'recompute.py')}
    jout(out,'necessary_checks.json',checks)
    print(json.dumps({'checks':checks,'summary':summary},ensure_ascii=True,indent=2),flush=True)
if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output-dir',type=Path,default=HERE);args=parser.parse_args();main(args.output_dir.resolve())
