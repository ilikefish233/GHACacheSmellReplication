"""Draw the fixed RQ2.3 repository sample from the complete workflow snapshot.

Usage: python scripts/sample_rq23_repositories.py
"""

import hashlib
import json
import random
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "data_26_6_9_complete_20260609" / "workflow_files.jsonl"
HISTORY = ROOT / "data_26_6_9_complete_20260609" / "workflow_changes.jsonl"
OUTPUT = ROOT / "output_rq23_history_sample_20260929"
SEED = 20260929
SAMPLE_SIZE = 1000


def repository_names(path: Path) -> set[str]:
    names = set()
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            if line.strip():
                name = json.loads(line)["repository_name"].strip().lower()
                if not name:
                    raise ValueError(f"Empty repository_name in {path}")
                names.add(name)
    return names


def digest_lines(lines: list[str]) -> str:
    return hashlib.sha256("".join(f"{line}\n" for line in lines).encode("utf-8")).hexdigest()


def main() -> None:
    frame = sorted(repository_names(SOURCE))
    if len(frame) != 21081:
        raise ValueError(f"Expected 21,081 snapshot repositories, found {len(frame)}")

    # Sort before drawing so the result cannot depend on JSONL record order.
    sampled = sorted(random.Random(SEED).sample(frame, SAMPLE_SIZE))
    existing_history = repository_names(HISTORY)
    manifest = {
        "study": "RQ2.3 within-workflow resource-cost analysis",
        "sampling_unit": "repository",
        "sampling_frame": str(SOURCE.relative_to(ROOT)).replace("\\", "/"),
        "sampling_frame_rule": "Distinct lower-case repository_name values with at least one current workflow snapshot",
        "frame_count": len(frame),
        "frame_sha256": digest_lines(frame),
        "method": "Simple random sample without replacement from lexicographically sorted frame",
        "rng": "Python random.Random.sample (Mersenne Twister)",
        "seed": SEED,
        "sample_count": len(sampled),
        "sample_sha256": digest_lines(sampled),
        "existing_history_repository_count_in_sample": len(set(sampled) & existing_history),
        "existing_history_note": "Audited after sampling; history availability did not affect selection",
        "sample_file": "repositories.txt",
    }

    OUTPUT.mkdir(parents=True, exist_ok=True)
    (OUTPUT / "repositories.txt").write_text(
        "".join(f"{name}\n" for name in sampled), encoding="utf-8"
    )
    (OUTPUT / "sampling_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
