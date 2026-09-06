# dashboard_v3 — runbook

## Local

```bash
git clone https://github.com/tsaaii/dashboard_v3.git && cd dashboard_v3
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                     # edit if needed; .env is git-ignored
cp /path/to/sites_master.csv .                           # git-ignored (contains site passwords)
set -a; source .env; set +a
python app.py                                            # http://localhost:8080
```

`/` public overview · `/?mode=tv` wall display · `/login` admin · `/sites`, `/reports` after sign-in.
Debug mode is on locally: edit templates/CSS and refresh; Python edits auto-restart.

To read the live CSV from GCS instead of the local file:
`gcloud auth application-default login` then `export SITES_MASTER_CSV=gs://advitia-weighbridge-data/dashboard_config/sites_master.csv`.

## First push (once)

```bash
git init -b main
git add -A && git commit -m "dashboard_v3: layout, four styles, data layer"
gh repo create tsaaii/dashboard_v3 --private --source=. --push     # or create on github.com and:
# git remote add origin https://github.com/tsaaii/dashboard_v3.git && git push -u origin main
```

Keep the repo private. Nothing in Git may contain a password: not `.env`, not `sites_master.csv`,
not `app.yaml`. `auth.py` still carries the admin hash; step 3 moves it to an env var.

## Day-to-day Git

```bash
git checkout main && git pull
git checkout -b ui/overview            # one branch per screen
# edit → run locally → check browser
git add -A && git commit -m "Overview: rotating agency slide"
git push -u origin ui/overview         # open a Pull Request on GitHub, merge into main
```

## Production — Cloud Run (same GCP project as the weighbridge API)

One-time:

```bash
gcloud config set project <project-id>
gcloud services enable run.googleapis.com cloudbuild.googleapis.com secretmanager.googleapis.com
python -c "import secrets; print(secrets.token_hex(32))" | gcloud secrets create dashboard-secret-key --data-file=-
```

Deploy (every release, from `main`):

```bash
git checkout main && git pull
gcloud run deploy dashboard-v3 --source . --region asia-south1 --allow-unauthenticated \
  --min-instances 1 --memory 512Mi \
  --set-env-vars SITES_MASTER_CSV=gs://advitia-weighbridge-data/dashboard_config/sites_master.csv,PROJECT_DEADLINE=2026-09-30,RECORDS_API_BASE=https://weighbridge-api-asia-south1-287877277037.asia-south1.run.app \
  --set-secrets SECRET_KEY=dashboard-secret-key:latest
```

`--source .` builds the container for you from `requirements.txt` + `Procfile`; no Dockerfile.
`--min-instances 1` keeps the TV wall from hitting a cold start.
The Cloud Run service account needs `roles/storage.objectViewer` on the bucket.

Custom domain: `gcloud run domain-mappings create --service dashboard-v3 --domain <your-domain>`
then add the DNS records it prints.

Rollback: `gcloud run revisions list --service dashboard-v3` →
`gcloud run services update-traffic dashboard-v3 --to-revisions <rev>=100`.

Logs: `gcloud run services logs tail dashboard-v3 --region asia-south1`.

## Data updates

`gsutil cp sites_master.csv gs://advitia-weighbridge-data/dashboard_config/sites_master.csv`
— visible within 30 min, or at once after a signed-in `POST /admin/refresh-cache`.

## Build steps

1. Layout + four styles — done
2. (nothing to delete in a fresh repo)
3. Login page
4. Overview
5. Sites list
6. Site detail
7. Reports

`data/site_daywise_pdf.py` is a stub; replace it with the real file from the old deployment.
