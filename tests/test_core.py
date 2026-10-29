from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from core import STATUS_LABELS, TaskStore, parse_datetime  # noqa: E402


class WorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = TaskStore(Path(self.temp.name) / "tasks.sqlite3")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_date_only_deadline_defaults_to_five_pm(self) -> None:
        task = self.store.create_task({"title": "收培训心得", "due": "2026-10-31"})
        self.assertEqual(task["due_at"], "2026-10-31T17:00:00+08:00")
        path = Path(task["path"])
        self.assertTrue(path.exists())
        content = path.read_text(encoding="utf-8")
        self.assertIn("status: open", content)
        self.assertIn("dingtalk_sync: true", content)

    def test_status_advances_one_stage_only_and_stops(self) -> None:
        task = self.store.create_task({"title": "培训心得", "due": "2026-10-31 10:00"})
        self.assertEqual(task["status"], "open")
        self.assertEqual(self.store.advance_task(task["id"])["status"], "notified")
        self.assertEqual(self.store.advance_task(task["id"])["status"], "followed-up")
        complete = self.store.advance_task(task["id"])
        self.assertEqual(complete["status"], "done")
        self.assertEqual(self.store.reminders_for_task(task["id"])[0]["state"], "closed")

    def test_notified_stage_reminds_at_eight_am_three_days_before_deadline(self) -> None:
        task = self.store.create_task({"title": "培训心得", "due": "2099-10-31 10:00"})
        next_task = self.store.advance_task(task["id"])
        reminders = self.store.reminders_for_task(task["id"])
        active = [r for r in reminders if r["state"] == "pending"]
        self.assertEqual(next_task["status"], "notified")
        self.assertEqual(active[0]["remind_at"], "2099-10-28T08:00:00+08:00")

    def test_source_id_detects_duplicate(self) -> None:
        self.store.create_task({"title": "A", "due": "2026-10-31", "source_id": "NOTICE-1"})
        result = self.store.find_possible_duplicates("B", "2026-11-01", source_id="NOTICE-1")
        self.assertEqual(len(result), 1)


if __name__ == "__main__":
    unittest.main()
