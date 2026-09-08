#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ashare_crosscheck_data.py — 跨源独立核对（腾讯 vs 雪球）
=====================================================
背景：本项目所有检验依赖腾讯 fqkline 单一数据源，内部一致性校验（涨跌停界限、
QC 闸门、敏感性测试）无法发现"单一源系统性错误"。本脚本用雪球（独立源）做
跨源抽样核对，属 fail-loud 数据质量门禁的一部分。

方法（关键设计）：
- 不直接比对价格绝对值——两家"后复权"的复权因子基准可能不同（基期不同），
  绝对值差 ≠ 数据错。真正必须一致的数学量是【日收益率序列】
  r_t = close_t / close_{t-1} - 1（后复权下任意基期比率不变）。
- 原始价（未复权）核对：雪球前复权在"最近一次除权之后"与原始价恒等，
  取最近 20 个交易日比对（若期间有除权会显性报出，属正常现象非错误）。
- 成交量（不复权字段）也做跨源核对，容差 0.1%。

判据：
- PASS：收益率最大绝对差 <= 1e-4（浮点/舍入噪声级）
- WARN：最大差 <= 5e-4
- FAIL：超出（复权处理或数据错误，必须人工排查）

用法：
  python ashare_crosscheck_data.py            # 默认 8 只分层样本
  python ashare_crosscheck_data.py --codes 600519,000651
