"""Read-only run3 impact audit; preserve the manuscript's context definitions.

No detector, collection, regression, manuscript, or figure is modified.
"""
from pathlib import Path
from collections import Counter, defaultdict
from functools import lru_cache
from concurrent.futures import ThreadPoolExecutor
import ast
import csv
import hashlib
import json
import re
import time
from types import SimpleNamespace
import numpy as np
import scipy
from scipy.stats import mannwhitneyu
import yaml

ROOT = Path('E:/gha_log_analysis')
CAN = ROOT / 'data_26_6_9_canonical_20260609'
RUN = ROOT / 'output_experimental_rq1_rq2_20261018_run3'
OLD = ROOT / 'output_canonical_rq1_final_20260921'
OUT = Path('E:/0_cache_smell/run3_impact_audit_20261019')
OUT.mkdir(exist_ok=True)
ALIASES = {'no_restore_keys': 'no_fallback_cache',
           'conflicting_cache_sources': 'overlapping_cache_mechanisms',
           'disposable_cache_key': 'overly_specific_cache_key'}
def norm(value): return ALIASES.get(value, value)
TYPES = ['no_dependency_cache', 'no_fallback_cache', 'redundant_cache_update',
         'cache_position_error', 'overlapping_cache_mechanisms', 'static_cache_key',
         'overly_specific_cache_key', 'no_docker_layer_cache', 'deprecated_cache_action',
         'unreachable_cache_write', 'cache_as_artifact']
def rows(path):
    with path.open(encoding='utf-8-sig') as stream:
        for line in stream:
            if line.strip(): yield json.loads(line)
def table(path):
    with path.open(encoding='utf-8-sig', newline='') as stream:
        return list(csv.DictReader(stream))
def write(name, data):
    with (OUT / name).open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(data[0]))
        writer.writeheader(); writer.writerows(data)
def sha(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024*1024), b''): h.update(block)
    return h.hexdigest()

# Load only these pure functions via AST: no project module initialization.
script = ROOT / 'scripts/finalize_canonical_rq1_20260921.py'
tree = ast.parse(script.read_text(encoding='utf-8-sig'))
selected = [node for node in tree.body if isinstance(node, ast.FunctionDef)
            and node.name in {'workflow_trigger_events', 'workflow_triggers', 'primary_role'}]
scope = {'Path': Path, 'Any': object, 're': re, 'yaml': yaml,
         'CORE_TRIGGERS': ('push', 'pull_request', 'schedule', 'workflow_dispatch', 'release'),
         'ROLE_TOKENS': [(v, v) for v in ['test', 'build', 'deploy', 'analyze', 'lint', 'release']]
                       + [('integrat', 'integration'), ('sync', 'sync'), ('update', 'update')]}
exec(compile(ast.Module(body=selected, type_ignores=[]), str(script), 'exec'), scope)
# LibYAML's all-string BaseLoader has the same parsing semantics. A separate
# 500-workflow equivalence check matched the original Python BaseLoader.
scope['yaml'] = SimpleNamespace(load=yaml.load, BaseLoader=yaml.CBaseLoader, YAMLError=yaml.YAMLError)
@lru_cache(maxsize=None)
def triggers(content_file): return scope['workflow_triggers'](CAN / str(content_file))

repos = {str(r['repository_id']): r for r in rows(CAN / 'repositories.jsonl')}
cohort = {r['workflow_key']: r for r in rows(CAN / 'rq1_cohort.jsonl')}
prevalence = json.loads((RUN/'rq1/smell_prevalence.json').read_text(encoding='utf-8-sig'))
repo_flags = defaultdict(set)
repo_wf = Counter(); repo_affected = Counter()
ecosystems = defaultdict(Counter); roles = defaultdict(Counter); trigger_groups = defaultdict(Counter)
multiplicity = Counter(); wf_counts = Counter(); instances = Counter(); pairs = Counter()
ndc_groups = defaultdict(Counter); errors = []; valid_keys = set()
manager_map = {'npm':'npm', 'yarn':'npm', 'pnpm':'npm', 'pip':'python',
               'python':'python', 'pipenv':'python', 'poetry':'python', 'uv':'python'}
