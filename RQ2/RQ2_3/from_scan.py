"""Rebuild RQ2.3 resource comparisons from scan.py candidates and archived runs."""
import argparse
import ast
import shutil
import sys
from collections import Counter, defaultdict
import json
from pathlib import Path
import random
from statistics import fmean

from scan import FORMAL
from artifact import ROOT, DataPaths, add_data_dir, canonical, digest, load, new_output, rows, write

EXACT_EVENTS = {"push", "schedule", "workflow_dispatch"}


def run_key(run):
    return str(run["repository_id"]), int(run["run_id"]), int(run["run_attempt"])


def match_event(event, runs):
    sides = {"before": [], "after": []}
    for side, start, end in (
        ("before", event["before_start"], event["change_time"]),
        ("after", event["change_time"], event["after_end"]),
    ):
        for run in runs:
            if not start <= run["created_at"] < end:
                continue
            started = run.get("run_started_at") or run["created_at"]
            if run["run_attempt"] > 1 and not start <= started < end:
                continue
            proof = run.get("proof") if run["event"] in EXACT_EVENTS else None
            if proof and proof["content_sha256"] != event[side + "_content_sha256"]:
                continue
            sides[side].append(run)
    counts = {side: Counter(run["workflow_id"] for run in group)
              for side, group in sides.items()}
    shared = counts["before"].keys() & counts["after"].keys()
    if not shared:
        return None, sides
    workflow_id = min(shared, key=lambda wid: (-min(counts[s][wid] for s in sides), wid))
    return workflow_id, {
        side: sorted((run for run in group if run["workflow_id"] == workflow_id),
                     key=lambda run: (run["created_at"], run_key(run)))
        for side, group in sides.items()
    }


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    add_data_dir(cli)
    cli.add_argument("--scan-dir", type=Path, required=True)
    cli.add_argument("--output", type=Path, required=True)
    args = cli.parse_args()
    paths = DataPaths(args.data_dir)
    if paths.mapping is None:
        cli.error("Run matching requires a Zenodo data manifest")
    match_dir = paths.path("RQ2/RQ2_3/data/matching")
    sys.path.insert(0, str(ROOT))
    from data_setup import require_data
    require_data('rq2_3', paths)
    candidates = list(rows(args.scan_dir / "candidate_transitions.jsonl"))
    wanted = {(str(e["repository_id"]), e["workflow_path"]) for e in candidates}
    runs = defaultdict(list)
    for record in rows(match_dir / "run_jobs.jsonl"):
        if (str(record["repository_id"]), record["workflow_path"]) in wanted:
            runs[str(record["repository_id"]), record["workflow_path"]].append(record)

    # Use full detector states for the descriptive type breakdown.
    states = {}
    for row in rows(args.scan_dir / "version_scan_states.jsonl"):
        if not row.get("error"):
            states[row["repository_name"], row["workflow_path"], row["commit_sha"]] = row["all_detector_types"]

    helper = load("resource_math", ROOT / "src/resources/recompute.py")
    out = new_output(args.output)
    costs, status, effects = {}, [], []
    selected_runs, used_runs = set(), set()
    for e in sorted(candidates, key=lambda row: (str(row["repository_id"]), row["workflow_path"], row["change_time"])):
        rid = str(e["repository_id"])
        workflow_id, sides = match_event(e, runs[rid, e["workflow_path"]])
        if workflow_id is None:
            status.append({**e, "status": "excluded_missing_two_sided_resource_data"})
            continue
        result = {**e, "workflow_id": workflow_id}
        means = {}
        for side, group in sides.items():
            complete = []
            for run in group:
                key = run_key(run)
                selected_runs.add(key)
                if key not in costs:
                    cost = helper.cost(run["jobs"])
                    costs[key] = cost if not cost["reasons"] and cost["allocated_jobs"] else None
                if costs[key] is not None:
                    complete.append(key)
            result[side + "_run_keys"] = complete
            means[side] = {
                field: fmean(costs[key][field] for key in complete) if complete else None
                for field in ("vm_seconds", "reference_cost_usd")
            }
        included = all(result[s + "_run_keys"] for s in sides)
        result["status"] = "included" if included else "excluded_missing_complete_resource_side"
        status.append(result)
        if not included:
            continue
        used_runs.update(result["before_run_keys"])
        used_runs.update(result["after_run_keys"])
        identity = e["repository_name"], e["workflow_path"]
        before, after = (
            set(states[(*identity, e[s + "_commit_sha"])]) for s in ("before", "after")
        )
        for smell in sorted(before ^ after):
            present, absent = ("after", "before") if smell in after else ("before", "after")
            effects.append({
                **result, "smell": smell,
                "scope": "original_11" if smell in FORMAL else "additional_detector_type",
                "present_side": present, "absent_side": absent,
                **{"delta_" + field: means[present][field] - means[absent][field]
                   for field in ("vm_seconds", "reference_cost_usd")},
            })
    # Keep the same detector type order as the frozen resource report.
    formal_order = (
        "no_dependency_cache", "no_fallback_cache", "redundant_cache_update",
        "cache_position_error", "conflicting_cache_sources", "static_cache_key",
        "overly_specific_cache_key", "no_docker_layer_cache", "deprecated_cache_action",
        "unreachable_cache_write", "cache_as_artifact",
    )
    additional = (
        "cache_path_pollution", "incorrect_cache_hit_condition", "malformed_cache_path",
        "non_portable_cache_path", "overly_broad_cache_path",
    )
    summary = []
    for smell in (*formal_order, *additional):
        group = [row for row in effects if row["smell"] == smell]
        summary.append({
            "smell": smell, "scope": "original_11" if smell in FORMAL else "additional_detector_type",
            **helper.aggregate(group, random.Random(20261019)),
        })
    helper.dump(out, "event_status.jsonl", status)
    helper.dump(out, "event_resource_differences.jsonl", effects)
    helper.csvout(out, "smell_resource_summary.csv", summary)
    write(out / "smell_resource_summary.json", summary)
    included = [row for row in status if row["status"] == "included"]
    audit = {
        "candidate_events": len(candidates), "runs_entering_resource_check": len(selected_runs),
        "final_events": len(included), "final_repositories": len({e["repository_id"] for e in included}),
        "unique_success_runs": len(used_runs), "smell_memberships": len(effects),
        "single_observation_events": sum(
            any(len(e[side + "_run_keys"]) == 1 for side in ("before", "after"))
            for e in included
        ),
    }
    write(out / "result_audit.json", audit)
    print(json.dumps(audit), flush=True)
    monthly_results(paths, out, match_dir / "manifest.json")


