"""Scan RQ2.3 histories and recompute the paper's resource results."""
import argparse
from pathlib import Path
import subprocess
import sys

HERE = Path(__file__).resolve().parent
PACKAGE = HERE.parents[1]
sys.path[:0] = [str(PACKAGE / "src"), str(PACKAGE)]
from artifact import DataPaths, add_data_dir
from data_setup import require_data


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    add_data_dir(cli)
    cli.add_argument("--output", type=Path, required=True)
    cli.add_argument("--workers", type=int, default=4)
    cli.add_argument("--scan-dir", type=Path, help="Use an existing scan.py output directory")
    args = cli.parse_args()
    paths = DataPaths(args.data_dir)
    if paths.mapping is None:
        cli.error("RQ2.3 requires the Zenodo data manifest")
    require_data("rq2_3", paths)
    output = args.output.resolve()
    if output.exists():
        cli.error(f"Use a new output directory: {output}")
    common = ["--data-dir", str(paths.root)]
    scan = args.scan_dir.resolve() if args.scan_dir else output.with_name(output.name + "_scan")
    if not args.scan_dir:
        subprocess.run([sys.executable, "-B", str(HERE / "scan.py"), *common,
                        "--output", str(scan), "--workers", str(args.workers)], check=True)
    subprocess.run([sys.executable, "-B", str(HERE / "from_scan.py"), *common,
                    "--scan-dir", str(scan), "--output", str(output)], check=True)


if __name__ == "__main__":
    main()
