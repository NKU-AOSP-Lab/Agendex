# Agendex Web Application

Agendex serves a searchable agent repository catalog and a workspace for defining classification policies, running jobs, and reviewing results. The application stores the catalog, evidence, policies, and job history in SQLite.

## Install and run

Use Python 3.12 and Node.js 20.19+ or 22.12+. Run these commands from the repository root.

```bash
python3.12 -m venv system/web/.venv
system/web/.venv/bin/python -m pip install -r system/web/backend/requirements.lock
npm --prefix system/web ci
```

Build the [offline example database](examples/catalog/README.md) or follow the [data pipeline](../pipeline/README.md) to prepare your own catalog. Start the app with the resulting database path.

```bash
python3 system/web/scripts/run.py --database system/web/data/agendex-demo.sqlite3
```

Open [localhost:8000](http://127.0.0.1:8000). Add `--build` to rebuild the frontend before launch. Add `--dev` to use the Vite development server at [localhost:5173](http://127.0.0.1:5173). You can also set the database path through `AGENDEX_DB_PATH`. The default database path is `system/web/data/agendex-keyword.sqlite3`.

## Classification and export

1. Create a classification policy with labels, decision rules, and thresholds. The optional GLM assistant can draft a policy for review.
2. Select repositories directly or by filtering the catalog. Start a job with a saved policy, a model, and an evidence source. View progress, usage, and results in the job list.
3. Preview threshold changes in the policy results and apply them when ready. The app uses saved scores for threshold changes and reuses compatible scores across jobs.
4. Review or manually adjust labels in repository details. Download the catalog or the selected policy results as CSV.

Classification jobs record the policy version, evidence snapshot, model, and results. The app keeps manual label decisions separately from model scores. Incomplete jobs pause when the service restarts.

## Model credentials

Set `OPENROUTER_API_KEY` for Jev. To enable GLM classification and the policy assistant, set `AGENDEX_GLM_BASE_URL` to an HTTPS OpenAI-compatible API base URL and set `AGENDEX_GLM_API_KEY`. Use `AGENDEX_GLM_MODEL` if the provider requires a custom model name. The backend reads these settings from its environment. See the [environment template](../../.env.example).

## Data and development

To append README evidence to an existing database, pass a completed collection directory or its `readmes.jsonl` file to the importer.

```bash
system/web/.venv/bin/python system/web/scripts/import_dataset.py \
  --readmes /path/to/snapshot/readmes \
  --database /path/to/catalog.sqlite3
```

The sync checks repository IDs and README versions, reuses records already present, and reports invalid or oversized entries separately. See the [data pipeline](../pipeline/README.md) for complete catalog import and rebuild commands.

The frontend lives in `src/`, the FastAPI backend in `backend/agendex/`, and the import and launch commands in `scripts/`. See the [API contract](CONTRACT.md) and the running service's API documentation at `/docs`.

```bash
PYTHONPATH=system/web/backend system/web/.venv/bin/python -m unittest discover -s system/web/backend/tests
npm --prefix system/web test
```

Local mode runs without sign-in by default. See the [deployment guide](deploy/README.md) to configure HTTPS and account authentication for a hosted instance.
