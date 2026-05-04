#!/usr/bin/env python3
"""
ZoozyJars Subscriptions Dashboard
Generates a self-contained HTML report from Stripe.
Run: ~/zoozy-tools/.venv/bin/python ~/zoozy-tools/subs_dashboard.py
"""

import os, sys, json, base64, datetime as dt
from pathlib import Path
from collections import defaultdict, Counter

import stripe

# ------------------------------------------------------------
# CONFIG
# ------------------------------------------------------------
# Output directory: ./public/ in CI (for GitHub Pages), ~/zoozy-tools/reports/ locally
ENV = Path.home() / ".zoozyjars_env"
if os.environ.get("CI"):
    OUT_DIR = Path("public")
else:
    OUT_DIR = Path(os.environ.get("OUT_DIR", str(Path.home() / "zoozy-tools" / "reports")))
OUT_DIR.mkdir(parents=True, exist_ok=True)

# Manual FX (1 unit native -> EUR). Update when needed.
FX_TO_EUR = {
    "eur": 1.0,
    "pln": 0.235,
    "usd": 0.92,
    "gbp": 1.18,
    "uah": 0.022,
}

# How far back to pull invoices for cohort/payback analysis
INVOICE_LOOKBACK_DAYS = 720

# Hard cutoff: ignore everything (subs + invoices) created before this date.
# Old data from previous business model — not relevant to current analytics.
CUTOFF_DATE = "2026-04-12"
CUTOFF_TS = int(dt.datetime.fromisoformat(CUTOFF_DATE).timestamp())

# Exclude specific subscriptions (e.g. duplicates, test data) from analytics.
EXCLUDE_SUB_IDS = {
    "sub_1TNu3cFTFpBXf4s3uXlKafw6",  # Igor test sub
    "sub_1TOvG6FTFpBXf4s3q32zlQFY",  # Fedya — canceled the old one and made a new one (sub_1TR9xY...)
}

# Exclude customers entirely (their subs AND invoices won't show up anywhere).
EXCLUDE_CUSTOMER_IDS = {
    "cus_UMcyIVM20sY0nm",  # Igor (cielo8008@gmail.com) — test account
}

# ------------------------------------------------------------
# ENV LOADER
# ------------------------------------------------------------
def load_env():
    """Load env from ~/.zoozyjars_env if exists. In CI, env is already set via Secrets."""
    if not ENV.exists():
        return  # CI / fresh setup — env should come from process env
    for line in ENV.read_text().splitlines():
        l = line.strip()
        if not l or l.startswith("#"):
            continue
        if l.startswith("export "):
            l = l[7:]
        if "=" in l:
            k, v = l.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

load_env()
key = os.environ.get("STRIPE_API_KEY")
if not key:
    sys.exit("STRIPE_API_KEY missing in ~/.zoozyjars_env")
stripe.api_key = key

# ------------------------------------------------------------
# FETCH HELPERS
# ------------------------------------------------------------
def fetch_all(method, **kw):
    out = []
    for x in method(limit=100, **kw).auto_paging_iter():
        out.append(x)
        if len(out) % 200 == 0:
            print(f"   ...{len(out)}", flush=True)
    return out

def to_eur(amount_minor, currency):
    if amount_minor is None:
        return 0.0
    cur = (currency or "eur").lower()
    return (amount_minor / 100.0) * FX_TO_EUR.get(cur, 1.0)

def period_days(rec):
    if not rec:
        return None
    n = (rec.get("interval_count") if isinstance(rec, dict) else rec.interval_count) or 1
    i = rec.get("interval") if isinstance(rec, dict) else rec.interval
    base = {"day": 1, "week": 7, "month": 30, "year": 365}.get(i, 0)
    return base * n if base else None

def normalize_status(s):
    status = s.status
    if status == "canceled":
        return "canceled"
    try:
        if s.pause_collection:
            return "paused"
    except AttributeError:
        pass
    try:
        if s.cancel_at_period_end:
            return "canceling"
    except AttributeError:
        pass
    if status in ("active", "trialing"):
        return "active"
    if status in ("past_due", "unpaid"):
        return "at_risk"
    if status in ("incomplete", "incomplete_expired"):
        return "incomplete"
    return status

def _md(md_obj):
    """Convert Stripe metadata object to plain dict safely."""
    if not md_obj:
        return {}
    if isinstance(md_obj, dict):
        return md_obj
    try:
        return md_obj.to_dict()
    except Exception:
        try:
            return {k: md_obj[k] for k in list(md_obj.keys())}
        except Exception:
            return {}

def detect_lang(customer):
    if not customer or isinstance(customer, str):
        return None
    md = _md(_attr(customer, "metadata"))
    lang = md.get("lang") or md.get("language") or md.get("locale")
    if lang:
        return str(lang).lower()[:2]
    locales = _attr(customer, "preferred_locales") or []
    if locales:
        return locales[0].lower()[:2]
    addr = _attr(customer, "address")
    country = _attr(addr, "country") if addr else None
    if country:
        return {
            "PL": "pl", "UA": "uk", "UK": "en", "GB": "en",
            "US": "en", "DE": "de", "FR": "fr", "ES": "es",
        }.get(country, country.lower())
    return None

# ------------------------------------------------------------
# FETCH
# ------------------------------------------------------------
print("→ products...", flush=True)
products = fetch_all(stripe.Product.list, active=True)
inactive = fetch_all(stripe.Product.list, active=False)
products.extend(inactive)
PRODUCT_NAME = {p.id: p.name for p in products}
print(f"   {len(products)} products")

print(f"→ subscriptions (created >= {CUTOFF_DATE})...", flush=True)
subs_all = fetch_all(
    stripe.Subscription.list,
    status="all",
    expand=["data.customer", "data.items.data.price"],
    created={"gte": CUTOFF_TS},
)
subs = [
    s for s in subs_all
    if s.created >= CUTOFF_TS
    and s.id not in EXCLUDE_SUB_IDS
    and (s.customer if isinstance(s.customer, str) else s.customer.id) not in EXCLUDE_CUSTOMER_IDS
]
n_excluded = len(subs_all) - len(subs)
print(f"   {len(subs)} subscriptions (excluded {n_excluded} of {len(subs_all)})")

print(f"→ paid invoices (created >= {CUTOFF_DATE})...", flush=True)
invoices_all = fetch_all(stripe.Invoice.list, status="paid", created={"gte": CUTOFF_TS})
invoices = [
    i for i in invoices_all
    if i.created >= CUTOFF_TS
    and i.customer not in EXCLUDE_CUSTOMER_IDS
]
print(f"   {len(invoices)} paid invoices (excluded {len(invoices_all) - len(invoices)})")

# For monthly revenue display we want to match Stripe Dashboard exactly,
# so we pull ALL PaymentIntents from the start of the calendar month containing
# CUTOFF_DATE (e.g. all of April even if cutoff is Apr 12). This way the
# "April" column matches Stripe's "April 1-30" view.
REVENUE_FETCH_FROM = dt.datetime(
    int(CUTOFF_DATE[:4]), int(CUTOFF_DATE[5:7]), 1
)
REVENUE_FETCH_TS = int(REVENUE_FETCH_FROM.timestamp())

print(f"→ payment intents (created >= {REVENUE_FETCH_FROM.date()})...", flush=True)
pis_all = fetch_all(stripe.PaymentIntent.list, created={"gte": REVENUE_FETCH_TS})
# For monthly revenue we DON'T exclude Igor / old subs — those are still real
# cash transactions and must match Stripe Dashboard. Cohort/sub analytics use
# their own exclusion logic.
payment_intents = [pi for pi in pis_all if pi.status == "succeeded"]
print(f"   {len(payment_intents)} succeeded PIs (out of {len(pis_all)})")

