#!/usr/bin/env python3
"""stripe_payouts_to_qbo: turn Stripe's net payouts into balanced QuickBooks Online journal entries.

Stripe deposits the *net* amount (sales minus fees, refunds and disputes). Your books need the
*gross* sales, with fees as an expense, and an entry that equals the bank deposit so you can match
it in the bank feed instead of booking the deposit as income.

This script reads Stripe's "Payout reconciliation" Itemized CSV and writes a QuickBooks Online
journal-entry import file with one balanced entry per payout.

    python3 stripe_payouts_to_qbo.py payout_reconciliation.csv
    python3 stripe_payouts_to_qbo.py export.csv --out books.csv --bank "Checking:Business" --fees "Merchant Fees"

Python 3.9+, standard library only, runs offline. MIT licensed.
Paid version with Xero, bank matching and a tie-out report: https://yarbrough56.gumroad.com/l/gxlcqf
"""

from __future__ import annotations

import argparse
import csv
import datetime as _dt
import io
import os
import re
import sys
from collections import OrderedDict
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Dict, List, Optional, Sequence, Tuple

__version__ = "1.0.0"

CENT = Decimal("0.01")
ZERO = Decimal("0")

#: Default account names. Each can be changed with a command-line flag (e.g. --sales "Sales:Online").
DEFAULT_ACCOUNTS: Dict[str, str] = {
    "bank": "Checking",
    "clearing": "Stripe Clearing",
    "sales": "Sales",
    "refunds": "Refunds",
    "disputes": "Chargebacks",
    "fees": "Stripe Fees",
    "tax": "Sales Tax Payable",
    "other": "Stripe Other Adjustments",
}

#: Stripe reporting_category -> account key. See https://docs.stripe.com/reports/reporting-categories
CATEGORY_TO_ACCOUNT: Dict[str, str] = {
    "charge": "sales",
    "charge_failure": "sales",
    "partial_capture_reversal": "sales",
    "payment": "sales",
    "platform_earning": "sales",
    "refund": "refunds",
    "refund_failure": "refunds",
    "payment_refund": "refunds",
    "payment_failure_refund": "refunds",
    "platform_earning_refund": "refunds",
    "dispute": "disputes",
    "dispute_reversal": "disputes",
    "fee": "fees",
    "stripe_fee": "fees",
    "network_cost": "fees",
    "tax_fee": "fees",
    "tax": "tax",
}

#: Older exports have a `type` column instead of `reporting_category`.
TYPE_TO_CATEGORY: Dict[str, str] = {
    "charge": "charge",
    "payment": "charge",
    "refund": "refund",
    "payment_refund": "refund",
    "adjustment": "other_adjustment",
    "stripe_fee": "fee",
    "tax_fee": "fee",
    "application_fee": "platform_earning",
    "payout": "payout",
}

PAYOUT_CATEGORIES = {"payout", "payout_reversal", "payout_cancel", "payout_failure"}

LINE_ORDER = ("sales", "refunds", "disputes", "tax", "other")
LABELS = {
    "sales": "Gross sales",
    "refunds": "Refunds",
    "disputes": "Disputes / chargebacks",
    "tax": "Tax collected",
    "other": "Other Stripe adjustments",
}

QBO_HEADER = ["Journal No.", "Journal Date", "Account Name", "Debits", "Credits", "Description", "Name", "Currency"]
#: QuickBooks Online rejects journal import files of 1,000 rows or more (header included).
QBO_MAX_DATA_ROWS = 998


class InputError(Exception):
    """A problem with the input file that the user needs to fix."""


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def parse_amount(raw: Optional[str], *, field_name: str = "amount", row_no: int = 0) -> Decimal:
    """Parse '1,234.56', '(12.00)', '$5' or '-3.2' into a Decimal. Blank means zero."""
    s = "" if raw is None else str(raw).strip()
    if not s:
        return ZERO
    neg = False
    if s.startswith("(") and s.endswith(")"):
        neg, s = True, s[1:-1]
    s = s.replace(",", "").replace("$", "").replace("\u20ac", "").replace("\u00a3", "").strip()
    try:
        val = Decimal(s)
    except InvalidOperation:
        raise InputError(f"Row {row_no}: could not read {field_name} value {raw!r} as a number")
    return -val if neg else val


