"""渲染逻辑的回归测试：全部用合成数据，不联网。"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta

import render_html as rh

TZ = datetime.now().astimezone().tzinfo
BASE = datetime(2026, 10, 22, 8, 0, tzinfo=TZ)
STEP = timedelta(minutes=10)
META = {"S1": {"code": "S1", "name": "测试店", "hours": "07:00-23:00", "distance_m": 400}}


def row(ts, categories, store="S1", item_count=None):
    return {
        "ts": ts.isoformat(),
        "samples": [
            {
                "store": store,
                "item_count": item_count if item_count is not None else sum(categories.values()),
                "hours": "07:00-23:00",
                "categories": [{"name": name, "codes": [str(i) for i in range(count)]} for name, count in categories.items()],
            }
        ],
    }


class TestSwitchPoints(unittest.TestCase):
    def test_detects_real_switch(self):
        series = [
            (BASE, {"早餐": 10, "饮品": 5}),
            (BASE + STEP, {"早餐": 10, "饮品": 5}),
            (BASE + STEP * 2, {"正餐": 20, "饮品": 5}),
        ]
        points = rh.switch_points(series, STEP)
        self.assertEqual(len(points), 1)
        self.assertEqual(points[0][1:], (1, 1))

    def test_ignores_sampling_gap(self):
        series = [
            (BASE, {"早餐": 10, "饮品": 5}),
            (BASE + STEP, {"早餐": 10, "饮品": 5}),
            (BASE + timedelta(hours=3), {"正餐": 20, "饮品": 5}),
        ]
        self.assertEqual(rh.switch_points(series, STEP), [])


class TestSegments(unittest.TestCase):
    def test_one_segment_when_menu_stable(self):
        series = [(BASE + STEP * i, {"早餐": 5}) for i in range(4)]
        segs = rh.segments(series, STEP)
        self.assertEqual(len(segs), 1)
        self.assertEqual(segs[0][2], 5)

    def test_splits_on_menu_change(self):
        series = [
            (BASE, {"早餐": 5}),
            (BASE + STEP, {"早餐": 5}),
            (BASE + STEP * 2, {"正餐": 9}),
        ]
        segs = rh.segments(series, STEP)
        self.assertEqual([s[2] for s in segs], [5, 9])


class TestTimetable(unittest.TestCase):
    def test_renders_rows(self):
        rows = [row(BASE + STEP * i, {"早餐": 2}) for i in range(3)]
        svg = rh.timetable_svg(rows, META, STEP)
        self.assertIn("<svg", svg)
        self.assertIn("测试店", svg)
        self.assertIn("2 款", svg)

    def test_no_data(self):
        self.assertIn("还没有采样数据", rh.timetable_svg([], META, STEP))


class TestCategoryChart(unittest.TestCase):
    def test_renders(self):
        series = [(BASE, {"早餐": 5}), (BASE + STEP, {"早餐": 5})]
        svg = rh.category_chart(series, STEP, "测试店")
        self.assertIn("<svg", svg)
        self.assertIn("早餐", svg)

    def test_empty(self):
        self.assertEqual(rh.category_chart([], STEP, "测试店"), "")


class TestHelpers(unittest.TestCase):
    def test_hours_window(self):
        self.assertEqual(rh.hours_window("07:00-23:00"), (420, 1380))
        self.assertIsNone(rh.hours_window(""))
        self.assertIsNone(rh.hours_window("23:00-07:00"))

    def test_step_of_uses_median_gap(self):
        series = [(BASE + STEP * i, {}) for i in range(5)]
        self.assertEqual(rh.step_of(series), STEP)

    def test_step_of_falls_back(self):
        self.assertEqual(rh.step_of([]), timedelta(minutes=10))


if __name__ == "__main__":
    unittest.main()
