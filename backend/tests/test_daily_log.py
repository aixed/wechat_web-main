from datetime import datetime
from pathlib import Path
import tempfile
import unittest

import daily_log


class DailyLogTests(unittest.TestCase):
    def test_entries_are_split_by_category_and_day(self):
        temp_dir = tempfile.TemporaryDirectory()
        old_root = daily_log.LOG_ROOT
        try:
            daily_log.LOG_ROOT = Path(temp_dir.name)
            first_day = datetime(2026, 8, 4, 23, 59, 59)
            next_day = datetime(2026, 8, 5, 0, 0, 1)

            daily_log.append_daily_log("main", "main entry", when=first_day)
            daily_log.append_daily_log("smart_reply", "reply entry", when=first_day)
            daily_log.append_daily_log("main", "next entry", when=next_day)
            self.assertTrue(daily_log.flush_daily_logs())

            self.assertEqual(
                "main entry\n",
                (Path(temp_dir.name) / "main" / "2026-08-04.log").read_text(encoding="utf-8"),
            )
            self.assertEqual(
                "reply entry\n",
                (Path(temp_dir.name) / "smart_reply" / "2026-08-04.log").read_text(encoding="utf-8"),
            )
            self.assertEqual(
                "next entry\n",
                (Path(temp_dir.name) / "main" / "2026-08-05.log").read_text(encoding="utf-8"),
            )
        finally:
            daily_log.LOG_ROOT = old_root
            temp_dir.cleanup()

    def test_invalid_category_is_ignored(self):
        daily_log.append_daily_log("../outside", "ignored")
        self.assertTrue(daily_log.flush_daily_logs())


if __name__ == "__main__":
    unittest.main()
