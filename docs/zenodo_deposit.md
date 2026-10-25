# Zenodo data deposit

GitHub distributes executable code, the validation workbook, small expected
results and usage instructions. Zenodo distributes only the omitted research
data and supporting data documentation, not a code snapshot.

## Current status

Reserved data DOI: [10.5281/zenodo.23640810](https://doi.org/10.5281/zenodo.23640810).
The author has created a draft; data are not yet published. A reserved DOI is
registered only when the record is published and may not resolve beforehand.
Keep the existing draft to retain this reservation.

`data/sources.json` contains the reserved DOI and pending-publication status.
`archive_url` remains `null`; do not guess a file URL or use the DOI landing page
as a ZIP download link. Readers do not need to edit the configuration.

## Finish the deposit

Upload the seven files prepared in the local Zenodo `to_upload/` directory to
the existing draft, including `replication_package_emse_20261025.zip`. Use type
Dataset. Declare CC BY 4.0 for author-owned derived data and documentation, with
third-party exclusions explained in `LICENSE_SCOPE.md`. MIT applies to the code
on GitHub, which is not included in the data deposit. Confirm creators and
version, add the actual GitHub repository URL, then preview and publish when ready.
The original GHALogs candidate list is excluded; obtain it from
https://doi.org/10.5281/zenodo.14796970 for initial filtering as described in README.

## After publication

The authors update `data/sources.json`: retain the actual data version DOI,
set `archive_url` to the HTTPS download link for the ZIP, and update
`publication_status`. Update both READMEs to state that data are publicly
available. Cite the data version used by the paper. Scientific data changes
should be released using Zenodo's New version workflow.

Readers may also download the ZIP manually, then run from the code root:

```powershell
python data_setup.py --archive "../replication_package_emse_20261025.zip"
```

Official instructions: https://help.zenodo.org/docs/deposit/create-new-upload/
