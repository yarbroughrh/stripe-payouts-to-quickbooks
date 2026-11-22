import csv
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from decimal import Decimal

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import stripe_payouts_to_qbo as s2q  # noqa: E402

SAMPLE = os.path.join(ROOT, "samples", "sample_payout_reconciliation_itemized.csv")
EXPECTED = os.path.join(ROOT, "samples", "expected_output", "quickbooks_journal_entries.csv")

HEADER = ("balance_transaction_id,created_utc,currency,gross,fee,net,reporting_category,"
          "automatic_payout_id,automatic_payout_effective_at\n")


def write(tmp, name, text):
    path = os.path.join(tmp, name)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)
    return path


def run_quiet(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = s2q.run(argv)
    return code, out.getvalue(), err.getvalue()


def read_rows(path):
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


class ParseTests(unittest.TestCase):
    def test_amounts(self):
        self.assertEqual(s2q.parse_amount("1,234.56"), Decimal("1234.56"))
        self.assertEqual(s2q.parse_amount("(12.00)"), Decimal("-12.00"))
        self.assertEqual(s2q.parse_amount("$5"), Decimal("5"))
        self.assertEqual(s2q.parse_amount(""), Decimal("0"))
        with self.assertRaises(s2q.InputError):
            s2q.parse_amount("abc")

    def test_dates(self):
        self.assertEqual(str(s2q.parse_date("2026-03-06 00:00:00")), "2026-03-06")
        self.assertEqual(str(s2q.parse_date("2026-03-06T10:00:00Z")), "2026-03-06")
        self.assertEqual(str(s2q.parse_date("03/06/2026")), "2026-03-06")
        self.assertIsNone(s2q.parse_date(""))


class SampleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = os.path.join(self.tmp.name, "qbo.csv")

    def tearDown(self):
        self.tmp.cleanup()

    def test_sample_matches_expected_output(self):
        code, _, _ = run_quiet([SAMPLE, "--out", self.out])
        self.assertEqual(code, 0)
        with open(self.out, encoding="utf-8") as a, open(EXPECTED, encoding="utf-8") as b:
            self.assertEqual(a.read(), b.read())

    def test_every_entry_balances_and_bank_line_equals_payout(self):
        run_quiet([SAMPLE, "--out", self.out])
        rows = read_rows(self.out)
        by_entry = {}
        for r in rows:
            by_entry.setdefault(r["Journal No."], []).append(r)
        self.assertEqual(len(by_entry), 2)
        bank = {}
        for no, lines in by_entry.items():
            dr = sum(Decimal(r["Debits"] or "0") for r in lines)
            cr = sum(Decimal(r["Credits"] or "0") for r in lines)
            self.assertEqual(dr, cr, no)
            bank[no] = sum(Decimal(r["Debits"] or "0") for r in lines if r["Account Name"] == "Checking")
        # Sum of Stripe 'net' per payout in the sample file.
        self.assertEqual(sorted(bank.values()), [Decimal("206.17"), Decimal("405.11")])

    def test_sample_fee_dispute_and_tax_lines(self):
        run_quiet([SAMPLE, "--out", self.out])
        rows = [r for r in read_rows(self.out) if r["Journal No."].endswith("-2")]
        acct = {r["Account Name"]: (r["Debits"], r["Credits"]) for r in rows}
        self.assertEqual(acct["Sales"], ("", "339.00"))
        self.assertEqual(acct["Chargebacks"], ("120.00", ""))
        self.assertEqual(acct["Sales Tax Payable"], ("", "12.60"))
        self.assertEqual(acct["Stripe Fees"], ("25.43", ""))  # 2.88 + 7.55 + 15.00 dispute fee
        self.assertEqual(acct["Checking"], ("206.17", ""))

    def test_unpaid_excluded_by_default_and_included_on_request(self):
        run_quiet([SAMPLE, "--out", self.out])
        self.assertNotIn("Stripe Clearing", {r["Account Name"] for r in read_rows(self.out)})
        run_quiet([SAMPLE, "--out", self.out, "--include-unpaid"])
        rows = read_rows(self.out)
        clearing = [r for r in rows if r["Account Name"] == "Stripe Clearing"]
        self.assertEqual(len(clearing), 1)
        self.assertEqual(clearing[0]["Debits"], "57.96")

    def test_custom_account_names_and_date_format(self):
        run_quiet([SAMPLE, "--out", self.out, "--bank", "Checking:Operating", "--fees", "Merchant Fees",
                   "--date-format", "%d/%m/%Y"])
        rows = read_rows(self.out)
        names = {r["Account Name"] for r in rows}
        self.assertIn("Checking:Operating", names)
        self.assertIn("Merchant Fees", names)
        self.assertEqual(rows[0]["Journal Date"], "06/03/2026")


class EdgeCaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = os.path.join(self.tmp.name, "qbo.csv")

    def tearDown(self):
        self.tmp.cleanup()

    def test_negative_payout_credits_bank(self):
        p = write(self.tmp.name, "neg.csv", HEADER +
                  "t1,2026-01-01,usd,10.00,0.59,9.41,charge,po_1,2026-01-03\n"
                  "t2,2026-01-02,usd,-50.00,0.00,-50.00,refund,po_1,2026-01-03\n")
        self.assertEqual(run_quiet([p, "--out", self.out])[0], 0)
        bank = [r for r in read_rows(self.out) if r["Account Name"] == "Checking"][0]
        self.assertEqual((bank["Debits"], bank["Credits"]), ("", "40.59"))

    def test_payout_rows_and_duplicates_are_not_double_counted(self):
        p = write(self.tmp.name, "dup.csv", HEADER +
                  "t1,2026-01-01,usd,100.00,3.20,96.80,charge,po_1,2026-01-03\n"
                  "t1,2026-01-01,usd,100.00,3.20,96.80,charge,po_1,2026-01-03\n"
                  "t2,2026-01-03,usd,-96.80,0.00,-96.80,payout,po_1,2026-01-03\n")
        code, out, _ = run_quiet([p, "--out", self.out])
        self.assertEqual(code, 0)
        self.assertIn("duplicate", out)
        bank = [r for r in read_rows(self.out) if r["Account Name"] == "Checking"][0]
        self.assertEqual(bank["Debits"], "96.80")

    def test_qbo_files_split_without_splitting_entries(self):
        lines = [HEADER]
        for i in range(400):  # 400 payouts x 3 lines = 1,200 rows -> 2 files
            lines.append(f"t{i},2026-01-01,usd,10.00,0.59,9.41,charge,po_{i:04d},2026-01-03\n")
            lines.append(f"r{i},2026-01-01,usd,-1.00,0.00,-1.00,refund,po_{i:04d},2026-01-03\n")
        p = write(self.tmp.name, "big.csv", "".join(lines))
        self.assertEqual(run_quiet([p, "--out", self.out])[0], 0)
        part2 = self.out.replace(".csv", "_part2.csv")
        self.assertTrue(os.path.exists(part2))
        a, b = read_rows(self.out), read_rows(part2)
        self.assertLessEqual(len(a), s2q.QBO_MAX_DATA_ROWS)
        self.assertEqual(len(a) + len(b), 1600)
        self.assertFalse({r["Journal No."] for r in a} & {r["Journal No."] for r in b})

    def test_type_column_and_header_aliases(self):
        p = write(self.tmp.name, "old.csv",
                  "id,Created (UTC),Currency,Amount,Fee,Net,Type,Transfer,Transfer Date (UTC)\n"
                  "t1,2026-01-01,usd,20.00,0.88,19.12,charge,po_x,2026-01-04\n"
                  "t2,2026-01-01,usd,-2.00,0.00,-2.00,stripe_fee,po_x,2026-01-04\n")
        self.assertEqual(run_quiet([p, "--out", self.out])[0], 0)
        acct = {r["Account Name"]: (r["Debits"], r["Credits"]) for r in read_rows(self.out)}
        self.assertEqual(acct["Stripe Fees"], ("2.88", ""))
        self.assertEqual(acct["Checking"], ("17.12", ""))

    def test_formula_injection_is_neutralised(self):
        self.assertEqual(s2q._safe("=HYPERLINK()"), "'=HYPERLINK()")
        self.assertEqual(s2q._safe("Gross sales"), "Gross sales")

    def test_wrong_file_gives_clear_error(self):
        p = write(self.tmp.name, "bank.csv", "Date,Description,Amount\n2026-01-01,Coffee,-4.00\n")
        code, _, err = run_quiet([p, "--out", self.out])
        self.assertEqual(code, 2)
        self.assertIn("Itemized", err)

    def test_balance_change_export_without_payout_ids_is_rejected(self):
        p = write(self.tmp.name, "bal.csv", "balance_transaction_id,created_utc,currency,gross,fee,net,reporting_category\n"
                  "t1,2026-01-01,usd,10,0.59,9.41,charge\n")
        code, _, err = run_quiet([p, "--out", self.out])
        self.assertEqual(code, 2)
        self.assertIn("automatic_payout_id", err)

    def test_multi_currency_payouts_are_separate_entries(self):
        p = write(self.tmp.name, "fx.csv", HEADER +
                  "t1,2026-01-01,usd,10.00,0.59,9.41,charge,po_1,2026-01-03\n"
                  "t2,2026-01-01,eur,10.00,0.50,9.50,charge,po_2,2026-01-03\n")
        run_quiet([p, "--out", self.out])
        rows = read_rows(self.out)
        self.assertEqual({r["Currency"] for r in rows}, {"USD", "EUR"})
        self.assertEqual(len({r["Journal No."] for r in rows}), 2)


if __name__ == "__main__":
    unittest.main()
