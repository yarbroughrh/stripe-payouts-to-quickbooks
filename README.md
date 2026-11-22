# stripe-payouts-to-quickbooks

Turn Stripe's **net** payouts into **balanced QuickBooks Online journal entries**, one per payout,
straight from Stripe's own export. It's a single Python file with no dependencies, runs offline,
and is MIT licensed.

Stripe deposits sales minus fees, refunds and chargebacks as one lump. Your books (and your CPA)
want gross sales on one line, fees as an expense, and refunds/chargebacks on their own lines, in
an entry that equals the bank deposit to the cent. That way you **match** the deposit in the QBO
bank feed instead of booking it as income and understating both revenue and fees.

```
python3 stripe_payouts_to_qbo.py payout_reconciliation.csv
```

## What you get

- **One balanced journal entry per Stripe payout.** The checking line equals the payout, so it
  matches the bank-feed deposit.
- Gross sales, refunds, disputes/chargebacks, sales tax collected, and **all Stripe fees** (per-charge
  fees, dispute fees, stand-alone billing fees) each go to their own account.
- Output is a **QuickBooks Online "Import journal entries" CSV**, split automatically into `_part2`,
  `_part3`... files to stay under QBO's 1,000-row limit, without ever splitting an entry.
- Account names are set with flags (sub-accounts as `Parent:Child`), so they match your chart of accounts.
- **Safe defaults:**
  - activity that hasn't been paid out yet is left out, so it can't be double-counted later;
  - duplicate rows and stray payout rows are ignored;
  - every entry is checked to balance before anything is written.
- **Python 3.9+, standard library only.** No installs, no API keys, nothing uploaded. Your Stripe
  data never leaves your computer.

## Quick start

1. **Export from Stripe:** Dashboard → **Reports** → **Payout reconciliation** → pick a date range →
   **Download** → report type **Itemized** → CSV. The default columns are fine.
2. **Run:**
   ```
   python3 stripe_payouts_to_qbo.py ~/Downloads/payout_reconciliation.csv
   ```
   This writes `quickbooks_journal_entries.csv` next to the input and prints a per-payout summary.
3. **Use your own account names** (they must match QBO exactly, or the import creates or flags them):
   ```
   python3 stripe_payouts_to_qbo.py export.csv \
       --bank "Checking:Operating" --sales "Sales:Online" --fees "Merchant Fees" \
       --out march_stripe_journals.csv
   ```