def monthly_results(paths, out, provenance):
    # Reuse original monthly-frequency implementation, changing only locations and the provenance lookup.
    monthly=ROOT/'src/resources/calculate_observed_monthly.py'
    s=monthly.read_text(encoding='utf-8-sig')
    s=s.replace("ROOT / 'data_26_6_9_canonical_20260609/repositories.jsonl'",'REPOSITORY_FILE')
    start=s.index("    expected = next(s['sha256']")
    end=s.index("    assert h.hexdigest() == expected",start)
    s=s[:start]+"    expected = digest(SOURCE / 'workflow_runs.jsonl')\n"+s[end:]
    t=ast.parse(s)
    # Keep immutable original module but override its location constants after initialization.
    mscope={'__name__':'resource_frequency','__file__':str(monthly),'digest':digest,'REPOSITORY_FILE':paths.path('data/canonical/repositories.jsonl')}
    exec(compile(t,str(monthly),'exec'),mscope)
    mscope.update(ROOT=ROOT,HERE=out,OUT=out/'observed_monthly_frequency',
        SOURCE=paths.path('RQ2/RQ2_3/data/frequency'))
    # Input provenance is historical and read-only; monthly calculation only reads it.
    shutil.copy2(provenance,out/'input_provenance.json')
    mscope['main']()
    for p in list(out.glob('*.json'))+list((out/'observed_monthly_frequency').glob('*.json')):
        write(p,canonical(json.loads(p.read_text(encoding='utf-8'))))
    print('RQ2.3 contrasts and actual-frequency monthly equivalents recomputed:',out,flush=True)


if __name__ == "__main__":
    main()