print(f"→ refunds (created >= {REVENUE_FETCH_FROM.date()})...", flush=True)
refunds_all = fetch_all(stripe.Refund.list, created={"gte": REVENUE_FETCH_TS})
refunds = [r for r in refunds_all if r.status == "succeeded"]
print(f"   {len(refunds)} successful refunds")

# ------------------------------------------------------------
# NORMALIZE SUBSCRIPTIONS
# ------------------------------------------------------------
def _attr(obj, name, default=None):
    """Safe attribute access on StripeObject."""
    try:
        return getattr(obj, name)
    except AttributeError:
        return default

def sub_row(s):
    cust = s.customer if not isinstance(s.customer, str) else None
    items = []
    mrr_eur = 0.0
    period_d = None
    item_period_start = None
    item_period_end = None
    for it in s["items"]["data"]:
        rec = it.price.recurring
        d = period_days(rec)
        if d:
            period_d = d  # last wins; usually all items same cycle
            amt = (it.price.unit_amount or 0) * (it.quantity or 1)
            mrr_eur += to_eur(amt, it.price.currency) * (30.0 / d)
        # In newer Stripe API versions period dates are on the item, not the sub
        item_period_start = item_period_start or _attr(it, "current_period_start")
        item_period_end = item_period_end or _attr(it, "current_period_end")
        prod_id = it.price.product if isinstance(it.price.product, str) else (it.price.product.id if it.price.product else None)
        items.append({
            "product_id": prod_id,
            "product_name": PRODUCT_NAME.get(prod_id, prod_id or "?"),
            "qty": it.quantity or 1,
            "currency": it.price.currency,
            "unit_amount": it.price.unit_amount,
            "interval": (rec.get("interval") if isinstance(rec, dict) else (rec.interval if rec else None)),
            "interval_count": (rec.get("interval_count") if isinstance(rec, dict) else (rec.interval_count if rec else None)),
        })

    pause = None
    pc = _attr(s, "pause_collection")
    if pc:
        pause = {
            "behavior": _attr(pc, "behavior"),
            "resumes_at": _attr(pc, "resumes_at"),
        }

    cust_addr = _attr(cust, "address") if cust else None
    # phase: are they still in their initial trial (haven't been charged a renewal yet)
    # or have they passed trial and started paying? Independent of cancel/pause intent.
    phase = "trial" if s.status == "trialing" else "paid"
    return {
        "id": s.id,
        "customer_id": _attr(cust, "id") if cust else s.customer,
        "email": _attr(cust, "email") if cust else None,
        "name": _attr(cust, "name") if cust else None,
        "country": _attr(cust_addr, "country") if cust_addr else None,
        "lang": detect_lang(cust) if cust else None,
        "phase": phase,
        "raw_status": s.status,
        "status": normalize_status(s),
        "created": s.created,
        "current_period_start": _attr(s, "current_period_start") or item_period_start,
        "current_period_end": _attr(s, "current_period_end") or item_period_end,
        "trial_end": _attr(s, "trial_end"),
        "canceled_at": _attr(s, "canceled_at"),
        "cancel_at": _attr(s, "cancel_at"),
        "cancel_at_period_end": _attr(s, "cancel_at_period_end") or False,
        "pause_collection": pause,
        "mrr_eur": round(mrr_eur, 2),
        "period_days": period_d,
        "items": items,
        "n_jars": sum(it["qty"] for it in items),
    }

print("→ normalize subscriptions...", flush=True)
sub_rows = [sub_row(s) for s in subs]

# ------------------------------------------------------------
# RENEWALS PER SUBSCRIPTION
# Step 1 = sub created (test box, subscribed)
# Step 2+ = paid `subscription_cycle` invoices (real renewals)
# `subscription_create` (€0) = trial setup, NOT a renewal
# `subscription_update` = sub modified (plan/qty change), NOT a renewal
# ------------------------------------------------------------
def invoice_sub_id(inv):
    """Get the subscription ID this invoice belongs to (new Stripe API structure)."""
    parent = _attr(inv, "parent")
    if not parent:
        return None
    sd = _attr(parent, "subscription_details")
    if not sd:
        return None
    return _attr(sd, "subscription")

renewals_per_sub = defaultdict(int)
revenue_per_sub = defaultdict(float)
for inv in invoices:
    if (inv.amount_paid or 0) <= 0:
        continue
    if inv.billing_reason != "subscription_cycle":
        # only count real cycle renewals; skip create/update/manual
        continue
    sid = invoice_sub_id(inv)
    if not sid:
        continue
    renewals_per_sub[sid] += 1
    revenue_per_sub[sid] += to_eur(inv.amount_paid, inv.currency)

# Annotate each sub with age, actual funnel step, and expected funnel step
# Step 1 = test box (everyone), Step 2 = 1st renewal billed, Step 3 = 2nd renewal, etc.
# Expected step = which step they SHOULD be on by now, based on subscription age.
TRIAL_DAYS = 9
NOW_TS = dt.datetime.utcnow().timestamp()

