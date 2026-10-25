"""Portable helpers; frozen scientific source files are preserved byte for byte."""
from pathlib import Path
import hashlib
import importlib.util
import json
import sys

ROOT = Path(__file__).resolve().parents[1]

def rows(path):
    with Path(path).open(encoding='utf-8-sig') as stream:
        for line in stream:
            if line.strip():
                yield json.loads(line)

def digest(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''): h.update(block)
    return h.hexdigest()

def load(name,path):
    spec=importlib.util.spec_from_file_location(name,path)
    module=importlib.util.module_from_spec(spec)
    sys.modules[name]=module
    spec.loader.exec_module(module)
    return module

def new_output(path):
    path=Path(path).resolve()
    if path.exists(): raise FileExistsError(f'Use a new output directory: {path}')
    path.mkdir(parents=True)
    return path

def write(path,value):
    Path(path).write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')

def canonical(value):
    aliases={'no_restore_keys':'no_fallback_cache','conflicting_cache_sources':'overlapping_cache_mechanisms',
             'disposable_cache_key':'overly_specific_cache_key'}
    if isinstance(value,dict): return {aliases.get(k,k):canonical(v) for k,v in value.items()}
    if isinstance(value,list): return [canonical(v) for v in value]
    return aliases.get(value,value) if isinstance(value,str) else value
