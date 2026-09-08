#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ashare_pool_probe.py — A 股细分池（农业/生猪产业链）成分股核定与取数可行性探测

目的
----
窄池策略的第一件事是**核定池宽 N**——MDE 直接由 N 和期数决定，N 不能靠估。
本脚本用东财 push2 接口：
  1. 拉取全市场行业板块（m:90 t:2）+ 概念板块（m:90 t:3）
  2. 按关键词匹配农业相关板块
  3. 逐个拉取板块成分股，取 data.total 得到**真实成分股数量**
  4. 抽样验证 mootdx 日线可取的历史长度（决定期数 T）

输出 JSON 到 outputs/2026-09-02/ashare_pool_probe.json

注意
----
- 东财接口必须走 em_get() 节流，否则会被临时封 IP。
- 成分股数量用 data.total（接口返回的总数），不是当前页的行数。
- 板块可能重叠（一只股票同时属多个概念），建池时要显式去重。
"""

from __future__ import annotations

import json
import random
import re
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    import requests
except ImportError:
    raise SystemExit("需要 requests: pip install requests")

_HERE = Path(__file__).resolve().parent
ROOT = _HERE.parents[3]
OUT_DIR = ROOT / "outputs" / "2026-09-02"

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": UA})
EM_MIN_INTERVAL = 1.2
_em_last = [0.0]


def em_get(url, params=None, timeout=25, retries=5, **kw):
    """
    东财统一请求入口：节流 + 复用 session + 代理抖动重试。

    踩坑：本机 HTTP(S)_PROXY 指向 127.0.0.1:64119，该代理会**间歇性**
    RemoteDisconnected（与响应体大小无明确关系，实测 pz=5 也会随机失败）。
    trust_env=False 直连则完全不通 —— 必须走代理 + 指数退避重试。
    """
    last = None
    for i in range(retries):
        wait = EM_MIN_INTERVAL - (time.time() - _em_last[0])
        if wait > 0:
            time.sleep(wait + random.uniform(0.1, 0.4))
        try:
            r = SESSION.get(url, params=params, timeout=timeout, **kw)
            _em_last[0] = time.time()
            return r
        except Exception as e:          # noqa: BLE001 - 代理抖动需全捕获重试
            last = e
            _em_last[0] = time.time()
            time.sleep(1.0 + 1.5 * i + random.uniform(0, 0.5))
    raise last


CLIST = "https://push2.eastmoney.com/api/qt/clist/get"

# 农业/生猪产业链相关关键词
KEYWORDS = [
    "农牧", "农林", "农业", "牧渔", "养殖", "种业", "饲料", "动保",
    "动物保健", "猪肉", "生猪", "水产", "渔业", "种植", "农产", "兽药",
]


def fetch_boards(market_type: str) -> list[dict]:
    """market_type: 't:2'=行业板块, 't:3'=概念板块, 't:1'=地域板块"""
    params = {
        "pn": "1", "pz": "500", "po": "1", "np": "1",
        "fltt": "2", "invt": "2",
        "fs": f"m:90+{market_type}",
        "fields": "f2,f3,f12,f13,f14,f104,f105,f136,f140",
    }
    r = em_get(CLIST, params=params)
    d = r.json()
    diff = (d.get("data") or {}).get("diff") or []
    out = []
    for it in diff:
        out.append({
            "code": it.get("f12", ""),
            "name": it.get("f14", ""),
            "change_pct": it.get("f3", 0),
            "up": it.get("f104", 0),
            "down": it.get("f105", 0),
        })
    return out


def fetch_constituents(board_code: str, page_size: int = 100) -> dict:
    """
    返回 {"total": int, "rows": [...]}

    ⚠️ 踩坑：push2 clist 单页**硬上限 100 条**，即便传 pz=500 也只回 100。
    而 data.total 会如实报告总数（如农林牧渔 114）→ 只取第一页会静默丢 14 只。
    必须按 data.total 翻页取全。
    """
    rows, total = [], None
    page = 1
    while True:
        params = {
            "pn": str(page), "pz": str(page_size), "po": "1", "np": "1",
            "fltt": "2", "invt": "2",
            "fs": f"b:{board_code}",
            "fields": "f2,f3,f12,f13,f14",
        }
        r = em_get(CLIST, params=params)
        d = r.json()
        data = d.get("data") or {}
        diff = data.get("diff") or []
        if total is None:
            total = int(data.get("total") or 0)
        if not diff:
            break
        for it in diff:
            rows.append({"code": it.get("f12", ""), "name": it.get("f14", ""),
                         "mkt": it.get("f13", ""), "change_pct": it.get("f3", 0)})
        if len(rows) >= total or len(diff) < page_size:
            break
        page += 1
    return {"total": total if total is not None else len(rows), "rows": rows}


def probe_mootdx(sample_codes: list[str]) -> dict:
    """抽样验证 mootdx 日线可取的历史长度。失败不抛异常，返回原因。"""
    res = {"available": False, "reason": None, "samples": []}
    try:
        from mootdx.quotes import Quotes
    except Exception as e:
        res["reason"] = f"mootdx 未安装: {e}"
        return res
    try:
        client = Quotes.factory(market="std", timeout=15)
    except Exception as e:
        res["reason"] = f"mootdx 连接失败: {e}"
        return res
    for code in sample_codes[:3]:
        try:
            df = client.bars(symbol=code, category=4, offset=5000)
            if df is None or len(df) == 0:
                res["samples"].append({"code": code, "bars": 0})
                continue
            dates = [str(x)[:10] for x in df.index]
            res["samples"].append({
                "code": code, "bars": len(df),
                "start": dates[0], "end": dates[-1],
            })
            res["available"] = True
        except Exception as e:
            res["samples"].append({"code": code, "error": str(e)[:200]})
    return res


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    report = {"generated_at": datetime.now().isoformat(timespec="seconds"),
              "boards": [], "matched": [], "mootdx": {}}

    print("拉取东财行业板块...")
    industry = fetch_boards("t:2")
    print(f"  行业板块 {len(industry)} 个")
    print("拉取东财概念板块...")
    concept = fetch_boards("t:3")
    print(f"  概念板块 {len(concept)} 个")

    allboards = ([{**b, "type": "行业"} for b in industry]
                 + [{**b, "type": "概念"} for b in concept])
    report["boards"] = {"industry_count": len(industry), "concept_count": len(concept)}

    pat = re.compile("|".join(KEYWORDS))
    matched = [b for b in allboards if pat.search(b["name"])]
    print(f"\n关键词匹配到 {len(matched)} 个农业相关板块：")
    for b in matched:
        print(f"  [{b['type']}] {b['code']} {b['name']}  涨{b['up']}跌{b['down']}")

    print("\n拉取成分股数量（每个板块 1 次请求，已节流）...")
    for b in matched:
        c = fetch_constituents(b["code"])
        b["constituent_total"] = c["total"]
        b["constituents"] = c["rows"]
        report["matched"].append(b)
        print(f"  {b['name']:<20} 成分股 {c['total']:>4} 只")

    # 合并去重后的候选池
    seen, pool = set(), []
    for b in report["matched"]:
        for r in b["constituents"]:
            if r["code"] and r["code"] not in seen:
                seen.add(r["code"])
                pool.append({**r, "from_board": b["name"]})
    report["merged_pool"] = {
        "size": len(pool),
        "codes": [p["code"] for p in pool],
        "names": {p["code"]: p["name"] for p in pool},
        "note": "跨板块去重后的并集；实际建池应按主业归属筛选，避免概念板块引入无关标的",
    }
    print(f"\n去重并集: {len(pool)} 只（含概念板块噪声，需人工按主业收敛）")

    print("\n探测 mootdx 日线历史长度...")
    sample = [p["code"] for p in pool[:5]]
    mdx = probe_mootdx(sample)
    report["mootdx"] = mdx
    print(f"  mootdx 可用: {mdx['available']}  {mdx.get('reason') or ''}")
    for s in mdx["samples"]:
        print(f"    {s}")

    out = OUT_DIR / "ashare_pool_probe.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n报告已写入: {out}")


if __name__ == "__main__":
    main()
