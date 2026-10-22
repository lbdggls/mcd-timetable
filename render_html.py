"""把采样数据渲染成一张静态页面 index.html。

页面只讲一件事：麦当劳的菜单一天换几次班。
主体是一张"时刻表"：一行一家店，横向 24 小时，色块就是那一版菜单。
纯 Python 拼 SVG，不依赖第三方库，也不需要浏览器 JS 才能看懂。
"""

from __future__ import annotations

import argparse
import html
import json
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
W = 1180
PAD_L, ROW_H, PAD_T, PAD_B = 196, 34, 64, 42


# ------------------------------------------------------------------ 数据读入
def load(data_dir: Path) -> list[dict]:
    path = data_dir / "samples.jsonl"
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    rows.sort(key=lambda r: r["ts"])
    return rows


def parse_ts(text: str) -> datetime:
    return datetime.fromisoformat(text)


def load_meta(data_dir: Path) -> dict[str, dict]:
    path = data_dir / "stores.json"
    if not path.exists():
        return {}
    return {item["code"]: item for item in json.loads(path.read_text(encoding="utf-8"))}


def store_series(rows: list[dict], code: str) -> list[tuple[datetime, dict[str, int]]]:
    series = []
    for row in rows:
        for sample in row.get("samples", []):
            if sample.get("store") != code or "error" in sample:
                continue
            counts = {c["name"]: len(c["codes"]) for c in sample.get("categories", [])}
            series.append((parse_ts(row["ts"]), counts))
    return series


