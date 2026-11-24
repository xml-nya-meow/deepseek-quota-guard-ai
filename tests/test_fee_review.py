from dataclasses import replace
from decimal import Decimal
import tkinter as tk
import unittest

from quota_guard.fee_review import FeeReviewData, FeeReviewFrame


def ledger_row(index, *, status="ok", amount=10, spent=5, complete=True):
    return dict(name=f"示例 {index}", tracking_id=f"synthetic-key-{index:03}",
                amount=Decimal(amount), share=Decimal(20),
                spent=Decimal(spent) if spent is not None else None,
                status=status, complete=complete, spent_is_lower_bound=spent is not None and not complete)


def review(rows):
    return FeeReviewData(tuple(rows), Decimal(101), 5, Decimal(20), "各 Key 创建以来",
                         "合成账户", "合成采样日期", True, True)


class FeeReviewTests(unittest.TestCase):
    def test_current_unknown_lower_bound_and_public_zero_follow_real_rules(self):
        rows = [ledger_row(1, status="triggered", spent=11),
                ledger_row(2, status="triggered", spent=11, complete=False),
                ledger_row(3, status="triggered", amount=0, spent=None, complete=False),
                ledger_row(4, status="skipped", amount=-1),
                dict(ledger_row(5, status="unassigned", amount=0), name="", tracking_id="")]
        data = review(rows)
        self.assertEqual(data.counts(), (3, 2, 2, 2))
        self.assertIn("下界", data.expense(rows[1]))
        self.assertEqual(data.state(rows[1]), "费用待补全")
        self.assertEqual(data.expense(rows[2]), "未知")
        self.assertEqual(data.expense(dict(rows[3], spent=None)), "未查询")
        self.assertEqual(data.expense(dict(rows[4], spent=None)), "未查询")
        self.assertIn("历史", data.expense(rows[3]))
        identity_only = replace(data, fees_current=False)
        self.assertEqual(identity_only.counts(), (3, 2, 3, 1))
        self.assertIn("历史", identity_only.expense(rows[0]))
        self.assertEqual(replace(identity_only, identity_current=False).counts()[-1], 0)
        self.assertTrue(identity_only.candidate(dict(rows[1], share=Decimal(0))))
        self.assertFalse(identity_only.candidate(dict(rows[2], tracking_id="")))

    def test_large_filtered_table_preserves_only_the_selected_full_identity(self):
        root = tk.Tk()
        root.withdraw()
        self.addCleanup(root.destroy)
        frame = FeeReviewFrame(root)
        rows = [ledger_row(index) for index in range(100)]
        rows[12] = dict(rows[12], status="triggered", spent=Decimal(11),
                        start_date="2026-10-03", end_date="2026-11-24")
        rows[18] = dict(rows[12], tracking_id=rows[18]["tracking_id"],
                        name=rows[18]["name"], spent_is_lower_bound=True, complete=False)
        frame.show(review(rows))
        frame.search.set("KEY-012")
        self.assertEqual(frame.tree.get_children(), ("12",))
        frame.tree.selection_set("12")
        frame.tree.event_generate("<<TreeviewSelect>>")
        root.update()
        self.assertEqual(frame.target_id.get(), "synthetic-key-012")
        self.assertIn("2026-10-03 至 2026-11-24", frame.detail.get())
        frame.show(review(reversed(rows)))
        self.assertEqual(frame.target_id.get(), "synthetic-key-012")
        self.assertEqual(frame.tree.selection(), ("87",))
        frame.search.set("")
        frame.filter.set("费用待补全")
        self.assertEqual(frame.tree.get_children(), ("81",))
        self.assertEqual(frame.tree.item("81", "tags"), ("unknown",))
        self.assertEqual(frame.target_id.get(), "")
