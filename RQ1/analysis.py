"""Recompute prevalence, paper contexts, co-occurrence and repository contrasts."""
from pathlib import Path
import argparse
import json
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from artifact import ROOT, load, rows, new_output, write, canonical, DataPaths, add_data_dir

def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--output',type=Path,required=True)
    ap.add_argument('--scan',type=Path)
    add_data_dir(ap)
    args=ap.parse_args()
    from data_setup import require_data
    paths = DataPaths(args.data_dir)
    require_data('rq1', paths)
    scan = args.scan or paths.path('RQ1/data/rq1_scan_direct.jsonl')
    out=new_output(args.output)
    # Run the original eligibility/counting implementation with a direct JSONL adapter.
    path=ROOT/'src/frozen/scripts/smell_prevalence_new.py'
    source=path.read_text(encoding='utf-8-sig')
    source=source.split('    # ---- 打印摘要 ----')[0]  # reports/plots use the corrected paper tables below
    scope={'__name__':'frozen_prevalence','__file__':str(path)}
    exec(compile(source,str(path),'exec'),scope)
    original_normalizer=scope['normalize_smell_type']
    legacy_aliases={'no_fallback_cache':'no_restore_keys','overlapping_cache_mechanisms':'conflicting_cache_sources'}
    scope['normalize_smell_type']=lambda name:original_normalizer(legacy_aliases.get(name,name))
    def direct_records(unused):
        for r in rows(scan):
            # Trigger contexts are recomputed below using the corrected manuscript
            # parser. Avoid the superseded parser and duplicate filesystem reads.
            yield r['workflow_key'],r['repository_name'],'',r['detectors'],r.get('summary',{}),r
    scope['iter_smell_records_new']=direct_records
    sys.argv=[str(path),'--data-dir',str(paths.path('RQ1/data')),'--output-dir',str(out),'--fast-triggers']
    scope['main']()
    distribution=json.loads((out/'smell_distribution.json').read_text(encoding='utf-8'))
    for key in ['trigger_overview','by_trigger_type','trigger_combinations']:
        distribution.pop(key,None)
    distribution['trigger_context_source']='triggers_preserving_paper_groups.csv (corrected manuscript parser)'
    write(out/'smell_distribution.json',distribution)
    # Preserve the exact manuscript grouping/parser/statistics source; only relocate paths.
    audit=ROOT/'src/analysis/audit_run3_manuscript_impact.py'
    s=audit.read_text(encoding='utf-8-sig')
    replacements={
        "Path('E:/gha_log_analysis')":repr(ROOT),
        "Path('E:/0_cache_smell/run3_impact_audit_20261019')":repr(out),
        "ROOT / 'data_26_6_9_canonical_20260609'":repr(paths.path('data/canonical')),
        "CAN / 'repositories.jsonl'":repr(paths.path('data/canonical/repositories.jsonl')),
        "CAN/'repositories.jsonl'":repr(paths.path('data/canonical/repositories.jsonl')),
        "ROOT / 'output_canonical_rq1_final_20260921'":"ROOT / 'docs/provenance/legacy'",
        "ROOT / 'scripts/finalize_canonical_rq1_20260921.py'":"ROOT / 'src/analysis/finalize_canonical_rq1_20260921.py'",
        "RUN/'analysis_inputs/rq1_scan_direct.jsonl'":repr(scan.resolve()),
        "RUN/'rq1/smell_prevalence.json'":repr(out/'smell_prevalence.json'),
        "ROOT/'output_canonical_rq1_trigger_corrected_20260928/rq1_trigger_distribution.csv'":"ROOT/'docs/provenance/legacy/rq1_trigger_distribution.csv'",
        "Path('E:/0_cache_smell/revision_20261015/repository_distribution_tests.csv')":"ROOT/'docs/provenance/legacy/repository_distribution_tests.csv'",
    }
    # repr(Path) is PosixPath/WindowsPath; use a portable Path constructor.
    for old,new in replacements.items():
        if isinstance(new,str) and new.startswith(('WindowsPath(', 'PosixPath(')):
            new='Path('+repr(str(eval(new,{'WindowsPath':Path,'PosixPath':Path})))+')'
        s=s.replace(old,new)
    exec(compile(s,str(audit),'exec'),{'__name__':'paper_recomputation','__file__':str(audit)})
    for p in out.glob('*.json'):
        write(p,canonical(json.loads(p.read_text(encoding='utf-8-sig'))))
    print('Paper-level RQ1 tables recomputed in',out,flush=True)

if __name__=='__main__': main()
