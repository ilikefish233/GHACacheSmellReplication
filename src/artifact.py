"""Portable helpers; frozen scientific source files are preserved byte for byte."""
from pathlib import Path
import hashlib
import importlib.util
import json
import sys

ROOT = Path(__file__).resolve().parents[1]


def add_data_dir(parser):
    parser.add_argument('--data-dir', type=Path, help='Extracted Zenodo directory containing manifest.json')


class DataPaths:
    def __init__(self, directory=None):
        self.root = Path(directory).resolve() if directory is not None else ROOT / 'zenodo_data'
        self.mapping = None
        if directory is not None or self.root.exists():
            manifest = self.root / 'manifest.json'
            if not manifest.is_file():
                raise ValueError(f'Missing Zenodo data manifest: {manifest}')
            config = json.loads(manifest.read_text(encoding='utf-8-sig'))
            if config.get('schema_version') != 1 or not isinstance(config.get('paths'), dict):
                raise ValueError(f'Unsupported data manifest: {manifest}')
            self.mapping = config['paths']

    def path(self, legacy):
        if self.mapping is None:
            return ROOT / legacy
        parts = Path(legacy).parts
        for n in range(len(parts), 0, -1):
            prefix = '/'.join(parts[:n])
            if prefix in self.mapping:
                relative = Path(self.mapping[prefix])
                target = self.root.joinpath(relative, *parts[n:]).resolve()
                if relative.anchor or '..' in relative.parts or not target.is_relative_to(self.root):
                    raise ValueError(f'Invalid archive path for {prefix}: {relative}')
                return target
        raise ValueError(f'No archive path for {legacy}')


def rq23_sample_files():
    directory = ROOT / 'RQ2/RQ2_3/data'
    return sorted(
        path for path in directory.glob('repositories_batch*.txt')
        if path.stem.removeprefix('repositories_batch').isdecimal()
    )


def rq23_sample_names():
    manifests = rq23_sample_files()
    if not manifests:
        raise FileNotFoundError('No RQ2.3 batch lists in RQ2/RQ2_3/data')
    names = set()
    for path in sorted(manifests):
        batch_names = [line.strip().lower() for line in path.read_text(encoding='utf-8-sig').splitlines() if line.strip()]
        if not batch_names or len(set(batch_names)) != len(batch_names) or names.intersection(batch_names):
            raise ValueError(f'Empty list or duplicate repository in RQ2.3 list: {path}')
        names.update(batch_names)
    return sorted(names)


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
