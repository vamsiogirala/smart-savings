from collections import defaultdict, OrderedDict
from datetime import datetime
import io
import time

import pandas as pd
import pdfplumber
from fastapi import FastAPI, Request, UploadFile, File, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from database import init_db, SessionLocal, Transaction
from parsers import detect_bank, parse_boa_pdf

app = FastAPI(title="Smart Savings")
init_db()

# static + templates
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

APP_START_TS = time.time()


# ---------- helpers ----------

def auto_categorize(description: str, amount: float | None = None) -> str:
    """
    Very simple rule-based categorizer based on keywords.
    Good enough to demo intelligence to your professor.
    """
    desc = (description or "").lower()

    rules = [
        (["uber", "lyft", "taxi", "ride"], "Transport"),
        (["shell", "chevron", "petro", "fuel", "gas station"], "Fuel"),
        (["starbucks", "coffee", "cafe", "restaurant", "dining"], "Dining"),
        (["walmart", "target", "grocery", "market", "supermarket"], "Groceries"),
        (["netflix", "spotify", "subscription", "prime"], "Subscriptions"),
        (["rent", "mortgage", "landlord"], "Housing"),
        (["electric", "water bill", "gas bill", "utility"], "Utilities"),
        (["salary", "payroll", "paycheck", "deposit"], "Income"),
        (["transfer", "savings", "stash", "vault"], "Savings"),
    ]

    for keywords, cat in rules:
        if any(k in desc for k in keywords):
            return cat

    if amount is not None and amount < 0:
        return "Refund"
    return "Uncategorized"


def load_transactions():
    """Return all transactions as simple dicts for charts + table."""
    with SessionLocal() as db:
        rows = db.query(Transaction).all()
        return [
            {
                "date": str(r.date),
                "category": r.category or "",
                "description": r.description or "",
                "amount": float(r.amount or 0.0),
            }
            for r in rows
        ]


def month_key(iso_date_str):
    # "2025-11-06" -> "2025-11"
    return str(iso_date_str)[:7] if iso_date_str else ""


def monthly_aggregate(txs, months: int = 12):
    """Return OrderedDict of last <months> months with totals (0 when missing)."""
    now = datetime.now().replace(day=1)
    labels: list[str] = []

    # build month labels oldest -> newest
    for i in range(months - 1, -1, -1):
        year = now.year
        month = now.month - i
        while month <= 0:
            month += 12
            year -= 1
        labels.append(f"{year}-{month:02d}")

    bucket = {m: 0.0 for m in labels}
    for t in txs:
        mk = month_key(t["date"])
        if mk in bucket:
            bucket[mk] += float(t["amount"])

    series = OrderedDict((m, round(bucket[m], 2)) for m in labels)
    return series


def compute_kpis(txs):
    if not txs:
        return {"balance": 12000.0, "mtd": 0.0, "forecast": 0.0}

    today = datetime.now()
    this_month = f"{today.year}-{today.month:02d}"
    mtd = sum(t["amount"] for t in txs if month_key(t["date"]) == this_month)

    series_vals = list(monthly_aggregate(txs, months=12).values())
    avg = sum(series_vals) / max(1, len(series_vals))
    if len(series_vals) > 1:
        trend = (series_vals[-1] - series_vals[0]) / max(1, len(series_vals) - 1)
    else:
        trend = 0.0
    forecast = avg + 0.5 * trend

    return {
        "balance": 12000.0,
        "mtd": round(mtd, 2),
        "forecast": round(forecast, 2),
    }


def category_breakdown(txs):
    if not txs:
        return {"Housing": 0, "Utilities": 0, "Dining": 0, "Savings": 0}
    by_cat: dict[str, float] = defaultdict(float)
    for t in txs:
        by_cat[t["category"]] += float(t["amount"])
    return dict(sorted(by_cat.items(), key=lambda x: x[1], reverse=True))


def suggestion_tips(by_cat: dict):
    if not by_cat:
        return ["Upload transactions to get personalized tips."]
    total = sum(by_cat.values()) or 1.0
    ranked = sorted(by_cat.items(), key=lambda x: x[1], reverse=True)
    tips: list[str] = []
    if ranked:
        top, top_amt = ranked[0]
        share = top_amt / total
        tips.append(f"Reduce {top} by 10% (it’s about {round(share * 100)}% of your spend).")
    tips.append("Set an automatic $200 transfer to savings after each paycheck.")
    tips.append("Review subscriptions and cancel ones unused in the last 90 days.")
    return tips[:3]


