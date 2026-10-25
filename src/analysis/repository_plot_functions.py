"""Canonical RQ1 descriptive repository violins: global no-smell vs each smell.

Eleven figures are produced.  Each figure has five vertical-violin panels;
the left group is always the same global no-detectable-smell reference and the
right group is repositories positive for the title smell.  This deliberately
does not create an eligibility-matched control or an effect estimate.
"""
from __future__ import annotations
from pathlib import Path as _PortablePath
_PORTABLE_PROJECT_ROOT = _PortablePath(__file__).resolve().parents[1]


import sys as _smell_sys
from pathlib import Path as _SmellPath
_SMELL_PROJECT_ROOT = str(_SmellPath(__file__).resolve().parents[1])
if _SMELL_PROJECT_ROOT not in _smell_sys.path:
    _smell_sys.path.insert(0, _SMELL_PROJECT_ROOT)
from src.smell_names import read_smell_frame


import hashlib
import json
import math
import shutil
import subprocess
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from scipy.stats import gaussian_kde

SKILL = Path(str(_PORTABLE_PROJECT_ROOT / 'tools/figure_checks'))
sys.path.insert(0, str(SKILL))
from audit_panel_alignment import require_matplotlib_panel_alignment  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
PRIOR = ROOT / "output_canonical_rq1_by_smell_repository_features_20260924"
OUT = ROOT / "output_canonical_rq1_global_nosmell_vs_each_smell_split_violin_20260925"
SEED = 42  # Reserved; KDE and sparse-point offsets are deterministic.
CANONICAL_DIR = ROOT / "data_26_6_9_canonical_20260609"
CANONICAL = {
    "rq1_cohort.jsonl": ("67cd6f0fbaca7f8ed7dea769b34d886eb2d2a59c44c412491ed093a70ed1bec7", 118_523),
    "workflow_snapshots.jsonl": ("c65cdfe51571a3c59adef3155a2a89c7a9bd1ae032af25b95ebf582e34dd4060", None),
    "detector_states.jsonl": ("7505739ee247bcfc2969958253b968716f6a7875d8e73c5a6f520e6d59eb37d0", 220_478),
}
SMELLS = [
    ("no_dependency_cache", "No dependency cache", "no_dependency_cache", 6_988),
    ("no_fallback_cache", "No fallback cache", "no_fallback_cache", 3_061),
    ("redundant_cache_update", "Redundant cache update", "redundant_cache_update", 1_120),
    ("cache_position_error", "Cache position error", "cache_position_error", 307),
    ("conflicting_cache_sources", "Overlapping cache mechanisms", "overlapping_cache_mechanisms", 351),
    ("static_cache_key", "Static cache key", "static_cache_key", 455),
    ("overly_specific_cache_key", "Overly specific cache key", "overly_specific_cache_key", 376),
    ("no_docker_layer_cache", "No Docker layer cache", "no_docker_layer_cache", 2_582),
    ("deprecated_cache_action", "Deprecated cache action", "deprecated_cache_action", 227),
    ("unreachable_cache_write", "Unreachable cache write", "unreachable_cache_write", 12),
    ("cache_as_artifact", "Cache as artifact", "cache_as_artifact", 39),
]
FEATURES = [
    ("stargazers", "Stargazers"), ("contributors", "Contributors"),
    ("workflow_count", "Workflow count"), ("runs_90d", "Workflow runs (90 d)"),
    ("commits", "Commits"),
]
SPARSE = {"unreachable_cache_write", "cache_as_artifact"}
GREY, BLUE, DARK = "#C7CDD3", "#4C78A8", "#1F4E79"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def line_count(path: Path) -> int:
    with path.open("rb") as handle:
        return sum(1 for _ in handle)


def tick_label(raw: float) -> str:
    if raw >= 1_000_000:
        return f"{raw / 1_000_000:g}M"
    if raw >= 1_000:
        return f"{raw / 1_000:g}k"
    return f"{raw:g}"


def raw_ticks(maximum: float) -> tuple[np.ndarray, list[str]]:
    # Skip the visually crowded 1 tick next to 0; all remaining labels retain
    # directly interpretable raw-number decades on the log10(x + 1) scale.
    candidates = np.array([0, 10, 100, 1_000, 10_000, 100_000, 1_000_000, 10_000_000])
    values = candidates[candidates <= max(maximum * 1.1, 10)]
    if len(values) < 3:
        values = candidates[:3]
    return np.log10(values + 1), [tick_label(value) for value in values]


