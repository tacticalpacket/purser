"""Dashboard analytics: read the ledger, emit one JSON-ready document.

Read-only. Nothing here writes to the database, and `open_read_only` opens the
file with DuckDB's own read-only flag so that a reporting run cannot alter the
ledger it is reporting on -- not even by applying a schema migration.

The document's shape is an external contract shared with the renderer. Keys are
not this module's to change.

Three metrics, kept apart on purpose
------------------------------------
`account_movement` is the per-account sum of amounts. It includes transfers,
and it is never called spending.

`spending` is consolidated across the tracked accounts: card purchases plus
checking outflows that are not a card payment, not an internal transfer and not
ambiguous. **A credit-card payment appears twice in the ledger** -- negative in
checking, positive on the card -- and neither leg is spending: the purchase it
settles was already counted, once, on the card. Counting either leg
double-counts, so both are classified `card_payment` and excluded.

`income` is money entering from OUTSIDE the tracked accounts. A drawdown from
the owner's own certificate or savings is not income; neither is an ACH credit
that only says the owner's own name. Treating those as income tells the owner
he is solvent when he is not, so the classifier requires a positive signal
(the institution's own income category, or a payroll-shaped descriptor) before
it will call an inflow income. Everything else that arrives lands in
`ambiguous`, which is reported with its own count and total and is folded into
neither spending nor income.

What the classifier has to work with
------------------------------------
The `categories`, `merchants` and `transfers` tables are empty on this ledger
and purser's own categoriser has not run over it. So classification uses the
institution's own `source_category` and `source_type` columns, which are
populated on essentially every row, and every figure derived from them carries
the `institution` provenance label. Merchant grouping is a cheap normalisation
of `description`; it is labelled `inferred` and carries up to three raw
descriptions so that a wrong grouping is visible rather than hidden.
"""

from __future__ import annotations

import re
import statistics
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import duckdb

# --------------------------------------------------------------------------
# Flow classification vocabulary.
#
# Matched case-insensitively against the institution's own strings. These are
# generic institution vocabulary, not descriptors lifted from any real export.
# --------------------------------------------------------------------------

CARD_PAYMENT = "card_payment"
INTERNAL = "internal"
INCOME = "income"
SPENDING = "spending"
AMBIGUOUS = "ambiguous"

FLOWS = (CARD_PAYMENT, INTERNAL, INCOME, SPENDING, AMBIGUOUS)

#: Checking-side category the institution puts on a payment to its own card.
CARD_PAYMENT_CATEGORIES = {"credit card payments", "credit card payment"}
#: Card-side type on the settling leg.
CARD_PAYMENT_TYPES = {"payment"}
CARD_PAYMENT_PATTERNS = (
    "transfer to card",
    "credit card payment",
    "payment to card",
    "card payment",
    "payment thank you",
)

#: Movement between the owner's own accounts. Not spending, and not income.
INTERNAL_CATEGORIES = {
    "transfer", "transfers", "savings", "certificate", "certificates",
    "investments", "loans", "loan payment", "credit card transfers",
}
INTERNAL_PATTERNS = (
    "certificate", "share savings", "savings transfer", "transfer to savings",
    "transfer from savings", "money market", "loan payment", "transfer to loan",
    "transfer from loan", "to share ", "from share ", "brokerage", "ira transfer",
)

#: An inflow is income only with one of these positive signals.
INCOME_CATEGORIES = {"income", "paycheck", "payroll", "salary", "wages"}
INCOME_PATTERNS = ("payroll", "direct deposit", "dir dep", "salary", "wages", "pension")

#: Types that carry no counterparty of their own. A negative row with one of
#: these AND no meaningful institution category cannot be placed.
UNPLACEABLE_TYPES = {"ach debit", "ach credit", "ach", "transfer", "eft", "wire"}
UNPLACEABLE_CATEGORIES = {
    "", "transfer", "transfers", "other", "uncategorized", "uncategorised",
    "miscellaneous", "misc",
}

INTEREST_PATTERNS = ("interest", "finance charge")

#: Card facts the imports simply cannot establish. Stated, never guessed.
CARD_UNAVAILABLE = [
    "APR", "credit limit", "utilisation", "minimum payment", "due date",
    "statement close date",
]


