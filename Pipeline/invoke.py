"""Run a bundled source module with network disabled for offline stages."""
from pathlib import Path
import argparse
import builtins
from functools import lru_cache
import io
import os
import runpy
import socket
import sys

def main():
    p=argparse.ArgumentParser()
    p.add_argument('--work',type=Path,required=True)
    p.add_argument('--online',action='store_true')
    p.add_argument('command',nargs=argparse.REMAINDER)
    a=p.parse_args()
    a.work=a.work.resolve()
    if not a.online:
        def blocked(*args,**kwargs):raise RuntimeError('Network disabled during offline pipeline replay')
        socket.create_connection=blocked
        socket.socket.connect=blocked
        socket.socket.connect_ex=blocked
        socket.getaddrinfo=blocked
    os.chdir(a.work)
    # Large sequential JSONL reads/writes benefit from larger buffers on Windows.
    # This changes I/O batching only; serializers and row order are unchanged.
    original_open=io.open
    def buffered_open(file,mode='r',buffering=-1,*args,**kwargs):
        if buffering==-1:buffering=4*1024*1024
        return original_open(file,mode,buffering,*args,**kwargs)
    io.open=buffered_open
    builtins.open=buffered_open
    sys.path.insert(0,str(a.work))
    cmd=a.command[1:] if a.command and a.command[0]=='--' else a.command
    path=Path(cmd[0])
    if not path.is_absolute():path=a.work/path
    sys.argv=[str(path),*cmd[1:]]
    if path.name=='prepare_rq2_final_model_input.py':
        # Preserve the duplicate-ID check while avoiding reconstruction of the
        # entire prior-ID set for each of 51,179 versions (quadratic work).
        source=path.read_text(encoding='utf-8-sig')
        replacements={
            '    versions: Dict[WorkflowKey, Dict[str, Any]] = {}':
                '    versions: Dict[WorkflowKey, Dict[str, Any]] = {}\n    seen_version_ids = set()',
            'if value["version_id"] in {v["version_id"] for v in versions.values()}:':
                'if value["version_id"] in seen_version_ids:',
            '        versions[key] = value':
                '        versions[key] = value\n        seen_version_ids.add(value["version_id"])',
        }
        for old,new in replacements.items():
            if source.count(old)!=1:raise ValueError('Model-input adapter source contract changed: '+old)
            source=source.replace(old,new)
        exec(compile(source,str(path),'exec'),{'__name__':'__main__','__file__':str(path)})
    elif path.name=='consolidate_rq2_full_acquisition.py':
        # The original callback resolves the identical provenance directory for
        # every run. Cache that pure path-label function, preserving its values.
        module=runpy.run_path(str(path),run_name='pipeline_consolidation')
        namespace=module['main'].__globals__
        namespace['relpath']=lru_cache(maxsize=None)(namespace['relpath'])
        result=namespace['main']()
        if result:raise SystemExit(result)
    else:
        runpy.run_path(str(path),run_name='__main__')

if __name__=='__main__':main()