def audit_pdf(pdf: Path) -> dict:
    task = subprocess.run([sys.executable, str(SKILL / "audit_pdf_text.py"), str(pdf), "--min-pt", "5.5"],
                          text=True, capture_output=True, check=False)
    return {"returncode": task.returncode, "stdout": task.stdout, "stderr": task.stderr}


def write_legends() -> None:
    lines = ["# Figure-legend drafts", ""]
    for _, display, stem, _ in SMELLS:
        dagger = " †" if stem in SPARSE else ""
        lines += [f"## Repository characteristics: {display}", "",
                  f"Repository-level distributions of five continuous features for 8,895 canonical repositories without any of the 11 detectable cache smells (left) and repositories with at least one canonical workflow containing {display}{dagger} (right). The right-hand repositories may contain other smells; each repository contributes once to a group within this figure. The left-hand group is a global descriptive reference, not a smell-specific eligibility-matched control. Features use log10(x + 1); violin width shows within-group density rather than prevalence or sample size; the internal line and dot show IQR and median. Comparisons are unadjusted and descriptive, and do not imply independent or causal effects. † Sparse target-smell groups have unstable KDE shapes; all their raw observations are overlaid.", ""]
    (OUT / "figure_legends_draft.md").write_text("\n".join(lines), encoding="utf-8")


def plot_group(ax, left: np.ndarray, right: np.ndarray, feature_display: str, target_display: str,
               left_n: int, right_n: int, sparse: bool, y_limits, y_ticks, y_ticklabels) -> list[dict]:
    # Split violins share the same central x=0 axis. Each half uses every
    # valid observation and Scott's deterministic Gaussian-KDE bandwidth.
    grid = np.linspace(y_limits[0], y_limits[1], 512)
    left_density, right_density = gaussian_kde(left, bw_method="scott")(grid), gaussian_kde(right, bw_method="scott")(grid)
    max_half_width = .43
    left_width = left_density / left_density.max() * max_half_width
    right_width = right_density / right_density.max() * max_half_width
    ax.fill_betweenx(grid, 0, -left_width, facecolor=GREY, edgecolor="#87919A", linewidth=.55, alpha=.80, zorder=2)
    ax.fill_betweenx(grid, 0, right_width, facecolor=BLUE, edgecolor="#315C85", linewidth=.55, alpha=.80, zorder=2)
    ax.axvline(0, color="#555555", linewidth=.55, zorder=3)
    summary = []
    for x, values, group, n in [(-.16, left, "no_detectable_smell", left_n), (.16, right, "target_smell_present", right_n)]:
        q1, median, q3 = np.quantile(values, [.25, .5, .75])
        ax.vlines(x, q1, q3, color="#202020", linewidth=2.05, zorder=4)
        ax.hlines([q1, q3], x-.07, x+.07, color="#202020", linewidth=.85, zorder=4)
        ax.scatter([x], [median], s=15, color="#151515", zorder=5)
        summary.append({"group": group, "valid_n": int(n), "median_log10_x_plus_1": float(median),
                        "q1_log10_x_plus_1": float(q1), "q3_log10_x_plus_1": float(q3)})
    if sparse:
        offsets = np.linspace(.06, .39, len(right)) if len(right) > 1 else np.array([.18])
        ax.scatter(offsets, right, s=14, color=DARK, alpha=.92, edgecolors="white", linewidths=.25, zorder=6)
    ax.set_ylim(*y_limits); ax.set_yticks(y_ticks, y_ticklabels)
    ax.set_ylabel(f"{feature_display}\n[log10(x + 1)]", fontsize=6.6, labelpad=2)
    target_tick = f"Target smell\npresent{' †' if sparse else ''}\nn={right_n:,}"
    ax.set_xlim(-.52, .52)
    ax.set_xticks([-.29, .29], [f"No detectable\nsmell\nn={left_n:,}", target_tick])
    ax.tick_params(axis="x", labelsize=5.65, pad=2, length=0)
    ax.tick_params(axis="y", labelsize=5.8, width=.45, length=2)
    ax.grid(axis="y", color="#E4E8EB", linewidth=.5); ax.set_axisbelow(True)
    ax.spines["top"].set_visible(False); ax.spines["right"].set_visible(False)
    ax.spines["bottom"].set_linewidth(.55); ax.spines["left"].set_linewidth(.55)
    return summary