# --------------------------------------------------------------------------
# Connection
# --------------------------------------------------------------------------

def open_read_only(db_path: str | Path) -> duckdb.DuckDBPyConnection:
    """Open the ledger read-only, with the session timezone pinned to UTC.

    Read-only is the point: a dashboard run must not be able to write, and
    DuckDB enforces that at the connection rather than by convention. The UTC
    pin matches `purser.db.database.connect` so that reading a day back out of
    a TIMESTAMPTZ cannot shift by one on a machine west of UTC.
    """
    con = duckdb.connect(str(db_path), read_only=True)
    con.execute("SET TimeZone='UTC'")
    return con


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def money(value) -> float:
    """A money figure as the contract wants it: a JSON number with 2 decimals."""
    if value is None:
        return 0.0
    return round(float(value), 2)


def _month(day: date) -> str:
    return f"{day.year:04d}-{day.month:02d}"


def _iso(day) -> str | None:
    return None if day is None else day.isoformat()


def _month_range(start: date, end: date) -> list[str]:
    months, y, m = [], start.year, start.month
    while (y, m) <= (end.year, end.month):
        months.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return months


def _last_day_of_month(day: date) -> date:
    first_next = date(day.year + 1, 1, 1) if day.month == 12 else date(day.year, day.month + 1, 1)
    return first_next - timedelta(days=1)


def _lower(value) -> str:
    return (value or "").strip().lower()


# --------------------------------------------------------------------------
# Merchant grouping -- INFERRED, and labelled so
# --------------------------------------------------------------------------

_CARD_SUFFIX = re.compile(r"\s*-\s*\d{3,6}\s*$")
_TRAILING_NOISE = re.compile(r"\s+(?:#?\d{2,}|[A-Z0-9]*\d[A-Z0-9]{4,})$")
_TRAILING_STATE = re.compile(r"\s+[A-Z]{2}$")


def merchant_group(description: str) -> str:
    """Cheap normalisation of a raw description into a grouping key.

    Uppercase, collapse whitespace, drop a trailing " - NNNN" card suffix, then
    repeatedly drop trailing terminal-id noise and a trailing two-letter state.
    It is a heuristic and it will occasionally merge two merchants or split
    one, which is why every group it produces carries `raw_examples`.
    """
    text = " ".join((description or "").upper().split())
    text = _CARD_SUFFIX.sub("", text)
    previous = None
    while previous != text:
        previous = text
        text = _TRAILING_NOISE.sub("", text)
        text = _TRAILING_STATE.sub("", text)
    text = text.strip(" -*#")
    return text or " ".join((description or "").upper().split())


# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------

def classify(row: dict) -> str:
    """Place one ledger row in exactly one flow bucket.

    Order is load-bearing. Card payments are caught first, on both legs, before
    anything can call either leg spending or income.
    """
    amount = row["amount"]
    description = _lower(row["description"])
    category = _lower(row["source_category"])
    source_type = _lower(row["source_type"])
    is_card = row["account_type"] == "credit_card"

    # 1. Card payment -- both legs, excluded from spending on both.
    if is_card and source_type in CARD_PAYMENT_TYPES:
        return CARD_PAYMENT
    if category in CARD_PAYMENT_CATEGORIES:
        return CARD_PAYMENT
    if any(p in description for p in CARD_PAYMENT_PATTERNS):
        return CARD_PAYMENT

    # 2. Movement between the owner's own accounts.
    if category in INTERNAL_CATEGORIES or any(p in description for p in INTERNAL_PATTERNS):
        return INTERNAL

    # 3. Inflows. Income needs a positive signal; without one the money may be
    #    the owner's own, so it is ambiguous rather than income.
    if amount > 0:
        if is_card:
            # A card credit that is not a payment is a refund or an
            # adjustment. It is not household income and it is not a purchase.
            return AMBIGUOUS
        if category in INCOME_CATEGORIES or any(p in description for p in INCOME_PATTERNS):
            return INCOME
        return AMBIGUOUS

    # 4. Outflows. A transfer-shaped type with no meaningful institution
    #    category names no counterparty, so it cannot be placed either.
    if amount == 0:
        return AMBIGUOUS
    if source_type in UNPLACEABLE_TYPES and category in UNPLACEABLE_CATEGORIES:
        return AMBIGUOUS
    return SPENDING


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------