def compute_expected_step(age_days, cycle_days):
    if age_days < TRIAL_DAYS:
        return 1
    return 2 + int((age_days - TRIAL_DAYS) // (cycle_days or 28))

for s in sub_rows:
    age = (NOW_TS - s["created"]) / 86400
    s["age_days"] = round(age, 1)
    s["actual_step"] = 1 + renewals_per_sub.get(s["id"], 0)
    s["expected_step"] = compute_expected_step(age, s["period_days"] or 28)

# Per-customer order history (kept for payback analysis — tracks ALL paid invoices)
by_customer = defaultdict(list)
for inv in invoices:
    if (inv.amount_paid or 0) <= 0:
        continue
    by_customer[inv.customer].append({
        "id": inv.id,
        "created": inv.created,
        "amount_eur": round(to_eur(inv.amount_paid, inv.currency), 2),
        "currency": inv.currency,
        "billing_reason": inv.billing_reason,
        "sub_id": invoice_sub_id(inv),
    })
for cid in by_customer:
    by_customer[cid].sort(key=lambda x: x["created"])

# ------------------------------------------------------------
# COHORT FUNNEL — time-aware retention
# For each step S (1..5+):
#   due     = subs that should have reached step S by now (based on age)
#   reached = subs that actually reached step S (paid S-1 renewals)
#   pending = subs that haven't yet had time to reach S (still in pipeline, not canceled)
#   lost    = subs that should have but didn't (canceled/canceling without paying enough)
# Conversion % = reached / due (only counts mature data).
# ------------------------------------------------------------
def funnel_for(subs_list):
    """For each step S, classify each sub into ONE of: reached / lost / pending.
    Sum equals total cohort size — every sub is in exactly one bucket per step.
      reached = actual_step >= S (already reached this step)
      lost    = actual_step < S AND status is canceled/canceling (will never reach)
      pending = actual_step < S AND status is alive (might still reach)
    `due` is also tracked: how many subs SHOULD have reached step S based on age.
    Conversion % = reached / due (mature-cohort retention only).
    """
    steps = []
    for S in (1, 2, 3, 4, 5):
        due = reached = lost = pending = 0
        for s in subs_list:
            if s["actual_step"] >= S:
                reached += 1
            elif s["status"] in ("canceled", "canceling"):
                lost += 1
            else:
                pending += 1
            if s["expected_step"] >= S:
                due += 1
        conv = round(reached / due * 100, 1) if due else None
        steps.append({
            "step": S,
            "due": due, "reached": reached, "pending": pending, "lost": lost,
            "conversion": conv,
        })
    return steps

total_funnel_steps = funnel_for(sub_rows)

# Per-cohort: group subs by creation month
subs_by_cohort = defaultdict(list)
for s in sub_rows:
    cohort = dt.datetime.utcfromtimestamp(s["created"]).strftime("%Y-%m")
    subs_by_cohort[cohort].append(s)

# Revenue BILLED during each calendar month — gross PaymentIntents minus refunds.
# Group by Warsaw timezone (Stripe account TZ) to match Stripe Dashboard exactly.
# Covers all payment methods (cards, BLIK, Klarna, etc.).
import zoneinfo
WARSAW = zoneinfo.ZoneInfo("Europe/Warsaw")
def month_warsaw(unix_ts):
    return dt.datetime.fromtimestamp(unix_ts, tz=WARSAW).strftime("%Y-%m")

revenue_by_calendar_month = defaultdict(float)
for pi in payment_intents:
    month = month_warsaw(pi.created)
    revenue_by_calendar_month[month] += to_eur(pi.amount, pi.currency)
# Subtract refunds (attributed to the month the refund was issued)
for r in refunds:
    month = month_warsaw(r.created)
    revenue_by_calendar_month[month] -= to_eur(r.amount, r.currency)

cohort_table = []
for k in sorted(subs_by_cohort.keys()):
    cs = subs_by_cohort[k]
    revenue = revenue_by_calendar_month.get(k, 0.0)
    cohort_table.append({
        "cohort": k,
        "size": len(cs),
        "steps": funnel_for(cs),
        "revenue_eur": round(revenue, 0),
        "rev_per_sub": round(revenue / len(cs), 2) if cs else 0,
    })

# ------------------------------------------------------------
# PRODUCTION FORECAST (active subs only, project upcoming charges)
# ------------------------------------------------------------
def forecast_window(days):
    now = dt.datetime.utcnow().timestamp()
    horizon = now + days * 86400
    by_product = defaultdict(lambda: {"jars": 0, "subs": set()})
    total_jars = 0
    total_subs = set()
    for s in sub_rows:
        if s["status"] != "active":
            continue
        if not s["current_period_end"] or not s["period_days"]:
            continue
        next_charge = s["current_period_end"]
        period_secs = s["period_days"] * 86400
        # walk forward; cap at 25 cycles to avoid runaway
        for _ in range(25):
            if next_charge > horizon:
                break
            if next_charge >= now:
                for it in s["items"]:
                    by_product[it["product_name"]]["jars"] += it["qty"]
                    by_product[it["product_name"]]["subs"].add(s["id"])
                total_jars += s["n_jars"]
                total_subs.add(s["id"])
            next_charge += period_secs

    products = sorted(
        [{"product": p, "jars": v["jars"], "subs": len(v["subs"])} for p, v in by_product.items()],
        key=lambda x: -x["jars"],
    )
    return {
        "products": products,
        "total_jars": total_jars,
        "total_subs": len(total_subs),
    }

forecast = {n: forecast_window(n) for n in (7, 14, 30, 60, 90)}

# ------------------------------------------------------------
# PAYBACK TIMELINE (per-customer cumulative revenue)
# ------------------------------------------------------------
ltv_data = []
for cid, orders in by_customer.items():
    if not orders:
        continue
    first = orders[0]["created"]
    cumulative = 0.0
    timeline = []
    for o in orders:
        cumulative += o["amount_eur"]
        days_since_first = (o["created"] - first) / 86400
        timeline.append({"days": round(days_since_first, 1), "cum": round(cumulative, 2)})
    ltv_data.append({
        "customer_id": cid,
        "first_order": first,
        "n_orders": len(orders),
        "total_eur": round(cumulative, 2),
        "timeline": timeline,
    })

# ------------------------------------------------------------
# KPIs
# ------------------------------------------------------------
status_count = Counter(s["status"] for s in sub_rows)
phase_count = Counter()
for s in sub_rows:
    if s["status"] in ("active", "paused", "canceling"):
        phase_count[(s["status"], s["phase"])] += 1
mrr_total = round(sum(s["mrr_eur"] for s in sub_rows if s["status"] == "active"), 2)
mrr_paid_only = round(sum(s["mrr_eur"] for s in sub_rows if s["status"] == "active" and s["phase"] == "paid"), 2)
arr_total = round(mrr_total * 12, 2)
n_active = max(status_count.get("active", 0), 1)
avg_mrr = round(mrr_total / n_active, 2)

now_ts = dt.datetime.utcnow().timestamp()
d30 = now_ts - 30 * 86400
new_30d = sum(1 for s in sub_rows if s["created"] >= d30)
churned_30d = sum(1 for s in sub_rows if s["canceled_at"] and s["canceled_at"] >= d30)

# Language breakdown of active MRR
mrr_by_lang = defaultdict(float)
for s in sub_rows:
    if s["status"] == "active":
        mrr_by_lang[s["lang"] or "?"] += s["mrr_eur"]
mrr_by_lang = {k: round(v, 2) for k, v in sorted(mrr_by_lang.items(), key=lambda x: -x[1])}

# Cycle breakdown of active subs
cycle_count = Counter()
for s in sub_rows:
    if s["status"] == "active":
        d = s["period_days"]
        if d == 14: cycle_count["14d"] += 1
        elif d == 28: cycle_count["28d"] += 1
        elif d == 30: cycle_count["1mo"] += 1
        elif d in (7 * n for n in (4, 6, 8)): cycle_count[f"{d//7}w"] += 1
        else: cycle_count[f"{d}d"] += 1

# ------------------------------------------------------------
# ASSEMBLE PAYLOAD
# ------------------------------------------------------------
data = {
    "generated_at": dt.datetime.utcnow().isoformat(timespec="seconds") + "Z",
    "cutoff_date": CUTOFF_DATE,
    "fx_rates": FX_TO_EUR,
    "kpi": {
        "active": status_count.get("active", 0),
        "active_trial": phase_count.get(("active", "trial"), 0),
        "active_paid": phase_count.get(("active", "paid"), 0),
        "paused": status_count.get("paused", 0),
        "paused_trial": phase_count.get(("paused", "trial"), 0),
        "paused_paid": phase_count.get(("paused", "paid"), 0),
        "canceling": status_count.get("canceling", 0),
        "canceling_trial": phase_count.get(("canceling", "trial"), 0),
        "canceling_paid": phase_count.get(("canceling", "paid"), 0),
        "canceled": status_count.get("canceled", 0),
        "at_risk": status_count.get("at_risk", 0),
        "incomplete": status_count.get("incomplete", 0),
        "mrr_eur": mrr_total,
        "mrr_paid_only": mrr_paid_only,
        "arr_eur": arr_total,
        "avg_mrr": avg_mrr,
        "new_30d": new_30d,
        "churned_30d": churned_30d,
    },
    "mrr_by_lang": mrr_by_lang,
    "cycle_count": dict(cycle_count),
    "subs": sub_rows,
    "cohorts": cohort_table,
    "total_funnel_steps": total_funnel_steps,
    "trial_days": TRIAL_DAYS,
    "forecast": forecast,
    "ltv": ltv_data,
}

# ------------------------------------------------------------
# RENDER HTML
# ------------------------------------------------------------
HTML_TEMPLATE = r"""<!doctype html>
<html lang="uk">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ZoozyJars · Subscriptions Dashboard</title>
<style>
* { box-sizing: border-box; }
body { font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif; color: #1a1a1a; background: #faf8f3; margin: 0; }
header { padding: 20px 28px; background: white; border-bottom: 1px solid #e8e4d8; display: flex; justify-content: space-between; align-items: baseline; }
h1 { margin: 0; font-size: 19px; font-weight: 600; letter-spacing: -0.2px; }
h1 .accent { color: #5d6f3d; }
.meta { color: #8b8775; font-size: 12px; font-variant-numeric: tabular-nums; }
.container { padding: 20px 28px 60px; max-width: 1500px; margin: 0 auto; }

/* KPI strip */
.kpi-row { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 10px; margin-bottom: 20px; }
.kpi { background: white; border: 1px solid #e8e4d8; border-radius: 10px; padding: 14px 16px; }
.kpi .label { font-size: 10px; text-transform: uppercase; color: #8b8775; letter-spacing: 0.7px; font-weight: 500; }
.kpi .value { font-size: 24px; font-weight: 600; margin-top: 4px; font-variant-numeric: tabular-nums; letter-spacing: -0.5px; }
.kpi .delta { font-size: 11px; color: #8b8775; margin-top: 2px; font-variant-numeric: tabular-nums; }
.kpi.active .value { color: #4a7c4a; }
.kpi.paused .value { color: #b58a30; }
.kpi.canceled .value { color: #a04540; }
.kpi.mrr .value { color: #5d6f3d; }

/* Tabs */
.tabs { display: flex; gap: 0; border-bottom: 1px solid #e8e4d8; margin-bottom: 18px; background: white; border-radius: 10px 10px 0 0; padding: 0 8px; overflow-x: auto; }
.tab { padding: 12px 18px; cursor: pointer; border-bottom: 2px solid transparent; font-weight: 500; color: #8b8775; white-space: nowrap; font-size: 13px; }
.tab:hover { color: #1a1a1a; }
.tab.active { color: #1a1a1a; border-color: #5d6f3d; }
.panel { display: none; }
.panel.active { display: block; }

/* Cards */
.card { background: white; border: 1px solid #e8e4d8; border-radius: 10px; padding: 18px; margin-bottom: 14px; }
.card h3 { margin: 0 0 12px; font-size: 13px; font-weight: 600; text-transform: uppercase; color: #6b6b6b; letter-spacing: 0.5px; }
.grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
.grid-3 { display: grid; grid-template-columns: 2fr 1fr 1fr; gap: 14px; }
@media (max-width: 900px) { .grid-2, .grid-3 { grid-template-columns: 1fr; } }

/* Table */
table { width: 100%; border-collapse: collapse; background: white; border: 1px solid #e8e4d8; border-radius: 10px; overflow: hidden; font-size: 13px; }
th, td { text-align: left; padding: 9px 12px; border-bottom: 1px solid #f3efe4; }
th { background: #f7f4eb; font-size: 10px; text-transform: uppercase; letter-spacing: 0.5px; color: #8b8775; cursor: pointer; user-select: none; font-weight: 600; }
th:hover { background: #efebde; }
th.sorted::after { content: " ↓"; color: #5d6f3d; }
th.sorted.asc::after { content: " ↑"; }
tbody tr:hover { background: #fcfaf3; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
.muted { color: #8b8775; }

/* Pills */
.pill { display: inline-block; padding: 2px 9px; border-radius: 12px; font-size: 11px; font-weight: 500; }
.pill.active { background: #e6efdc; color: #4a7c4a; }
.pill.paused { background: #faecc8; color: #8a6a25; }
.pill.canceled { background: #f6d8d4; color: #8b3530; }
.pill.canceling { background: #fde6cf; color: #9a5a25; }
.pill.at_risk { background: #ffd9c5; color: #9a3820; }
.pill.incomplete { background: #e8e8e8; color: #555; }
.pill.trial { background: #e8eef5; color: #4a6585; border: 1px dashed #a8b8c8; }
.pill.paid { background: #f0ede4; color: #6b6b6b; }

/* Filter bar */
.filters { display: flex; gap: 8px; margin-bottom: 12px; flex-wrap: wrap; align-items: center; background: white; padding: 10px 12px; border: 1px solid #e8e4d8; border-radius: 10px; }
.filters input, .filters select { padding: 6px 10px; border: 1px solid #d6d2c5; border-radius: 6px; background: white; font: inherit; outline: none; }
.filters input:focus, .filters select:focus { border-color: #5d6f3d; }
.filter-pill { padding: 4px 12px; border-radius: 20px; border: 1px solid #d6d2c5; background: white; cursor: pointer; font-size: 12px; user-select: none; transition: all 0.1s; }
.filter-pill:hover { border-color: #5d6f3d; }
.filter-pill.on { background: #5d6f3d; color: white; border-color: #5d6f3d; }
.filters .sep { color: #d6d2c5; padding: 0 4px; }
.filters .label { font-size: 11px; color: #8b8775; text-transform: uppercase; letter-spacing: 0.5px; font-weight: 500; }

/* Heatmap */
.heat td { text-align: center; }
.heat .hb { display: inline-block; padding: 4px 10px; border-radius: 4px; font-variant-numeric: tabular-nums; font-weight: 500; min-width: 60px; }

/* Forecast */
.window-tabs { display: flex; gap: 6px; margin-bottom: 10px; }
.window-tab { padding: 6px 14px; border: 1px solid #d6d2c5; border-radius: 20px; background: white; cursor: pointer; font-size: 12px; }
.window-tab.on { background: #5d6f3d; color: white; border-color: #5d6f3d; }
.big-num { font-size: 32px; font-weight: 600; color: #5d6f3d; font-variant-numeric: tabular-nums; }

/* Status pie (simple SVG) */
.legend { display: flex; flex-direction: column; gap: 6px; font-size: 12px; }
.legend .row { display: flex; align-items: center; gap: 8px; }
.legend .sw { width: 10px; height: 10px; border-radius: 2px; }

/* Subscription detail row */
.detail { background: #fcfaf3; padding: 12px 16px; font-size: 12px; }
.detail .items { display: flex; gap: 12px; flex-wrap: wrap; }
.detail .item { background: white; padding: 6px 10px; border-radius: 6px; border: 1px solid #e8e4d8; }
.detail .item .qty { color: #5d6f3d; font-weight: 600; }

/* Inputs section */
.input-row { display: flex; align-items: center; gap: 12px; margin-bottom: 14px; }
.input-row input { padding: 8px 12px; border: 1px solid #d6d2c5; border-radius: 6px; font: inherit; width: 120px; }
.input-row label { font-size: 12px; color: #8b8775; text-transform: uppercase; letter-spacing: 0.5px; font-weight: 500; }

a.email { color: #5d6f3d; text-decoration: none; }
a.email:hover { text-decoration: underline; }
.copy { cursor: pointer; opacity: 0.5; font-size: 10px; margin-left: 4px; }
.copy:hover { opacity: 1; }

/* Password lock screen */
#lock-screen { position: fixed; inset: 0; background: #faf8f3; display: none; align-items: center; justify-content: center; z-index: 9999; }
#lock-screen.show { display: flex; }
#lock-box { background: white; padding: 32px 40px; border: 1px solid #e8e4d8; border-radius: 12px; max-width: 360px; width: 90%; text-align: center; }
#lock-box h2 { margin: 0 0 6px; font-size: 18px; }
#lock-box p { margin: 0 0 20px; color: #8b8775; font-size: 13px; }
#lock-input { width: 100%; padding: 10px 14px; border: 1px solid #d6d2c5; border-radius: 6px; font: inherit; outline: none; }
#lock-input:focus { border-color: #5d6f3d; }
#lock-btn { margin-top: 12px; width: 100%; padding: 10px; background: #5d6f3d; color: white; border: 0; border-radius: 6px; font: inherit; font-weight: 500; cursor: pointer; }
#lock-btn:hover { background: #4d5e30; }
#lock-error { color: #a04540; font-size: 12px; margin-top: 10px; min-height: 16px; }
#app { display: none; }
#app.show { display: block; }
</style>
</head>
<body>

<!-- Password lock (shown only if data is encrypted) -->
<div id="lock-screen">
  <div id="lock-box">
    <h2>ZoozyJars Dashboard</h2>
    <p>Enter the access password</p>
    <input type="password" id="lock-input" autofocus autocomplete="current-password">
    <button id="lock-btn">Unlock</button>
    <div id="lock-error"></div>
  </div>
</div>

<div id="app">
<header>
  <h1>ZoozyJars · <span class="accent">Subscriptions</span></h1>
  <div class="meta" id="meta"></div>
</header>

<div class="container">
  <!-- KPI strip -->
  <div class="kpi-row" id="kpi-strip"></div>

  <!-- Tabs -->
  <div class="tabs">
    <div class="tab active" data-panel="overview">Overview</div>
    <div class="tab" data-panel="subs">Subscriptions</div>
    <div class="tab" data-panel="forecast">Production</div>
    <div class="tab" data-panel="cohorts">Cohorts</div>
    <div class="tab" data-panel="payback">Payback</div>
  </div>

  <!-- Overview -->
  <div class="panel active" id="panel-overview">
    <div class="grid-3">
      <div class="card">
        <h3>Status breakdown</h3>
        <div style="display:flex; gap:20px; align-items:center;">
          <svg id="status-pie" width="160" height="160" viewBox="0 0 160 160"></svg>
          <div class="legend" id="status-legend"></div>
        </div>
      </div>
      <div class="card">
        <h3>MRR by language (€)</h3>
        <table style="border:0;">
          <tbody id="lang-mrr"></tbody>
        </table>
      </div>
      <div class="card">
        <h3>Billing cycles (active)</h3>
        <table style="border:0;">
          <tbody id="cycle-table"></tbody>
        </table>
      </div>
    </div>

    <div class="card">
      <h3>FX rates (1 → EUR)</h3>
      <div id="fx-rates" class="muted" style="font-size:12px;"></div>
    </div>
  </div>

  <!-- Subscriptions -->
  <div class="panel" id="panel-subs">
    <div class="filters">
      <span class="label">Status</span>
      <span class="filter-pill on" data-status="all">All</span>
      <span class="filter-pill" data-status="active">Active</span>
      <span class="filter-pill" data-status="paused">Paused</span>
      <span class="filter-pill" data-status="canceling">Canceling</span>
      <span class="filter-pill" data-status="canceled">Canceled</span>
      <span class="filter-pill" data-status="at_risk">At risk</span>
      <span class="sep">|</span>
      <span class="label">Lang</span>
      <select id="filter-lang"><option value="">all</option></select>
      <span class="sep">|</span>
      <span class="label">Cycle</span>
      <select id="filter-cycle">
        <option value="">all</option>
        <option value="14">14d</option>
        <option value="28">28d</option>
      </select>
      <span class="sep">|</span>
      <span class="label">Phase</span>
      <select id="filter-phase">
        <option value="">all</option>
        <option value="trial">trial</option>
        <option value="paid">paid</option>
      </select>
      <span class="sep">|</span>
      <input id="search" placeholder="email, name, sub_id..." style="flex:1; min-width:200px;">
      <span class="muted" id="filter-count" style="font-size:12px;"></span>
    </div>
    <table id="subs-table">
      <thead><tr>
        <th data-key="status">Status</th>
        <th data-key="email">Customer</th>
        <th data-key="lang">Lang</th>
        <th class="num" data-key="n_jars">Jars</th>
        <th class="num" data-key="period_days">Cycle</th>
        <th class="num" data-key="mrr_eur">MRR €</th>
        <th data-key="created">Started</th>
        <th data-key="current_period_end">Next bill</th>
      </tr></thead>
      <tbody></tbody>
    </table>
  </div>

  <!-- Forecast -->
  <div class="panel" id="panel-forecast">
    <div class="card">
      <h3>Production forecast — jars needed</h3>
      <div class="window-tabs">
        <span class="window-tab" data-window="7">7 days</span>
        <span class="window-tab" data-window="14">14 days</span>
        <span class="window-tab on" data-window="30">30 days</span>
        <span class="window-tab" data-window="60">60 days</span>
        <span class="window-tab" data-window="90">90 days</span>
      </div>
      <div style="display:flex; gap:32px; align-items:baseline; margin: 14px 0;">
        <div>
          <div class="muted" style="font-size:11px; text-transform:uppercase;">Total jars</div>
          <div class="big-num" id="forecast-jars">—</div>
        </div>
        <div>
          <div class="muted" style="font-size:11px; text-transform:uppercase;">Subscriptions</div>
          <div class="big-num" id="forecast-subs">—</div>
        </div>
        <div class="muted" style="font-size:12px; max-width:380px;">
          Only <b>active</b> subscriptions counted. Each scheduled billing within the window = one delivery with the same line items.
        </div>
      </div>
      <table>
        <thead><tr>
          <th>Product</th>
          <th class="num">Jars</th>
          <th class="num">Subs</th>
          <th class="num">Avg per sub</th>
        </tr></thead>
        <tbody id="forecast-table"></tbody>
      </table>
    </div>
  </div>

  <!-- Cohorts -->
  <div class="panel" id="panel-cohorts">
    <div class="card">
      <h3>Funnel — time-aware retention</h3>
      <div class="muted" style="font-size:12px; margin-bottom: 14px;">
        Conversion for each step = <b>reached / due by time</b>.
        Subscriptions younger than required age aren't evaluated yet — they're shown as <span style="color:#4a6585;">pipeline</span> (will appear in future reports).
        Lost = canceled/canceling before they were due to pay.
      </div>
      <div id="total-funnel" style="display:flex; gap:14px; align-items:stretch; padding: 4px 0 8px;"></div>
    </div>
    <div class="card">
      <h3>Cohorts by signup month</h3>
      <div class="muted" style="font-size:12px; margin-bottom:10px;">
        Each cell: <b>reached / due</b> · <b>%</b> · <span style="color:#4a6585;">+ pipeline</span>. "—" means no one was due to reach this step yet.
      </div>
      <table class="heat" id="cohort-table-wrap">
        <thead><tr>
          <th>Cohort</th>
          <th class="num">Size</th>
          <th class="num" title="Canceled/canceling subs (any time)">Lost</th>
          <th class="num">2nd (1st renewal)</th>
          <th class="num">3rd (2nd renewal)</th>
          <th class="num">4th</th>
          <th class="num">5+</th>
          <th class="num" title="All renewal revenue billed during this calendar month, regardless of which cohort the subscription belongs to.">Revenue € (this month)</th>
          <th class="num" title="Revenue this month / cohort size">€/sub</th>
        </tr></thead>
        <tbody id="cohort-table"></tbody>
      </table>
    </div>
  </div>

  <!-- Payback -->
  <div class="panel" id="panel-payback">
    <div class="card">
      <h3>Payback period</h3>
      <div class="input-row">
        <label>CAC (€)</label>
        <input type="number" id="cac-input" value="25" step="1" min="1">
        <span class="muted" style="font-size:12px;">Average cost to acquire one customer (Meta ads + other). Edit and everything recalculates.</span>
      </div>
      <div class="grid-3" style="margin-top:14px;">
        <div>
          <div class="muted" style="font-size:11px; text-transform:uppercase;">Recovered (total)</div>
          <div class="big-num" id="pb-recovered">—</div>
          <div class="muted" id="pb-recovered-pct" style="font-size:12px;">—</div>
        </div>
        <div>
          <div class="muted" style="font-size:11px; text-transform:uppercase;">Median days to payback</div>
          <div class="big-num" id="pb-median">—</div>
          <div class="muted" style="font-size:12px;">only among those who recovered</div>
        </div>
        <div>
          <div class="muted" style="font-size:11px; text-transform:uppercase;">Avg LTV €</div>
          <div class="big-num" id="pb-ltv">—</div>
          <div class="muted" id="pb-ltv-cac" style="font-size:12px;">—</div>
        </div>
      </div>
      <div style="margin-top:18px;">
        <h3 style="margin-bottom:8px;">% of customers recovered by...</h3>
        <table>
          <thead><tr>
            <th>Period</th>
            <th class="num">Customers</th>
            <th class="num">% of cohort</th>
          </tr></thead>
          <tbody id="pb-buckets"></tbody>
        </table>
      </div>
    </div>
  </div>
</div>
</div><!-- /#app -->

<script>
let DATA = __DATA__;
const ENCRYPTED = __ENCRYPTED__;

// ============ utilities ============
const fmt = {
  eur: n => "€" + (n || 0).toLocaleString("en-US", {maximumFractionDigits: 0}),
  eur2: n => "€" + (n || 0).toLocaleString("en-US", {minimumFractionDigits: 2, maximumFractionDigits: 2}),
  num: n => (n || 0).toLocaleString("en-US"),
  pct: n => (n || 0).toFixed(1) + "%",
  date: ts => ts ? new Date(ts * 1000).toISOString().slice(0, 10) : "—",
  daysAgo: ts => ts ? Math.round((Date.now()/1000 - ts) / 86400) + "d" : "—",
};

// ============ KPI strip ============
function renderKPI() {
  const k = DATA.kpi;
  document.getElementById("meta").innerHTML =
    `Data from <b>${DATA.cutoff_date}</b> · ${DATA.subs.length} subs · ${DATA.ltv.length} customers · generated ${DATA.generated_at}`;
  const cards = [
    {cls: "active", label: "Active", value: fmt.num(k.active), delta: `${k.active_paid} paying · ${k.active_trial} in trial`},
    {cls: "paused", label: "Paused", value: fmt.num(k.paused), delta: `${k.paused_paid} paid · ${k.paused_trial} in trial`},
    {cls: "", label: "Canceling", value: fmt.num(k.canceling), delta: `${k.canceling_paid} paid · ${k.canceling_trial} in trial`},
    {cls: "canceled", label: "Canceled", value: fmt.num(k.canceled), delta: `−${k.churned_30d} in 30d`},
    {cls: "mrr", label: "MRR €", value: fmt.eur(k.mrr_eur), delta: `paid-only €${fmt.num(k.mrr_paid_only)}`},
    {cls: "mrr", label: "ARR €", value: fmt.eur(k.arr_eur), delta: ""},
    {cls: "", label: "Avg sub", value: fmt.eur2(k.avg_mrr), delta: "MRR / active"},
    {cls: "", label: "New 30d", value: fmt.num(k.new_30d), delta: "new subs"},
  ];
  document.getElementById("kpi-strip").innerHTML = cards.map(c =>
    `<div class="kpi ${c.cls}">
       <div class="label">${c.label}</div>
       <div class="value">${c.value}</div>
       <div class="delta">${c.delta || "&nbsp;"}</div>
     </div>`
  ).join("");
}

// ============ Status pie (SVG) ============
function renderStatusPie() {
  const k = DATA.kpi;
  const data = [
    {label: "Active", value: k.active, color: "#5d8a3d"},
    {label: "Paused", value: k.paused, color: "#d4a843"},
    {label: "Canceling", value: k.canceling, color: "#c87830"},
    {label: "Canceled", value: k.canceled, color: "#b85450"},
    {label: "At risk", value: k.at_risk, color: "#9a3820"},
    {label: "Incomplete", value: k.incomplete, color: "#999"},
  ].filter(d => d.value > 0);
  const total = data.reduce((s, d) => s + d.value, 0) || 1;
  const cx = 80, cy = 80, r = 70;
  let angle = -Math.PI / 2;
  const arcs = data.map(d => {
    const slice = (d.value / total) * 2 * Math.PI;
    const x1 = cx + r * Math.cos(angle), y1 = cy + r * Math.sin(angle);
    angle += slice;
    const x2 = cx + r * Math.cos(angle), y2 = cy + r * Math.sin(angle);
    const large = slice > Math.PI ? 1 : 0;
    return `<path d="M ${cx} ${cy} L ${x1} ${y1} A ${r} ${r} 0 ${large} 1 ${x2} ${y2} Z" fill="${d.color}"/>`;
  }).join("");
  document.getElementById("status-pie").innerHTML = arcs;
  document.getElementById("status-legend").innerHTML = data.map(d =>
    `<div class="row"><div class="sw" style="background:${d.color}"></div>
       <span>${d.label}</span>
       <span class="muted">${d.value} · ${(d.value/total*100).toFixed(0)}%</span>
     </div>`
  ).join("");
}

// ============ Lang & cycle tables ============
function renderSecondaryTables() {
  const lang = Object.entries(DATA.mrr_by_lang);
  const total = lang.reduce((s, [_, v]) => s + v, 0) || 1;
  document.getElementById("lang-mrr").innerHTML = lang.map(([k, v]) =>
    `<tr><td>${k}</td><td class="num">${fmt.eur(v)}</td><td class="num muted">${(v/total*100).toFixed(0)}%</td></tr>`
  ).join("");
  const cycles = Object.entries(DATA.cycle_count);
  const cTotal = cycles.reduce((s, [_, v]) => s + v, 0) || 1;
  document.getElementById("cycle-table").innerHTML = cycles.map(([k, v]) =>
    `<tr><td>${k}</td><td class="num">${v}</td><td class="num muted">${(v/cTotal*100).toFixed(0)}%</td></tr>`
  ).join("");
  document.getElementById("fx-rates").textContent =
    Object.entries(DATA.fx_rates).map(([k, v]) => `${k.toUpperCase()} → ${v}`).join("  ·  ");
}

// ============ Subscriptions table ============
let subsState = {status: "all", lang: "", cycle: "", phase: "", search: "", sortKey: "mrr_eur", sortDir: -1};

function populateLangFilter() {
  const langs = [...new Set(DATA.subs.map(s => s.lang).filter(Boolean))].sort();
  const sel = document.getElementById("filter-lang");
  sel.innerHTML = '<option value="">all</option>' + langs.map(l => `<option>${l}</option>`).join("");
}

function filterSubs() {
  const q = subsState.search.toLowerCase();
  return DATA.subs.filter(s => {
    if (subsState.status !== "all" && s.status !== subsState.status) return false;
    if (subsState.lang && s.lang !== subsState.lang) return false;
    if (subsState.cycle && String(s.period_days) !== subsState.cycle) return false;
    if (subsState.phase && s.phase !== subsState.phase) return false;
    if (q) {
      const hay = `${s.email||""} ${s.name||""} ${s.id} ${s.customer_id}`.toLowerCase();
      if (!hay.includes(q)) return false;
    }
    return true;
  });
}

function renderSubs() {
  const rows = filterSubs();
  rows.sort((a, b) => {
    const av = a[subsState.sortKey], bv = b[subsState.sortKey];
    if (av == null) return 1; if (bv == null) return -1;
    return (av < bv ? -1 : av > bv ? 1 : 0) * subsState.sortDir;
  });
  document.getElementById("filter-count").textContent = `${rows.length} / ${DATA.subs.length}`;
  document.querySelectorAll("#subs-table th").forEach(th => {
    th.classList.remove("sorted", "asc");
    if (th.dataset.key === subsState.sortKey) {
      th.classList.add("sorted");
      if (subsState.sortDir > 0) th.classList.add("asc");
    }
  });
  const tbody = document.querySelector("#subs-table tbody");
  tbody.innerHTML = rows.map(s => {
    const cycle = s.period_days ? `${s.period_days}d` : "—";
    const itemsHtml = s.items.map(it =>
      `<span class="item"><span class="qty">${it.qty}×</span> ${it.product_name}</span>`
    ).join("");
    const phasePill = s.phase === "trial" ? `<span class="pill trial" title="in 9-day trial, no renewal paid yet">trial</span>` : "";
    return `
      <tr class="row" data-id="${s.id}">
        <td><span class="pill ${s.status}">${s.status}</span> ${phasePill}</td>
        <td>
          <div>${s.name || "<span class='muted'>—</span>"}</div>
          <div class="muted" style="font-size:11px;">${s.email ? `<a class="email" href="mailto:${s.email}">${s.email}</a>` : ""}</div>
        </td>
        <td>${s.lang || "<span class='muted'>—</span>"}</td>
        <td class="num">${s.n_jars}</td>
        <td class="num">${cycle}</td>
        <td class="num">${fmt.eur2(s.mrr_eur)}</td>
        <td>${fmt.date(s.created)} <span class="muted" style="font-size:11px;">(${fmt.daysAgo(s.created)})</span></td>
        <td>${fmt.date(s.current_period_end)}</td>
      </tr>
      <tr class="detail-row" style="display:none;"><td colspan="8" class="detail">
        <div><b>${s.id}</b> · cust ${s.customer_id} · raw_status: <code>${s.raw_status}</code>
          ${s.cancel_at_period_end ? "· cancel_at_period_end" : ""}
          ${s.pause_collection ? `· paused (${s.pause_collection.behavior || ""})` : ""}
        </div>
        <div class="items" style="margin-top:8px;">${itemsHtml}</div>
      </td></tr>`;
  }).join("");

  // expand/collapse on row click
  document.querySelectorAll("#subs-table tr.row").forEach(r => {
    r.onclick = () => {
      const next = r.nextElementSibling;
      if (next && next.classList.contains("detail-row")) {
        next.style.display = next.style.display === "none" ? "table-row" : "none";
      }
    };
  });
}

function bindSubsFilters() {
  document.querySelectorAll(".filter-pill[data-status]").forEach(p => {
    p.onclick = () => {
      document.querySelectorAll(".filter-pill[data-status]").forEach(x => x.classList.remove("on"));
      p.classList.add("on");
      subsState.status = p.dataset.status;
      renderSubs();
    };
  });
  document.getElementById("filter-lang").onchange = e => { subsState.lang = e.target.value; renderSubs(); };
  document.getElementById("filter-cycle").onchange = e => { subsState.cycle = e.target.value; renderSubs(); };
  document.getElementById("filter-phase").onchange = e => { subsState.phase = e.target.value; renderSubs(); };
  document.getElementById("search").oninput = e => { subsState.search = e.target.value; renderSubs(); };
  document.querySelectorAll("#subs-table th").forEach(th => {
    th.onclick = () => {
      const k = th.dataset.key;
      if (subsState.sortKey === k) subsState.sortDir *= -1;
      else { subsState.sortKey = k; subsState.sortDir = -1; }
      renderSubs();
    };
  });
}

// ============ Forecast ============
let forecastWindow = 30;
function renderForecast() {
  const f = DATA.forecast[forecastWindow];
  document.getElementById("forecast-jars").textContent = fmt.num(f.total_jars);
  document.getElementById("forecast-subs").textContent = fmt.num(f.total_subs);
  document.getElementById("forecast-table").innerHTML = f.products.map(p =>
    `<tr>
       <td>${p.product}</td>
       <td class="num">${fmt.num(p.jars)}</td>
       <td class="num">${fmt.num(p.subs)}</td>
       <td class="num muted">${(p.jars / Math.max(p.subs, 1)).toFixed(1)}</td>
     </tr>`
  ).join("");
}
function bindForecast() {
  document.querySelectorAll(".window-tab").forEach(t => {
    t.onclick = () => {
      document.querySelectorAll(".window-tab").forEach(x => x.classList.remove("on"));
      t.classList.add("on");
      forecastWindow = +t.dataset.window;
      renderForecast();
    };
  });
}

// ============ Cohorts ============
function heatColor(pct) {
  // 0% = pale, 100% = green
  const t = Math.min(pct / 100, 1);
  const r = Math.round(247 + (93 - 247) * t);
  const g = Math.round(244 + (111 - 244) * t);
  const b = Math.round(235 + (61 - 235) * t);
  const fg = t > 0.4 ? "white" : "#1a1a1a";
  return `background:rgb(${r},${g},${b}); color:${fg};`;
}
const STEP_LABELS = {
  1: "1st (test box)",
  2: "2nd (1st renewal)",
  3: "3rd (2nd renewal)",
  4: "4th (3rd renewal)",
  5: "5+ (4+ renewals)",
};

function renderCohorts() {
  const steps = DATA.total_funnel_steps;
  document.getElementById("total-funnel").innerHTML = steps.map(s => {
    const conv = s.conversion;
    const convDisplay = conv == null ? "—" : conv.toFixed(0) + "%";
    const convColor = conv == null ? "#8b8775" : (conv >= 50 ? "#5d8a3d" : conv >= 25 ? "#b58a30" : "#a04540");
    return `
      <div class="card" style="flex:1; padding:14px; margin:0; min-width:0;">
        <div class="muted" style="font-size:10px; text-transform:uppercase; letter-spacing:0.5px;">${STEP_LABELS[s.step]}</div>
        <div style="display:flex; align-items:baseline; gap:8px; margin-top:6px;">
          <div style="font-size:26px; font-weight:600; color:${convColor}; font-variant-numeric:tabular-nums;">${convDisplay}</div>
          <div class="muted" style="font-size:12px;">${s.reached} / ${s.due}</div>
        </div>
        <div style="margin-top:10px; display:flex; flex-direction:column; gap:3px; font-size:11px;">
          <div>✓ Reached: <b>${s.reached}</b></div>
          <div style="color:#a04540;">✗ Lost: <b>${s.lost}</b></div>
          <div style="color:#4a6585;">⏳ Pipeline: <b>${s.pending}</b></div>
        </div>
      </div>`;
  }).join("");

  document.getElementById("cohort-table").innerHTML = DATA.cohorts.map(c => {
    const step2 = c.steps.find(x => x.step === 2);
    const lostCount = step2 ? step2.lost : 0;
    const cells = [2, 3, 4, 5].map(S => {
      const st = c.steps.find(x => x.step === S);
      if (!st || st.due === 0) {
        return `<td class="num"><span class="muted">—</span>${st && st.pending ? ` <span style="color:#4a6585; font-size:11px;">+${st.pending}</span>` : ""}</td>`;
      }
      const pct = st.conversion;
      const pendingTxt = st.pending ? ` <span style="color:#4a6585; font-size:11px;" title="still in pipeline">+${st.pending}</span>` : "";
      return `<td class="num">
        <span class="hb" style="${heatColor(pct)}">${st.reached}/${st.due} · ${pct}%</span>${pendingTxt}
      </td>`;
    }).join("");
    return `<tr>
       <td><b>${c.cohort}</b></td>
       <td class="num">${c.size}</td>
       <td class="num" style="color:#a04540;">${lostCount || "—"}</td>
       ${cells}
       <td class="num">${fmt.eur(c.revenue_eur)}</td>
       <td class="num">${fmt.eur2(c.rev_per_sub)}</td>
     </tr>`;
  }).join("");
}

// ============ Payback ============
function renderPayback() {
  const cac = +document.getElementById("cac-input").value || 0;
  const data = DATA.ltv;
  let recovered = 0, totalLtv = 0;
  const daysToPayback = [];
  const buckets = {30: 0, 60: 0, 90: 0, 180: 0, 365: 0};
  const bucketTotal = data.length || 1;

  for (const c of data) {
    totalLtv += c.total_eur;
    let dRecovered = null;
    for (const t of c.timeline) {
      if (t.cum >= cac) { dRecovered = t.days; break; }
    }
    if (dRecovered != null) {
      recovered++;
      daysToPayback.push(dRecovered);
      for (const k of Object.keys(buckets)) {
        if (dRecovered <= +k) buckets[k]++;
      }
    }
  }
  daysToPayback.sort((a, b) => a - b);
  const median = daysToPayback.length ? daysToPayback[Math.floor(daysToPayback.length / 2)] : null;
  const avgLtv = data.length ? totalLtv / data.length : 0;

  document.getElementById("pb-recovered").textContent = fmt.num(recovered);
  document.getElementById("pb-recovered-pct").textContent =
    `${(recovered / bucketTotal * 100).toFixed(1)}% з ${bucketTotal} клієнтів`;
  document.getElementById("pb-median").textContent = median != null ? Math.round(median) + "d" : "—";
  document.getElementById("pb-ltv").textContent = fmt.eur2(avgLtv);
  document.getElementById("pb-ltv-cac").textContent = `LTV/CAC = ${(avgLtv / Math.max(cac, 1)).toFixed(2)}`;

  document.getElementById("pb-buckets").innerHTML = Object.entries(buckets).map(([k, v]) =>
    `<tr><td>≤ ${k} днів</td><td class="num">${v}</td><td class="num">${(v/bucketTotal*100).toFixed(1)}%</td></tr>`
  ).join("");
}
function bindPayback() {
  document.getElementById("cac-input").oninput = renderPayback;
}

// ============ Tabs ============
function bindTabs() {
  document.querySelectorAll(".tab").forEach(t => {
    t.onclick = () => {
      document.querySelectorAll(".tab").forEach(x => x.classList.remove("active"));
      document.querySelectorAll(".panel").forEach(x => x.classList.remove("active"));
      t.classList.add("active");
      document.getElementById("panel-" + t.dataset.panel).classList.add("active");
    };
  });
}

// ============ Init ============
function initApp() {
  document.getElementById("app").classList.add("show");
  document.getElementById("lock-screen").classList.remove("show");
  renderKPI();
  renderStatusPie();
  renderSecondaryTables();
  populateLangFilter();
  renderSubs();
  renderForecast();
  renderCohorts();
  renderPayback();
  bindTabs();
  bindSubsFilters();
  bindForecast();
  bindPayback();
}

// ============ Password gate (AES-GCM via Web Crypto) ============
function b64ToBytes(b64) {
  return Uint8Array.from(atob(b64), c => c.charCodeAt(0));
}

async function decryptPayload(password, payload) {
  const enc = new TextEncoder();
  const baseKey = await crypto.subtle.importKey(
    "raw", enc.encode(password), {name: "PBKDF2"}, false, ["deriveKey"]
  );
  const key = await crypto.subtle.deriveKey(
    {name: "PBKDF2", salt: b64ToBytes(payload.salt), iterations: payload.iter, hash: "SHA-256"},
    baseKey,
    {name: "AES-GCM", length: 256},
    false, ["decrypt"]
  );
  const plaintextBuf = await crypto.subtle.decrypt(
    {name: "AES-GCM", iv: b64ToBytes(payload.iv)},
    key,
    b64ToBytes(payload.ct)
  );
  return JSON.parse(new TextDecoder().decode(plaintextBuf));
}

async function tryUnlock() {
  const input = document.getElementById("lock-input");
  const errEl = document.getElementById("lock-error");
  const btn = document.getElementById("lock-btn");
  errEl.textContent = "";
  btn.disabled = true;
  btn.textContent = "Unlocking...";
  try {
    DATA = await decryptPayload(input.value, ENCRYPTED);
    sessionStorage.setItem("zj_pwd", input.value);  // remember within tab session
    initApp();
  } catch (e) {
    errEl.textContent = "Wrong password";
    btn.disabled = false;
    btn.textContent = "Unlock";
    input.select();
  }
}

if (ENCRYPTED) {
  document.getElementById("lock-screen").classList.add("show");
  document.getElementById("lock-btn").onclick = tryUnlock;
  document.getElementById("lock-input").onkeydown = e => { if (e.key === "Enter") tryUnlock(); };
  // Auto-unlock if password was cached this session
  const cached = sessionStorage.getItem("zj_pwd");
  if (cached) {
    document.getElementById("lock-input").value = cached;
    tryUnlock();
  }
} else {
  initApp();
}
</script>
</body>
</html>
"""

ts = dt.datetime.utcnow().strftime("%Y-%m-%d_%H%M")
print("→ render HTML...", flush=True)
data_json = json.dumps(data, default=str)

# Optional AES-GCM encryption gate. If DASHBOARD_PASSWORD is set,
# the data is encrypted with PBKDF2(SHA-256, 250k iter) + AES-GCM,
# and decrypted in-browser via Web Crypto API on password entry.
PBKDF2_ITERATIONS = 250_000
password = os.environ.get("DASHBOARD_PASSWORD")
if password:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    from cryptography.hazmat.primitives import hashes
    salt = os.urandom(16)
    iv = os.urandom(12)
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=PBKDF2_ITERATIONS,
    )
    key = kdf.derive(password.encode())
    ciphertext = AESGCM(key).encrypt(iv, data_json.encode(), None)
    encrypted_payload = {
        "salt": base64.b64encode(salt).decode(),
        "iv": base64.b64encode(iv).decode(),
        "ct": base64.b64encode(ciphertext).decode(),
        "iter": PBKDF2_ITERATIONS,
    }
    html_out = (HTML_TEMPLATE
        .replace("__DATA__", "null")
        .replace("__ENCRYPTED__", json.dumps(encrypted_payload)))
    print("   (encrypted with DASHBOARD_PASSWORD)")
else:
    html_out = (HTML_TEMPLATE
        .replace("__DATA__", data_json)
        .replace("__ENCRYPTED__", "null"))

# Always write index.html (entry point for GitHub Pages / web hosting)
index_file = OUT_DIR / "index.html"
index_file.write_text(html_out)
print(f"\n✓ Saved: {index_file}")

# Locally also keep a timestamped archive copy
if not os.environ.get("CI"):
    archive = OUT_DIR / f"subs_dashboard_{ts}.html"
    archive.write_text(html_out)
    legacy = OUT_DIR / "subs_dashboard.html"
    legacy.write_text(html_out)  # backwards compat
    print(f"✓ Archive: {archive}")
    print(f"\nOpen: open {index_file}")