# ---------- pages ----------

@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    txs = load_transactions()
    kpi = compute_kpis(txs)
    cats = category_breakdown(txs)
    series = monthly_aggregate(txs, months=12)
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "kpi": kpi,
            "categories": cats,
            "trend_labels": list(series.keys()),
            "trend_values": list(series.values()),
            "transactions": txs,
        },
    )


@app.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request):
    txs = load_transactions()
    cats = category_breakdown(txs)
    uptime_s = int(time.time() - APP_START_TS)
    stats = {
        "total_transactions": len(txs),
        "distinct_categories": len(cats),
        "uptime_seconds": uptime_s,
    }
    return templates.TemplateResponse(
        "admin.html",
        {"request": request, "stats": stats, "categories": cats},
    )


@app.get("/help", response_class=HTMLResponse)
def help_page(request: Request):
    return templates.TemplateResponse("help.html", {"request": request})


@app.get("/about", response_class=HTMLResponse)
def about_page(request: Request):
    return templates.TemplateResponse("about.html", {"request": request})


# ---------- CSV upload API ----------

@app.post("/api/upload")
async def api_upload(file: UploadFile = File(...)):
    if not file.filename.lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="Please upload a .csv file")

    content = await file.read()
    try:
        df = pd.read_csv(io.BytesIO(content))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"CSV read error: {e}")

    cols = {c.lower(): c for c in df.columns}

    # Accept different header names
    date_candidates = ["date", "posting date", "transaction date"]
    amount_candidates = ["amount", "debit", "credit"]
    desc_candidates = ["description", "details", "memo"]
    cat_candidates = ["category", "type"]

    def pick(cands):
        for cand in cands:
            if cand in cols:
                return cols[cand]
        return None

    date_col = pick(date_candidates)
    amount_col = pick(amount_candidates)
    desc_col = pick(desc_candidates)
    cat_col = pick(cat_candidates)

    if not date_col or not amount_col:
        raise HTTPException(
            status_code=400,
            detail="CSV must have at least a date column and an amount column.",
        )

    df["date_norm"] = pd.to_datetime(df[date_col], errors="coerce").dt.strftime("%Y-%m-%d")
    df["amount_norm"] = pd.to_numeric(df[amount_col], errors="coerce")
    df["desc_norm"] = df[desc_col].astype(str) if desc_col else ""
    df["cat_norm"] = df[cat_col].astype(str) if cat_col else ""

    before = len(df)
    df = df.dropna(subset=["date_norm", "amount_norm"])

    inserted_rows: list[dict] = []
    with SessionLocal() as db:
        for _, row in df.iterrows():
            date = str(row["date_norm"])
            desc = str(row["desc_norm"]) if desc_col else ""
            amt = float(row["amount_norm"])
            cat = str(row["cat_norm"]) if cat_col else ""

            if not cat or cat.strip().lower() in ("", "uncategorized", "other"):
                cat = auto_categorize(desc, amt)

            tx = Transaction(
                date=date,
                category=cat,
                description=desc,
                amount=amt,
            )
            db.add(tx)
            inserted_rows.append(
                {"date": date, "description": desc, "category": cat, "amount": amt}
            )
        db.commit()

    sample = inserted_rows[:5]
    return {
        "status": "ok",
        "inserted": len(inserted_rows),
        "skipped": before - len(inserted_rows),
        "sample": sample,
        "message": "CSV uploaded and parsed successfully.",
    }


# ---------- PDF upload API ----------

