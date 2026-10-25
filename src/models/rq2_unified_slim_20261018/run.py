"""EXPERIMENTAL: full-population unified models, compact controls, exact grouping."""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import time

import numpy as np
from scipy.special import expit

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))
from src.smell_names import normalize_smell_data
from features import extract
from engine import fit, covariance, coefficient_rows

TYPES = ('no_dependency_cache', 'no_fallback_cache', 'redundant_cache_update',
         'cache_position_error', 'conflicting_cache_sources', 'static_cache_key',
         'overly_specific_cache_key', 'no_docker_layer_cache', 'deprecated_cache_action',
         'unreachable_cache_write', 'cache_as_artifact')
DEFAULT_INPUT = ROOT / 'output_experimental_rq1_rq2_20261018_run3/rq2_model_input'


def rows(path):
    with path.open(encoding='utf-8') as f:
        for line in f:
            if line.strip():
                yield json.loads(line)


def dump(path, data):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    temporary.replace(path)


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda:f.read(1024*1024), b''):
            h.update(block)
    return h.hexdigest()


def feature_worker(task):
    version, path, workflow_path, expected_hash, repository_name = task
    try:
        path=Path(path)
        if not path.is_file():
            folder=ROOT/'data_26_6_9_complete_20260609/workflows'/repository_name
            for candidate in folder.glob(Path(workflow_path).stem+'*'):
                if not candidate.is_file(): continue
                raw=candidate.read_bytes()
                variants=(raw,raw.replace(b'\r\n',b'\n'),candidate.read_text(encoding='utf-8').encode('utf-8'))
                if any(hashlib.sha256(value).hexdigest()==expected_hash for value in variants):
                    path=candidate; break
        result=extract(path.read_text(encoding='utf-8'), workflow_path)
        result['yaml_source']=str(path)
        return version,result,None
    except Exception as exc:
        return version, None, f'{type(exc).__name__}: {exc}'


