# Optional Workflow Cache Inspector prototype

The UI/service source is a snapshot taken during artifact assembly. `run.py` binds it to the paper-selected run3 detector rather than the newer active development detector. It is separate from the offline statistical replication.

```bash
python src/tool/run.py --host 127.0.0.1 --port 8765
```

Open http://127.0.0.1:8765 locally. A user-requested GitHub scan fetches current public workflow content and requires network access. Live results are not expected to reproduce historical workflow results. No token or credential is bundled. All paper analyses can run without starting this server.