@app.post("/api/upload-pdf")
async def upload_pdf(file: UploadFile = File(...)):
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Please upload a PDF file")

    content = await file.read()

    # 1) Try to detect bank from first page text
    try:
        with pdfplumber.open(io.BytesIO(content)) as pdf:
            first_text = pdf.pages[0].extract_text() or ""
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"PDF read error: {e}")

    bank = detect_bank(first_text)
    extracted: list[dict] = []

    # 2) Bank of America → use special parser
    if bank == "boa":
        try:
            df = parse_boa_pdf(content)
        except Exception as e:
            raise HTTPException(
                status_code=400,
                detail=f"Bank of America parser failed: {e}",
            )

        if df.empty:
            raise HTTPException(
                status_code=400,
                detail="Bank of America PDF parsed, but no transactions were found.",
            )

        for _, row in df.iterrows():
            extracted.append(
                {
                    "date": str(row["date"]),
                    "description": str(row["description"]),
                    "category": str(row.get("category", "")),
                    "amount": float(row["amount"]),
                }
            )
    else:
        # 3) Fallback: generic table reader (for other banks)
        try:
            with pdfplumber.open(io.BytesIO(content)) as pdf:
                for page in pdf.pages:
                    tables = page.extract_tables()
                    if not tables:
                        continue
                    for table in tables:
                        if not table or len(table) < 2:
                            continue
                        for row in table[1:]:
                            if not row or len(row) < 3:
                                continue

                            raw_date = row[0]
                            raw_desc = row[1]
                            raw_amount = row[2]

                            date = pd.to_datetime(raw_date, errors="coerce")
                            if pd.isna(date):
                                continue
                            date_str = date.strftime("%Y-%m-%d")

                            try:
                                amount = float(
                                    str(raw_amount).replace(",", "").replace("$", "")
                                )
                            except Exception:
                                continue

                            desc = raw_desc or ""
                            cat = auto_categorize(desc, amount)

                            extracted.append(
                                {
                                    "date": date_str,
                                    "description": desc,
                                    "category": cat,
                                    "amount": amount,
                                }
                            )
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Generic PDF parse error: {e}")

    if not extracted:
        raise HTTPException(
            status_code=400,
            detail=(
                "No valid transaction rows found in PDF. "
                "Parser expects lines that start with a date and end with an amount."
            ),
        )

    # 4) Insert into DB
    with SessionLocal() as db:
        for row in extracted:
            tx = Transaction(
                date=row["date"],
                description=row["description"],
                category=row["category"],
                amount=row["amount"],
            )
            db.add(tx)
        db.commit()

    sample = extracted[:5]
    return {
        "status": "ok",
        "bank_detected": bank,
        "inserted": len(extracted),
        "sample": sample,
        "message": "PDF extracted successfully.",
    }


# ---------- PDF debug API ----------

@app.post("/api/debug-pdf")
async def debug_pdf(file: UploadFile = File(...)):
    """
    Debug endpoint: returns the first ~40 text lines from the PDF
    so we can see the real layout and design regex accordingly.
    """
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Please upload a PDF file")

    try:
        content = await file.read()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to read uploaded file: {e!r}")

    try:
        all_lines: list[str] = []
        with pdfplumber.open(io.BytesIO(content)) as pdf:
            for page_index, page in enumerate(pdf.pages[:2]):  # first 2 pages only
                text = page.extract_text() or ""
                for line in text.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    all_lines.append(f"[p{page_index + 1}] {line}")
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"PDF parse error in debug endpoint: {e!r}",
        )

    sample_lines = all_lines[:40]
    bank_guess = detect_bank("\n".join(sample_lines)) if sample_lines else "unknown"

    return {
        "bank_guess": bank_guess,
        "line_count": len(all_lines),
        "sample_lines": sample_lines,
    }


# ---------- prediction & health ----------

@app.get("/api/predict")
def api_predict():
    txs = load_transactions()
    kpi = compute_kpis(txs)
    cats = category_breakdown(txs)
    tips = suggestion_tips(cats)
    months = monthly_aggregate(txs, months=12)
    data_points = sum(1 for v in months.values() if v > 0)
    confidence = min(0.5 + 0.05 * data_points, 0.95)
    return {
        "forecast": kpi["forecast"],
        "confidence": round(confidence, 2),
        "tips": tips,
    }


@app.get("/api/health")
def health():
    return {"status": "ok", "uptime_seconds": int(time.time() - APP_START_TS)}


# ---------- smoke test ----------

@app.get("/add-transaction")
def add_transaction():
    with SessionLocal() as db:
        new_tx = Transaction(
            date=datetime.now().strftime("%Y-%m-%d"),
            category="Dining",
            description="Coffee Shop",
            amount=9.5,
        )
        db.add(new_tx)
        db.commit()
    return {"message": "Added test transaction!"}