# ------------------------------------------------------------------ 基本派生
def step_of(series, fallback=timedelta(minutes=10)):
    gaps = sorted((b[0] - a[0]).total_seconds() for a, b in zip(series, series[1:]))
    if not gaps:
        return fallback
    return timedelta(seconds=max(60, gaps[len(gaps) // 2]))


def segments(series, step):
    """把连续采样压成"菜单版本"段：(开始, 结束, 款数, 分类签名)。"""
    out = []
    if not series:
        return out
    start = series[0][0]
    signature = None
    total = 0
    last_ts = series[0][0]
    for ts, counts in series:
        sig = tuple(sorted((k, v) for k, v in counts.items() if v > 0))
        now_total = sum(v for _, v in sig)
        if signature is None:
            start, signature, total = ts, sig, now_total
        elif sig != signature or (ts - last_ts) > step * 2.5:
            out.append((start, last_ts + step, total, signature))
            start, signature, total = ts, sig, now_total
        last_ts = ts
    out.append((start, last_ts + step, total, signature))
    return out


def switch_points(series, step):
    """(时刻, 下架分类数, 上架分类数)；采样中断就跳过。"""
    points = []
    for (t0, c0), (t1, c1) in zip(series, series[1:]):
        if (t1 - t0) > step * 2.5:
            continue
        gone = sum(1 for k, v in c0.items() if v > 0 and c1.get(k, 0) == 0)
        new = sum(1 for k, v in c1.items() if v > 0 and c0.get(k, 0) == 0)
        if gone + new >= 2:
            points.append((t1, gone, new))
    return points


def runs(series, category, step):
    out, start, peak, last = [], None, 0, None
    for ts, counts in series:
        value = counts.get(category, 0)
        if last is not None and (ts - last) > step * 2.5 and start is not None:
            out.append((start, last + step, peak))
            start, peak = None, 0
        if value > 0:
            if start is None:
                start, peak = ts, value
            peak = max(peak, value)
        elif start is not None:
            out.append((start, ts, peak))
            start, peak = None, 0
        last = ts
    if start is not None and last is not None:
        out.append((start, last + step, peak))
    return out


def hours_window(text):
    try:
        start, end = text.split("-")
        h1, m1 = map(int, start.split(":"))
        h2, m2 = map(int, end.split(":"))
    except (ValueError, AttributeError):
        return None
    a, b = h1 * 60 + m1, h2 * 60 + m2
    return (a, b) if b > a else None


def business_hours(rows, code):
    for row in reversed(rows):
        for sample in row.get("samples", []):
            if sample.get("store") == code and sample.get("hours"):
                return sample["hours"]
    return ""


# ------------------------------------------------------------------ 时刻表
def timetable_svg(rows, meta, step) -> str:
    series_map = {code: store_series(rows, code) for code in meta if store_series(rows, code)}
    if not series_map:
        return '<p class="muted">还没有采样数据。</p>'

    all_ts = [ts for series in series_map.values() for ts, _ in series]
    days = sorted({ts.date() for ts in all_ts})
    overall_span = (max(all_ts) - min(all_ts)).total_seconds()
    zoomed = overall_span < 8 * 3600

    inner_w = W - PAD_L - 26
    band_gap = 16
    store_row = 34
    bands = []
    for day in days:
        day_series = {
            code: [(ts, counts) for ts, counts in series if ts.date() == day]
            for code, series in series_map.items()
        }
        day_series = {code: series for code, series in day_series.items() if series}
        if not day_series:
            continue
        if zoomed:
            day_ts = [ts for series in day_series.values() for ts, _ in series]
            t0 = min(day_ts) - step
            t1 = max(day_ts) + step * 2
        else:
            tzinfo = min(ts for series in day_series.values() for ts, _ in series).tzinfo
            t0 = datetime(day.year, day.month, day.day, tzinfo=tzinfo)
            t1 = t0 + timedelta(days=1)
        bands.append((day, day_series, t0, t1))

    height = PAD_T + sum(24 + store_row * len(b[1]) + band_gap for b in bands) + 26
    parts = [f'<svg viewBox="0 0 {W} {height}" width="100%" role="img" aria-label="各门店菜单时刻表">']

    y_cursor = PAD_T
    for day, day_series, t0, t1 in bands:
        span = max((t1 - t0).total_seconds(), 1)

        def x_of(ts, t0=t0, span=span):
            return PAD_L + (ts - t0).total_seconds() / span * inner_w

        # 日期带标题
        parts.append(f'<text x="0" y="{y_cursor - 6}" fill="#7d8899" font-size="11.5">{day.strftime("%m月%d日")}　{len(day_series)} 家店</text>')
        y_cursor += 8

        # 时间刻度
        span_hours = span / 3600
        tick = 1 if span_hours <= 8 else (2 if span_hours <= 14 else 3)
        cursor = t0.replace(minute=0, second=0, microsecond=0)
        while cursor <= t1:
            x = x_of(cursor)
            if PAD_L - 1 <= x <= PAD_L + inner_w + 1:
                parts.append(f'<line x1="{x:.1f}" y1="{y_cursor}" x2="{x:.1f}" y2="{y_cursor + store_row * len(day_series) + 4}" stroke="#1a232e"/>')
                parts.append(f'<text x="{x:.1f}" y="{y_cursor + store_row * len(day_series) + 20}" fill="#5d6b7d" font-size="11" text-anchor="middle">{cursor.strftime("%H:%M")}</text>')
            cursor += timedelta(hours=tick)

        # 换班竖线（当天所有店合并）
        moments = {}
        for code, series in day_series.items():
            for ts, gone, new in switch_points(series, step_of(series, step)):
                key = ts.replace(second=0, microsecond=0)
                entry = moments.setdefault(key, {"open": 0, "close": 0, "swap": 0})
                if new and not gone:
                    entry["open"] += 1
                elif gone and not new:
                    entry["close"] += 1
                else:
                    entry["swap"] += 1
        for ts in sorted(moments):
            x = x_of(ts)
            if not (PAD_L - 1 <= x <= PAD_L + inner_w + 1):
                continue
            entry = moments[ts]
            if entry["open"] >= entry["close"] + entry["swap"]:
                verb = "开市"
            elif entry["close"] >= entry["open"] + entry["swap"]:
                verb = "打烊"
            else:
                verb = "换班"
            parts.append(f'<line x1="{x:.1f}" y1="{y_cursor}" x2="{x:.1f}" y2="{y_cursor + store_row * len(day_series) + 4}" stroke="#f6c453" stroke-width="1" stroke-dasharray="3 3" opacity="0.85"/>')
            parts.append(f'<text x="{x:.1f}" y="{y_cursor - 4}" fill="#f6c453" font-size="10.5" text-anchor="middle">{ts.strftime("%H:%M")} {verb}</text>')

        now = datetime.now().astimezone()
        if t0 <= now <= t1:
            x = x_of(now)
            parts.append(f'<line x1="{x:.1f}" y1="{y_cursor}" x2="{x:.1f}" y2="{y_cursor + store_row * len(day_series) + 4}" stroke="#ff5d73" stroke-width="1.2"/>')
            parts.append(f'<text x="{x:.1f}" y="{y_cursor - 4}" fill="#ff5d73" font-size="10.5" text-anchor="middle">现在</text>')

        # 门店行
        for index, code in enumerate(sorted(day_series, key=lambda c: meta.get(c, {}).get("distance_m", 0))):
            series = day_series[code]
            store_step = step_of(series, step)
            y = y_cursor + index * store_row
            info = meta.get(code, {})
            name = info.get("name", code).replace("麦当劳", "").replace("深圳", "")
            parts.append(f'<text x="{PAD_L - 14}" y="{y + 15:.1f}" fill="#c2cedd" font-size="12.5" text-anchor="end">{html.escape(name)}</text>')
            parts.append(f'<text x="{PAD_L - 14}" y="{y + 28:.1f}" fill="#4b5867" font-size="9.5" text-anchor="end">{info.get("distance_m", 0)} m · {html.escape(info.get("hours", ""))}</text>')

            window = hours_window(info.get("hours", ""))
            for seg_start, seg_end, count, _signature in segments(series, store_step):
                if count <= 0:
                    continue
                left = max(x_of(seg_start), PAD_L)
                right = min(x_of(seg_end), PAD_L + inner_w)
                if right - left < 1.5:
                    continue
                # 营业时间之外接口仍可能返回菜单，这里降透明度并注明，避免看起来像在营业
                minutes = seg_start.hour * 60 + seg_start.minute
                closed = bool(window) and not (window[0] <= minutes <= window[1])
                opacity = "0.08" if closed else "0.22"
                note = " · 门店已关门（接口仍有返回）" if closed else ""
                parts.append(
                    f'<rect x="{left:.1f}" y="{y + 4}" width="{max(1.0, right - left - 1.6):.1f}" height="{store_row - 14}" rx="3" '
                    f'fill="#3ddc97" opacity="{opacity}" stroke="#3ddc97" stroke-opacity="0.5" stroke-width="1">'
                    f'<title>{seg_start.strftime("%H:%M")}–{seg_end.strftime("%H:%M")} · {count} 款{note}</title></rect>'
                )
                if right - left > 58 and not closed:
                    parts.append(f'<text x="{(left + right) / 2:.1f}" y="{y + 16:.1f}" fill="#e6eef7" font-size="11.5" text-anchor="middle">{count} 款</text>')

        y_cursor += store_row * len(day_series) + band_gap + 22

    note = "有数据的时段（自动缩放）" if zoomed else "一天 24 小时"
    parts.append(f'<text x="{PAD_L}" y="{height - 8}" fill="#4b5867" font-size="10.5">横向：{note}　纵向：每家店一条，色块是一版菜单</text>')
    parts.append("</svg>")
    return "\n".join(parts)


# ------------------------------------------------------------------ 分类明细
def category_chart(series, step, title, hours=""):
    if not series:
        return ""
    categories = []
    for _, counts in series:
        for name in counts:
            if name and name not in categories:
                categories.append(name)
    categories = [n for n in categories if any(c.get(n, 0) > 0 for _, c in series)]
    if not categories:
        return ""
    categories.sort(key=lambda n: min((ts for ts, c in series if c.get(n, 0) > 0), default=datetime.max))

    left = 168
    row_h = 22
    top = 30
    inner_w = W - left - 30
    all_ts = [ts for ts, _ in series]
    t0 = min(all_ts).replace(minute=0, second=0, microsecond=0)
    t1 = max(all_ts) + step
    span = max((t1 - t0).total_seconds(), 1)
    height = top + row_h * len(categories) + 40

    def x_of(ts):
        return left + (ts - t0).total_seconds() / span * inner_w

    parts = [f'<svg viewBox="0 0 {W} {height}" width="100%" role="img" aria-label="{html.escape(title)} 各分类出现区间">']
    cursor = t0
    while cursor <= t1:
        x = x_of(cursor)
        parts.append(f'<line x1="{x:.1f}" y1="{top - 8}" x2="{x:.1f}" y2="{height - 30}" stroke="#1a232e"/>')
        parts.append(f'<text x="{x:.1f}" y="{height - 12}" fill="#5d6b7d" font-size="11" text-anchor="middle">{cursor.strftime("%H:%M")}</text>')
        cursor += timedelta(hours=1 if span <= 8 * 3600 else 3)

    for index, name in enumerate(categories):
        y = top + index * row_h
        parts.append(f'<text x="{left - 10}" y="{y + row_h / 2 + 4:.1f}" fill="#8b98aa" font-size="11.5" text-anchor="end">{html.escape(name)}</text>')
        for start, end, peak in runs(series, name, step):
            x1, x2 = x_of(start), max(x_of(end), x_of(start) + 2)
            parts.append(
                f'<rect x="{x1:.1f}" y="{y + 4}" width="{x2 - x1:.1f}" height="{row_h - 9}" rx="2" fill="#3ddc97" opacity="{0.3 + 0.6 * min(1.0, peak / 8.0):.2f}">'
                f'<title>{html.escape(name)} · {start.strftime("%H:%M")} → {end.strftime("%H:%M")} · 最多 {peak} 款</title></rect>'
            )
    parts.append("</svg>")
    return "\n".join(parts)


# ------------------------------------------------------------------ 页面
def snapshot_table(rows, meta) -> str:
    latest = {}
    for row in rows:
        ts = parse_ts(row["ts"])
        for sample in row.get("samples", []):
            if "error" in sample:
                continue
            code = sample.get("store", "")
            if code not in latest or ts > latest[code][0]:
                latest[code] = (ts, sample.get("item_count", 0))

    now = datetime.now().astimezone()
    newest = max((ts for ts, _ in latest.values()), default=now)
    body = []
    for code, info in sorted(meta.items(), key=lambda kv: kv[1].get("distance_m", 0)):
        if code not in latest:
            continue
        stale_min = (newest - latest[code][0]).total_seconds() / 60
        stale_mark = f'<span class="stale">数据停在 {latest[code][0].strftime("%H:%M")}</span>' if stale_min > 20 else ""
        window = hours_window(info.get("hours", ""))
        if window:
            left_min = window[1] - (now.hour * 60 + now.minute)
            text = f"{left_min // 60}h{left_min % 60:02d}m" if left_min > 0 else "已关门"
            cls = "" if left_min > 60 else ("warn" if left_min > 0 else "off")
        else:
            text, cls = "—", ""
        body.append(
            f'<tr><td>{html.escape(info.get("name", code).replace("麦当劳", ""))}</td>'
            f'<td class="num">{info.get("distance_m", 0)} m</td>'
            f'<td class="num">{html.escape(info.get("hours", "—"))}</td>'
            f'<td class="num strong">{latest[code][1]}{stale_mark}</td>'
            f'<td class="num {cls}">{text}</td></tr>'
        )
    return (
        '<table class="snapshot"><thead><tr><th>门店</th><th>距离</th><th>营业</th><th>此刻在售</th><th>距关门</th></tr></thead>'
        f'<tbody>{"".join(body)}</tbody></table>'
    )


def headline(rows, meta, step) -> str:
    """一句话结论：几点开市、几点换班、几点打烊。"""
    moments = {}
    for code in meta:
        series = store_series(rows, code)
        for ts, gone, new in switch_points(series, step_of(series, step)):
            key = ts.strftime("%H:%M")
            entry = moments.setdefault(key, {"stores": set(), "open": 0, "close": 0, "swap": 0})
            entry["stores"].add(code)
            if new and not gone:
                entry["open"] += 1
            elif gone and not new:
                entry["close"] += 1
            else:
                entry["swap"] += 1
    if not moments:
        return ""
    parts = []
    for moment in sorted(moments):
        entry = moments[moment]
        count = len(entry["stores"])
        if entry["open"] >= entry["close"] + entry["swap"]:
            verb = "开市"
        elif entry["close"] >= entry["open"] + entry["swap"]:
            verb = "打烊"
        else:
            verb = "换班"
        parts.append(f"{count} 家店在 {moment} {verb}")
    return "；".join(parts) + "。"


def switch_cards(rows, meta, step) -> str:
    """每次换班：下架了哪些分类、上架了哪些分类。这是本工具的主要内容。"""
    groups = {}
    for code, info in meta.items():
        series = store_series(rows, code)
        if not series:
            continue
        store_step = step_of(series, step)
        for (t0, c0), (t1, c1) in zip(series, series[1:]):
            if (t1 - t0) > store_step * 2.5:
                continue
            gone = tuple(sorted(((k, v) for k, v in c0.items() if v > 0 and c1.get(k, 0) == 0), key=lambda kv: -kv[1]))
            new = tuple(sorted(((k, v) for k, v in c1.items() if v > 0 and c0.get(k, 0) == 0), key=lambda kv: -kv[1]))
            if len(gone) + len(new) < 2:
                continue
            key = (t1.strftime("%H:%M"), gone, new)
            groups.setdefault(key, []).append(info.get("name", code).replace("麦当劳", "").replace("深圳", ""))

    if not groups:
        return '<p class="muted">还没有观测到换班。</p>'

    def chips(items, css):
        if not items:
            return ""
        shown = items[:8]
        text = "　".join(f"{html.escape(name)} <i>{count}</i>" for name, count in shown)
        more = f"　等 {len(items)} 类" if len(items) > len(shown) else ""
        return f'<div class="diff"><span class="tag {css}">{"下架" if css == "off" else "上架"}</span><span>{text}{more}</span></div>'

    cards = []
    for (moment, gone, new), stores in sorted(groups.items(), key=lambda kv: kv[0][0]):
        if gone and not new:
            verb, css = "打烊", "shut"
        elif new and not gone:
            verb, css = "开市", "open"
        else:
            verb, css = "换班", "swap"
        who = f"{len(stores)} 家店" if len(stores) > 1 else stores[0]
        counts = []
        if gone:
            counts.append(f"下架 {len(gone)} 类")
        if new:
            counts.append(f"上架 {len(new)} 类")
        cards.append(
            f'<article class="card {css}"><header><b>{moment}</b><span class="verb">{verb}</span>'
            f'<span class="who">{html.escape(who)} · {("，".join(counts))}</span></header>'
            f'{chips(gone, "off")}{chips(new, "on")}</article>'
        )
    return "".join(cards)


def render(data_dir: Path) -> str:
    rows = load(data_dir)
    meta = load_meta(data_dir)
    if not rows:
        return "<!doctype html><meta charset='utf-8'><title>麦门时刻表</title><p>还没有采样数据。</p>"

    times = [parse_ts(r["ts"]) for r in rows]
    gaps = sorted((b - a).total_seconds() for a, b in zip(times, times[1:])) if len(times) > 2 else []
    step = timedelta(seconds=max(60, gaps[len(gaps) // 2])) if gaps else timedelta(minutes=10)

    now = datetime.now().astimezone()
    stale = (now - times[-1]).total_seconds() / 60
    if stale <= 25:
        state, color = "记录中", "#3ddc97"
    elif stale <= 120:
        state, color = f"采样中断 {int(stale)} 分钟", "#f6c453"
    else:
        state, color = f"采样已停止 {int(stale / 60)} 小时", "#ff5d73"

    switches = 0
    for code in meta:
        series = store_series(rows, code)
        switches += len(switch_points(series, step_of(series, step)))
    heroes = [
        (str(len(meta)), "家门店"),
        (str(len(rows)), "轮采样"),
        (str(switches), "次换班"),
        (headline(rows, meta, step).rstrip("。").split("；")[0] if switches else "—", "最集中的换班"),
    ]
    strip = '<div class="stats">' + "".join(f"<div><b>{v}</b><span>{k}</span></div>" for v, k in heroes[:3]) + "</div>"

    table = timetable_svg(rows, meta, step)
    cards = switch_cards(rows, meta, step)
    note = headline(rows, meta, step)

    details = []
    for code, info in sorted(meta.items(), key=lambda kv: kv[1].get("distance_m", 0)):
        series = store_series(rows, code)
        detail = category_chart(series, step_of(series, step), info.get("name", code), info.get("hours", ""))
        if detail:
            details.append(
                f'<details><summary>{html.escape(info.get("name", code))} · 逐个分类的出现区间</summary>{detail}</details>'
            )

    return f"""<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>麦门时刻表 · 一家麦当劳的菜单一天换几次</title>
<meta property="og:title" content="麦当劳的菜单，是一张时刻表">
<meta property="og:description" content="每 10 分钟采样一次门店在售菜单，记录一天里几点开市、几点换班、换掉了什么。">
<meta property="og:image" content="https://lbdggls.github.io/mcd-timetable/docs/share-card.png">
<meta property="og:type" content="website">
<meta name="twitter:card" content="summary_large_image">
<style>
:root {{ color-scheme: dark; }}
body {{ margin:0; background:#0a0e13; color:#d8e0ea; font:15px/1.7 -apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif; }}
.wrap {{ max-width:1240px; margin:0 auto; padding:44px 24px 80px; }}
h1 {{ font-size:28px; margin:0 0 8px; letter-spacing:.3px; }}
h2 {{ font-size:15px; margin:40px 0 8px; font-weight:600; color:#c9d4e2; }}
.lead {{ color:#94a3b5; margin:0; font-size:16px; }}
.takeaway {{ margin:20px 0 2px; color:#e6eef7; font-size:18px; font-weight:500; }}
.status {{ display:inline-flex; align-items:center; gap:8px; margin:16px 0 0; padding:5px 12px; border:1px solid #1e2733; border-radius:999px; font-size:12.5px; color:#9fb0c4; }}
.dot {{ width:7px; height:7px; border-radius:50%; background:{color}; box-shadow:0 0 8px {color}; }}
.stats {{ display:grid; grid-template-columns:repeat(3,1fr); gap:10px; margin:24px 0 0; }}
.stats div {{ border:1px solid #141c26; border-radius:10px; padding:12px 14px; background:#0d1319; }}
.stats b {{ display:block; font-size:19px; color:#cfe0f0; font-weight:600; }}
.stats span {{ font-size:11.5px; color:#5d6b7d; }}
.snapshot {{ width:100%; border-collapse:collapse; margin:26px 0 0; font-size:13.5px; }}
.snapshot th {{ text-align:left; font-weight:400; color:#5d6b7d; font-size:11.5px; padding:0 0 8px; border-bottom:1px solid #141c26; }}
.snapshot td {{ padding:9px 0; border-bottom:1px solid #101720; color:#a9b6c6; }}
.snapshot td.num {{ text-align:right; font-variant-numeric:tabular-nums; }}
.snapshot td.strong {{ color:#d8e0ea; font-weight:600; }}
.snapshot td.warn {{ color:#f6c453; }}
.snapshot td.off {{ color:#5d6b7d; }}
.stale {{ display:block; font-size:10px; color:#5d6b7d; font-weight:400; }}
.muted {{ color:#5d6b7d; font-size:12.5px; }}
.legend {{ color:#5d6b7d; font-size:12.5px; margin:0 0 10px; }}
.card {{ border:1px solid #141c26; border-left:3px solid #2a3542; border-radius:8px; padding:12px 16px; margin:0 0 10px; background:#0d1319; }}
.card.open {{ border-left-color:#3ddc97; }}
.card.shut {{ border-left-color:#5d6b7d; }}
.card.swap {{ border-left-color:#f6c453; }}
.card header {{ display:flex; align-items:baseline; gap:10px; margin-bottom:8px; }}
.card b {{ font-size:15px; color:#e6eef7; font-variant-numeric:tabular-nums; }}
.card .verb {{ font-size:13px; color:#c2cedd; }}
.card .who {{ font-size:12px; color:#5d6b7d; }}
.diff {{ display:flex; gap:10px; font-size:13px; color:#a9b6c6; margin:4px 0; line-height:1.9; }}
.diff i {{ font-style:normal; color:#5d6b7d; font-size:11.5px; }}
.tag {{ flex:0 0 auto; font-size:11px; padding:1px 7px; border-radius:4px; height:20px; line-height:18px; }}
.tag.off {{ background:#241a1a; color:#e08a8a; }}
.tag.on {{ background:#12241c; color:#6fce9e; }}
details {{ margin-top:12px; font-size:13px; color:#8b98aa; }}
summary {{ cursor:pointer; color:#7d8899; }}
footer {{ margin-top:46px; border-top:1px solid #141c26; padding-top:18px; color:#5d6b7d; font-size:12.5px; }}
code {{ background:#111820; padding:1px 5px; border-radius:4px; font-size:12.5px; }}
</style></head>
<body><div class="wrap">
<h1>麦门时刻表</h1>
<p class="lead">麦当劳的菜单不是一张纸，是一张时刻表。每 10 分钟记录一次门店在售菜单，看它几点换班、几点清零。</p>
<div class="status"><span class="dot"></span>{state} · 最近一次 {times[-1].strftime('%m-%d %H:%M')}</div>
<p class="takeaway">{html.escape(note)}</p>
{strip}
<h2>换班记录</h2>
<p class="legend">每一次换班，下架了什么、上架了什么。分类后面的数字是这一版里的款数；换班时刻记为第一次观测到新菜单的采样点，真实切换发生在两次采样之间（间隔约 10 分钟）。</p>
{cards}
<h2>各店时间轴</h2>
<p class="legend">每个色块是一版菜单，块里的数字是当时能点多少款。</p>
{table}
<h2>此刻各店</h2>
{snapshot_table(rows, meta)}
<h2>细节</h2>
{''.join(details)}
<footer>
<p>采样间隔约 {int(step.total_seconds() // 60)} 分钟。色块是一版菜单，颜色相同表示菜单相同。数据来自麦当劳中国 MCP 的 <code>query-meals</code> 实时返回，未做人工修饰；采样中断的时段留空，不插值。</p>
<p>本项目为麦当劳程序员节创意开发大赛参赛作品，非麦当劳官方产品；餐品供应以门店当时实际为准。</p>
</footer>
</div></body></html>
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="把采样数据渲染成静态页面")
    parser.add_argument("--data-dir", default="data", help="数据目录，默认 data")
    parser.add_argument("--out", default="index.html", help="输出文件，默认 index.html")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    data_dir = ROOT / args.data_dir
    out = ROOT / args.out
    page = render(data_dir)
    out.write_text(page, encoding="utf-8")
    print(f"写出 {out.name}（{len(page)} 字节）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
