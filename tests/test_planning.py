import datetime as dt
import os
import sys
import tempfile
import unittest
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "octoprint_pianifica"))
import planning  # noqa: E402

ROME = ZoneInfo("Europe/Rome")


class ParseTests(unittest.TestCase):
    def test_parse_duration(self):
        self.assertEqual(planning.parse_duration("1h 11m 20s"), 4280)
        self.assertEqual(planning.parse_duration("37m 48s"), 2268)
        self.assertEqual(planning.parse_duration("1d 2h"), 93600)
        self.assertIsNone(planning.parse_duration("boh"))

    def test_estimate_prusaslicer(self):
        text = "G1 X1\n; estimated printing time (normal mode) = 1h 0m 4s\n; foo = 1\n"
        self.assertEqual(planning.estimate_from_gcode_text(text), 3604)

    def test_estimate_cura(self):
        self.assertEqual(planning.estimate_from_gcode_text(";FLAVOR:Marlin\n;TIME:3723\n"), 3723)

    def test_estimate_from_file_tail(self):
        with tempfile.NamedTemporaryFile("w", suffix=".gcode", delete=False) as f:
            f.write("G1 X1 Y1\n" * 100000)
            f.write("; estimated printing time (normal mode) = 28m 21s\n")
        try:
            self.assertEqual(planning.estimate_from_file(f.name), 1701)
        finally:
            os.unlink(f.name)


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.now = dt.datetime(2026, 11, 22, 14, 0, tzinfo=ROME)

    def test_end_today(self):
        end = planning.parse_end_time("16:00", self.now)
        self.assertEqual(end, dt.datetime(2026, 11, 22, 16, 0, tzinfo=ROME))

    def test_end_tomorrow_if_past(self):
        end = planning.parse_end_time("9:30", self.now)
        self.assertEqual(end, dt.datetime(2026, 11, 23, 9, 30, tzinfo=ROME))

    def test_bad_time(self):
        with self.assertRaises(planning.PlanError):
            planning.parse_end_time("25:00", self.now)
        with self.assertRaises(planning.PlanError):
            planning.parse_end_time("sedici", self.now)

    def test_plan_start(self):
        end = planning.parse_end_time("16:00", self.now)
        plan = planning.make_plan(2268, end, self.now, margin_percent=5, heatup_minutes=4)
        # 2268 * 1.05 + 240 = 2621 s -> partenza 15:16:19
        self.assertEqual(plan.planned_s, 2621)
        self.assertEqual(plan.start, dt.datetime(2026, 11, 22, 15, 16, 19, tzinfo=ROME))
        d = plan.to_dict(self.now)
        self.assertEqual(d["start_label"], "oggi alle 15:16")
        self.assertEqual(d["end_label"], "oggi alle 16:00")

    def test_too_late(self):
        end = planning.parse_end_time("14:20", self.now)
        with self.assertRaises(planning.PlanError) as cm:
            planning.make_plan(2268, end, self.now)
        self.assertIn("Troppo tardi", str(cm.exception))

    def test_dst_change(self):
        # Notte del cambio ora (25/10/2026, alle 3 si torna alle 2): una stampa
        # di 4 ore che finisce alle 5:00 deve partire alle 2:00 ora legale,
        # non all'1:00 come darebbe la sottrazione "da orologio".
        now = dt.datetime(2026, 10, 24, 23, 0, tzinfo=ROME)
        end = planning.parse_end_time("2026-10-25T05:00", now)
        plan = planning.make_plan(14400, end, now, margin_percent=0, heatup_minutes=0)
        self.assertEqual(end.timestamp() - plan.start.timestamp(), 14400)
        self.assertEqual((plan.start.hour, plan.start.utcoffset()), (2, dt.timedelta(hours=2)))


if __name__ == "__main__":
    unittest.main()