_DATE_FORMATS = (
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d",
    "%m/%d/%Y", "%m/%d/%Y %H:%M", "%m/%d/%Y %H:%M:%S", "%m/%d/%y",
)


def parse_date(raw: Optional[str]) -> Optional[_dt.date]:
    s = "" if raw is None else str(raw).strip()
    if not s:
        return None
    s = re.sub(r"\s*(UTC|Z)$", "", s)
    s = re.sub(r"([+-]\d{2}:?\d{2})$", "", s).strip()
    if re.fullmatch(r"\d{9,11}", s):  # unix seconds
        return _dt.datetime.fromtimestamp(int(s), _dt.timezone.utc).date()
    for fmt in _DATE_FORMATS:
        try:
            return _dt.datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    try:
        return _dt.datetime.fromisoformat(s).date()
    except ValueError:
        return None


def _norm_header(h: str) -> str:
    h = (h or "").replace("\ufeff", "").strip().lower()
    h = re.sub(r"\s*\(utc\)\s*", "_utc", h)
    return re.sub(r"[^a-z0-9]+", "_", h).strip("_")


HEADER_ALIASES = {
    "id": "balance_transaction_id",
    "balance_transaction": "balance_transaction_id",
    "created": "created_utc",
    "available_on": "available_on_utc",
    "amount": "gross",
    "category": "reporting_category",
    "payout_id": "automatic_payout_id",
    "transfer": "automatic_payout_id",
    "automatic_payout_effective_at_utc": "automatic_payout_effective_at",
    "payout_date": "automatic_payout_effective_at",
}


@dataclass
class Txn:
    row_no: int
    id: str
    created: Optional[_dt.date]
    currency: str
    gross: Decimal
    fee: Decimal
    net: Decimal
    category: str
    payout_id: str
    payout_date: Optional[_dt.date]


def load_transactions(path: str, warnings: List[str]) -> List[Txn]:
    """Read one Stripe Itemized payout reconciliation CSV."""
    with open(path, "r", encoding="utf-8-sig", newline="") as fh:
        text = fh.read()
    rows = [r for r in csv.reader(io.StringIO(text)) if any(c.strip() for c in r)]
    if len(rows) < 2:
        raise InputError(f"{path} has no transactions")
    headers = rows[0]
    idx: Dict[str, int] = {}
    for i, h in enumerate(headers):
        n = _norm_header(h)
        idx.setdefault(HEADER_ALIASES.get(n, n), i)
    if "gross" not in idx and "net" not in idx:
        raise InputError(
            "This doesn't look like a Stripe Itemized export (no 'gross' or 'net' column). In Stripe: "
            "Reports > Payout reconciliation > Download > report type 'Itemized'."
        )
    if "reporting_category" not in idx and "type" not in idx:
        raise InputError(
            "No 'reporting_category' (or 'type') column found, so this doesn't look like a Stripe export. "
            "In Stripe: Reports > Payout reconciliation > Download > report type 'Itemized'."
        )
    if "automatic_payout_id" not in idx:
        raise InputError(
            "No 'automatic_payout_id' column, so transactions can't be grouped by payout. Use the "
            "'Payout reconciliation' Itemized report (not 'Balance change from activity')."
        )

    def col(r: List[str], name: str) -> str:
        i = idx.get(name)
        return r[i].strip() if i is not None and i < len(r) else ""

    txns: List[Txn] = []
    seen = set()
    for row_no, r in enumerate(rows[1:], start=2):
        cat = col(r, "reporting_category").lower()
        if not cat:
            typ = col(r, "type").lower()
            cat = TYPE_TO_CATEGORY.get(typ, typ)
        if not cat:
            warnings.append(f"Row {row_no}: no category; booked to the 'other' account")
            cat = "other_adjustment"
        gross = parse_amount(col(r, "gross"), field_name="gross", row_no=row_no)
        fee = parse_amount(col(r, "fee"), field_name="fee", row_no=row_no)
        net_raw = col(r, "net")
        net = parse_amount(net_raw, field_name="net", row_no=row_no) if net_raw else gross - fee
        if "gross" not in idx:
            gross = net + fee
        if abs(gross - fee - net) > CENT / 2:
            warnings.append(f"Row {row_no}: gross {gross} - fee {fee} != net {net}; using gross and fee")
            net = gross - fee
        tid = col(r, "balance_transaction_id") or f"row{row_no}"
        if tid in seen:
            warnings.append(f"Row {row_no}: duplicate transaction {tid} skipped")
            continue
        seen.add(tid)
        if cat not in CATEGORY_TO_ACCOUNT and cat not in PAYOUT_CATEGORIES:
            warnings.append(f"Row {row_no}: unfamiliar category '{cat}' booked to the 'other' account")
        txns.append(Txn(
            row_no=row_no,
            id=tid,
            created=parse_date(col(r, "created_utc")),
            currency=(col(r, "currency") or "usd").upper(),
            gross=gross,
            fee=fee,
            net=net,
            category=cat,
            payout_id=col(r, "automatic_payout_id"),
            payout_date=parse_date(col(r, "automatic_payout_effective_at")),
        ))
    if not txns:
        raise InputError("The export has a header row but no transactions.")
    return txns


