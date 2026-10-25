# License scope

This is a mixed-material research artifact. The author-approved grant permits
academic and commercial use, modification and redistribution of the authors'
own contributions under the applicable license below. It does not grant rights
the authors do not hold. Existing file-specific notices take precedence.

| Material | Applicable grant |
| --- | --- |
| Author-owned software contributions in the root Python entry points, `src/`, `Pipeline/` (including acquisition/curation source), `RQ1/`, `RQ2/`, `Validation/`, `src/render_figures.py`, `src/tool/` and `src/tests/` | MIT, full text in `LICENSE` |
| Author-owned annotations, detector labels, eligibility/selection annotations, derived scientific features, aggregate/model results, figure source tables, documentation and provenance descriptions | CC BY 4.0, full text in `LICENSES/CC-BY-4.0.txt`; applies wherever these contributions occur, including `Validation/`, `RQ1/`, `RQ2/`, `data/`, `Pipeline/`, `docs/verification/`, `src/render_figures.py`, `docs/` and `docs/provenance/` |
| Third-party workflow YAML, embedded workflow/code excerpts, copied third-party code and resource assets | Excluded from the grants above; retain applicable upstream terms and notices |
| GHALogs `repositories.json` / `repositories.json.gz` | Not distributed; obtain separately using the root README; original terms apply |
| Raw GitHub API responses, collected upstream metadata and third-party fields embedded in derived tables | No new ownership or blanket license is asserted over third-party content; the CC BY grant covers only author-owned contributions, if any |
| Manuscript snapshot and supplied publication assets in `docs/paper/` | Excluded from these package grants; no additional manuscript publication license is granted here |

The directory names in this table identify material, not an unconditional
relicensing of everything stored in those directories. For example, annotation
cells in `Validation/data/annotations.xlsx` fall within the author-owned data
grant, while embedded upstream YAML does not. The same distinction applies to
source fields carried into generated tables and compressed acquisition archives.
Facts and other material not subject to the licensed rights acquire no additional
restrictions merely by appearing in this package.

## Attribution and citation

For MIT software, retain the copyright and license notice. For CC BY material,
give appropriate credit to its creators, link the license and indicate changes.
Use the creators and title in `CITATION.cff` for attribution of this package's
author-owned contributions; retain any more specific existing attribution.
Academic users are additionally requested to cite the accompanying paper and
the exact archived package version. This citation request does not add a new
condition to either standard license. A DOI will be added only after a real
deposit exists. Attribute third-party material to its original creators.

## Current release boundary

Third-party workflow licensing has not been audited in this update, as directed
by the author. Retaining upstream attribution is not a representation that
redistribution permission has been established. See `docs/third_party_notices.md`.