ndc_opportunity_repos = set()
started = time.monotonic()
content_files = list(dict.fromkeys(r.get('content_file') for r in cohort.values()))
trigger_cache = {}
with ThreadPoolExecutor(max_workers=16) as pool:
    for start in range(0,len(content_files),2000):
        batch=content_files[start:start+2000]
        trigger_cache.update(zip(batch,pool.map(triggers,batch)))
        if start%20000==0:print('Trigger files',start+len(batch),'seconds',round(time.monotonic()-started,1),flush=True)
triggers.cache_clear()
for index, r in enumerate(rows(RUN/'analysis_inputs/rq1_scan_direct.jsonl'), 1):
    if index % 20000 == 0: print('Processed', index, 'seconds', round(time.monotonic()-started,1), flush=True)
    key = r['workflow_key']; assert key in cohort
    assert r['content_hash'] == cohort[key]['content_hash']
    d = r['detectors']['cache_smell']
    if d.get('error'):
        errors.append({'workflow_key':key, 'error':str(d['error'])}); continue
    assert key not in valid_keys; valid_keys.add(key)
    rid = str(r['repository_id']); repo_wf[rid] += 1
    counts = Counter()
    for name, value in d.get('smells_by_type', {}).items():
        if norm(name) in TYPES: counts[norm(name)] += len(value) if isinstance(value,list) else int(value or 0)
    flags = {name for name, count in counts.items() if count}
    repo_flags[rid].update(flags); repo_affected[rid] += bool(flags)
    wf_counts.update(flags); instances.update(counts)
    multiplicity[len(flags)] += 1
    for i,a in enumerate(TYPES):
        for b in TYPES[i+1:]:
            if a in flags and b in flags: pairs[(a,b)] += 1
    def accumulate(counter):
        counter['workflow_count'] += 1; counter['any_of_11'] += bool(flags)
        counter['any_non_ndc'] += bool(flags - {'no_dependency_cache'}); counter.update(flags)
    accumulate(ecosystems[repos[rid]['primary_ecosystem']])
    accumulate(roles[scope['primary_role'](d)])
    ts, parsed = trigger_cache[cohort[key]['content_file']]
    for trigger in ts if parsed else ['unclassified']: accumulate(trigger_groups[trigger])
    languages = set(); eligible = False
    for job in d.get('metadata', {}).get('jobs', {}).values():
        a = job.get('analysis', {})
        eligible |= bool(a.get('dep_langs') or a.get('dep_managers') or a.get('dep_install_indices') or a.get('dep_install_steps'))
        languages.update(a.get('dep_langs') or [])
        for manager in a.get('dep_managers') or []:
            assert manager in manager_map, manager
            languages.add(manager_map[manager])
    assert languages <= {'npm','python'}, languages
    assert eligible == bool(languages), key
    if eligible:
        group = 'both' if len(languages)==2 else next(iter(languages))
        ndc_groups[group]['eligible_workflows'] += 1
        ndc_groups[group]['ndc_workflows'] += 'no_dependency_cache' in flags
        ndc_opportunity_repos.add(rid)
    else: assert 'no_dependency_cache' not in flags

assert len(valid_keys)==118402 and len(repo_wf)==19324 and len(errors)==121
assert sum(repo_affected.values())==26388
assert sum(bool(v) for v in repo_flags.values())==10528
for raw, expected in prevalence['by_smell_type'].items():
    name=norm(raw)
    assert wf_counts[name]==expected['workflow_count']
    assert instances[name]==expected['instance_count']
    assert sum(name in f for f in repo_flags.values())==expected['repo_count']
