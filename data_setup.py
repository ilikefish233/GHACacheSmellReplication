"""Attach the separately archived research data to this GitHub checkout."""
from pathlib import Path, PurePosixPath
import argparse
import json
import shutil
import sys
import urllib.parse
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parent


def config():
    return json.loads((ROOT / 'data/sources.json').read_text(encoding='utf-8'))


def missing(scope='all'):
    cfg = config()
    scopes = cfg['required_files'] if scope == 'all' else {scope: cfg['required_files'][scope]}
    return sorted({name for names in scopes.values() for name in names if not (ROOT / name).is_file()})


def require_data(scope='all'):
    names = missing(scope)
    if names:
        raise SystemExit('Research data are separate from this GitHub checkout.\n'
                         'Missing: ' + ', '.join(names[:4]) + '\n'
                         'Run: python data_setup.py --archive PATH_TO_COMPLETE_PACKAGE.zip\n'
                         'See README.md for the archive publication status and GHALogs instructions.')


def selected(name, cfg):
    return name in cfg['external_files'] or any(name.startswith(p) for p in cfg['external_prefixes'])


def install(archive, root=ROOT, cfg=None):
    cfg = cfg or config()
    root = Path(root).resolve()
    copied = skipped = 0
    with zipfile.ZipFile(archive) as z:
        infos = z.infolist()
        if len({i.filename for i in infos}) != len(infos):
            raise ValueError('Duplicate archive entries')
        manifests = [i for i in infos if i.filename.endswith('/replication_manifest.json') or i.filename == 'replication_manifest.json']
        if len(manifests) != 1:
            raise ValueError('Expected one complete-package replication_manifest.json')
        marker = manifests[0].filename
        prefix = marker[:-len('replication_manifest.json')]
        meta = json.loads(z.read(marker))
        for key, value in cfg['identity'].items():
            actual = meta['detector']['sha256'] if key == 'detector_sha256' else meta.get(key)
            if actual != value:
                raise ValueError('Archive does not match this paper snapshot: ' + key)
        wanted = []
        for info in infos:
            if not info.filename.startswith(prefix):
                raise ValueError('Unexpected archive root')
            name = info.filename[len(prefix):]
            path = PurePosixPath(name)
            if '\\' in name or path.is_absolute() or '..' in path.parts or ':' in name:
                raise ValueError('Unsafe archive path: ' + name)
            if info.is_dir() or not selected(name, cfg):
                continue
            if (info.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError('Archive symlink is not accepted: ' + name)
            wanted.append((info, name))
        if len(wanted) != cfg['external_file_count'] or sum(i.file_size for i, _ in wanted) != cfg['external_uncompressed_bytes']:
            raise ValueError('External data layout/size differs from the selected complete package')
        # Validate all destinations before writing any selected data.
        for info, name in wanted:
            target = (root / name).resolve()
            if root not in target.parents:
                raise ValueError('Destination escapes checkout: ' + name)
            if target.exists() and (not target.is_file() or target.stat().st_size != info.file_size):
                raise FileExistsError('Existing data differ in size; use a clean checkout: ' + name)
        for info, name in wanted:
            target = root / name
            if target.is_file():
                skipped += 1
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(target.name + '.partial')
            with z.open(info) as src, temporary.open('wb') as dst:
                shutil.copyfileobj(src, dst, length=4 * 1024 * 1024)
            temporary.replace(target)
            copied += 1
            if copied % 20000 == 0:
                print('Restored', copied, 'data files', flush=True)
    return {'restored': copied, 'already_present_same_size': skipped}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    source = p.add_mutually_exclusive_group()
    source.add_argument('--archive', type=Path, help='Local complete-package ZIP; only external data are copied')
    source.add_argument('--url', help='Actual archive download URL; never a DOI landing page')
    p.add_argument('--status', action='store_true', help='Show required-file availability without downloading')
    a = p.parse_args()
    if a.status:
        print(json.dumps({'missing_required_files': missing(), 'archive_url': config()['archive_url'],
                          'doi': config()['doi'], 'note': 'This checks presence, not content integrity.'}, indent=2))
        return
    archive = a.archive
    if archive is None:
        url = a.url or config()['archive_url']
        if not url:
            p.error('The Zenodo archive is not published yet. Use --archive with your local complete ZIP. No public download URL has been invented.')
        if urllib.parse.urlparse(url).scheme != 'https':
            p.error('Use an HTTPS archive URL')
        cache = ROOT / '.downloads'
        cache.mkdir(exist_ok=True)
        archive = cache / 'replication_package.zip'
        temporary = cache / 'replication_package.zip.partial'
        with urllib.request.urlopen(url, timeout=60) as src, temporary.open('wb') as dst:
            shutil.copyfileobj(src, dst, length=4 * 1024 * 1024)
        temporary.replace(archive)
    print(json.dumps(install(archive), indent=2))
    print('Data attached. Original repository code and expected tables were not overwritten.')


if __name__ == '__main__':
    main()