# ---------------------------------------------------------------------------
# Grouping and journal entries
# ---------------------------------------------------------------------------

@dataclass
class Payout:
    payout_id: str  # "UNPAID" for transactions not yet in an automatic payout
    currency: str
    date: Optional[_dt.date]
    txns: List[Txn] = field(default_factory=list)

    def total(self, attr: str) -> Decimal:
        return sum((getattr(t, attr) for t in self.txns), ZERO)


def group_by_payout(txns: Sequence[Txn], warnings: List[str]) -> List[Payout]:
    groups: "OrderedDict[Tuple[str, str], Payout]" = OrderedDict()
    for t in txns:
        if t.category in PAYOUT_CATEGORIES:
            continue  # the payout itself is the bank line we compute; counting it would double up
        key = (t.payout_id or "UNPAID", t.currency)
        g = groups.setdefault(key, Payout(key[0], t.currency, None))
        if t.payout_date and (g.date is None or t.payout_date > g.date):
            g.date = t.payout_date
        g.txns.append(t)
    unpaid = sum(len(g.txns) for g in groups.values() if g.payout_id == "UNPAID")
    if unpaid:
        warnings.append(
            f"{unpaid} transaction(s) have no automatic payout yet (still in your Stripe balance, or paid "
            "by a manual payout)."
        )
    for g in groups.values():
        if g.date is None:
            ds = [t.created for t in g.txns if t.created]
            g.date = max(ds) if ds else None
    return sorted(groups.values(), key=lambda g: (g.date or _dt.date.max, g.payout_id))


@dataclass
class Line:
    account: str  # account key, e.g. "sales"
    debit: Decimal
    credit: Decimal
    memo: str


@dataclass
class Entry:
    number: str
    date: Optional[_dt.date]
    currency: str
    payout: Payout
    lines: List[Line]

    def totals(self) -> Tuple[Decimal, Decimal]:
        return (sum((l.debit for l in self.lines), ZERO), sum((l.credit for l in self.lines), ZERO))


def _q(d: Decimal) -> Decimal:
    return d.quantize(CENT, ROUND_HALF_UP)


