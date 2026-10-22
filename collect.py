"""采一次样，记录门店此刻在售的菜单。

用法：
    python collect.py                                  # 读 config.json
    python collect.py --city 上海 --keyword 人民广场    # 临时换地方
    python collect.py --stores 1420944,1421217         # 直接指定门店编码

不给门店编码时，按 city + keyword 自动找最近的几家。
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime
from pathlib import Path

from mcp_client import McpClient, tool_payload

ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = {
    "city": "深圳",
    "keyword": "深大南",
    "stores": [],
    "max_stores": 2,
    "data_dir": "data",
}


def token() -> str:
    value = os.environ.get("MCD_MCP_TOKEN", "").strip()
    if value:
        return value
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if line.startswith("MCD_MCP_TOKEN="):
                return line.split("=", 1)[1].strip().strip("'\"")
    raise SystemExit("没有 MCD_MCP_TOKEN：设环境变量，或写在项目根目录的 .env 里")


class Backend:
    def __init__(self, client: McpClient):
        self.client = client

    def call(self, tool: str, args: dict | None = None):
        args = args or {}
        schema = (self.client.tool_schemas.get(tool) or {}).get("properties") or {}
        if schema:
            args = {k: v for k, v in args.items() if k in schema}
        payload = tool_payload(self.client.call_tool(tool, args))
        # 业务失败（success=false）必须报错，不能当成"菜单为空"，否则会被画成打烊
        if isinstance(payload, dict) and payload.get("success") is False:
            raise RuntimeError(f"{tool} 返回失败：{payload.get('message') or payload.get('code')}")
        return payload


# ------------------------------------------------------------------ 配置与门店
def load_config(args: argparse.Namespace) -> dict:
    config = dict(DEFAULT_CONFIG)
    path = Path(args.config) if args.config else ROOT / "config.json"
    if path.exists():
        config.update(json.loads(path.read_text(encoding="utf-8")))
    for key in ("city", "keyword", "data_dir"):
        value = getattr(args, key, None)
        if value:
            config[key] = value
    if getattr(args, "max_stores", None):
        config["max_stores"] = args.max_stores
    if getattr(args, "stores", None):
        config["stores"] = [s.strip() for s in args.stores.split(",") if s.strip()]
    return config


def resolve_stores(backend: Backend, config: dict, data_dir: Path) -> list[dict]:
    """有配置就用配置，没有就按 city + keyword 自己找。结果缓存在 data/stores.json。"""
    cache = data_dir / "stores.json"
    if config.get("stores"):
        known = {}
        if cache.exists():
            known = {s["code"]: s for s in json.loads(cache.read_text(encoding="utf-8"))}
        return [known.get(code, {"code": code, "name": code, "hours": ""}) for code in config["stores"]]

    if not config.get("city"):
        raise SystemExit("config.json 里既没有 stores 也没有 city —— 不知道去哪儿采")

    rows = backend.call(
        "query-nearby-stores",
        {"beType": 1, "searchType": 2, "city": config["city"], "keyword": config.get("keyword", "")},
    )
    rows = rows.get("data") if isinstance(rows, dict) else rows
    found = []
    for row in (rows or [])[: config.get("max_stores", 2)]:
        if not row.get("storeCode"):
            continue
        start = row.get("businessStartTime", "")
        end = row.get("businessEndTime", "")
        found.append(
            {
                "code": row["storeCode"],
                "name": row.get("storeName", row["storeCode"]),
                "hours": f"{start}-{end}" if start and end else "",
                "distance_m": row.get("distance", 0),
                "city": config.get("city", ""),
            }
        )
    if not found:
        raise SystemExit(f'{config.get("city")} / {config.get("keyword")} 没有搜到门店')
    cache.write_text(json.dumps(found, ensure_ascii=False, indent=2), encoding="utf-8")
    return found


# ---------------------------------------------------------------------- 采样
def load_names(path: Path) -> dict[str, str]:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def sample_store(backend: Backend, store: dict, names: dict[str, str]) -> dict:
    data = backend.call("query-meals", {"storeCode": store["code"], "orderType": 1, "beType": 1})
    data = data.get("data") if isinstance(data, dict) else data
    categories = []
    for category in (data or {}).get("categories", []) or []:
        categories.append(
            {
                "name": category.get("name", "").replace("\n", " ").strip(),
                "codes": [m["code"] for m in category.get("meals", []) or [] if m.get("code")],
            }
        )

    unknown = [c for cat in categories for c in cat["codes"] if c not in names]
    for code in dict.fromkeys(unknown):
        try:
            detail = backend.call(
                "query-meal-detail",
                {"storeCode": store["code"], "orderType": 1, "beType": 1, "code": code},
            )
            detail = detail.get("data") if isinstance(detail, dict) else detail
            if isinstance(detail, dict) and detail.get("name"):
                names[code] = detail["name"]
        except Exception:
            pass

    return {
        "store": store["code"],
        "store_name": store.get("name", store["code"]),
        "hours": store.get("hours", ""),
        "categories": categories,
        "item_count": sum(len(c["codes"]) for c in categories),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="采样麦当劳门店的在售菜单")
    parser.add_argument("--city", help="城市，如 深圳")
    parser.add_argument("--keyword", default="", help="区域关键词，如 深大南")
    parser.add_argument("--stores", help="直接指定门店编码，逗号分隔（跳过自动搜索）")
    parser.add_argument("--max-stores", type=int, help="自动搜索时取最近几家，默认 2")
    parser.add_argument("--data-dir", help="数据目录，默认 data")
    parser.add_argument("--config", help="配置文件路径，默认 ./config.json")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    config = load_config(args)
    data_dir = ROOT / config["data_dir"]
    data_dir.mkdir(parents=True, exist_ok=True)

    client = McpClient(token())
    client.initialize()
    client.list_tools()
    backend = Backend(client)

    stores = resolve_stores(backend, config, data_dir)
    names = load_names(data_dir / "names.json")

    now = datetime.now().astimezone()
    samples = []
    for store in stores:
        try:
            samples.append(sample_store(backend, store, names))
        except Exception as exc:
            samples.append({"store": store["code"], "store_name": store.get("name", ""), "error": str(exc)[:200]})

    record = {"ts": now.isoformat(timespec="seconds"), "samples": samples}
    with (data_dir / "samples.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    (data_dir / "names.json").write_text(json.dumps(names, ensure_ascii=False, indent=0), encoding="utf-8")

    summary = ", ".join(f"{s['store_name']}={s.get('item_count', 'ERR')}" for s in samples)
    print(f"{now.strftime('%m-%d %H:%M')} {summary} 缓存名称 {len(names)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