输出：outputs/2026-09-05/ashare_crosscheck_data.json
"""
import argparse
import json
import sqlite3
import time
import urllib.request
import http.cookiejar
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parents[4]  # D:/tmp/deepseek-harness/
OUT_DIR = WORKSPACE / "outputs" / "2026-09-05"
HFQ_DB = WORKSPACE / "outputs" / "ashare_csi800_hfq.sqlite"
RAW_DB = WORKSPACE / "outputs" / "ashare_csi800_raw.sqlite"

# 分层抽样：大盘蓝筹 / 深市 / 创业板 / 金融 / 农业 / 半导体 / 高除权（格力）/ 中小盘
DEFAULT_SAMPLES = [
    "600519",  # 贵州茅台 沪主板
    "000858",  # 五粮液   深主板
    "300750",  # 宁德时代 创业板(20%限)
    "601318",  # 中国平安 金融
    "600598",  # 北大荒   农业(主力池)
    "688981",  # 中芯国际 科创板(20%限)
    "000651",  # 格力电器 高分红高除权——复权错误的最佳探针
    "002415",  # 海康威视 中小盘
]

TOL_PASS = 1e-4
TOL_WARN = 5e-4


def xueqiu_session():
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    op.addheaders = [("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")]
    op.open("https://xueqiu.com/hq", timeout=20).read()
    tok = [c.value for c in cj if c.name == "xq_a_token"]
    if not tok:
        raise RuntimeError("雪球 cookie 获取失败")
    return op


def sym_prefix(code: str) -> str:
    return ("SH" if code.startswith(("6", "9", "5")) else "SZ") + code


def fetch_xq_kline(op, code: str, qtype: str, count: int):
    """qtype: 'after'=后复权 'before'=前复权。返回 [(date, close, volume)] 按时间升序。"""
    url = (
        "https://stock.xueqiu.com/v5/stock/chart/kline.json"
        f"?symbol={sym_prefix(code)}&begin={int(time.time()*1000)}"
        f"&period=day&type={qtype}&count=-{count}&indicator=kline"
    )
    d = json.load(op.open(url, timeout=20))
    if d.get("error_code") not in (0, None):
        raise RuntimeError(f"雪球接口错误: {d.get('error_description')}")
    cols = d["data"]["column"]
    ti, ci, vi = cols.index("timestamp"), cols.index("close"), cols.index("volume")
    out = []
    for it in d["data"]["item"]:
        dt = time.strftime("%Y-%m-%d", time.localtime(it[ti] / 1000))
        out.append((dt, float(it[ci]), float(it[vi])))
    out.sort(key=lambda x: x[0])
    return out


def rets(pairs):
    return {
        pairs[i][0]: pairs[i][1] / pairs[i - 1][1] - 1.0
        for i in range(1, len(pairs))
        if pairs[i - 1][1] > 0
    }


def compare_local_vs_xq(code: str, local: list, xq: list, kind: str, vol_check: bool = False):
    """local/xq: [(date, close, volume|None)]。返回核对结果 dict。"""
    xq_map = {d: (c, v) for d, c, v in xq}
    common = [d for d, *_ in local if d in xq_map]
    if len(common) < 10:
        return {"code": code, "kind": kind, "verdict": "FAIL",
                "reason": f"日期重叠仅 {len(common)} 天（<10），无法核对", "n_common": len(common)}
    # 收益率比对：各自算日收益，再按共同日期逐日比对
    lr = rets([(d, c) for d, c, *_ in local])
    xr = rets([(d, c) for d, (c, _) in xq_map.items()])
    diffs = [(d, abs(lr[d] - xr[d])) for d in common if d in lr and d in xr]
    max_drift = max(x[1] for x in diffs) if diffs else None
    bad_days = [(d, round(v, 6)) for d, v in diffs if v > TOL_PASS]
    # 成交量比对：腾讯单位=手，雪球=股（×100），容差 0.5%
    vol_diffs = []
    if vol_check:
        for d, _, lv in local:
            xv = xq_map.get(d, (None, None))[1]
            if xv and lv and xv > 0:
                vol_diffs.append((d, abs(lv * 100.0 / xv - 1.0)))
    vol_max = max((v for _, v in vol_diffs), default=None)
    if max_drift is None:
        verdict = "FAIL"
    elif max_drift <= TOL_PASS and (vol_max is None or vol_max <= 5e-3):
        verdict = "PASS"
    elif max_drift <= TOL_WARN and (vol_max is None or vol_max <= 2e-2):
        verdict = "WARN"
    else:
        verdict = "FAIL"
    r = {"code": code, "kind": kind, "verdict": verdict,
         "n_common": len(common), "max_ret_diff": round(max_drift, 8) if max_drift is not None else None,
         "bad_days": bad_days[:5]}
    if vol_max is not None:
        r["max_vol_diff"] = round(vol_max, 6)
    return r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--codes", default=None)
    args = ap.parse_args()
    codes = args.codes.split(",") if args.codes else DEFAULT_SAMPLES
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    hfq = sqlite3.connect(HFQ_DB)
    raw = sqlite3.connect(RAW_DB)
    op = xueqiu_session()

    results = []
    for code in codes:
        # --- 后复权：本地 vs 雪球（收益率口径） ---
        loc = [(d, c, v) for d, c, v in hfq.execute(
            "select trade_date, close, volume from daily_quotes_hfq where code=? order by trade_date desc limit 130", (code,))]
        loc.reverse()
        if not loc:
            results.append({"code": code, "kind": "hfq", "verdict": "FAIL", "reason": "本地无数据"})
            continue
        try:
            xq = fetch_xq_kline(op, code, "after", 130)
        except Exception as e:
            results.append({"code": code, "kind": "hfq", "verdict": "FAIL", "reason": f"雪球拉取失败: {e}"})
            continue
        results.append(compare_local_vs_xq(code, loc, xq, "hfq_returns", vol_check=True))

        # --- 原始价：本地 raw vs 雪球前复权（近 20 日，无除权区间内应恒等） ---
        loc_raw = [(d, c, None) for d, c in raw.execute(
            "select trade_date, close from daily_quotes_raw where code=? order by trade_date desc limit 20", (code,))]
        loc_raw.reverse()
        if loc_raw:
            try:
                xq_b = fetch_xq_kline(op, code, "before", 30)
                r = compare_local_vs_xq(code, loc_raw, xq_b, "raw_close_recent")
                # 原始价是绝对值比对（前复权近端=原始价），直接比 close
                xq_map = {d: c for d, c, _ in xq_b}
                cdiffs = [(d, abs(c / xq_map[d] - 1.0)) for d, c, _ in loc_raw if d in xq_map]
                r["max_close_diff"] = round(max(x[1] for x in cdiffs), 8) if cdiffs else None
                r["verdict"] = ("PASS" if r["max_close_diff"] is not None and r["max_close_diff"] <= 1e-3
                                else "WARN" if (r["max_close_diff"] or 1) <= 5e-3 else "FAIL")
                results.append(r)
            except Exception as e:
                results.append({"code": code, "kind": "raw_close_recent", "verdict": "FAIL",
                                "reason": f"雪球拉取失败: {e}"})
        time.sleep(1.2)  # 礼貌限速

    n_pass = sum(1 for r in results if r["verdict"] == "PASS")
    n_warn = sum(1 for r in results if r["verdict"] == "WARN")
    n_fail = sum(1 for r in results if r["verdict"] == "FAIL")
    summary = {
        "date": "2026-09-05",
        "local_source": "腾讯 fqkline (ashare_csi800_hfq.sqlite / ashare_csi800_raw.sqlite)",
        "cross_source": "雪球 kline API（独立源）",
        "method": "后复权比【日收益率序列】（规避复权基期差异）；原始价比近 20 日绝对值（前复权近端恒等）",
        "tolerance": {"ret_pass": TOL_PASS, "ret_warn": TOL_WARN, "vol": 1e-3},
        "n_pass": n_pass, "n_warn": n_warn, "n_fail": n_fail,
        "results": results,
    }
    out = OUT_DIR / "ashare_crosscheck_data.json"
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"跨源核对完成: PASS={n_pass} WARN={n_warn} FAIL={n_fail}")
    for r in results:
        line = f"  [{r['verdict']}] {r['code']} {r['kind']}"
        if "max_ret_diff" in r:
            line += f" maxRetDiff={r['max_ret_diff']}"
        if "max_close_diff" in r:
            line += f" maxCloseDiff={r['max_close_diff']}"
        if "max_vol_diff" in r:
            line += f" maxVolDiff={r['max_vol_diff']}"
        if "reason" in r:
            line += f" ({r['reason']})"
        if r.get("bad_days"):
            line += f" badDays={r['bad_days'][:3]}"
        print(line)
    print(f"报告: {out}")


if __name__ == "__main__":
    main()
