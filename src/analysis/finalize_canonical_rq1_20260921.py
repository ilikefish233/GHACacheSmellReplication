"""Freeze canonical RQ1 descriptive results without rerunning the detector.

This script reads only the canonical RQ1 cohort, snapshots, cached detector
states, repository metadata, and their already-derived direct scan view.  It
validates the content-hash/version join before emitting results and figures.
"""
from __future__ import annotations

import sys as _smell_sys
from pathlib import Path as _SmellPath
_SMELL_PROJECT_ROOT = str(_SmellPath(__file__).resolve().parents[1])
if _SMELL_PROJECT_ROOT not in _smell_sys.path:
    _smell_sys.path.insert(0, _SMELL_PROJECT_ROOT)
from src.smell_names import loads_smell_json


import argparse
import csv
import hashlib
import itertools
import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import yaml


ROOT = Path(__file__).resolve().parents[1]
CANONICAL = ROOT / "data_26_6_9_canonical_20260609"
DERIVED = ROOT / "output_canonical_rerun_20260918" / "analysis_inputs" / "canonical_views" / "rq1_scan_direct.jsonl"
PRIOR_DISTRIBUTION = ROOT / "output_canonical_rerun_20260918" / "rq1" / "prevalence_retry2" / "smell_distribution.json"
OUT = ROOT / "output_canonical_rq1_final_20260921"
DV = "cache_smell_detector_sha256:a6f162b5715c872215a7135407f4028ba7007ddef519143aa439ae0946456340"
TV = "cache-smell-taxonomy-2026-09-01"
CUTOFF = "2026-06-09T23:59:59Z"
TYPES = [
    "no_dependency_cache", "no_fallback_cache", "redundant_cache_update",
    "cache_position_error", "conflicting_cache_sources", "static_cache_key",
    "overly_specific_cache_key", "no_docker_layer_cache", "deprecated_cache_action",
    "unreachable_cache_write", "cache_as_artifact",
]
RAW_KEY = {"overly_specific_cache_key": "disposable_cache_key"}
DISPLAY = {
    "no_dependency_cache": "No dependency cache", "no_fallback_cache": "No fallback cache",
    "redundant_cache_update": "Redundant cache update", "cache_position_error": "Cache position error",
    "conflicting_cache_sources": "Overlapping cache mechanisms", "static_cache_key": "Static cache key",
    "overly_specific_cache_key": "Overly specific cache key", "no_docker_layer_cache": "No Docker layer cache",
    "deprecated_cache_action": "Deprecated cache action", "unreachable_cache_write": "Unreachable cache write",
    "cache_as_artifact": "Cache as artifact", "any_of_11": "Any of the 11 detectable smells",
}
HASHES = {
    "rq1_cohort.jsonl": "67cd6f0fbaca7f8ed7dea769b34d886eb2d2a59c44c412491ed093a70ed1bec7",
    "workflow_snapshots.jsonl": "c65cdfe51571a3c59adef3155a2a89c7a9bd1ae032af25b95ebf582e34dd4060",
    "detector_states.jsonl": "7505739ee247bcfc2969958253b968716f6a7875d8e73c5a6f520e6d59eb37d0",
}
FEATURES = {
    "stargazers": [("<100", 0, 100), ("100-999", 100, 1000), ("1,000-4,999", 1000, 5000), (">=5,000", 5000, None)],
    "contributors": [("<10", 0, 10), ("10-49", 10, 50), ("50-199", 50, 200), (">=200", 200, None)],
    "workflow_count": [("0-2", 0, 3), ("3-5", 3, 6), ("6-10", 6, 11), (">10", 11, None)],
    "total_runs_90d": [("<100", 0, 100), ("100-299", 100, 300), ("300-599", 300, 600), (">=600", 600, None)],
    "commits": [("<500", 0, 500), ("500-1,999", 500, 2000), ("2,000-4,999", 2000, 5000), (">=5,000", 5000, None)],
}
CORE_TRIGGERS = ("push", "pull_request", "schedule", "workflow_dispatch", "release")
ROLE_TOKENS = [("test", "test"), ("build", "build"), ("deploy", "deploy"), ("analyze", "analyze"), ("lint", "lint"), ("release", "release"), ("integrat", "integration"), ("sync", "sync"), ("update", "update")]


