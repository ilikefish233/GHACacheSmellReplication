# GitHub release checklist

- Code, licenses, sub-RQ entry points and small frozen output CSVs are included.
- Large inputs and the original GHALogs candidate file are excluded and ignored.
- Complete offline package verification reports in `docs/verification/original/` and `docs/verification/pipeline/`
  are historical evidence; they do not claim full re-execution in this checkout.
- The new layout validation is recorded separately in `docs/verification/github_layout.json`.
- Before claiming public data access: create the real archive record and fill
  `archive_url`/`doi` in `data/sources.json`, README and citation metadata.
- Third-party workflow licensing review is deferred at the author's request.
- Fresh dependency installation and other operating systems remain untested.
- No repository, upload or DOI was created during local assembly.

- Current 383-workflow validation supersedes the historical 176-case benchmark;
  see `docs/verification/github_layout.json` (section `validation_383`). Include the three full YAML snapshots when publishing the validation correction.
