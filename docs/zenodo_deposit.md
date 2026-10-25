# Zenodo deposit instructions

Recommended arrangement: GitHub hosts the readable code, entry points, small
expected tables and documentation; Zenodo preserves the complete versioned ZIP
with its analysis data. The GitHub README points to the actual Zenodo record.
For this package, uploading the complete ZIP is simpler than splitting datasets
and preserves all relative paths. The removed GHALogs source is obtained from
its original record when replaying initial filtering.

## Prepare the record

1. Sign in at https://zenodo.org and choose **New upload**. Start with a draft.
2. Upload `replication_package_emse_20261025.zip`. Optionally also upload the
   root README, `docs/license_scope.md` and `CITATION.cff` as small standalone files.
   Zenodo currently documents up to 100 files and 50 GB per upload, so this
   approximately 3 GB ZIP fits without splitting.
3. Use the title **Cache Configuration Smells in GitHub Actions: A Large-Scale
   Empirical Study — Replication Package**. Select resource type **Dataset** for
   this mixed data/code artifact. Copy creators from `CITATION.cff`; check their
   order, affiliations and ORCIDs rather than inventing identifiers.
4. Choose an actual release version (for example `1.0.0`) and synchronize it in
   citation metadata and the deposit. Describe the frozen October analyses,
   RQ1/RQ2 scope, validation, shared YAML, run data, acquisition/curation code,
   external GHALogs download and known validation limits.
5. In **Licenses**, declare MIT and CC BY 4.0 with their scope described in the
   record and `docs/license_scope.md`. Do not mark all bundled third-party workflows
   or raw upstream responses as CC BY. The unresolved third-party workflow
   distribution status remains separate from this author-owned license grant.
6. Add the actual GitHub URL as a related identifier when it exists; relate the
   GHALogs record https://doi.org/10.5281/zenodo.14796970 as a source. Add the
   manuscript DOI later, if assigned; it is not the dataset DOI.
7. If the package has no DOI, choose **No** for an existing DOI and use **Get a
   DOI now!** to reserve one. A reservation becomes registered only upon
   publication. Insert only the assigned identifier into the README/citation
   metadata and final files. Do not reuse the GHALogs DOI for this package.
8. Save and preview the draft. When intentionally ready to publish, choose the
   intended access setting and publish. No upload, DOI reservation or external
   publication has been performed by the local package preparation scripts.

## After publication

- Link GitHub to the actual record; state which ZIP to download, where to
  extract it and the commands to run. The code-only GitHub checkout does not
  contain the complete frozen analysis data.
- Cite the specific archived version in the manuscript's Data Availability
  statement and reference list. Update `CITATION.cff` with the real DOI/URL.
- If scientific files change for a revision, create a new Zenodo version and
  cite that version. Keep earlier versions available for traceability.
- A DOI identifies and preserves a deposit; it does not resolve third-party
  distribution rights. That review is deferred in the present local update.

## Description draft

This replication package accompanies “Cache Configuration Smells in GitHub
Actions: A Large-Scale Empirical Study”. It contains the frozen October
paper-selected detector and statistical implementations, workflow configurations,
workflow-run analysis inputs, human-validation annotations, expected results,
figure source data and acquisition/curation pipeline for RQ1 and RQ2. The retired
RQ3 is outside the study. Default paper analyses use frozen inputs offline after
dependency installation. Initial candidate filtering additionally requires
`repositories.json.gz` from GHALogs v1.0.1 (DOI 10.5281/zenodo.14796970), which is
not redistributed. Author-owned code is MIT; author-owned derived data and
documentation are CC BY 4.0. Third-party material retains upstream rights and
is excluded from these grants; see docs/license_scope.md and docs/third_party_notices.md.
Tested with Python 3.12.3 on Windows. Fresh installation and other operating
systems have not been tested; a new full population detector scan was not run
during package assembly. See the included validation reports for completed checks.

## Official guidance

- Upload and DOI reservation: https://help.zenodo.org/docs/deposit/create-new-upload/
- Mixed licenses: https://help.zenodo.org/docs/deposit/describe-records/licenses/
- Versioning: https://help.zenodo.org/docs/deposit/manage-versions/