def rows(path: Path) -> Iterable[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            if line.strip():
                value = loads_smell_json(line)
                if not isinstance(value, dict):
                    raise ValueError(f"non-object JSON at {path}:{number}")
                yield value


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def key(record: dict[str, Any]) -> tuple[str, str]:
    return str(record.get("repository_id") or ""), str(record.get("workflow_path") or "")


def number(value: Any) -> int:
    return len(value) if isinstance(value, list) else int(value or 0)


def labels(state: dict[str, Any]) -> dict[str, int]:
    values = state.get("smells_by_type") or {}
    return {name: number(values.get(RAW_KEY.get(name, name))) for name in TYPES}


def workflow_trigger_events(content: Path) -> tuple[set[str], str]:
    """Read only the top-level ``on`` value, not nested event filters.

    BaseLoader keeps the YAML 1.1 keyword ``on`` as a string.  Parsing its
    bounded block supports scalar, list, and mapping forms while ensuring
    ``branches``, ``paths``, ``types``, and ``cron`` are not event names.
    """
    try:
        text = content.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return set(), "unreadable"
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if re.match(r"^(?:on|'on'|\"on\")\s*:", line):
            start = i
            break
    if start is None:
        return set(), "no_top_level_on"
    end = len(lines)
    for i in range(start + 1, len(lines)):
        line = lines[i]
        if line.strip() and not line.lstrip().startswith("#") and not line[0].isspace():
            end = i
            break
    try:
        parsed = yaml.load("\n".join(lines[start:end]), Loader=yaml.BaseLoader)
    except yaml.YAMLError:
        return set(), "invalid_on_block"
    if not isinstance(parsed, dict):
        return set(), "invalid_on_block"
    on_value = parsed.get("on")
    if isinstance(on_value, str):
        events = {on_value.strip().casefold()} if on_value.strip() else set()
    elif isinstance(on_value, list):
        events = {value.strip().casefold() for value in on_value if isinstance(value, str) and value.strip()}
    elif isinstance(on_value, dict):
        events = {str(value).strip().casefold() for value in on_value if str(value).strip()}
    else:
        events = set()
    events.difference_update({"null", "none", "~"})
    return events, "parsed" if events else "empty_on"


def workflow_triggers(content: Path) -> tuple[set[str], bool]:
    """Map parsed top-level events to the five core groups or ``other``."""
    events, reason = workflow_trigger_events(content)
    return {event if event in CORE_TRIGGERS else "other" for event in events}, reason == "parsed"


def primary_role(state: dict[str, Any]) -> str:
    jobs = ((state.get("metadata") or {}).get("jobs") or {})
    names = " ".join(str(name) + " " + str((data or {}).get("display_name") or "") for name, data in jobs.items()).casefold()
    found = [role for token, role in ROLE_TOKENS if token in names]
    if not found:
        return "unknown" if not jobs else "other"
    return found[0] if len(set(found)) == 1 else "mixed"


def opportunity(state: dict[str, Any]) -> set[str]:
    jobs = ((state.get("metadata") or {}).get("jobs") or {})
    dep = cache = docker = False
    cache_jobs: list[tuple[str, list[Any]]] = []
    for jobname, data in jobs.items():
        analysis = ((data or {}).get("analysis") or {})
        dep = dep or bool(analysis.get("dep_langs") or analysis.get("dep_managers") or analysis.get("dep_install_indices") or analysis.get("dep_install_steps"))
        steps = analysis.get("cache_steps") or []
        if steps:
            cache = True
            cache_jobs.append((str(jobname), analysis.get("needs") or []))
        docker = docker or bool(analysis.get("docker_build_detected"))
    result: set[str] = set()
    if dep: result.add("no_dependency_cache")
    if cache: result.update({"no_fallback_cache", "cache_position_error", "conflicting_cache_sources", "static_cache_key", "overly_specific_cache_key", "deprecated_cache_action", "unreachable_cache_write", "cache_as_artifact"})
    if docker: result.add("no_docker_layer_cache")
    if len(cache_jobs) >= 2 and any(not needs for _, needs in cache_jobs): result.add("redundant_cache_update")
    return result


def bin_value(value: Any, bins: list[tuple[str, float, float | None]]) -> str:
    if not isinstance(value, (int, float)):
        return "Missing"
    for label, low, high in bins:
        if value >= low and (high is None or value < high): return label
    return "Missing"


def write_csv(path: Path, fields: list[str], values: Iterable[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader(); writer.writerows(values)


def load_and_validate() -> tuple[dict[tuple[str, str], tuple[dict[str, Any], dict[str, Any], dict[str, Any]]], dict[str, dict[str, Any]], dict[str, Any]]:
    manifests = loads_smell_json((CANONICAL / "dataset_manifest.json").read_text(encoding="utf-8"))
    actual = {name: sha256(CANONICAL / name) for name in HASHES}
    if actual != HASHES: raise RuntimeError(f"canonical hashes differ: {actual}")
    if manifests.get("study_cutoff") != CUTOFF or manifests["detector"]["detector_version"] != DV or manifests["detector"]["taxonomy_version"] != TV:
        raise RuntimeError("manifest cutoff or detector/taxonomy version differs")
    cohort: dict[tuple[str, str], dict[str, Any]] = {}
    snapshots: dict[tuple[str, str], dict[str, Any]] = {}
    states: dict[tuple[str, str, str], dict[str, Any]] = {}
    for record in rows(CANONICAL / "rq1_cohort.jsonl"):
        if key(record) in cohort: raise RuntimeError("duplicate RQ1 workflow key")
        cohort[key(record)] = record
    for record in rows(CANONICAL / "workflow_snapshots.jsonl"):
        if key(record) in snapshots: raise RuntimeError("duplicate snapshot key")
        snapshots[key(record)] = record
    for record in rows(CANONICAL / "detector_states.jsonl"):
        state_key = (record.get("content_hash"), record.get("detector_version"), record.get("taxonomy_version"))
        if state_key in states: raise RuntimeError("duplicate detector-state key")
        states[state_key] = record
    repos = {str(record.get("repository_id")): record for record in rows(CANONICAL / "repositories.jsonl")}
    joined = {}
    errors = []
    for workflow_key, cohort_record in cohort.items():
        snapshot = snapshots.get(workflow_key)
        if snapshot is None or snapshot.get("content_hash") != cohort_record.get("content_hash") or snapshot.get("study_cutoff") != CUTOFF:
            errors.append(workflow_key); continue
        state = states.get((cohort_record.get("content_hash"), DV, TV))
        if state is None: errors.append(workflow_key); continue
        joined[workflow_key] = (cohort_record, snapshot, state)
    if len(cohort) != 118523 or len(joined) != 118523 or len({item[0] for item in joined}) != 19330 or errors:
        raise RuntimeError(f"canonical join failed: cohort={len(cohort)}, joined={len(joined)}, repositories={len({item[0] for item in joined})}, errors={len(errors)}")
    return joined, repos, {"hashes": actual, "manifest": manifests, "cohort": len(cohort), "snapshots": len(snapshots), "states": len(states)}


def verify_direct(joined: dict[tuple[str, str], tuple[dict[str, Any], dict[str, Any], dict[str, Any]]]) -> None:
    seen = set()
    for record in rows(DERIVED):
        k = key(record); seen.add(k); joined_record = joined.get(k)
        if joined_record is None or record.get("content_hash") != joined_record[0].get("content_hash"):
            raise RuntimeError("derived direct view has an unknown or mismatched workflow")
        cached = ((record.get("detectors") or {}).get("cache_smell") or {}).get("smells_by_type") or {}
        if cached != (joined_record[2].get("smells_by_type") or {}):
            raise RuntimeError("derived direct view disagrees with cached canonical detector state")
    if seen != set(joined): raise RuntimeError("derived direct view does not cover the canonical RQ1 join")


def compute(joined: dict[tuple[str, str], tuple[dict[str, Any], dict[str, Any], dict[str, Any]]], repos: dict[str, dict[str, Any]]) -> dict[str, Any]:
    wf = Counter(); inst = Counter(); rep = {name: set() for name in TYPES}; eligible = {name: set() for name in TYPES}; eligible_repo = {name: set() for name in TYPES}
    flags: dict[tuple[str, str], set[str]] = {}; roles=Counter(); role_flags=Counter(); feature_rows=[]; trigger_rows=[]; missing_repo=0
    for k, (cohort, snapshot, state) in joined.items():
        labels_now = labels(state); present = {name for name, value in labels_now.items() if value}
        flags[k] = present
        for name, value in labels_now.items():
            inst[name] += value
            if value: wf[name] += 1; rep[name].add(k[0])
        for name in opportunity(state): eligible[name].add(k); eligible_repo[name].add(k[0])
        triggers, parsed = workflow_triggers(CANONICAL / str(cohort["content_file"]))
        if parsed:
            for trigger in triggers:
                trigger_rows.append((trigger, present))
        else:
            trigger_rows.append(("unclassified", present))
        role = primary_role(state); roles[role] += 1
        for name in present:
            role_flags[(role, name)] += 1
        if present:
            role_flags[(role, "any_of_11")] += 1
        repo = repos.get(k[0])
        if repo is None: missing_repo += 1
        for feature, bins in FEATURES.items(): feature_rows.append((feature, bin_value((repo or {}).get(feature), bins), bool(present), present))
    any_keys = {k for k, present in flags.items() if present}; any_repos = {k[0] for k in any_keys}
    expected = {"no_dependency_cache":16430,"no_fallback_cache":5826,"redundant_cache_update":1375,"cache_position_error":504,"conflicting_cache_sources":613,"static_cache_key":642,"overly_specific_cache_key":566,"no_docker_layer_cache":4382,"deprecated_cache_action":428,"unreachable_cache_write":12,"cache_as_artifact":47}
    if dict(wf) != expected or len(any_keys) != 26542 or len(any_repos) != 10435: raise RuntimeError("recomputed 11-smell counts do not match canonical baseline")
    if sum(roles.values()) != 118523:
        raise RuntimeError("workflow-role denominators do not sum to the RQ1 cohort")
    if sum(role_flags[(role, "any_of_11")] for role in roles) != 26542:
        raise RuntimeError("workflow-role any_of_11 counts do not sum to canonical total")
    if any(role_flags[(role, "any_of_11")] > count for role, count in roles.items()):
        raise RuntimeError("workflow-role any_of_11 count exceeds its denominator")
    if roles and all(role_flags[(role, "any_of_11")] == count for role, count in roles.items()):
        raise RuntimeError("workflow-role any_of_11 cannot be 100 percent in every role")
    return {"wf":wf,"inst":inst,"rep":rep,"eligible":eligible,"eligible_repo":eligible_repo,"flags":flags,"any_keys":any_keys,"any_repos":any_repos,"trigger_rows":trigger_rows,"roles":roles,"role_flags":role_flags,"feature_rows":feature_rows,"missing_repo":missing_repo}


def render_figures(result: dict[str, Any], out: Path, project_figures: Path | None) -> dict[str, str]:
    figs = out / "figure_source_data"; figs.mkdir(parents=True, exist_ok=True)
    plt.rcParams.update({"font.family":"DejaVu Sans","font.size":8,"pdf.fonttype":42,"figure.facecolor":"white","axes.facecolor":"white"})
    names = sorted(TYPES, key=lambda item: result["wf"][item], reverse=True); vals = [100*result["wf"][x]/118523 for x in names]
    fig, ax = plt.subplots(figsize=(7.1,5.2)); ax.barh(range(len(names)), vals, color="#4C78A8"); ax.set_yticks(range(len(names)), [DISPLAY[x] for x in names]); ax.invert_yaxis();ax.set_xlabel("Workflows with smell (% of 118,523)");ax.set_title("Overall workflow prevalence of the 11 statically detectable cache smells")
    for y,v in enumerate(vals):ax.text(v+0.08,y,f"{v:.2f}",va="center",fontsize=7)
    ax.set_xlim(0,max(vals)*1.17);fig.tight_layout(); base="smell_prevalence_canonical_rq1_20260921"; fig.savefig(figs/(base+".png"),dpi=300);fig.savefig(figs/(base+".pdf"));plt.close(fig)
    trig = [(name, sum(1 for trigger, _ in result["trigger_rows"] if trigger == name)) for name in (*CORE_TRIGGERS,"other")]
    fig, ax = plt.subplots(figsize=(7.1,3.7)); x=np.arange(len(trig));ax.bar(x,[100*v/118523 for _,v in trig],color="#72B7B2");ax.set_xticks(x,[n.replace("_"," ") for n,_ in trig],rotation=22,ha="right");ax.set_ylabel("Workflows with trigger (% of 118,523)");ax.set_title("Canonical workflow triggers (categories are non-mutually-exclusive)");fig.tight_layout();base2="trigger_distribution_canonical_rq1_20260921";fig.savefig(figs/(base2+".png"),dpi=300);fig.savefig(figs/(base2+".pdf"));plt.close(fig)
    bins=[]; matrix=[]
    for feat in FEATURES:
        for label,_,_ in FEATURES[feat]:
            records=[r for r in result["feature_rows"] if r[0]==feat and r[1]==label]; bins.append(f"{feat}: {label}");matrix.append([100*sum(t in r[3] for r in records)/len(records) if records else np.nan for t in TYPES])
    fig, ax=plt.subplots(figsize=(9.0,7.4));im=ax.imshow(np.array(matrix),aspect="auto",cmap="YlGnBu",vmin=0,vmax=max(15,np.nanmax(matrix)));ax.set_xticks(range(len(TYPES)),[DISPLAY[t] for t in TYPES],rotation=45,ha="right",fontsize=7);ax.set_yticks(range(len(bins)),bins,fontsize=6.6);fig.colorbar(im,ax=ax,label="Within-stratum workflow prevalence (%)");ax.set_title("RQ1 repository-feature strata (unadjusted descriptive prevalence)");fig.tight_layout();base3="repository_feature_smell_heatmap_canonical_rq1_20260921";fig.savefig(figs/(base3+".png"),dpi=300);fig.savefig(figs/(base3+".pdf"));plt.close(fig)
    outputs={}
    for base_name in (base,base2,base3):
        for suffix in (".png",".pdf"):
            source=figs/(base_name+suffix); target=(project_figures or figs)/(base_name+suffix)
            if project_figures: target.parent.mkdir(parents=True,exist_ok=True); target.write_bytes(source.read_bytes())
            outputs[str(target)]=sha256(target)
    return outputs


def main() -> None:
    parser=argparse.ArgumentParser();parser.add_argument("--output",type=Path,default=OUT);parser.add_argument("--project-figures",type=Path,default=None);args=parser.parse_args();out=args.output
    if out.exists(): raise RuntimeError(f"output directory already exists: {out}")
    joined,repos,audit=load_and_validate();verify_direct(joined);result=compute(joined,repos);out.mkdir(parents=True)
    overall=[];opp=[]
    for name in TYPES:
        overall.append({"smell_type":name,"display_name":DISPLAY[name],"instance_count":result["inst"][name],"workflow_count":result["wf"][name],"overall_workflow_denominator":118523,"overall_workflow_prevalence_pct":round(100*result["wf"][name]/118523,4),"repository_count":len(result["rep"][name]),"overall_repository_denominator":19330,"overall_repository_prevalence_pct":round(100*len(result["rep"][name])/19330,4)})
        e=len(result["eligible"][name]);er=len(result["eligible_repo"][name]);opp.append({"smell_type":name,"display_name":DISPLAY[name],"eligible_workflows":e,"smell_workflow_count":result["wf"][name],"opportunity_conditioned_workflow_prevalence_pct":round(100*result["wf"][name]/e,4) if e else None,"eligible_repositories":er,"smell_repository_count":len(result["rep"][name]),"opportunity_conditioned_repository_prevalence_pct":round(100*len(result["rep"][name])/er,4) if er else None,"instances_per_100_eligible_workflows":round(100*result["inst"][name]/e,4) if e else None})
    write_csv(out/"rq1_prevalence_overall.csv",list(overall[0]),overall);write_csv(out/"rq1_prevalence_opportunity_conditioned.csv",list(opp[0]),opp)
    co=[]
    for left,right in itertools.product(TYPES,TYPES):
        both=sum(left in x and right in x for x in result["flags"].values())
        union=result["wf"][left]+result["wf"][right]-both
        co.append({"smell_type_row":left,"smell_type_column":right,"pair_workflow_count":both,"overall_workflow_denominator":118523,"corpus_share_pct":round(100*both/118523,4),"jaccard":round(both/union,6) if union else None})
    write_csv(out/"rq1_cooccurrence.csv",list(co[0]),co)
    multiplicity=Counter(len(present) for present in result["flags"].values())
    mult_rows=[]
    for label, values in [("0", multiplicity[0]), ("1", multiplicity[1]), ("2", multiplicity[2]), ("3", multiplicity[3]), ("4+", sum(v for n,v in multiplicity.items() if n>=4))]:
        mult_rows.append({"detectable_smell_count":label,"workflow_count":values,"workflow_prevalence_pct":round(100*values/118523,4)})
    write_csv(out/"rq1_smell_multiplicity.csv",list(mult_rows[0]),mult_rows)
    trig=[]
    for name in (*CORE_TRIGGERS, "other", "unclassified"):
        records=[present for trigger,present in result["trigger_rows"] if trigger == name]
        denominator=len(records)
        for smell in ["any_of_11", *TYPES]:
            count=sum(bool(present) if smell=="any_of_11" else smell in present for present in records)
            if count > denominator: raise RuntimeError("trigger numerator exceeds denominator")
            trig.append({"trigger":name,"trigger_workflow_count":denominator,"smell_type":smell,"smell_workflow_count":count,"within_trigger_prevalence_pct":round(100*count/denominator,4) if denominator else None,"overall_corpus_share_pct":round(100*denominator/118523,4),"note":"Trigger categories can overlap. other means a parsed non-core event; unclassified means no trigger category was parsed."})
    write_csv(out/"rq1_trigger_distribution.csv",list(trig[0]),trig)
    eco=[]
    for ecosystem in sorted({str((repos.get(k[0]) or {}).get("primary_ecosystem") or "Missing") for k in joined}):
        ks=[k for k in joined if str((repos.get(k[0]) or {}).get("primary_ecosystem") or "Missing")==ecosystem]
        for name in ["any_of_11",*TYPES]:
            n=sum(bool(result["flags"][k]) if name=="any_of_11" else name in result["flags"][k] for k in ks);eco.append({"primary_ecosystem":ecosystem,"smell_type":name,"workflow_denominator":len(ks),"workflow_count":n,"workflow_prevalence_pct":round(100*n/len(ks),4) if ks else None})
    write_csv(out/"rq1_ecosystem_distribution.csv",list(eco[0]),eco)
    feat=[]
    for feature in FEATURES:
        for label,_,_ in FEATURES[feature]:
            rs=[x for x in result["feature_rows"] if x[0]==feature and x[1]==label]
            for name in ["any_of_11",*TYPES]:
                n=sum(x[2] if name=="any_of_11" else name in x[3] for x in rs);feat.append({"feature":feature,"stratum":label,"workflow_denominator":len(rs),"missing_feature_workflows":sum(x[1]=="Missing" for x in result["feature_rows"] if x[0]==feature),"smell_type":name,"workflow_count":n,"workflow_prevalence_pct":round(100*n/len(rs),4) if rs else None})
        rs=[x for x in result["feature_rows"] if x[0]==feature and x[1]=="Missing"]
        for name in ["any_of_11",*TYPES]: feat.append({"feature":feature,"stratum":"Missing","workflow_denominator":len(rs),"missing_feature_workflows":len(rs),"smell_type":name,"workflow_count":sum(x[2] if name=="any_of_11" else name in x[3] for x in rs),"workflow_prevalence_pct":round(100*sum(x[2] if name=="any_of_11" else name in x[3] for x in rs)/len(rs),4) if rs else None})
    write_csv(out/"rq1_repository_feature_distribution.csv",list(feat[0]),feat)
    roles=[]
    for role,count in sorted(result["roles"].items()):
        for name in ["any_of_11",*TYPES]: roles.append({"workflow_role":role,"workflow_denominator":count,"smell_type":name,"workflow_count":result["role_flags"][(role,name)],"workflow_prevalence_pct":round(100*result["role_flags"][(role,name)]/count,4)})
    write_csv(out/"rq1_workflow_role_distribution.csv",list(roles[0]),roles)
    exposure=[]
    for name in ["any_of_11",*TYPES]:
        exposed_repos=result["any_repos"] if name=="any_of_11" else result["rep"][name]; exposed_wf=len(result["any_keys"]) if name=="any_of_11" else result["wf"][name]
        exposure.append({"smell_type":name,"affected_workflows":exposed_wf,"workflow_denominator":118523,"workflow_prevalence_pct":round(100*exposed_wf/118523,4),"affected_repositories":len(exposed_repos),"repository_denominator":19330,"repository_prevalence_pct":round(100*len(exposed_repos)/19330,4)})
    write_csv(out/"rq1_repository_exposure.csv",list(exposure[0]),exposure)
    by_repo=defaultdict(list)
    for workflow_key, present in result["flags"].items(): by_repo[workflow_key[0]].append(bool(present))
    concentration=[]
    for label,low,high in [("1",1,2),("2-3",2,4),("4-5",4,6),("6-10",6,11),("11-20",11,21),(">20",21,None)]:
        groups=[values for values in by_repo.values() if len(values)>=low and (high is None or len(values)<high)]
        repositories=len(groups); workflows=sum(len(values) for values in groups); exposed=sum(any(values) for values in groups); affected=sum(sum(values) for values in groups)
        concentration.append({"repository_workflow_stratum":label,"repository_count":repositories,"exposed_repository_count":exposed,"repository_prevalence_pct":round(100*exposed/repositories,4) if repositories else None,"workflow_count":workflows,"affected_workflow_count":affected,"workflow_prevalence_pct":round(100*affected/workflows,4) if workflows else None})
    write_csv(out/"rq1_repository_concentration.csv",list(concentration[0]),concentration)
    figures=render_figures(result,out,args.project_figures)
    final={"status":"validated_pending_manuscript_sync","study_cutoff":CUTOFF,"detector_version":DV,"taxonomy_version":TV,"taxonomy_scope":"16 catalogued cache smells; the current static detector covers 11 statically detectable cache smells. The five smells not detected by the current static detector are excluded, not assigned zero prevalence.","workflow_key":"(repository_id, normalized workflow_path)","cohort":{"source_cutoff_snapshot_records":131009,"canonical_stable_workflow_identities":118883,"parseable_rq1_workflows":118523,"rq1_repositories":19330,"collapsed_alias_workflow_identities":11807},"any_of_11_detectable_smells":{"workflow_count":26542,"repository_count":10435},"by_smell_type":overall,"opportunity_conditioned":opp,"missing":{"repository_metadata_missing_workflows":result["missing_repo"],"feature_missing_rows_recorded":True,"trigger_unclassified_rows_recorded":True},"figures":figures}
    (out/"rq1_final_results.json").write_text(json.dumps(final,indent=2)+"\n",encoding="utf-8")
    manifest={"status":"validated_pending_manuscript_sync","canonical_inputs":{name:{"path":str(CANONICAL/name),"sha256":digest} for name,digest in audit["hashes"].items()},"cutoff":CUTOFF,"detector_version":DV,"taxonomy_version":TV,"workflow_key":"(repository_id, normalized workflow_path)","denominators":{"overall_workflow":118523,"overall_repository":19330,"opportunity":"per-smell detector-eligible workflow/repository set"},"source_policy":"No API calls; detector was not run; canonical files were not modified.","script":str(Path(__file__).resolve()),"outputs":{p.name:sha256(p) for p in sorted(out.iterdir()) if p.is_file()}}
    (out/"rq1_final_manifest.json").write_text(json.dumps(manifest,indent=2)+"\n",encoding="utf-8")
    print(json.dumps({"status":"validated_pending_manuscript_sync","output":str(out),"any_workflows":26542,"any_repositories":10435,"figures":figures},indent=2))


if __name__ == "__main__": main()
