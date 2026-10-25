"""Replay only the RQ1/RQ2 functions of the historical canonical builder.

No RQ3 sampling, history materialization, detector or candidate code is called.
Content is relocated by its frozen hash; historical local paths are provenance.
"""
from pathlib import Path
from collections import defaultdict
import argparse
import hashlib
import json
import os
import sys

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--package',type=Path,required=True)
    p.add_argument('--output-dir',type=Path,required=True)
    a=p.parse_args()
    from scripts import build_canonical_dataset as b
    root=Path.cwd()
    # Paths are already absolute and local. Avoid repeated Windows filesystem
    # resolution when recording provenance for every snapshot.
    def portable_source_label(path):
        if path is None:return None
        try:return Path(path).relative_to(root).as_posix()
        except ValueError:return Path(path).as_posix()
    b.source_label=portable_source_label
    package=a.package.resolve()
    expected=package/'data/canonical'
    meta=json.loads((expected/'dataset_manifest.json').read_text())
    sources={k:root/v['path'] for k,v in meta['sources'].items()}
    out=a.output_dir
    if out.exists():raise FileExistsError(out)
    out.mkdir(parents=True)
    audit=b.Audit()
    cutoff=b.parse_timestamp(meta['study_cutoff'])
    repositories,aliases=b.build_repository_table(sources['repository_source'],audit)
    b.enrich_repository_languages(repositories,sources['repository_source'])
    print('Canonical repository identities prepared',flush=True)
    # Preserve the historical availability boundary, including missing content.
    allowed={r['content_hash'] for r in b.load_jsonl(expected/'workflow_snapshots.jsonl') if r.get('content_file')}
    content_dir=package/'data/canonical/content'
    available={p.name for p in os.scandir(content_dir)}
    index={h:content_dir/(h+'.yml') for h in allowed if h+'.yml' in available}
    def relocated(record,roots,content_index,trusted,cache,audit,kind):
        h=str(record.get('content_hash') or '').lower()
        # Original build_content_index also trusts the collection's hash index.
        # Package-wide SHA256 checks verify stored bytes independently.
        source=index.get(h)
        if source is None:audit.count('content_file_missing:'+kind)
        return source
    b.resolve_content_file=relocated
    def shared_content(source,content_hash,output_dir):
        return Path(os.path.relpath(source,output_dir)).as_posix() if source is not None else None
    b.materialize_content=shared_content
    snapshots,by_key=b.build_snapshots(sources['snapshot_source'],sources['scan_source'],sources['snapshot_source'].parent,
        root/'data_26_6_9_complete_20260609',root/'data_26_6_9',repositories,aliases,index,{},out,cutoff,audit,{})
    print('Canonical snapshots prepared:',len(snapshots),flush=True)
    histories=b.build_history_index(sources['history_source'],aliases,cutoff,audit)
    boundaries=b.build_boundaries(snapshots,histories,cutoff)
    empty=out/'empty_retired_history_cache.jsonl';empty.write_text('',encoding='utf-8')
    detector_version=meta['detector']['detector_version']
    taxonomy=meta['detector']['taxonomy_version']
    states=b.load_detector_states(sources['scan_source'],empty,detector_version,taxonomy,audit,
        {(b.normalize_name(r.get('repository_name') or r.get('canonical_name')),r['workflow_path']):r['content_hash'] for r in snapshots},
        {str(r['file_blob_sha']):r['content_hash'] for r in snapshots if r.get('file_blob_sha')})
    rq1=b.build_rq1_cohort(snapshots,states,detector_version,taxonomy)
    print('Canonical RQ1 prepared:',len(rq1),flush=True)
    qualifying=b.normalize_runs(sources['run_source'],out/'workflow_runs.jsonl',by_key,
        {(str(r['repository_id']),r['workflow_path']):r for r in boundaries},repositories,aliases,cutoff,set(meta['rq2']['events']),audit)
    rq2=b.build_rq2_cohort(snapshots,boundaries,qualifying,cutoff)
    b.write_repository_table(out,repositories)
    for name,rows in [('workflow_snapshots',snapshots),('workflow_boundaries',boundaries),('detector_states',states.values()),('rq1_cohort',rq1),('rq2_cohort',rq2)]:
        b.write_jsonl(out/(name+'.jsonl'),rows)
    from verify_canonical import verify
    verify(package,out)

if __name__=='__main__':main()
