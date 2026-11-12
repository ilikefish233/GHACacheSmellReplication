"""Draw the next RQ2.3 repository batch from the Zenodo workflow snapshots."""
import argparse
import hashlib
import json
from pathlib import Path
import random
import sys

PACKAGE = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PACKAGE / "src"))
import artifact
from artifact import DataPaths, add_data_dir, rows, rq23_sample_files, rq23_sample_names


def digest_names(names):
    return hashlib.sha256("".join(f"{name}\n" for name in sorted(names)).encode("utf-8")).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    add_data_dir(parser)
    parser.add_argument("--size", type=int, default=1000)
    parser.add_argument("--seed", type=int, help="Defaults to 20260929 plus batch number minus one")
    args = parser.parse_args()
    paths = DataPaths(args.data_dir)
    if paths.mapping is None:
        parser.error("Sampling requires zenodo_data/manifest.json or --data-dir")
    source = paths.path("data/canonical/workflow_snapshots.jsonl")
    frame = set()
    for record in rows(source):
        name = (record.get("canonical_name") or record.get("repository_name") or "").strip().lower()
        if not name:
            raise ValueError(f"Missing repository name in {source}")
        frame.add(name)

    existing = rq23_sample_files()
    previous = set(rq23_sample_names()) if existing else set()
    batch = max((int(path.stem.removeprefix("repositories_batch")) for path in existing), default=0) + 1
    available = sorted(frame - previous)
    if not 1 <= args.size <= len(available):
        parser.error(f"--size must be between 1 and the remaining repository count ({len(available)})")
    seed = args.seed if args.seed is not None else 20260929 + batch - 1
    selected = sorted(random.Random(seed).sample(available, args.size))
    output_dir = artifact.ROOT / "RQ2/RQ2_3/data"
    output_dir.mkdir(parents=True, exist_ok=True)
    sample = output_dir / f"repositories_batch{batch}.txt"
    manifest_path = output_dir / f"sampling_manifest_batch{batch}.json"
    if manifest_path.exists():
        raise FileExistsError(manifest_path)
    manifest = {
        "batch": batch,
        "sampling_unit": "repository",
        "sampling_frame": source.relative_to(paths.root).as_posix(),
        "frame_count": len(frame),
        "frame_sha256": digest_names(frame),
        "excluded_previously_listed_count": len(frame & previous),
        "remaining_frame_count": len(available),
        "method": "Simple random sampling without replacement from sorted repository names after excluding existing lists",
        "seed": seed,
        "sample_count": len(selected),
        "sample_sha256": digest_names(selected),
        "sample_file": sample.name,
    }
    with sample.open("x", encoding="utf-8", newline="\n") as stream:
        stream.writelines(f"{name}\n" for name in selected)
    with manifest_path.open("x", encoding="utf-8") as stream:
        json.dump(manifest, stream, indent=2)
        stream.write("\n")
    print(f"Saved {len(selected)} repositories to {sample}")


if __name__ == "__main__":
    main()
