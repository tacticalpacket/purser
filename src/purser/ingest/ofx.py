"""OFX reader.

DESIGN.md scopes this as "stub, future institutions": NFCU dropped OFX/QFX in
April 2026, so OFX is not the transaction path and no transaction parser is
built here.

What is implemented is the balance block, because that is what the balance
check needs. NFCU's CSV export carries no balance at all -- neither an opening
nor a closing figure -- so the only authoritative ending balance available for
reconciliation is <BALAMT> inside the OFX <LEDGERBAL>, with its <DTASOF>.

Ledger vs available
-------------------
Both files carry <LEDGERBAL> and <AVAILBAL>. Reconcile against LEDGERBAL. The
available balance nets pending holds the ledger has not yet posted, so it will
not agree with a sum of posted transactions and using it would manufacture a
delta that is not an error. On the captain's checking export the two differ,
which is exactly the trap.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path


class OfxParseError(ValueError):
    """The file is not readable as OFX."""


@dataclass(frozen=True)
class OfxBalance:
    balance_type: str          # 'ledger' | 'available'
    amount: Decimal
    as_of: datetime
    currency: str
    source_file: str


@dataclass(frozen=True)
class OfxStatementInfo:
    currency: str
    start_date: datetime | None
    end_date: datetime | None
    ledger: OfxBalance | None
    available: OfxBalance | None
    transaction_count: int


def _tag(text: str, tag: str) -> str | None:
    match = re.search(rf"<{tag}>([^<]*)", text)
    return match.group(1).strip() if match else None


def parse_ofx_datetime(value: str) -> datetime:
    """OFX datetimes are YYYYMMDDHHMMSS[.SSS], optionally truncated."""
    cleaned = value.strip().split("[")[0]
    for fmt in ("%Y%m%d%H%M%S.%f", "%Y%m%d%H%M%S", "%Y%m%d"):
        try:
            return datetime.strptime(cleaned, fmt)
        except ValueError:
            continue
    raise OfxParseError(f"unrecognized OFX datetime {value!r}")


def read_statement(path: str | Path) -> OfxStatementInfo:
    """Read balances and statement extent. Does not parse transactions."""
    path = Path(path)
    text = path.read_text(encoding="utf-8", errors="replace")
    if "<OFX>" not in text:
        raise OfxParseError(f"{path}: no <OFX> element")

    currency = _tag(text, "CURDEF") or "USD"

    balances: dict[str, OfxBalance] = {}
    for block, kind in (("LEDGERBAL", "ledger"), ("AVAILBAL", "available")):
        match = re.search(rf"<{block}>(.*?)</{block}>", text, re.DOTALL)
        if not match:
            continue
        body = match.group(1)
        amount, as_of = _tag(body, "BALAMT"), _tag(body, "DTASOF")
        if amount is None or as_of is None:
            raise OfxParseError(f"{path}: <{block}> is missing BALAMT or DTASOF")
        balances[kind] = OfxBalance(
            balance_type=kind,
            amount=Decimal(amount),
            as_of=parse_ofx_datetime(as_of),
            currency=currency,
            source_file=str(path),
        )

    start, end = _tag(text, "DTSTART"), _tag(text, "DTEND")
    return OfxStatementInfo(
        currency=currency,
        start_date=parse_ofx_datetime(start) if start else None,
        end_date=parse_ofx_datetime(end) if end else None,
        ledger=balances.get("ledger"),
        available=balances.get("available"),
        transaction_count=len(re.findall(r"<STMTTRN>", text)),
    )


def ledger_balance(path: str | Path) -> OfxBalance:
    """The authoritative ending balance for reconciliation."""
    info = read_statement(path)
    if info.ledger is None:
        raise OfxParseError(f"{path}: no <LEDGERBAL> block")
    return info.ledger


def parse_transactions(path: str | Path):  # pragma: no cover - deliberate stub
    """Not implemented: DESIGN.md keeps the OFX transaction path as a stub."""
    raise NotImplementedError(
        "OFX transaction parsing is deliberately unbuilt; DESIGN.md scopes this "
        "module as a stub for future institutions. NFCU's transaction path is "
        "src/purser/ingest/nfcu_csv.py."
    )