def prepare(input_dir, out, workers, timings, reuse_features=None):
    started = time.perf_counter()
    versions = {}
    label_errors = []
    for row in rows(input_dir/'version_scan_results.jsonl'):
        state = row['detector']
        if state.get('error'):
            label_errors.append(row['version_id'])
            continue
        labels = set()
        for smell in state.get('smells', []):
            name = normalize_smell_data(smell['type'])
            labels.add('overly_specific_cache_key' if name=='disposable_cache_key' else name)
        versions[row['version_id']] = dict(repository_id=str(row['repository_id']),repository_name=row['repository_name'],
            workflow_path=row['workflow_path'], content_hash=row['content_hash'], labels=sorted(labels & set(TYPES)),
            detector_version=row.get('detector_version'))
    cached={r['version_id']:r for r in rows(reuse_features/'workflow_features.jsonl')} if reuse_features else {}
    for key,record in cached.items():
        if key not in versions or record['content_hash']!=versions[key]['content_hash']:
            raise ValueError('Feature cache identity/content mismatch')
        versions[key].update({name:value for name,value in record.items() if name!='version_id'})
    tasks = [(key,str(ROOT/'data_26_6_9_canonical_20260609/content'/(v['content_hash']+'.yml')),v['workflow_path'],v['content_hash'],v['repository_name'])
             for key,v in versions.items() if key not in cached]
    feature_errors = []
    cache = out/'workflow_features.jsonl'
    coverage = {name:Counter() for name in ['workflow_role','ecosystem','runner_os','runner_hosting']}
    with cache.open('w',encoding='utf-8') as f, ProcessPoolExecutor(max_workers=workers) as pool:
        for key,feature in cached.items():
            for name in coverage: coverage[name][feature[name]]+=1
            f.write(json.dumps(dict(version_id=key,**versions[key]),ensure_ascii=False)+'\n')
        for n,(key,feature,error) in enumerate(pool.map(feature_worker,tasks,chunksize=40),1):
            if error:
                feature_errors.append(dict(version_id=key,error=error)); continue
            versions[key].update(feature)
            for name in coverage:
                coverage[name][feature[name]] += 1
            f.write(json.dumps(dict(version_id=key,**versions[key]),ensure_ascii=False)+'\n')
            if n % 5000 == 0:
                print(f'YAML controls {n}/{len(tasks)}',flush=True)
    for error in feature_errors:
        versions.pop(error['version_id'])
    dump(out/'feature_audit.json',dict(detector_error_versions=len(label_errors),feature_errors=feature_errors,
         coverage={k:dict(v) for k,v in coverage.items()},rules='features.py; static definitions; no outcome/CCS-based role classification'))
    timings['feature_extraction_seconds'] = time.perf_counter()-started
    started = time.perf_counter()
    groups = {}
    counts = Counter()
    for row in rows(input_dir/'run_records.jsonl'):
        counts['input_runs'] += 1
        version = row.get('version_id')
        if version not in versions:
            counts['excluded_detector_or_feature_error_runs'] += 1; continue
        if row.get('status') != 'completed':
            counts['excluded_not_completed'] += 1; continue
        created = str(row.get('created_at') or '')
        if not created:
            counts['excluded_missing_created_at'] += 1; continue
        day = datetime.fromisoformat(created.replace('Z','+00:00')).date()
        # Fixed bins anchored to the study window; not data-selected calendar levels.
        week = str((day-datetime(2026,5,10).date()).days//7)
        event = str(row.get('event') or 'unknown')
        key = (version,event,week)
        if key not in groups:
            groups[key] = np.zeros(8,dtype=float)
        a = groups[key]
        a[7] += 1
        conclusion = row.get('conclusion')
        success = conclusion == 'success'
        if conclusion in {'success','failure','startup_failure','timed_out'}:
            a[0] += 1; a[1] += success
        try:
            duration = float(row.get('duration_seconds') or 0)
        except (ValueError,TypeError):
            duration = 0.
        if np.isfinite(duration) and 0 < duration <= 35*24*3600:
            log_duration = float(np.log(duration))
            a[2] += 1; a[3] += log_duration; a[4] += log_duration**2
            if success:
                a[5] += log_duration; a[6] += log_duration**2
        # Successful duration denominator needs its own positive-duration count.
        if success and np.isfinite(duration) and 0 < duration <= 35*24*3600:
            counts['valid_successful_duration_runs'] += 1
            # Stored separately to avoid deriving it from technical successes with invalid duration.
            counts[('success_duration',key)] += 1
        counts['included_completed_runs'] += 1
        if counts['input_runs'] % 200000 == 0:
            print(f"Run aggregation {counts['input_runs']:,}",flush=True)
    group_keys = sorted(groups)
    values = np.vstack([groups[key] for key in group_keys])
    success_n = np.asarray([counts[('success_duration',key)] for key in group_keys],dtype=float)
    serial_counts = {k:v for k,v in counts.items() if isinstance(k,str)}
    dump(out/'sample_audit.json',dict(**serial_counts,group_count=len(group_keys),version_count=len(versions)))
    with (out/'group_keys.jsonl').open('w',encoding='utf-8') as f:
        for key in group_keys:
            f.write(json.dumps(key)+'\n')
    np.savez_compressed(out/'grouped_outcomes.npz',values=values,success_n=success_n)
    timings['run_read_and_aggregation_seconds'] = time.perf_counter()-started
    return versions,group_keys,values,success_n


def design(versions, keys):
    names = ['intercept']+list(TYPES)
    columns = [np.ones(len(keys))]
    columns += [np.array([name in versions[key[0]]['labels'] for key in keys],dtype=float) for name in TYPES]
    for field in ('job_count','non_cache_step_count'):
        col = np.log1p([versions[key[0]][field] for key in keys])
        # Scaling only affects parameterization, not grouped/expanded equivalence.
        col = (col-np.mean(col))/(np.std(col) or 1.)
        names.append('log1p_'+field+'_z'); columns.append(col)
    names.append('has_matrix'); columns.append(np.array([versions[key[0]]['has_matrix'] for key in keys],dtype=float))
    for field in ('workflow_role','ecosystem','runner_os','runner_hosting','event','calendar_week'):
        vals = [key[1] if field=='event' else (key[2] if field=='calendar_week' else versions[key[0]][field]) for key in keys]
        # Most frequent level is a reproducible reference; every remaining level gets a coefficient.
        levels = sorted(set(vals),key=lambda level:(-vals.count(level),level))
        for level in levels[1:]:
            names.append(field+'='+level); columns.append(np.array([v==level for v in vals],dtype=float))
    return np.column_stack(columns),names


def independent_columns(x,names):
    # Keep exposures ahead of controls; mark ALL exposure coefficients involved in exact aliases.
    _,s,vt = np.linalg.svd(x,full_matrices=x.shape[0]<x.shape[1])
    tolerance = max(x.shape)*np.finfo(float).eps*s[0]
    rank = int(np.sum(s>tolerance))
    unidentified = {names[i] for i in range(len(names)) if names[i] in TYPES and np.any(np.abs(vt[rank:,i])>1e-7)}
    keep = []
    q = np.empty((len(x),0))
    for i in range(x.shape[1]):
        col = x[:,i].copy()
        for _ in range(2):
            if q.shape[1]: col -= q @ (q.T @ col)
        size = np.linalg.norm(col)
        if size > 1e-8*max(1.,np.linalg.norm(x[:,i])):
            keep.append(i); q=np.column_stack([q,col/size])
    return x[:,keep],[names[i] for i in keep],dict(rank=rank,original_columns=len(names),
        dropped=[name for i,name in enumerate(names) if i not in keep],unidentified_smells=sorted(unidentified))


def analyze_endpoint(endpoint,versions,keys,values,success_n,out,bootstrap,seed):
    logistic = endpoint=='reliability'
    if logistic:
        count,total,sq = values[:,0],values[:,1],np.zeros(len(keys))
    elif endpoint=='all_completed_duration':
        count,total,sq = values[:,2],values[:,3],values[:,4]
    else:
        count,total,sq = success_n,values[:,5],values[:,6]
    active = count>0
    keys = [key for key,yes in zip(keys,active) if yes]
    count,total,sq = count[active],total[active],sq[active]
    version_n = Counter()
    for key,n in zip(keys,count): version_n[key[0]] += n
    weight = np.array([1./version_n[key[0]] for key in keys])
    repo = np.array([versions[key[0]]['repository_id'] for key in keys])
    repo_names,repo_codes=np.unique(repo,return_inverse=True)
    x,names = design(versions,keys)
    x,names,rank_audit = independent_columns(x,names)
    print(f'{endpoint}: runs={int(sum(count)):,}, design groups={len(x):,}, columns={len(names)}',flush=True)
    start = time.perf_counter()
    beta,bread,converged,iterations=fit(x,count,total,sq,weight,logistic)
    fit_seconds=time.perf_counter()-start
    # Marginal separation is diagnostic, never a claim to exhaust all separation patterns.
    separation=[]
    if logistic:
        for name in TYPES:
            if name not in names: continue
            col=x[:,names.index(name)]
            for state in (0,1):
                sel=col==state
                trials=float(sum(count[sel])); wins=float(sum(total[sel]))
                if trials and (wins==0 or wins==trials):
                    separation.append(dict(smell_type=name,state=state,trials=trials,successes=wins))
    unstable=bool(logistic and (not converged or separation or np.max(np.abs(beta))>20))
    covariance_start=time.perf_counter()
    try:
        cov=covariance(x,count,total,weight,repo_codes,beta,bread,logistic)
        coeff=coefficient_rows(names,beta,cov,TYPES,suppress=unstable)
    except np.linalg.LinAlgError:
        unstable=True
        coeff=[dict(feature=name,coefficient=float(value),inference_status='singular_information',cluster_robust_p=None,fdr_q=None) for name,value in zip(names,beta)]
    unidentified=set(rank_audit['unidentified_smells'])
    for row in coeff:
        if row['feature'] in unidentified:
            row.update(inference_status='not_identifiable',cluster_robust_p=None,fdr_q=None)
    # Recompute FDR after excluding any non-identifiable target coefficients.
    from engine import bh
    qvals=bh({r['feature']:r['cluster_robust_p'] for r in coeff if r['feature'] in TYPES})
    for row in coeff: row['fdr_q']=qvals.get(row['feature'])
    covariance_seconds=time.perf_counter()-covariance_start
    boot=[]; failed_bootstrap=0
    boot_start=time.perf_counter()
    if bootstrap and unstable:
        print('Skipping ordinary bootstrap for separation-limited/nonconverged logistic; see Firth sensitivity',flush=True)
    elif bootstrap:
        rng=np.random.default_rng(seed)
        with (out/(endpoint+'_bootstrap.jsonl')).open('w',encoding='utf-8') as f:
            for replicate in range(bootstrap):
                multiples=rng.multinomial(len(repo_names),np.full(len(repo_names),1./len(repo_names)))
                w=weight*multiples[repo_codes]
                try:
                    b,_,ok,_=fit(x,count,total,sq,w,logistic,initial=beta if logistic else None)
                    if not ok: raise ValueError('nonconverged bootstrap')
                    boot.append(b); f.write(json.dumps(dict(replicate=replicate,coefficients=b.tolist()))+'\n'); f.flush()
                except (ValueError,np.linalg.LinAlgError,FloatingPointError): failed_bootstrap+=1
                if (replicate+1)%50==0: print(f'{endpoint} bootstrap {replicate+1}/{bootstrap}',flush=True)
        if len(boot)>=20:
            intervals=np.percentile(np.asarray(boot),[2.5,97.5],axis=0)
            for i,row in enumerate(coeff): row['bootstrap_exponentiated_ci_95']=np.exp(np.clip(intervals[:,i],-700,700)).tolist()
    result=dict(endpoint=endpoint,experimental=True,status='separation_limited_or_nonconverged' if unstable else 'ok',
        runs=int(sum(count)),workflow_versions=len(version_n),repositories=len(repo_names),design_groups=len(x),
        coefficients=coeff,not_estimable_smells=sorted(set(TYPES)-set(names)|unidentified),
        rank_audit=rank_audit,fit_converged=converged,fit_iterations=iterations,
        separation_diagnostics=separation,
        grouping='Exact sufficient statistics for identical version/event/calendar-week design vectors; endpoint-specific version weights',
        bootstrap_requested=bootstrap,bootstrap_valid_replicates=len(boot),bootstrap_failed_replicates=failed_bootstrap,
        timings=dict(fit_seconds=fit_seconds,covariance_seconds=covariance_seconds,bootstrap_seconds=time.perf_counter()-boot_start))
    if not logistic:
        fitted=x@beta
        sse=float(sum(weight*(sq-2*fitted*total+count*fitted*fitted)))
        mean=float(sum(weight*total)/sum(weight*count))
        sst=float(sum(weight*(sq-2*mean*total+count*mean*mean)))
        result['weighted_log_r_squared']=1-sse/sst if sst>0 else None
    dump(out/(endpoint+'.json'),result)
    if unstable and logistic:
        print('Fitting unified Firth sensitivity; model-based intervals only, outside primary FDR',flush=True)
        from scripts.analyze_rq2_regression import fit_firth_logistic
        firth_start=time.perf_counter()
        ffit=fit_firth_logistic(x,total,count,weight)
        fcoeff=coefficient_rows(names,ffit['coefficients'],ffit['covariance'],TYPES,suppress=True)
        for row in fcoeff: row['inference_status']='firth_model_based_sensitivity_not_cluster_robust'; row['model_based_standard_error']=row.pop('standard_error')
        sensitivity=dict(status='ok' if ffit['converged'] else 'nonconverged',coefficients=fcoeff,
            uncertainty='model-based Firth sensitivity only; not repository-cluster robust; excluded from primary FDR',
            iterations=ffit['iterations'],fit_seconds=time.perf_counter()-firth_start)
        dump(out/'reliability_firth_sensitivity.json',sensitivity)
        result['firth_sensitivity_status']=sensitivity['status']
        result['timings']['firth_seconds']=sensitivity['fit_seconds']
        dump(out/(endpoint+'.json'),result)
    return result


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--input-dir',type=Path,default=DEFAULT_INPUT)
    p.add_argument('--output-dir',type=Path,required=True)
    p.add_argument('--reuse-prepared',type=Path,help='Reuse verified grouped inputs; hash match of original run/scan files required')
    p.add_argument('--reuse-features',type=Path,help='Reuse verified static YAML controls; retry previously missing feature records')
    p.add_argument('--bootstrap',type=int,default=0,help='Optional sensitivity; primary robust inference does not require bootstrap')
    p.add_argument('--workers',type=int,default=6)
    p.add_argument('--seed',type=int,default=42)
    args=p.parse_args()
    out=args.output_dir.resolve()
    if out.exists(): raise FileExistsError(f'Refusing to overwrite {out}')
    out.mkdir(parents=True)
    started=time.perf_counter(); timings={}
    manifest=dict(experimental=True,status='running',started_at=datetime.now(timezone.utc).isoformat(),
        input_dir=str(args.input_dir.resolve()),bootstrap=args.bootstrap,seed=args.seed,
        design='full-population unified, no per-smell applicability filtering; slim controls',
        code_sha256={str(f.relative_to(HERE)):digest(f) for f in HERE.glob('*.py')},
        input_sha256={},weighting='endpoint-specific version-balanced',cluster_unit='repository',
        endpoints_completed=[],timings=timings)
    dump(out/'manifest.json',manifest)
    try:
        hash_start=time.perf_counter()
        for name in ['run_records.jsonl','version_scan_results.jsonl']:
            manifest['input_sha256'][name]=digest(args.input_dir/name)
        timings['input_hash_seconds']=time.perf_counter()-hash_start
        if args.reuse_features:
            reference=json.loads((args.reuse_features/'manifest.json').read_text(encoding='utf-8'))
            if reference['input_sha256']!=manifest['input_sha256']: raise ValueError('Feature input hashes differ')
            if reference['code_sha256']['features.py']!=manifest['code_sha256']['features.py']: raise ValueError('Feature rules differ')
            manifest['feature_cache_source']=str(args.reuse_features.resolve())
        if args.reuse_prepared:
            reference=json.loads((args.reuse_prepared/'manifest.json').read_text(encoding='utf-8'))
            if reference['input_sha256']!=manifest['input_sha256']: raise ValueError('Prepared input hash mismatch')
            if reference['code_sha256']!=manifest['code_sha256']: raise ValueError('Prepared code hash mismatch')
            versions={r['version_id']:r for r in rows(args.reuse_prepared/'workflow_features.jsonl')}
            keys=[tuple(k) for k in rows(args.reuse_prepared/'group_keys.jsonl')]
            data=np.load(args.reuse_prepared/'grouped_outcomes.npz'); values=data['values']; success_n=data['success_n']
            manifest['prepared_source']=str(args.reuse_prepared)
        else:
            versions,keys,values,success_n=prepare(args.input_dir,out,args.workers,timings,args.reuse_features)
        dump(out/'manifest.json',manifest)
        for index,endpoint in enumerate(['all_completed_duration','successful_duration','reliability']):
            endpoint_start=time.perf_counter()
            result=analyze_endpoint(endpoint,versions,keys,values,success_n,out,args.bootstrap,args.seed+index)
            timings[endpoint+'_seconds']=time.perf_counter()-endpoint_start
            manifest['endpoints_completed'].append(dict(endpoint=endpoint,status=result['status']))
            dump(out/'manifest.json',manifest)
            print(f'Completed {endpoint}: {timings[endpoint+"_seconds"]:.1f}s',flush=True)
        timings['total_seconds']=time.perf_counter()-started
        manifest.update(status='complete',completed_at=datetime.now(timezone.utc).isoformat())
        dump(out/'manifest.json',manifest)
    except BaseException:
        import traceback
        manifest.update(status='failed',error=traceback.format_exc())
        dump(out/'manifest.json',manifest)
        raise


if __name__=='__main__': main()
