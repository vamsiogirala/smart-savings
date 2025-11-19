import io
import re
from datetime import datetime

import pdfplumber
import pandas as pd

# Matches MM/DD or MM/DD/YYYY
DATE_RE = re.compile(r"^(0[1-9]|1[0-2])/(0[1-9]|[12]\d|3[01])(?:/\d{2,4})?$")
AMOUNT_RE = re.compile(r"^-?\$?\d+(?:,\d{3})*(?:\.\d{2})?$")


def detect_bank(text: str) -> str:
    """Very simple bank detector based on PDF text."""
    t = (text or "").lower()
    if "bank of america" in t:
        return "boa"
    # later we can add: chase, wells fargo, etc.
    return "unknown"


def _clean_amount(value: str) -> float | None:
    if value is None:
        return None
    s = str(value).strip().replace("$", "").replace(",", "")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def parse_boa_pdf(file_bytes: bytes) -> pd.DataFrame:
    """
    Bank of America parser with 2 passes:
      1) Try tables
      2) If no rows, fall back to text-line regex
    Returns: date, description, amount, category
    """
    rows: list[dict] = []
    buf = io.BytesIO(file_bytes)

    # ---------- PASS 1: TABLES ----------
    with pdfplumber.open(buf) as pdf:
        for page in pdf.pages:
            tables = page.extract_tables()
            if not tables:
                continue

            for table in tables:
                if not table or len(table) < 2:
                    continue

                # Assume first row is header, skip it
                for raw_row in table[1:]:
                    if not raw_row or len(raw_row) < 3:
                        continue

                    cells = [str(c or "").strip() for c in raw_row]
                    first = cells[0]

                    if not DATE_RE.match(first):
                        continue

                    # Find amount column (from right)
                    amt_idx = None
                    for i in range(len(cells) - 1, -1, -1):
                        if AMOUNT_RE.match(
                            cells[i].replace(",", "").replace("$", "")
                        ):
                            amt_idx = i
                            break

                    if amt_idx is None:
                        continue

                    amount = _clean_amount(cells[amt_idx])
                    if amount is None:
                        continue

                    desc_parts = cells[1:amt_idx]
                    desc = " ".join(p for p in desc_parts if p).strip()
                    if not desc:
                        desc = "Bank of America transaction"

                    # Normalize date (assume current year if missing)
                    year = datetime.now().year
                    date_str = first
                    if len(first.split("/")) == 2:  # MM/DD
                        date_str = f"{first}/{year}"

                    try:
                        dt = datetime.strptime(date_str, "%m/%d/%Y").date()
                    except ValueError:
                        continue

                    rows.append(
                        {
                            "date": dt.strftime("%Y-%m-%d"),
                            "description": desc,
                            "amount": amount,
                            "category": "Uncategorized",
                        }
                    )

    # ---------- PASS 2: TEXT LINES (fallback) ----------
    if not rows:
        buf2 = io.BytesIO(file_bytes)
        with pdfplumber.open(buf2) as pdf:
            for page in pdf.pages:
                text = page.extract_text() or ""
                for line in text.splitlines():
                    line = line.strip()
                    if not line:
                        continue

                    # Rough pattern: DATE .... AMOUNT
                    parts = line.split()
                    if not parts:
                        continue

                    if not DATE_RE.match(parts[0]):
                        continue

                    # Try last token as amount
                    last = parts[-1]
                    if not AMOUNT_RE.match(last.replace(",", "").replace("$", "")):
                        continue

                    amount = _clean_amount(last)
                    if amount is None:
                        continue

                    date_token = parts[0]
                    middle_desc = " ".join(parts[1:-1]).strip()
                    if not middle_desc:
                        middle_desc = "Bank of America transaction"

                    year = datetime.now().year
                    date_str = date_token
                    if len(date_token.split("/")) == 2:  # MM/DD
                        date_str = f"{date_token}/{year}"

                    try:
                        dt = datetime.strptime(date_str, "%m/%d/%Y").date()
                    except ValueError:
                        continue

                    rows.append(
                        {
                            "date": dt.strftime("%Y-%m-%d"),
                            "description": middle_desc,
                            "amount": amount,
                            "category": "Uncategorized",
                        }
                    )

    if not rows:
        return pd.DataFrame(columns=["date", "description", "amount", "category"])

    df = pd.DataFrame(rows)

    # Simple keyword-based categorization
    def guess_category(desc: str) -> str:
        d = desc.lower()
        if any(k in d for k in ["uber", "lyft", "taxi", "ride"]):
            return "Transport"
        if any(k in d for k in ["shell", "chevron", "gas", "fuel"]):
            return "Fuel"
        if any(k in d for k in ["starbucks", "coffee", "cafe", "restaurant", "dining"]):
            return "Dining"
        if any(k in d for k in ["walmart", "target", "grocery", "market"]):
            return "Groceries"
        if any(k in d for k in ["netflix", "spotify", "prime", "subscription"]):
            return "Subscriptions"
        if any(k in d for k in ["rent", "mortgage"]):
            return "Housing"
        if any(k in d for k in ["salary", "payroll", "deposit"]):
            return "Income"
        if any(k in d for k in ["interest charge", "fee", "penalty"]):
            return "Fees"
        return "Other"

    df["category"] = df["description"].astype(str).apply(guess_category)
    return df[["date", "description", "amount", "category"]]