def load_rows(con: duckdb.DuckDBPyConnection) -> list[dict]:
    """Every ledger row, joined to its account, classified, one dict each.

    Grouped by `transaction_date` where the institution supplied one: that is
    when the money was spent. `posted_date` is when the balance moved and is
    what reconciliation uses; see the schema's note on the two.
    """
    sql = """
        SELECT t.transaction_id,
               a.alias,
               a.account_type,
               a.balance_sign,
               COALESCE(t.transaction_date, t.posted_date) AS on_date,
               t.posted_date,
               t.amount,
               t.description,
               t.source_type,
               t.source_category
        FROM transactions t
        JOIN accounts a USING (account_id)
        ORDER BY on_date, t.transaction_id
    """
    rows = []
    for r in con.execute(sql).fetchall():
        row = {
            "transaction_id": r[0],
            "alias": r[1],
            "account_type": r[2],
            "balance_sign": r[3],
            "on_date": r[4],
            "posted_date": r[5],
            "amount": Decimal(r[6]) if not isinstance(r[6], Decimal) else r[6],
            "description": r[7] or "",
            "source_type": r[8],
            "source_category": r[9],
        }
        row["month"] = _month(row["on_date"])
        row["flow"] = classify(row)
        row["group"] = merchant_group(row["description"])
        rows.append(row)
    return rows


def load_accounts(con: duckdb.DuckDBPyConnection) -> list[dict]:
    """The account registry as mirrored into the ledger, with its newest balances.

    The newest balance is the institution's own figure at its own `as_of`. It
    is never presented as a "current" balance, because nothing here can know
    what has happened since.
    """
    accounts = []
    for alias, institution, account_type, balance_sign in con.execute(
        "SELECT alias, institution, account_type, balance_sign FROM accounts ORDER BY alias"
    ).fetchall():
        accounts.append({
            "alias": alias,
            "institution": institution,
            "type": account_type,
            "balance_sign": balance_sign,
            "latest_balance": None,
            "available_balance": None,
        })

    # CAST via AT TIME ZONE 'UTC' explicitly: a bare cast resolves in the
    # session zone, which would move the as_of day west of UTC.
    balances = con.execute(
        """
        SELECT a.alias, b.balance_type,
               CAST(b.as_of AT TIME ZONE 'UTC' AS DATE) AS as_of_day,
               b.amount, b.source_kind
        FROM balances b JOIN accounts a USING (account_id)
        ORDER BY a.alias, b.as_of
        """
    ).fetchall()

    newest: dict[tuple[str, str], tuple] = {}
    for alias, balance_type, as_of_day, amount, source_kind in balances:
        newest[(alias, balance_type)] = (as_of_day, amount, source_kind)

    for account in accounts:
        for key, field in (("ledger", "latest_balance"), ("available", "available_balance")):
            found = newest.get((account["alias"], key))
            if found is None:
                continue
            as_of_day, amount, source_kind = found
            account[field] = {
                "amount": money(amount),
                "as_of": _iso(as_of_day),
                "balance_type": key,
                "source_kind": source_kind,
                "provenance": "direct",
            }
    return accounts


# --------------------------------------------------------------------------
# Document sections
# --------------------------------------------------------------------------

def reconciliation(rows: list[dict]) -> dict:
    """The auditable count: rows_total is exactly the sum of the five buckets."""
    counts = {flow: 0 for flow in FLOWS}
    for row in rows:
        counts[row["flow"]] += 1
    ambiguous_total = sum((r["amount"] for r in rows if r["flow"] == AMBIGUOUS), Decimal("0"))
    return {
        "rows_total": len(rows),
        "card_payment": counts[CARD_PAYMENT],
        "internal": counts[INTERNAL],
        "income": counts[INCOME],
        "spending": counts[SPENDING],
        "ambiguous": counts[AMBIGUOUS],
        "ambiguous_total_amount": money(ambiguous_total),
    }