assert sum(g['eligible_workflows'] for g in ndc_groups.values())==28450
assert len(ndc_opportunity_repos)==10196
assert sum(g['ndc_workflows'] for g in ndc_groups.values())==16138
old_trigger_counts=Counter()
for r in cohort.values():
    ts,parsed=trigger_cache[r['content_file']]
    old_trigger_counts.update(ts if parsed else ['unclassified'])
expected_triggers={r['trigger']:int(r['trigger_workflow_count']) for r in
    table(ROOT/'output_canonical_rq1_trigger_corrected_20260928/rq1_trigger_distribution.csv')}
assert dict(old_trigger_counts)==expected_triggers, (old_trigger_counts,expected_triggers)

old_prevalence={norm(r['smell_type']):r for r in table(OLD/'rq1_prevalence_overall.csv')}
prevalence_rows=[]
for raw, value in prevalence['by_smell_type'].items():
    name=norm(raw); old=old_prevalence[name]
    prevalence_rows.append({'smell':name, 'old_instances':int(old['instance_count']), 'new_instances':value['instance_count'],
        'old_workflows':int(old['workflow_count']), 'new_workflows':value['workflow_count'],
        'old_overall_workflow_pct':float(old['overall_workflow_prevalence_pct']),
        'new_overall_workflow_pct':100*value['workflow_count']/118402,
        'old_repositories':int(old['repository_count']), 'new_repositories':value['repo_count'],
        'old_overall_repository_pct':float(old['overall_repository_prevalence_pct']),
        'new_overall_repository_pct':100*value['repo_count']/19324,
        'new_eligible_workflows':value['eligible_workflows'], 'new_conditional_workflow_pct':value['workflow_pct'],
        'new_eligible_repositories':value['eligible_repos'], 'new_conditional_repository_pct':value['repo_pct']})
write('prevalence_comparison.csv',prevalence_rows)
def export_groups(name, groups):
    out=[]
    for group, counts in groups.items():
        out.append({'group':group, **dict(counts), **{k+'_pct':100*counts[k]/counts['workflow_count']
            for k in ['any_of_11','any_non_ndc']+TYPES}})
    # Make the sparse Counter fields explicit, retaining a rectangular table.
    fields=['group','workflow_count','any_of_11','any_non_ndc']+TYPES
    fields += [k+'_pct' for k in ['any_of_11','any_non_ndc']+TYPES]
    write(name,[{k:r.get(k,0) for k in fields} for r in out])
export_groups('ecosystem_preserving_paper_groups.csv',ecosystems)
export_groups('roles_preserving_paper_groups.csv',roles)
export_groups('triggers_preserving_paper_groups.csv',trigger_groups)
write('ndc_exclusive_metadata_opportunities.csv',[{'group':k,**dict(v),'ndc_pct':100*v['ndc_workflows']/v['eligible_workflows']} for k,v in ndc_groups.items()])
write('excluded_workflows.csv',errors)
write('multiplicity.csv',[{'number_of_types':k,'workflow_count':v,'corpus_pct':100*v/118402,
    'affected_workflow_pct':100*v/26388 if k else ''} for k,v in sorted(multiplicity.items())])
write('cooccurrence.csv',[{'smell_a':a,'smell_b':b,'workflows':n,'corpus_pct':100*n/118402,
    'jaccard':n/(wf_counts[a]+wf_counts[b]-n)} for (a,b),n in sorted(pairs.items(), key=lambda kv:-kv[1])])
concentration=defaultdict(Counter)
for rid,n in repo_wf.items():
    group = '1' if n==1 else '2-3' if n<=3 else '4-5' if n<=5 else '6-10' if n<=10 else '11-20' if n<=20 else '>20'
    c=concentration[group]; c['repositories']+=1; c['exposed_repositories']+=bool(repo_flags[rid])
    c['workflows']+=n; c['affected_workflows']+=repo_affected[rid]
write('repository_concentration.csv',[{'group':k,**dict(v),'repository_pct':100*v['exposed_repositories']/v['repositories'],
    'workflow_pct':100*v['affected_workflows']/v['workflows']} for k,v in sorted(concentration.items())])

