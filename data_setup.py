"""Check that the extracted Zenodo data package contains the analysis inputs."""

from pathlib import Path
import argparse
import json
import sys

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))
from artifact import DataPaths, add_data_dir


def config():
    return json.loads((ROOT / "data/sources.json").read_text(encoding="utf-8"))


def missing(scope="all", paths=None):
    paths = paths or DataPaths()
    required = config()["required_files"]
    scopes = required if scope == "all" else {scope: required[scope]}
    return sorted({str(paths.path(name)) for names in scopes.values() for name in names
                   if not paths.path(name).is_file()})


def require_data(scope="all", paths=None):
    names = missing(scope, paths)
    if names:
        raise SystemExit(
            "Missing extracted Zenodo data: " + ", ".join(names[:4]) +
            "\nSee README.md and pass --data-dir PATH_TO_ZENODO_DATA."
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--status", action="store_true", help="List missing required files")
    add_data_dir(parser)
    args = parser.parse_args()
    if not args.status:
        parser.error("Extract the Zenodo data package, then use --status --data-dir PATH_TO_ZENODO_DATA")
    print(json.dumps({
        "missing_required_files": missing(paths=DataPaths(args.data_dir)),
        "doi": config()["doi"],
        "note": "Presence check only; reproduction also verifies analysis results.",
    }, indent=2))


if __name__ == "__main__":
    main()