def coverage(rows: list[dict]) -> dict:
    """Observed extent, with the partial edge months named rather than assumed."""
    if not rows:
        return {"start": None, "end": None, "months": 0, "transactions": 0,
                "complete_months": [], "partial_months": []}
    start = min(r["on_date"] for r in rows)
    end = max(r["on_date"] for r in rows)
    months = _month_range(start, end)
    partial = set()
    if start.day != 1:
        partial.add(_month(start))
    if end != _last_day_of_month(end):
        partial.add(_month(end))
    return {
        "start": _iso(start),
        "end": _iso(end),
        "months": len(months),
        "transactions": len(rows),
        "complete_months": [m for m in months if m not in partial],
        "partial_months": sorted(partial),
    }


def _spend(rows) -> Decimal:
    """Spending as a positive magnitude: what left, counted once."""
    return -sum((r["amount"] for r in rows if r["flow"] == SPENDING), Decimal("0"))


def _income(rows) -> Decimal:
    return sum((r["amount"] for r in rows if r["flow"] == INCOME), Decimal("0"))


def cash_flow(rows: list[dict]) -> list[dict]:
    by_month: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_month[row["month"]].append(row)

    out = []
    for month in sorted(by_month):
        bucket = by_month[month]
        income = _income(bucket)
        spending = _spend(bucket)
        out.append({
            "month": month,
            "income": money(income),
            "spending": money(spending),
            "net": money(income - spending),
            # Every row, transfers included. Not spending, and not labelled so.
            "account_movement": money(sum((r["amount"] for r in bucket), Decimal("0"))),
            "provenance": "derived",
        })
    return out


def income_detail(rows: list[dict]) -> list[dict]:
    """One line per month and payer group, for inflows that came from outside."""
    grouped: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in rows:
        if row["flow"] == INCOME:
            grouped[(row["month"], row["group"])].append(row)

    out = []
    for (month, label), bucket in sorted(grouped.items()):
        # If the institution's own category placed it, say so; if only the
        # descriptor did, that is a heuristic and must read as one.
        by_category = any(_lower(r["source_category"]) in INCOME_CATEGORIES for r in bucket)
        out.append({
            "month": month,
            "amount": money(sum((r["amount"] for r in bucket), Decimal("0"))),
            "label": label,
            "n": len(bucket),
            "provenance": "institution" if by_category else "inferred",
        })
    return sorted(out, key=lambda d: (d["month"], -d["amount"]))


def spend_by_category(rows: list[dict]) -> list[dict]:
    """The institution's own category strings, used as given."""
    spending = [r for r in rows if r["flow"] == SPENDING]
    total = _spend(spending)
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in spending:
        grouped[(row["source_category"] or "Uncategorised").strip()].append(row)

    out = []
    for category, bucket in grouped.items():
        amount = _spend(bucket)
        out.append({
            "category": category,
            "n": len(bucket),
            "amount": money(amount),
            "share": round(float(amount / total), 4) if total else 0.0,
            "provenance": "institution",
        })
    return sorted(out, key=lambda d: -d["amount"])


