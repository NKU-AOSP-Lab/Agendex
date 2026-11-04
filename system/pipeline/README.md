# Build the catalog database

Import a saved screening snapshot locally, or collect and score a new batch. Run the commands from the repository root on macOS or Linux after installing the [application dependencies](../../README.md).

## Import a saved snapshot

Use inputs from one batch. Keep the prescreen scores and retained candidate list, the README journal and its files, and the fine screening scores, kept list and `SUMMARY.json`. Relative README paths resolve from the journal directory. If you use manually supplemented candidates, preserve any additional evidence paths recorded relative to the repository root.

The following commands load metadata, attach README evidence and activate the fine-screened catalog. Choose a new database path.

```bash
agendex_python=system/web/.venv/bin/python
agendex_prescreen=/path/to/prescreen/scoring
agendex_readmes=/path/to/readmes
agendex_fine=/path/to/fine
agendex_database=system/web/data/agendex-rebuilt.sqlite3

test ! -e "$agendex_database" &&
  "$agendex_python" system/web/scripts/import_dataset.py --dataset keyword \
    --source "$agendex_prescreen/kept-augmented.jsonl" \
    --classifications "$agendex_prescreen/scores.jsonl" --database "$agendex_database" &&
  "$agendex_python" system/web/scripts/import_dataset.py \
    --readmes "$agendex_readmes" --database "$agendex_database" &&
  "$agendex_python" system/web/scripts/import_dataset.py --dataset fine \
    --source "$agendex_fine/kept.jsonl" \
    --classifications "$agendex_fine/scores.jsonl" --database "$agendex_database"
```

```bash
"$agendex_python" system/web/scripts/run.py --database "$agendex_database"
```

Open [localhost:8000](http://127.0.0.1:8000). Use the [offline example](../web/examples/catalog/README.md) to try this process without a snapshot of your own. Restore an SQLite backup to recover user policies, labels, jobs and classification history.

## Collect and screen a new batch

Load `GITHUB_TOKEN` and `OPENROUTER_API_KEY` from your local environment. Collection uses the GitHub API. The two scoring steps send requests to OpenRouter, with a spending stop line set by `--max-cost`. Results reflect the repository metadata, README versions and model outputs seen during that run.

Choose a fresh output directory. Search discovers candidates, and verification checks the saved pages and deduplicated metadata locally.

```bash
agendex_python=system/web/.venv/bin/python
agendex_batch=system/pipeline/output/catalog-001

"$agendex_python" system/pipeline/collect_repositories.py --output-dir "$agendex_batch/search" &&
  "$agendex_python" system/pipeline/verify_collection.py \
    "$agendex_batch/search" "$agendex_batch/search/VERIFICATION.json"
```

Prescreen scores repository metadata. The supplement command prepares the retained list for the importer and can also accept an optional `--reviews` JSONL of supplemental README judgments.

```bash
"$agendex_python" system/pipeline/prescreen_metadata.py plan \
  "$agendex_batch/search/repositories.jsonl" --output-dir "$agendex_batch/prescreen" &&
  "$agendex_python" system/pipeline/prescreen_metadata.py score \
    --output-dir "$agendex_batch/prescreen" --max-cost 1 &&
  "$agendex_python" system/pipeline/prescreen_metadata.py supplement \
    --output-dir "$agendex_batch/prescreen"
```

Fetch README evidence and then score the retained candidates. Fine screening expects a complete UTF-8 README for each candidate in the supplied list. If planning stops, check the collection journal for missing or incomplete inputs.

```bash
"$agendex_python" system/experiment_tools/fetch_readmes.py \
  "$agendex_batch/prescreen/scoring/kept-augmented.jsonl" \
  --output-dir "$agendex_batch/readmes" --reuse-source-metadata --workers 8 --interval 0.2 &&
  "$agendex_python" system/pipeline/fine_screen.py plan \
    --source "$agendex_batch/prescreen/scoring/kept-augmented.jsonl" \
    --readmes-dir "$agendex_batch/readmes" --output-dir "$agendex_batch/fine" &&
  "$agendex_python" system/pipeline/fine_screen.py run \
    --output-dir "$agendex_batch/fine" --workers 8 --max-cost 3
```

For a paused search or README collection, repeat the same command and add `--resume`. Repeat a scoring command with its existing output directory to resume recorded progress. Review failed requests before allowing an explicit retry.

Use the paths below with the import commands above once screening finishes.

```bash
agendex_prescreen="$agendex_batch/prescreen/scoring"
agendex_readmes="$agendex_batch/readmes"
agendex_fine="$agendex_batch/fine"
agendex_database="$agendex_batch/agendex.sqlite3"
```
