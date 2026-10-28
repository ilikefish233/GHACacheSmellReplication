"""Evaluate all 383 annotated workflows with the frozen study detector."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import runpy
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from artifact import digest, new_output


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--output', type=Path, required=True)
    args = ap.parse_args()
    provenance = json.loads((ROOT / 'docs/replication_manifest.json').read_text(encoding='utf-8'))['validation']
    for name, expected in provenance['input_sha256'].items():
        if digest(ROOT / name) != expected:
            raise ValueError('Validation input SHA256 mismatch: ' + name)
    out = new_output(args.output)
    frozen = ROOT / 'src/frozen'
    sys.path[:0] = [str(frozen), str(ROOT / 'src/analysis')]
    import src.detectors.smell.cache_smell_detector as detector
    import src.parsers.workflow_parser as parser
    for module in (detector, parser):
        if not Path(module.__file__).resolve().is_relative_to(frozen.resolve()):
            raise RuntimeError('Validation must load the packaged frozen detector/parser')
    # Git can check Python out with LF or CRLF; compare canonical source bytes.
    # Historical raw SHA-256 values remain in provenance for the study snapshot.
    for module, key in ((detector, 'detector_source_lf_sha256'), (parser, 'parser_source_lf_sha256')):
        source = Path(module.__file__).read_bytes().replace(b'\r\n', b'\n')
        if hashlib.sha256(source).hexdigest() != provenance[key]:
            raise ValueError('Frozen source SHA256 mismatch: ' + key)
    # Add only the naming adapter missing from the frozen source tree.
    spec = importlib.util.spec_from_file_location('src.smell_names', ROOT / 'src/smell_names.py')
    adapter = importlib.util.module_from_spec(spec)
    sys.modules['src.smell_names'] = adapter
    spec.loader.exec_module(adapter)
    sys.argv = ['evaluate_completed_validation.py', '--xlsx',
                str(ROOT / 'Validation/data/annotations.xlsx'), '--output-dir', str(out)]
    runpy.run_path(str(ROOT / 'src/analysis/evaluate_completed_validation.py'), run_name='__main__')
    report = json.loads((out / 'evaluation.json').read_text(encoding='utf-8'))
    if report['physical_cases'] != 383 or report['parsed_and_detected_cases'] != 383 or report['failures']:
        raise RuntimeError('Validation did not successfully process all 383 workflows; see evaluation.json')


if __name__ == '__main__':
    main()
