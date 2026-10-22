"""采集层的回归测试：用假客户端，不联网。"""

from __future__ import annotations

import json
import unittest

import collect


class FakeClient:
    tool_schemas: dict = {}

    def __init__(self, payload):
        self.payload = payload

    def call_tool(self, name, args):
        return {"content": [{"type": "text", "text": json.dumps(self.payload, ensure_ascii=False)}]}


class TestBackend(unittest.TestCase):
    def test_business_failure_raises(self):
        backend = collect.Backend(FakeClient({"success": False, "code": 600050, "message": "收藏餐厅列表为空"}))
        with self.assertRaises(RuntimeError):
            backend.call("query-meals", {})

    def test_success_passes_through(self):
        payload = {"success": True, "code": 200, "data": {"categories": []}}
        backend = collect.Backend(FakeClient(payload))
        self.assertEqual(backend.call("query-meals", {})["data"], {"categories": []})

    def test_schema_filters_unknown_args(self):
        class WithSchema(FakeClient):
            tool_schemas = {"t": {"properties": {"a": {"type": "string"}}}}

        backend = collect.Backend(WithSchema({"success": True, "data": {}}))
        captured = {}
        backend.client.call_tool = lambda name, args: (captured.update(args), {"content": []})[1]
        backend.call("t", {"a": "1", "b": "2"})
        self.assertEqual(captured, {"a": "1"})


class TestSampleStore(unittest.TestCase):
    def test_empty_menu_is_recorded_as_zero(self):
        payload = {"success": True, "code": 200, "data": {"categories": []}}
        store = {"code": "S1", "name": "店", "hours": "07:00-23:00"}
        sample = collect.sample_store(collect.Backend(FakeClient(payload)), store, {})
        self.assertEqual(sample["item_count"], 0)
        self.assertEqual(sample["categories"], [])
        self.assertEqual(sample["hours"], "07:00-23:00")

    def test_failed_menu_raises_instead_of_zero(self):
        payload = {"success": False, "code": 429, "message": "限流"}
        store = {"code": "S1", "name": "店"}
        with self.assertRaises(RuntimeError):
            collect.sample_store(collect.Backend(FakeClient(payload)), store, {})


class TestConfig(unittest.TestCase):
    def test_cli_overrides_config(self):
        args = collect.build_parser().parse_args(["--city", "成都", "--max-stores", "3"])
        config = collect.load_config(args)
        self.assertEqual(config["city"], "成都")
        self.assertEqual(config["max_stores"], 3)

    def test_stores_parsing(self):
        args = collect.build_parser().parse_args(["--stores", "1, 2 ,3"])
        config = collect.load_config(args)
        self.assertEqual(config["stores"], ["1", "2", "3"])


if __name__ == "__main__":
    unittest.main()
