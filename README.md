# ZoozyJars · Subscriptions Dashboard

Auto-generated subscriptions analytics from Stripe. Deployed to GitHub Pages, refreshed every 2 hours.

## What's inside

- **Overview** — status pie, MRR by language, billing cycles
- **Subscriptions** — sortable/filterable table with status, phase (trial/paid), cycle, MRR
- **Production** — jars needed for next 7/14/30/60/90 days based on active subs
- **Cohorts** — time-aware retention funnel (1st test box → 1st renewal → 2nd renewal → ...)
- **Payback** — interactive CAC input, median days to payback, LTV/CAC ratio

Data is filtered to subscriptions created from `2026-04-12` onwards.
Test/excluded subs and customers configured at top of `dashboard.py`.

## Local development

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt
export STRIPE_API_KEY="rk_live_..."
.venv/bin/python dashboard.py
open ~/zoozy-tools/reports/subs_dashboard.html
```

Set `DASHBOARD_PASSWORD` to encrypt the output (recommended for prod).

## Deployment (GitHub Pages)

1. Repo Secrets (Settings → Secrets and variables → Actions):
   - `STRIPE_API_KEY` — restricted Stripe key with read-only access
   - `DASHBOARD_PASSWORD` — password for the AES gate (share with partner)
2. Pages: Settings → Pages → Source: **GitHub Actions**
3. The workflow (`.github/workflows/build.yml`) runs every 2 hours via cron and on push to `main`.
4. URL: `https://<your-username>.github.io/<repo-name>/`

## Security model

- Stripe key lives only in GitHub Secrets, never in repo
- HTML data payload is encrypted with PBKDF2(SHA-256, 250k iter) + AES-GCM in the build step
- Browser decrypts client-side via Web Crypto on password entry
- Without correct password, no customer data is recoverable from the HTML

## Customization

- FX rates: edit `FX_TO_EUR` dict at top of `dashboard.py`
- Cutoff date: edit `CUTOFF_DATE`
- Excludes: edit `EXCLUDE_SUB_IDS` / `EXCLUDE_CUSTOMER_IDS`
- Trial length: edit `TRIAL_DAYS` (default 9)
- Build cadence: edit cron in `.github/workflows/build.yml`
