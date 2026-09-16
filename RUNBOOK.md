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
The Cloud Run service account needs `roles/storage.objectAdmin` on the bucket (the /admin page writes `sites_master.csv` and `users.csv` there).

Custom domain: `gcloud run domain-mappings create --service dashboard-v3 --domain <your-domain>`
then add the DNS records it prints.

Rollback: `gcloud run revisions list --service dashboard-v3` →
`gcloud run services update-traffic dashboard-v3 --to-revisions <rev>=100`.

Logs: `gcloud run services logs tail dashboard-v3 --region asia-south1`.

## Data updates

`gsutil cp sites_master.csv gs://advitia-weighbridge-data/dashboard_config/sites_master.csv`
— visible within 30 min, or at once after a signed-in `POST /admin/refresh-cache`.

## Admin login

`ADMIN_USERNAME` / `ADMIN_PASSWORD_HASH` env vars (see `.env.example`). Without them the
old hardcoded hash in `auth.py` is used — treat that as local-only. On Cloud Run pass the hash
as a secret: `--set-secrets SECRET_KEY=dashboard-secret-key:latest,ADMIN_PASSWORD_HASH=dashboard-admin-hash:latest`.

## Giving someone access

Sign in with the env-var admin → **Admin** in the nav (`/admin`):
- **Users**: username, password, access = "All sites + reports" or one site. Saved hashed to
  `users.csv` in the data folder. Re-adding a username resets its password. Remove = revoke.
- **Data files**: upload a new `sites_master.csv` (header validated first) or `users.csv`;
  download the current copies. Upload clears the caches, so the dashboard updates at once.
- Only the env-var admin sees `/admin`. `users.csv` users never can, so nobody you add can lock you out.

## Data by ULB (public /ulb)

Upload `sites_phases.csv` in Admin (header: location, site_name, phase, agency_name, cluster,
target_mt, remediated_mt, rdf_disposed_mt, inert_disposed_mt, soil_disposed_mt, cnd_disposed_mt,
start_date, deadline_date, status, link_to, notes). One row per (ULB, phase). "ULB" = site_name;
location distinguishes entries (Kadiri1 / Kadiri2). Dates accept dd-mm-yyyy or yyyy-mm-dd.
`link_to` (or site_name) matching `sites_master.csv` adds the open-site icon on the row.
Phase tabs order themselves: "...current" first, then Phase 2, 1, "Phase 1 - 15% Excess", Old.

## Weighbridge photos

`/reports` shows a camera chip per record when the API reports captures
(`image_slots`, derived from the upstream `images{}` dict). It opens
`/record-images/<site>/<date>/<ticket>` — a 2x2 sheet served through Flask, never
directly from the records API, so the open upstream endpoint is not published in a
gated page. Global login sees all sites; a per-site login only the upstream names in
that slug's `api_site_names` (exact match). Bytes are cached in-process (48 MB, 15 min)
and in the browser (`private, max-age=86400`); Admin → Refresh cache clears it.

## Access-request emails (Gmail SMTP)

1. Gmail account with 2-step verification on → myaccount.google.com/apppasswords → create an
   App Password (16 characters). Your normal Gmail password will NOT work.
2. Set `SMTP_HOST=smtp.gmail.com`, `SMTP_PORT=587`, `SMTP_USER=you@gmail.com`,
   `SMTP_PASSWORD=<app password>`, `ACCESS_REQUEST_TO=you@gmail.com` (see `.env.example`).
3. On Cloud Run pass `SMTP_PASSWORD` as a secret, the rest as env vars.

Without these, requests are still accepted and written to the app log (`Access request:` lines).

## Build steps

All screens built: layout + four styles, login, overview, sites list, site detail, reports.

`data/site_daywise_pdf.py` is a stub; replace it with the real file from the old deployment.
