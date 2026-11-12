
# Workflow cache smell web tool

The tool uses the bundled CacheSmellDetector and displays the eleven CCS
types studied in the paper. Historical detector identifiers are mapped
to the names used in the paper.

Follow the installation and launch commands in the parent tool README.
Open `http://127.0.0.1:8765` and enter a repository such as `owner/repo`
and a path such as `.github/workflows/ci.yml`. The tool reads the current
file on the default branch. It does not execute the workflow.

The tool supports public repositories. An optional `GITHUB_TOKEN` with
appropriate read access can be set for private repositories or higher
request limits. The local server sends the token to the GitHub API.
When the API rate limit is exhausted, the tool may retrieve a public raw
file and report only the content SHA-256.

The web report and JSON download include findings, locations, explanations,
recommendations, and available file hashes. Findings are static heuristics
that should be checked against the workflow context. The tool is a research
prototype.