def build_entries(payouts: Sequence[Payout], *, prefix: str = "STRIPE") -> List[Entry]:
    """One balanced journal entry per payout.

    Each Stripe line's gross goes to its account (sales/refunds/disputes/tax/other): money into
    Stripe is a credit, money out is a debit. All per-line fees go to the fees expense account.
    The balancing debit is the net payout to the bank, which equals the bank deposit.
    """
    entries: List[Entry] = []
    for n, p in enumerate(payouts, start=1):
        sums: "OrderedDict[str, Decimal]" = OrderedDict((k, ZERO) for k in LINE_ORDER)
        fees = p.total("fee")
        for t in p.txns:
            acct = CATEGORY_TO_ACCOUNT.get(t.category, "other")
            if acct == "fees":
                # Stand-alone Stripe fee rows (billing, Radar, etc.) carry the amount in gross.
                fees += -t.gross
            else:
                sums[acct] += t.gross
        label = f"Stripe payout {p.payout_id}" if p.payout_id != "UNPAID" else "Stripe activity not yet paid out"
        lines: List[Line] = []
        for acct, amt in sums.items():
            amt = _q(amt)
            if amt > 0:
                lines.append(Line(acct, ZERO, amt, f"{LABELS[acct]} - {label}"))
            elif amt < 0:
                lines.append(Line(acct, -amt, ZERO, f"{LABELS[acct]} - {label}"))
        fees = _q(fees)
        if fees > 0:
            lines.append(Line("fees", fees, ZERO, f"Stripe fees - {label}"))
        elif fees < 0:
            lines.append(Line("fees", ZERO, -fees, f"Stripe fee credits - {label}"))
        target = "bank" if p.payout_id != "UNPAID" else "clearing"
        dr, cr = (sum((l.debit for l in lines), ZERO), sum((l.credit for l in lines), ZERO))
        net = cr - dr  # equals the payout amount
        what = "Payout deposit" if target == "bank" else "Held in Stripe balance"
        if net > 0:
            lines.append(Line(target, net, ZERO, f"{what} - {label}"))
        elif net < 0:
            lines.append(Line(target, ZERO, -net, f"{what} (negative payout) - {label}"))
        if not lines:
            continue
        stamp = p.date.strftime("%Y%m%d") if p.date else "nodate"
        entries.append(Entry(f"{prefix}-{stamp}-{n}", p.date, p.currency, p, lines))
    return entries


def payout_amount(e: Entry) -> Decimal:
    """The net amount of the entry's bank (or clearing) line: what should hit the bank."""
    return sum((l.debit - l.credit for l in e.lines if l.account in ("bank", "clearing")), ZERO)


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def _safe(text: str) -> str:
    """Stop spreadsheet apps from treating free text as a formula."""
    return "'" + text if text and text[0] in "=+-@\t\r" else text


def _money(d: Decimal) -> str:
    return f"{_q(d):.2f}"


