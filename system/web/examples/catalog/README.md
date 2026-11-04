# Offline Catalog Example

This example contains two fictional repositories, their metadata and READMEs, and sample screening results. It uses the regular import commands to build a working catalog without GitHub or model credentials.

Install the [web application dependencies](../../README.md#install-and-run), then run the following commands from the repository root.

```bash
sample=system/web/examples/catalog
db=system/web/data/agendex-demo.sqlite3
python=system/web/.venv/bin/python

"$python" system/web/scripts/import_dataset.py --dataset keyword \
  --source "$sample/candidates.jsonl" --classifications "$sample/prescreen-scores.jsonl" \
  --database "$db"
"$python" system/web/scripts/import_dataset.py --readmes "$sample/readmes" --database "$db"
"$python" system/web/scripts/import_dataset.py --dataset fine \
  --source "$sample/fine/kept.jsonl" --classifications "$sample/fine/scores.jsonl" \
  --database "$db"
"$python" system/web/scripts/run.py --database "$db"
```

Use a new database path for the first run. The database stores both candidates and both READMEs, and the catalog shows `agendex-demo/terminal-agent`. Open [localhost:8000](http://127.0.0.1:8000), or add `--port 8001` if port 8000 is in use. To open an existing example database, run the last command again.

See the [data pipeline](../../../pipeline/README.md) to build a catalog from collected repositories.