4. **Import into QBO:** ⚙ Settings → **Import data** → **Journal entries** → upload the CSV → map
   the columns (they use QBO's own names) → review → import.
5. **Match the deposits:** in Banking, each Stripe deposit now has a journal entry for the exact
   amount. Choose **Match** rather than "Add".

Windows: use `py` instead of `python3` if `python3` isn't found.

### All options

| Flag | Default | What it does |
|---|---|---|
| `--out PATH` | `quickbooks_journal_entries.csv` next to the input | Output file (extra parts get `_part2`, ...) |
| `--bank NAME` | `Checking` | Account the payout lands in |
| `--sales NAME` | `Sales` | Gross charges |
| `--refunds NAME` | `Refunds` | Refunds (use your contra-revenue account) |
| `--disputes NAME` | `Chargebacks` | Disputes and dispute reversals |
| `--fees NAME` | `Stripe Fees` | All Stripe fees (expense) |
| `--tax NAME` | `Sales Tax Payable` | Rows with reporting category `tax` |
| `--other NAME` | `Stripe Other Adjustments` | Anything Stripe labels otherwise (reserves, transfers, adjustments) |
| `--clearing NAME` | `Stripe Clearing` | Used only with `--include-unpaid` |
| `--include-unpaid` | off | Also book activity not yet paid out, with its net going to Stripe Clearing |
| `--prefix TEXT` | `STRIPE` | Journal number prefix (`STRIPE-20260306-1`) |
| `--date-format FMT` | `%m/%d/%Y` | e.g. `%d/%m/%Y` for UK/AU/CA QBO files |

## Worked example (the included sample data)

`samples/sample_payout_reconciliation_itemized.csv` is a made-up Stripe export with two payouts and
one charge that hasn't been paid out yet:

| Payout | Activity in it | Stripe `net` |
|---|---|---:|
| `po_sample_A` (Mar 6) | 3 charges ($120, $45, $300), a $45 refund, a $0.50 Stripe billing fee | **$405.11** |
| `po_sample_B` (Mar 13) | 2 charges ($89, $250), a $120 chargeback with a $15 dispute fee, $12.60 sales tax collected | **$206.17** |
| (not paid out) | 1 charge ($60) | $57.96 |

```
$ python3 stripe_payouts_to_qbo.py samples/sample_payout_reconciliation_itemized.csv --out out.csv
Date        Payout                        Gross sales       Fees      Other          Net
2026-03-06  po_sample_A                        465.00      14.89     -45.00       405.11  USD
2026-03-13  po_sample_B                        339.00      25.43    -107.40       206.17  USD

Note: 1 transaction(s) have no automatic payout yet (still in your Stripe balance, or paid by a manual payout).
Note: Not-yet-paid-out activity was left out of the import (it will show up under its payout in a later export). Use --include-unpaid to book it to Stripe Clearing now.
Wrote out.csv
2 journal entries, every one balanced. In QBO: Settings > Import data > Journal entries. Then match each bank deposit to its entry.
```

The journal entry for `po_sample_B`:

| Account | Debit | Credit |
|---|---:|---:|
| Sales | | 339.00 |
| Chargebacks | 120.00 | |
| Sales Tax Payable | | 12.60 |
| Stripe Fees (2.88 + 7.55 + 15.00 dispute fee) | 25.43 | |
| **Checking** | **206.17** | |
| **Total** | **351.60** | **351.60** |

The $206.17 checking line is exactly the deposit on the bank statement, so you match it in QBO's bank feed.
The file QBO imports is in [`samples/expected_output/quickbooks_journal_entries.csv`](samples/expected_output/quickbooks_journal_entries.csv):

```
Journal No.,Journal Date,Account Name,Debits,Credits,Description,Name,Currency
STRIPE-20260313-2,03/13/2026,Sales,,339.00,Gross sales - Stripe payout po_sample_B,,USD
STRIPE-20260313-2,03/13/2026,Chargebacks,120.00,,Disputes / chargebacks - Stripe payout po_sample_B,,USD
STRIPE-20260313-2,03/13/2026,Sales Tax Payable,,12.60,Tax collected - Stripe payout po_sample_B,,USD
STRIPE-20260313-2,03/13/2026,Stripe Fees,25.43,,Stripe fees - Stripe payout po_sample_B,,USD
STRIPE-20260313-2,03/13/2026,Checking,206.17,,Payout deposit - Stripe payout po_sample_B,,USD
```

## How it works

1. Reads the Itemized payout reconciliation CSV and groups rows by `automatic_payout_id`, plus
   currency. A refund of an older sale lands in the payout it was actually deducted from, which
   is exactly why grouping by payout beats grouping by date.
2. For each payout, it totals `gross` by Stripe `reporting_category`:
   - charges go to Sales;
   - refunds go to Refunds;
   - disputes go to Chargebacks;
   - `tax` goes to Sales Tax Payable;
   - anything else goes to Other.

   Money into Stripe is a credit and money out is a debit.
3. All `fee` amounts, plus stand-alone fee rows, are debited to Stripe Fees.
4. The balancing debit is the payout itself (Σ net), booked to Checking. It is checked against the
   entry totals before writing.

## Caveats

- **Review before importing.** This prepares a file; it isn't accounting or tax advice.
- **If you already record sales elsewhere,** don't import these entries as-is or you'll count revenue
  twice. That applies to invoices paid via Stripe in QBO, or a Shopify/WooCommerce sync. In that
  setup you want a clearing-account workflow instead (see below).
- **Only the Payout reconciliation export works.** The *Balance change from activity* export has no
  payout IDs, so it's rejected with a message.
- **One file per run.** If two exports overlap, pick date ranges that don't, or merge them yourself.
- **Doesn't handle revenue recognition.** Deferred revenue on annual plans is a separate step.

## Tests

```
python3 -m unittest discover -s tests -v
```

## NetToGross (paid, $29)

This repo is the free core, and it's meant to be useful on its own. If you also need any of the
following, there's a paid version called **NetToGross**: https://yarbrough56.gumroad.com/l/gxlcqf

- **Xero** manual-journal import files (with Xero's 300-line split and clearing-account handling)
- **Bank matching:** give it your bank CSV and it pairs each payout with its deposit (±5 days), and
  flags payouts with no deposit and Stripe deposits with no payout
- **Tie-out report:** `report.html` plus `payout_tieout.csv` showing gross, refunds, disputes, fees and net per payout
- **Multiple exports at once,** with overlapping rows de-duplicated by Stripe transaction ID (handy for catch-up)
- **Clearing-account mode** for the *Balance change from activity* export: monthly, weekly or daily
  entries through Stripe Clearing, with payouts as transfers. This is useful when sales are already
  recorded elsewhere
- A **config file** for account names and category overrides, plus double-click launchers for Windows and Mac
- Step-by-step guides for the Stripe export, QBO import and Xero import

It costs $29 one-time for one business, or $59 for bookkeepers (unlimited client businesses). It
also runs offline, with no subscription.

## License

MIT, see [LICENSE](LICENSE). Stripe, QuickBooks and Xero are trademarks of their respective
owners. This project isn't affiliated with or endorsed by them.