# Same canonical repository measurements and test settings as the manuscript.
feature_keys={'stargazers':'stargazers','contributors':'contributors','workflow_count':'workflow_count',
              'runs_90d':'total_runs_90d','commits':'commits'}
reference={rid for rid in repo_wf if not repo_flags[rid]}
assert len(reference)==8796
stat_rows=[]
for smell in TYPES:
    target={rid for rid in repo_wf if smell in repo_flags[rid]}
    assert target.isdisjoint(reference)
    for feature,raw_key in feature_keys.items():
        x=np.array([repos[rid][raw_key] for rid in sorted(target) if isinstance(repos[rid].get(raw_key),(int,float))],dtype=float)
        y=np.array([repos[rid][raw_key] for rid in sorted(reference) if isinstance(repos[rid].get(raw_key),(int,float))],dtype=float)
        test=mannwhitneyu(x,y,alternative='two-sided',method='asymptotic',use_continuity=True)
        stat_rows.append({'smell':smell,'feature':feature,'n_target':len(x),'n_reference':len(y),
            'median_target':float(np.median(x)),'median_reference':float(np.median(y)),
            'U':float(test.statistic),'p':float(test.pvalue),'cliffs_delta':float(2*test.statistic/(len(x)*len(y))-1),
            'missing_target':len(target)-len(x),'missing_reference':len(reference)-len(y)})
order=np.argsort([r['p'] for r in stat_rows]); adj=np.maximum.accumulate((55-np.arange(55))*np.array([r['p'] for r in stat_rows])[order])
for index,value in zip(order,adj):stat_rows[index]['p_holm']=float(min(value,1))
write('repository_distribution_tests.csv',stat_rows)
old_stats={(norm(r['smell']),r['feature']):r for r in table(Path('E:/0_cache_smell/revision_20261015/repository_distribution_tests.csv'))}
comparison=[]
for r in stat_rows:
    old=old_stats[(r['smell'],r['feature'])]
    o={'smell':r['smell'],'feature':r['feature']}
    for k in r:
        if k not in {'smell','feature'}:o['old_'+k]=old[k];o['new_'+k]=r[k]
    comparison.append(o)
write('repository_distribution_tests_comparison.csv',comparison)

manifest={'read_only_sources':True,'scope':'run3 detection labels; original paper context grouping and canonical repository measurements',
    'valid_workflows':len(valid_keys),'repositories':len(repo_wf),'affected_workflows':sum(repo_affected.values()),
    'affected_repositories':sum(bool(v) for v in repo_flags.values()),'reference_repositories':len(reference),
    'excluded_workflows':len(errors),'multiplicity':dict(multiplicity),
    'ndc_opportunities':{k:dict(v) for k,v in ndc_groups.items()},
    'context_definition':'original canonical primary_ecosystem, original job-name substring role function, original bounded top-level on-block parser',
    'trigger_parser_engine':'LibYAML CBaseLoader; same all-string BaseLoader semantics; validated against Python BaseLoader on 500 examples and original full-cohort trigger totals',
    'ndc_opportunity_definition':'broad install metadata predicate used in run3 smell_prevalence.json; NOT strict non-auxiliary NDC decision scope',
    'source_files':{str(p):sha(p) for p in [RUN/'analysis_inputs/rq1_scan_direct.jsonl', RUN/'rq1/smell_prevalence.json',CAN/'repositories.jsonl',CAN/'rq1_cohort.jsonl',script]},
    'software':{'numpy':np.__version__,'scipy':scipy.__version__,'pyyaml':yaml.__version__},
    'statistics':'two-sided asymptotic tie/continuity-corrected Mann-Whitney; Holm over 55 tests; target-oriented Cliff delta; feature-wise missing omission',
    'not_run':['detector','GitHub collection','RQ2 models','plot rendering','manuscript editing']}
(OUT/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({k:v for k,v in manifest.items() if k not in {'source_files'}},ensure_ascii=False,indent=2),flush=True)