def category_trend(rows: list[dict], complete_months: list[str]) -> list[dict]:
    """Direction per category over COMPLETE months only.

    A partial edge month reads as a collapse in spending that never happened,
    so it is excluded from the comparison and never drives a direction claim.
    """
    spending = [r for r in rows if r["flow"] == SPENDING]
    grouped: dict[str, dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    for row in spending:
        grouped[(row["source_category"] or "Uncategorised").strip()][row["month"]] += -row["amount"]

    out = []
    for category, months in grouped.items():
        series = [float(months[m]) for m in complete_months if m in months]
        direction, delta_pct = "insufficient", None
        if len(series) >= 3:
            half = len(series) // 2
            early = statistics.fmean(series[:half])
            late = statistics.fmean(series[half:] if len(series) % 2 == 0 else series[half + 1:])
            if early:
                delta_pct = round((late - early) / early * 100, 1)
                direction = "flat" if abs(delta_pct) < 10 else ("up" if delta_pct > 0 else "down")
            else:
                direction = "up" if late else "flat"
        out.append({
            "category": category,
            "months": {m: money(v) for m, v in sorted(months.items())},
            "direction": direction,
            "delta_pct": delta_pct,
            "complete_months_used": len(series),
            "provenance": "derived",
        })
    return sorted(out, key=lambda d: -sum(d["months"].values()))


def top_merchants(rows: list[dict], limit: int = 15) -> list[dict]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        if row["flow"] == SPENDING:
            grouped[row["group"]].append(row)

    out = []
    for group, bucket in grouped.items():
        examples: list[str] = []
        for row in bucket:
            if row["description"] not in examples:
                examples.append(row["description"])
            if len(examples) == 3:
                break
        out.append({
            "group": group,
            "n": len(bucket),
            "amount": money(_spend(bucket)),
            "first": _iso(min(r["on_date"] for r in bucket)),
            "last": _iso(max(r["on_date"] for r in bucket)),
            "accounts": sorted({r["alias"] for r in bucket}),
            "provenance": "inferred",
            "raw_examples": examples,
        })
    return sorted(out, key=lambda d: -d["amount"])[:limit]


def largest_transactions(rows: list[dict], limit: int = 15) -> list[dict]:
    spending = sorted(
        (r for r in rows if r["flow"] == SPENDING), key=lambda r: r["amount"]
    )
    return [{
        "date": _iso(r["on_date"]),
        "account": r["alias"],
        "amount": money(-r["amount"]),
        "description": r["description"],
        "category": r["source_category"] or "Uncategorised",
    } for r in spending[:limit]]


def _monthly_spend(rows) -> list[dict]:
    by_month: dict[str, Decimal] = defaultdict(Decimal)
    for row in rows:
        if row["flow"] == SPENDING:
            by_month[row["month"]] += -row["amount"]
    return [{"month": m, "amount": money(v)} for m, v in sorted(by_month.items())]


def card_section(rows: list[dict], accounts: list[dict]) -> dict:
    card_accounts = [a for a in accounts if a["type"] == "credit_card"]
    aliases = {a["alias"] for a in card_accounts}
    card_rows = [r for r in rows if r["alias"] in aliases]

    interest_by_month: dict[str, Decimal] = defaultdict(Decimal)
    for row in card_rows:
        text = f"{_lower(row['description'])} {_lower(row['source_category'])} {_lower(row['source_type'])}"
        if row["amount"] < 0 and any(p in text for p in INTEREST_PATTERNS):
            interest_by_month[row["month"]] += -row["amount"]

    balance = None
    for account in card_accounts:
        if account["latest_balance"]:
            balance = account["latest_balance"]["amount"]
            break

    return {
        # Signed as the ledger stores it: NEGATIVE when money is owed. One
        # convention, stated here and in data_quality, never mixed.
        "balance": balance,
        "balance_convention": "negative means owed (liability)",
        "monthly_spend": _monthly_spend(card_rows),
        "top_merchants": top_merchants(card_rows, limit=10),
        "top_categories": spend_by_category(card_rows)[:10],
        "interest": [{"month": m, "amount": money(v)} for m, v in sorted(interest_by_month.items())],
        "interest_total": money(sum(interest_by_month.values(), Decimal("0"))),
        "unavailable": list(CARD_UNAVAILABLE),
    }


def snapshot(rows: list[dict], accounts: list[dict], window_months: int = 3) -> dict:
    """Position and a recent window. Never a "current" balance, never net worth."""
    def position(alias_filter) -> dict:
        used = [a for a in accounts if alias_filter(a) and a["latest_balance"]]
        if not used:
            return {"amount": None, "provenance": "unavailable", "as_of": None,
                    "note": "no institution balance has been recorded for this account"}
        total = sum(Decimal(str(a["latest_balance"]["amount"])) for a in used)
        as_of = min(a["latest_balance"]["as_of"] for a in used)
        return {
            "amount": money(total),
            "provenance": "derived",
            "as_of": as_of,
            "note": ("sum of each account's own newest recorded balance, each at its own "
                     "as_of; the oldest of those is shown. Not a live balance."),
        }

    tracked = position(lambda a: True)
    tracked["note"] = (
        "the two tracked accounts only, each at its own newest recorded as_of. "
        "This is not net worth."
    )

    months = sorted({r["month"] for r in rows})
    recent_months = set(months[-window_months:])
    recent_rows = [r for r in rows if r["month"] in recent_months]
    income = _income(recent_rows)
    spending = _spend(recent_rows)

    return {
        "tracked_position": tracked,
        "checking": position(lambda a: a["type"] == "checking"),
        "card": position(lambda a: a["type"] == "credit_card"),
        "recent": {
            "window_months": window_months,
            "months": sorted(recent_months),
            "income": money(income),
            "spending": money(spending),
            "net": money(income - spending),
            "provenance": "derived",
        },
    }


# --------------------------------------------------------------------------
# Recurrence -- two occurrences is not a subscription
# --------------------------------------------------------------------------

MIN_OCCURRENCES = 3
MIN_DISTINCT_MONTHS = 3
STABLE_IQR_FRACTION = 0.15


def _iqr_spread(values: list[float]) -> float:
    """Interquartile spread as a fraction of the median. 0.0 when identical."""
    median = statistics.median(values)
    if not median:
        return 0.0
    ordered = sorted(values)
    if len(ordered) < 4:
        return (max(ordered) - min(ordered)) / abs(median)
    quartiles = statistics.quantiles(ordered, n=4, method="inclusive")
    return (quartiles[2] - quartiles[0]) / abs(median)


def _cadence(dates: list[date]) -> str:
    if len(dates) < 2:
        return "irregular"
    gaps = [(b - a).days for a, b in zip(dates, dates[1:])]
    median_gap = statistics.median(gaps)
    if 25 <= median_gap <= 35:
        return "monthly"
    if 330 <= median_gap <= 400:
        return "annual"
    return "irregular"


def recurring(rows: list[dict], coverage_end: date | None) -> list[dict]:
    """Merchant groups that recur often enough to be worth naming.

    The thresholds are the contract's, and they are floors, not suggestions:
    at least 3 occurrences across at least 3 distinct months before anything is
    emitted at all. Two occurrences is a coincidence, not a subscription.
    `high` additionally requires a stable amount -- interquartile spread within
    ~15% of the median -- and a regular cadence.
    """
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        if row["flow"] == SPENDING:
            grouped[row["group"]].append(row)

    out = []
    for group, bucket in grouped.items():
        months = sorted({r["month"] for r in bucket})
        if len(bucket) < MIN_OCCURRENCES or len(months) < MIN_DISTINCT_MONTHS:
            continue
        dates = sorted(r["on_date"] for r in bucket)
        amounts = [float(-r["amount"]) for r in bucket]
        median_amount = statistics.median(amounts)
        spread = _iqr_spread(amounts)
        cadence = _cadence(dates)
        stable = spread <= STABLE_IQR_FRACTION

        if stable and cadence in ("monthly", "annual"):
            confidence = "high"
        elif stable or cadence in ("monthly", "annual"):
            confidence = "medium"
        else:
            confidence = "low"

        still_active = (
            coverage_end is not None and (coverage_end - dates[-1]).days <= 45
        )
        out.append({
            "group": group,
            "cadence": cadence,
            "median_amount": round(median_amount, 2),
            "occurrences": len(bucket),
            "distinct_months": len(months),
            "first": _iso(dates[0]),
            "last": _iso(dates[-1]),
            "still_active": still_active,
            "confidence": confidence,
            "evidence": (
                f"{len(bucket)} occurrences across {len(months)} distinct months, "
                f"{cadence} cadence, median {median_amount:.2f}, "
                f"interquartile spread {spread * 100:.0f}% of median, "
                f"last seen {dates[-1].isoformat()}"
            ),
            "provenance": "inferred",
        })
    return sorted(out, key=lambda d: (-d["occurrences"], -d["median_amount"]))


# --------------------------------------------------------------------------
# Leaks -- each one classified, never dressed up as a fact
# --------------------------------------------------------------------------

def leaks(rows: list[dict], card: dict, recurrences: list[dict], months: int) -> list[dict]:
    out: list[dict] = []
    divisor = months or 1

    if card["interest_total"]:
        out.append({
            "title": "Credit-card interest charged",
            "classification": "FACT",
            "detail": ("Interest posted on the card over the covered period. This is a "
                       "charge that carrying no balance would remove entirely."),
            "monthly_impact": round(card["interest_total"] / divisor, 2),
            "evidence": (f"{card['interest_total']:.2f} in interest across "
                         f"{len(card['interest'])} months of the covered period"),
        })

    # Interest is already reported above as a fact of its own; a finance charge
    # is not a subscription and must not be counted twice.
    subscriptions = [
        r for r in recurrences
        if r["confidence"] == "high" and r["cadence"] == "monthly" and r["still_active"]
        and not any(p in r["group"].lower() for p in INTEREST_PATTERNS)
    ]
    if subscriptions:
        monthly = sum(r["median_amount"] for r in subscriptions)
        out.append({
            "title": f"{len(subscriptions)} stable monthly charges still running",
            "classification": "INFERENCE",
            "detail": ("Merchant groups charging a stable amount every month. Grouping is "
                       "inferred from descriptions, so check the raw examples before "
                       "treating any one of these as a subscription."),
            "monthly_impact": round(monthly, 2),
            "evidence": ", ".join(f"{r['group']} ({r['median_amount']:.2f})" for r in subscriptions[:5]),
        })

    ambiguous_rows = [r for r in rows if r["flow"] == AMBIGUOUS]
    if ambiguous_rows:
        # Signed as the ledger stores it, and the same figure reconciliation
        # reports -- so the two cannot disagree about which way the money went.
        total = sum((r["amount"] for r in ambiguous_rows), Decimal("0"))
        out.append({
            "title": f"{len(ambiguous_rows)} rows could not be classified",
            "classification": "POSSIBLE_OPPORTUNITY",
            "detail": ("These carry no counterparty the imports can resolve -- notably "
                       "self-named ACH movement. They are excluded from both spending and "
                       "income. Naming them is the single largest improvement available "
                       "to this dashboard's accuracy."),
            "monthly_impact": None,
            "evidence": (f"net {money(total)} across {len(ambiguous_rows)} rows, "
                         f"held out of every headline figure"),
        })

    return out


# --------------------------------------------------------------------------
# The document
# --------------------------------------------------------------------------

def build_document(con: duckdb.DuckDBPyConnection, *, generated_at: datetime | None = None) -> dict:
    """The whole dashboard document. One read of the ledger, no writes."""
    rows = load_rows(con)
    accounts = load_accounts(con)
    cover = coverage(rows)
    counts = reconciliation(rows)
    coverage_end = max((r["on_date"] for r in rows), default=None)
    card = card_section(rows, accounts)
    recurrences = recurring(rows, coverage_end)
    now = generated_at or datetime.now(timezone.utc)

    document = {
        "generated_at": now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "coverage": cover,
        "accounts": accounts,
        "snapshot": snapshot(rows, accounts),
        "cash_flow": cash_flow(rows),
        "income_detail": income_detail(rows),
        "spend_by_category": spend_by_category(rows),
        "category_trend": category_trend(rows, cover["complete_months"]),
        "top_merchants": top_merchants(rows),
        "largest_transactions": largest_transactions(rows),
        "card": card,
        "recurring": recurrences,
        "leaks": leaks(rows, card, recurrences, cover["months"]),
        "data_quality": {
            "notes": [
                "Spending counts each real purchase exactly once. A credit-card payment "
                "appears twice in the ledger -- negative in checking, positive on the "
                "card -- and both legs are classified card_payment and excluded.",
                "Income means money from outside the tracked accounts. Certificate and "
                "savings drawdowns are internal transfers, not income; an inflow with no "
                "positive income signal is held in ambiguous rather than counted.",
                "Ambiguous rows are folded into neither spending nor income. Their count "
                "and net total are in reconciliation.",
                "Categories are the institution's own strings, used as given. purser's "
                "own categoriser has not run over this ledger and the categories, "
                "merchants and transfers tables are empty.",
                "Merchant groups are inferred from descriptions by normalisation, not "
                "matched to a merchant table; each group carries up to 3 raw examples.",
                "Balances are the institution's own figures at their own as_of. Nothing "
                "here is a live balance.",
                "The card is a liability and its balance is negative when owed, "
                "throughout this document.",
                "tracked_position covers the tracked accounts only. It is not net worth.",
                f"Partial edge months, excluded from trend claims: "
                f"{', '.join(cover['partial_months']) or 'none'}.",
            ],
            "unavailable": [
                "card APR, credit limit, utilisation, minimum payment, due date and "
                "statement close date -- no import carries them",
                "matched transfer pairs -- the transfers table is empty and no matcher ran",
                "canonical merchant identities -- the merchants table is empty",
                "purser's own category taxonomy -- the categories table is empty",
            ],
            "reconciliation": counts,
        },
    }
    return document
