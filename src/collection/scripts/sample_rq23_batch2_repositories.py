"""Freeze a second disjoint 1,000-repository RQ2.3 probability sample."""

import hashlib
import json
import random
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FRAME = ROOT / "data_26_6_9_complete_20260609" / "workflow_files.jsonl"
BATCH1 = ROOT / "output_rq23_history_sample_20260929" / "repositories.txt"
OUT = ROOT / "output_rq23_history_sample_batch2_20260930"
SEED = 20260930


def sha(lines):
    return hashlib.sha256("".join(f"{line}\n" for line in lines).encode("utf-8")).hexdigest()


def main():
    names = set()
    with FRAME.open(encoding="utf-8") as stream:
        for line in stream:
            names.add(json.loads(line)["repository_name"].strip().lower())
    first = set(BATCH1.read_text(encoding="utf-8").splitlines())
    if len(names) != 21081 or len(first) != 1000 or not first <= names:
        raise ValueError("The frozen sampling frame or first batch changed")
    remaining = sorted(names - first)
    selected = sorted(random.Random(SEED).sample(remaining, 1000))
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "repositories.txt").write_text("".join(f"{x}\n" for x in selected), encoding="utf-8")
    manifest = {
        "study": "RQ2.3 repository expansion, batch 2",
        "source": str(FRAME.relative_to(ROOT)).replace("\\", "/"),
        "frame_count": len(names),
        "first_batch_count": len(first),
        "remaining_frame_count": len(remaining),
        "method": "Simple random sample without replacement from lexicographically sorted repositories excluding frozen batch 1",
        "seed": SEED,
        "sample_count": len(selected),
        "frame_sha256": sha(sorted(names)),
        "first_batch_sha256": sha(sorted(first)),
        "sample_sha256": sha(selected),
        "study_cutoff": "2026-06-09T23:59:59Z",
        "selection_used_history_availability_or_smell_outcome": False,
    }
    (OUT / "sampling_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