def write_qbo(entries: Sequence[Entry], accounts: Dict[str, str], out_path: str, *,
              date_fmt: str = "%m/%d/%Y", max_rows: int = QBO_MAX_DATA_ROWS) -> List[str]:
    """Write QBO journal import CSV(s). Splits into _part2, _part3... under QBO's row limit,
    never splitting one entry across files. Returns the paths written."""
    base, ext = os.path.splitext(out_path)
    ext = ext or ".csv"
    files: List[str] = []
    chunk: List[List[str]] = []

    def flush() -> None:
        if not chunk:
            return
        path = f"{base}{ext}" if not files else f"{base}_part{len(files) + 1}{ext}"
        with open(path, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(QBO_HEADER)
            w.writerows(chunk)
        files.append(path)
        chunk.clear()

    for e in entries:
        rows = [[e.number, e.date.strftime(date_fmt) if e.date else "", accounts[l.account],
                 _money(l.debit) if l.debit else "", _money(l.credit) if l.credit else "",
                 _safe(l.memo), "", e.currency] for l in e.lines]
        if len(rows) > max_rows:
            raise InputError(f"Entry {e.number} has more than {max_rows} lines")
        if len(chunk) + len(rows) > max_rows:
            flush()
        chunk.extend(rows)
    flush()
    return files


def summary_table(entries: Sequence[Entry]) -> str:
    out = [f"{'Date':<10}  {'Payout':<28} {'Gross sales':>12} {'Fees':>10} {'Other':>10} {'Net':>12}"]
    for e in entries:
        p = e.payout
        sales = sum((t.gross for t in p.txns if CATEGORY_TO_ACCOUNT.get(t.category) == "sales"), ZERO)
        fees = sum((l.debit - l.credit for l in e.lines if l.account == "fees"), ZERO)
        net = payout_amount(e)
        other = net - sales + fees
        d = e.date.isoformat() if e.date else "-"
        out.append(f"{d:<10}  {p.payout_id[:28]:<28} {_money(sales):>12} {_money(fees):>10} "
                   f"{_money(other):>10} {_money(net):>12}  {e.currency}")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="stripe_payouts_to_qbo.py",
        description="Turn a Stripe Itemized payout reconciliation CSV into a QuickBooks Online "
                    "journal-entry import with one balanced entry per payout.",
    )
    ap.add_argument("stripe_csv", help="Stripe Reports > Payout reconciliation > Download > Itemized")
    ap.add_argument("--out", "-o", default=None,
                    help="output CSV path (default: quickbooks_journal_entries.csv next to the input)")
    ap.add_argument("--prefix", default="STRIPE", help="journal number prefix (default: STRIPE)")
    ap.add_argument("--include-unpaid", action="store_true",
                    help="also write one entry for activity not yet in an automatic payout, posting its net "
                         "to the Stripe Clearing account. Off by default: those transactions will appear "
                         "under their payout in a later export, and importing both would double count.")
    ap.add_argument("--date-format", default="%m/%d/%Y",
                    help="date format for QBO, e.g. %%d/%%m/%%Y for UK/AU files (default: %%m/%%d/%%Y)")
    for key, name in DEFAULT_ACCOUNTS.items():
        ap.add_argument(f"--{key}", default=name, metavar="NAME",
                        help=f"account name for {key} (default: {name!r}); use Parent:Child for sub-accounts")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return ap


def run(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    accounts = {k: getattr(args, k) for k in DEFAULT_ACCOUNTS}
    warnings: List[str] = []
    try:
        txns = load_transactions(args.stripe_csv, warnings)
        payouts = group_by_payout(txns, warnings)
        if not args.include_unpaid and any(p.payout_id == "UNPAID" for p in payouts):
            payouts = [p for p in payouts if p.payout_id != "UNPAID"]
            warnings.append("Not-yet-paid-out activity was left out of the import (it will show up under "
                            "its payout in a later export). Use --include-unpaid to book it to Stripe Clearing now.")
        if not payouts:
            raise InputError("No paid-out transactions found. Pick a date range that includes completed payouts, "
                             "or use --include-unpaid.")
        entries = build_entries(payouts, prefix=args.prefix)
        for e in entries:
            dr, cr = e.totals()
            if dr != cr:  # cannot happen by construction; refuse to write a bad file if it ever does
                raise InputError(f"Internal check failed: entry {e.number} does not balance")
        out = args.out or os.path.join(os.path.dirname(os.path.abspath(args.stripe_csv)),
                                       "quickbooks_journal_entries.csv")
        files = write_qbo(entries, accounts, out, date_fmt=args.date_format)
    except (InputError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    print(summary_table(entries))
    print()
    for w in warnings:
        print(f"Note: {w}")
    for f in files:
        print(f"Wrote {f}")
    print(f"{len(entries)} journal entr{'y' if len(entries) == 1 else 'ies'}, every one balanced. "
          "In QBO: Settings > Import data > Journal entries. Then match each bank deposit to its entry.")
    return 0


def main() -> None:
    sys.exit(run())


if __name__ == "__main__":
    main()