def main() -> None:
    if OUT.exists():
        raise FileExistsError(f"Refusing to overwrite existing output: {OUT}")
    OUT.mkdir(parents=True)
    input_audit = {}
    for name, (expected_hash, expected_lines) in CANONICAL.items():
        path = CANONICAL_DIR / name
        actual_hash, records = sha256(path), line_count(path)
        if actual_hash != expected_hash or (expected_lines is not None and records != expected_lines):
            raise RuntimeError(f"Canonical validation failed for {name}")
        input_audit[name] = {"path": str(path), "sha256": actual_hash, "line_count": records}
    source = read_smell_frame(PRIOR / "repository_source_data.csv")
    membership = read_smell_frame(PRIOR / "repository_smell_memberships.csv")
    if len(source) != 19_330 or source.repository_id.nunique() != 19_330:
        raise RuntimeError("Repository source is not the validated 19,330-row RQ1 table")
    codes = [row[0] for row in SMELLS]
    membership = membership[membership.smell_type.isin(codes)].copy()
    expected = {code: count for code, _, _, count in SMELLS}
    observed = membership[membership.group.eq("positive")].groupby("smell_type").repository_id.nunique().to_dict()
    if observed != expected:
        raise RuntimeError(f"Canonical positive-membership mismatch: {observed}")
    positive_ids = set(membership.loc[membership.group.eq("positive"), "repository_id"])
    reference_ids = set(source.loc[~source.repository_id.isin(positive_ids), "repository_id"])
    if len(reference_ids) != 8_895:
        raise RuntimeError("Global no-detectable-smell group is not n=8,895")
    all_membership = []
    for repo in sorted(reference_ids):
        all_membership.append({"repository_id": repo, "role": "global_no_detectable_smell", "target_smell": "all"})
    targets = {}
    for code, display, stem, count in SMELLS:
        ids = set(membership.loc[(membership.smell_type == code) & membership.group.eq("positive"), "repository_id"])
        targets[code] = ids
        all_membership.extend({"repository_id": repo, "role": "target_smell_present", "target_smell": code,
                               "target_display": display, "sparse_target": code in SPARSE} for repo in sorted(ids))
    pd.DataFrame(all_membership).to_csv(OUT / "repository_group_membership_audit.csv", index=False)

    # Fixed same-feature limits/ticks across all 11 target-smell figures.
    scales = {}
    for field, display in FEATURES:
        # All five fields are non-negative counts; the explicit +1 preserves zeros.
        values = np.log10(source[field].dropna().to_numpy(dtype=float) + 1)
        pad = max(.05, .04 * (values.max() - values.min()))
        ticks, labels = raw_ticks(float(source[field].max()))
        scales[field] = ((max(0, float(values.min() - pad)), float(values.max() + pad)), ticks, labels)

    plt.rcParams.update({"font.family": "Arial", "font.sans-serif": ["Arial", "Helvetica", "DejaVu Sans"],
                         "font.size": 6.3, "pdf.fonttype": 42, "ps.fonttype": 42, "svg.fonttype": "none"})
    long_rows, summary_rows, qa = [], [], {"canonical_input_validation": input_audit, "repository_count": 19_330,
        "workflow_count": 118_523, "detector_state_records": 220_478, "positive_counts": observed,
        "global_no_detectable_smell_n": 8_895, "kde": "Gaussian KDE, Scott bandwidth; all valid observations; no sampling.",
        "fixed_feature_scales_across_11_figures": True, "alignment": {}, "pdf_font_audit": {}}
    for code, display, stem, expected_n in SMELLS:
        left_df = source[source.repository_id.isin(reference_ids)]
        right_df = source[source.repository_id.isin(targets[code])]
        fig = plt.figure(figsize=(7.15, 5.25))
        gs = fig.add_gridspec(2, 3, left=.085, right=.985, top=.885, bottom=.115, hspace=.62, wspace=.52)
        axes = [fig.add_subplot(gs[0, col]) for col in range(3)] + [fig.add_subplot(gs[1, col]) for col in range(2)]
        fig.suptitle(f"Repository characteristics: {display}", fontsize=9.0, weight="bold", y=.965)
        for index, ((field, feature_display), ax) in enumerate(zip(FEATURES, axes)):
            # Missingness is retained and written as membership_n - valid_n below.
            left_raw, right_raw = left_df[field].dropna().to_numpy(float), right_df[field].dropna().to_numpy(float)
            left, right = np.log10(left_raw + 1), np.log10(right_raw + 1)
            limits, ticks, labels = scales[field]
            per_panel = plot_group(ax, left, right, feature_display, display, len(left), len(right), code in SPARSE,
                                   limits, ticks, labels)
            ax.set_title(feature_display, fontsize=7.0, pad=5, weight="bold")
            ax.text(-.20, 1.105, chr(97 + index), transform=ax.transAxes, fontweight="bold", fontsize=8.0,
                    va="top", ha="left")
            for row, raw_values in zip(per_panel, [left_raw, right_raw]):
                row.update({"target_smell": code, "target_display": display, "feature": field,
                            "feature_display": feature_display, "membership_n": 8_895 if row["group"] == "no_detectable_smell" else expected_n,
                            "missing_n": (8_895 if row["group"] == "no_detectable_smell" else expected_n) - row["valid_n"],
                            "median_raw": float(np.median(raw_values)), "q1_raw": float(np.quantile(raw_values,.25)),
                            "q3_raw": float(np.quantile(raw_values,.75)), "sparse_target": code in SPARSE})
                summary_rows.append(row)
            for group_name, group_df in [("global_no_detectable_smell", left_df), ("target_smell_present", right_df)]:
                use = group_df[["repository_id", field]].dropna()
                long_rows.extend({"repository_id": r.repository_id, "target_smell": code, "target_display": display,
                                  "group": group_name, "feature": field, "feature_display": feature_display,
                                  "raw_value": float(getattr(r, field)), "log10_x_plus_1": math.log10(float(getattr(r, field))+1)}
                                 for r in use.itertuples(index=False))
        base = OUT / f"{stem}_global_nosmell_comparison"
        require_matplotlib_panel_alignment(fig, axes=axes, panel_ids=["a", "b", "c", "d", "e"],
            row_groups=[["a", "b", "c"], ["d", "e"]], column_groups=[["a", "d"], ["b", "e"]],
            json_out=f"{base}.alignment.json", overlay_svg=f"{base}.alignment.svg", tolerance_pt=1.5,
            gutter_tolerance_pt=1.5, require_panel_labels=True, strict=True)
        for extension in ("png", "pdf", "svg"):
            fig.savefig(f"{base}.{extension}", dpi=400 if extension == "png" else None, facecolor="white")
        plt.close(fig)
        qa["alignment"][base.name] = "PASS"
        qa["pdf_font_audit"][base.name] = audit_pdf(base.with_suffix(".pdf"))

    long = pd.DataFrame(long_rows); summary = pd.DataFrame(summary_rows)
    long.to_csv(OUT / "repository_feature_comparison_long_data.csv", index=False)
    summary.to_csv(OUT / "repository_feature_comparison_summary.csv", index=False)
    # Backend-native contact sheet: preview only, not a formal result figure.
    images = [plt.imread(OUT / f"{stem}_global_nosmell_comparison.png") for _, _, stem, _ in SMELLS]
    sheet, axs = plt.subplots(4, 3, figsize=(10, 10.3))
    for ax, image, (_, display, _, _) in zip(axs.flat, images, SMELLS):
        ax.imshow(image); ax.set_title(display, fontsize=7, weight="bold"); ax.axis("off")
    for ax in axs.flat[len(images):]: ax.axis("off")
    sheet.subplots_adjust(.02, .02, .98, .96, .08, .14)
    sheet.savefig(OUT / "contact_sheet_preview.png", dpi=400, facecolor="white")
    plt.close(sheet)
    write_legends()
    shutil.copy2(Path(__file__), OUT / Path(__file__).name)
    (OUT / "RUN_COMMAND.txt").write_text("python scripts/plot_rq1_global_nosmell_vs_each_smell_violin_20260925.py\nSeed: 42 (reserved; no random sampling or jitter).\n", encoding="utf-8")
    (OUT / "qa_record.json").write_text(json.dumps(qa, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(OUT), "figures": len(SMELLS), "positive_counts": observed}, indent=2))


if __name__ == "__main__":
    main()
