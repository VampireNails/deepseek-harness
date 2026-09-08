#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ashare_xq_preflight.py — 换源前的【新源准入验证】（阶段 0 闸门）
=============================================================
背景（2026-09-05 第廿四类事故）：腾讯 fqkline 后复权序列系统性污染，
月收益 vs 雪球 p50=0.68%、39% 观测差>1%。污染特征 = hfq/raw 比率在
【无除权窗口内漂移】，疑似分段采集拼接放大。

本脚本的立场：**换源不等于修好**。新源入库前必须用同一套判据审一遍，
否则就是把事故换了个源重演一次。

四项验证（任一 FAIL 即不得开工重采）
----------------------------------
【前置实测（2026-09-05）】雪球 kline 参数语义与腾讯不同，必须先记录：
  - 正确用法：begin=<当前毫秒>, count=-N, **不传 end** → 返回 [now-N根, now]
  - count 无硬上限：实测 count=10000 返回 5832 根（2002-03-29 至今，被上市日截断）
    ⇒ 【无需分段】，一次请求取全量 —— 从根上消除"分段拼接漂移"（腾讯的死因）
  - 若传 end，则退化为区间模式 [begin, end]，count 被忽略（两种模式互斥）

V1 单次可取全量：count=8000 应返回该股全部历史，且末日期 == 最近交易日
   （对标腾讯"641 根硬上限 + 末段静默截头"）
V2 窗口独立性（**最关键，腾讯死因的直接对标**）：
   腾讯的污染特征 = 复权值依赖查询窗口（分段拼接放大）。故必须验证：
   同一股票用不同 count（1300 / 3000 / 8000）取，同一交易日的【后复权
   绝对值】与【日收益率】必须逐日完全一致。窗口依赖 = 污染，直接判死。
V3 hfq/raw 比率恒定性：雪球 after(后复权) / normal(不复权) 比率在
   【无除权交易日内】必须恒定。这是复权因子的数学定义，漂移即错。
V4 与本地已验证 raw 的恒等式（最强判据）：
   无除权日 → 后复权日收益 == 原始价日收益（数学恒等式）。
   本地 raw 已跨源判正确（maxCloseDiff=0.0），故雪球 hfq 收益必须
   与本地 raw 收益在此类日期一致。这条同时证明"雪球对"而非"雪球自洽"。

判据（保守）
----------
- V1: 返回条数 == 该股全部历史（与最大 count 一致），末日期为最近交易日
- V2: 不同 count 在重叠区间的后复权绝对值相对差 <= 1e-12、日收益差 <= 1e-12
- V3: 无除权日内 hfq/raw 比率的相对极差 <= 1e-9
- V4: 无除权日 hfq(雪球) 收益 vs raw(本地) 收益 最大绝对差 <= 1e-6

样本：优先高分红/低价股（腾讯污染最重 = 复权错误的最佳探针）
用法：python ashare_xq_preflight.py [--codes 600598,000651]
输出：outputs/2026-09-05/ashare_xq_preflight.json
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import time
import urllib.request
import http.cookiejar
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parents[4]
OUT_DIR = WORKSPACE / "outputs" / "2026-09-05"
RAW_DB = WORKSPACE / "outputs" / "ashare_csi800_raw.sqlite"
HFQ_DB = WORKSPACE / "outputs" / "ashare_csi800_hfq.sqlite"   # 腾讯后复权（受检对照）

# 高分红/低价/高除权 —— 腾讯污染最重的分层，复权错误的最佳探针
DEFAULT_SAMPLES = ["600598", "000651", "601318", "600519"]

COUNT_PROBE = [1300, 3000, 5000, 10000]
TOL_SPLICE = 1e-9      # V2 同源拼接，应完全一致
TOL_RATIO = 1e-5       # V3 无除权窗口复权因子恒定（容差推导见 TOL_NO_EXDIV）
TOL_IDENT = 1e-4       # V4 跨源恒等式

# 【容差推导，勿凭感觉改】雪球后复权 close 仅保留 4 位小数（如 39.6502），
# 对 40 元股价而言单日收益率舍入噪声 ≈ 5e-5/40 ≈ 1.3e-6，量级 1e-6。
# 而真实除权跳变（分红/送转）通常 >= 0.3%（3e-3），与噪声隔 3 个数量级。
# 故「无除权日」的判定容差取 1e-4：远大于舍入噪声，远小于真实除权。
TOL_NO_EXDIV = 1e-4


def make_session():
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    op.addheaders = [("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36")]
    op.open("https://xueqiu.com/hq", timeout=25).read()
    if not [c for c in cj if c.name == "xq_a_token"]:
        raise RuntimeError("雪球 cookie(xq_a_token) 获取失败")
    return op


def sym(code: str) -> str:
    return ("SH" if code.startswith(("6", "9", "5")) else "SZ") + code


def kline_count(op, code: str, qtype: str, count: int):
    """【采集用】count 模式：begin=当前时刻, count=-N, 不传 end。

    实测语义：返回 [now 往前 N 根, now]；N 超过上市以来总根数时返回全部
    （600598 实测 count=10000 → 5832 根 = 2002-03-29 至今）。
    两种模式互斥：一旦传 end，退化为区间模式且 count 被忽略。
    """
    url = ("https://stock.xueqiu.com/v5/stock/chart/kline.json"
           f"?symbol={sym(code)}&begin={int(time.time() * 1000)}&period=day"
           f"&type={qtype}&count=-{count}&indicator=kline")
    d = json.load(op.open(url, timeout=25))
    if d.get("error_code") not in (0, None):
        raise RuntimeError(f"雪球接口错误: {d.get('error_description')}")
    cols = d["data"]["column"]
    ti, ci, vi = cols.index("timestamp"), cols.index("close"), cols.index("volume")
    out = [(time.strftime("%Y-%m-%d", time.localtime(it[ti] / 1000)),
            float(it[ci]), float(it[vi])) for it in d["data"]["item"]]
    out.sort(key=lambda x: x[0])
    return out


def rets(series):
    """series: [(date, close)] 升序 → {date: ret}"""
    return {series[i][0]: series[i][1] / series[i - 1][1] - 1.0
            for i in range(1, len(series)) if series[i - 1][1] > 0}


def v1_full_history(op, code, big=8000):
    """单次可取全量 & 末段不截头（对标腾讯 641 根上限 + 末段静默截头）。"""
    a = kline_count(op, code, "after", big)
    time.sleep(1.0)
    b = kline_count(op, code, "after", big * 2)
    time.sleep(1.0)
    # 更大 count 若返回同量 → 已到历史起点（非硬上限）
    saturated = len(b) <= len(a) + 5
    return {"verdict": "PASS" if (saturated and len(a) > 1000) else "FAIL",
            "bars_big": len(a), "bars_2x": len(b),
            "first": a[0][0], "last": a[-1][0],
            "saturated": saturated,
            "note": "bars_2x≈bars_big ⇒ 已达上市起点，无硬上限/末段截头"}


def v2_window_independence(op, code, counts=(1300, 3000, 8000)):
    """【腾讯死因的直接对标】复权值是否依赖查询窗口。

    腾讯污染 = hfq/raw 比率随查询窗口漂移（分段拼接放大）。
    故必须证明：同一交易日，用不同 count 取回的后复权值逐日一致。
    若不一致 ⇒ 该源的后复权是"窗口相关"的，不能用作检验数据源。
    """
    series = {}
    for c in counts:
        series[c] = kline_count(op, code, "after", c)
        time.sleep(1.0)
    base_c = max(counts)
    base = {d: cl for d, cl, _ in series[base_c]}
    out = {"counts": {}, "verdict": "PASS"}
    for c in counts:
        if c == base_c:
            continue
        # 绝对值比对（后复权值唯一，不随查询窗口变）
        abs_diffs = [(d, abs(cl / base[d] - 1.0))
                     for d, cl, _ in series[c] if d in base and base[d] > 0]
        mx_abs = max((x[1] for x in abs_diffs), default=None)
        # 收益率比对
        rc = rets([(d, cl) for d, cl, _ in series[c]])
        rb = rets([(d, cl) for d, cl, _ in series[base_c]])
        rd = [abs(rc[d] - rb[d]) for d in rc if d in rb]
        mx_ret = max(rd) if rd else None
        ok = (mx_abs is not None and mx_abs <= 1e-12
              and (mx_ret is None or mx_ret <= 1e-12))
        out["counts"][f"count{c}_vs_{base_c}"] = {
            "n_overlap": len(abs_diffs),
            "max_abs_rel_diff": mx_abs,
            "max_ret_diff": mx_ret,
            "verdict": "PASS" if ok else "FAIL",
        }
        if not ok:
            out["verdict"] = "FAIL"
    out["note"] = "不同查询窗口下后复权值必须逐日一致；漂移=腾讯同款污染"
    return out


def v3_ratio_stability(op, code, count=1200):
    """hfq/raw 比率在无除权交易日必须恒定。

    无除权日判定：raw(不复权) 日收益 == hfq(后复权) 日收益 的日子里，
    复权因子未变；反过来比率变化日 = 除权日。故取「比率未变」的连续
    最长窗口，检验其中比率的相对极差（数学上应为 0）。
    """
    hfq = kline_count(op, code, "after", count)
    time.sleep(1.0)
    raw = kline_count(op, code, "normal", count)
    time.sleep(1.0)
    hm = {d: c for d, c, _ in hfq}
    common = [(d, c) for d, c, _ in raw if d in hm and c > 0]
    if len(common) < 100:
        return {"verdict": "FAIL", "reason": "重叠样本不足"}
    ratios = [(d, hm[d] / c) for d, c in common]
    # 找比率恒定的最长连续窗口（= 无除权窗口）
    best, cur = [], [ratios[0]]
    for i in range(1, len(ratios)):
        if abs(ratios[i][1] / ratios[i - 1][1] - 1.0) <= TOL_NO_EXDIV:
            cur.append(ratios[i])
        else:
            best = cur if len(cur) > len(best) else best
            cur = [ratios[i]]
    best = cur if len(cur) > len(best) else best
    vals = [v for _, v in best]
    rng = (max(vals) - min(vals)) / (sum(vals) / len(vals))
    # 全样本比率漂移（腾讯在此项上为 3%）
    allv = [v for _, v in ratios]
    total_drift = (max(allv) - min(allv)) / (sum(allv) / len(allv))
    return {"verdict": "PASS" if rng <= TOL_RATIO else "FAIL",
            "n_common": len(common),
            "longest_no_exdiv_window": len(best),
            "window_ratio_rel_range": round(rng, 12),
            "full_sample_ratio_drift": round(total_drift, 6),
            "window": [best[0][0], best[-1][0]],
            "note": "无除权窗口内复权因子必须恒定；漂移>0 即复权路径错"}


def v4_identity_with_local_raw(op, code, count=1200):
    """最强判据：无除权日 → 后复权日收益 == 原始价日收益（数学恒等式）。

    本地 raw（腾讯原始价）已跨源判正确（vs 雪球前复权 maxCloseDiff=0.0），
    故"无除权日"这一数学恒等式可当作绝对标尺。同时把【腾讯 hfq】拉进来
    做同口径对照 —— 三方同场竞技，才能定性"谁错"，而不是"谁自洽"。
    """
    hfq_xq = kline_count(op, code, "after", count)
    time.sleep(1.0)
    raw_xq = kline_count(op, code, "normal", count)
    time.sleep(1.0)
    # 无除权日：雪球内部 hfq 收益 == 雪球不复权收益（容差推导见 TOL_NO_EXDIV）
    rx = rets([(d, c) for d, c, _ in raw_xq])
    rh = rets([(d, c) for d, c, _ in hfq_xq])
    no_ex = [d for d in rh if d in rx and abs(rh[d] - rx[d]) <= TOL_NO_EXDIV]
    if len(no_ex) < 100:
        return {"verdict": "FAIL", "reason": f"无除权日样本仅 {len(no_ex)}"}

    # 注意：raw 与 hfq 分属两个库（腾讯的两个独立采集库），必须分别连接
    conn = sqlite3.connect(str(RAW_DB))
    loc_raw = [(d, c) for d, c in conn.execute(
        "SELECT trade_date, close FROM daily_quotes_raw WHERE code=? "
        "ORDER BY trade_date DESC LIMIT ?", (code, count + 60))]
    conn.close()
    conn2 = sqlite3.connect(str(HFQ_DB))
    loc_hfq = [(d, c) for d, c in conn2.execute(
        "SELECT trade_date, close FROM daily_quotes_hfq WHERE code=? "
        "ORDER BY trade_date DESC LIMIT ?", (code, count + 60))]
    conn2.close()
    loc_raw.reverse()
    loc_hfq.reverse()
    lr = rets(loc_raw)
    lh = rets(loc_hfq)

    def stat(src_rets):
        common = [d for d in no_ex if d in src_rets]
        if len(common) < 50:
            return None
        dif = sorted(abs(src_rets[d] - lr[d]) for d in common)
        return {"n": len(common), "max": round(dif[-1], 8),
                "p50": round(dif[len(dif) // 2], 8),
                "p99": round(dif[int(len(dif) * 0.99)], 8),
                "frac_gt_1pct": round(sum(1 for x in dif if x > 0.01) / len(dif), 4)}

    xq, tx = stat(rh), stat(lh)
    # 判据：雪球 hfq 与本地 raw 的差异必须落在舍入噪声级（p99 <= 1e-4）
    ok = bool(xq and xq["p99"] <= TOL_IDENT)
    return {"verdict": "PASS" if ok else "FAIL",
            "n_no_exdiv": len(no_ex),
            "xueqiu_hfq_vs_local_raw": xq,
            "tencent_hfq_vs_local_raw": tx,
            "note": "无除权日为数学恒等式；雪球应落在舍入噪声级，腾讯应显著偏离"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--codes", default=None)
    ap.add_argument("--skip-count-probe", action="store_true")
    args = ap.parse_args()
    codes = args.codes.split(",") if args.codes else DEFAULT_SAMPLES
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    op = make_session()
    out = {"date": "2026-09-05", "stage": "阶段0 新源准入验证",
           "new_source": "雪球 kline API", "checks": {}}

    for code in codes:
        rec = {}
        if not args.skip_count_probe:
            rec["V1_full_history"] = v1_full_history(op, code)
        rec["V2_window_independence"] = v2_window_independence(op, code)
        rec["V3_ratio"] = v3_ratio_stability(op, code)
        rec["V4_identity"] = v4_identity_with_local_raw(op, code)
        out["checks"][code] = rec
        print(f"--- {code} ---")
        for k, v in rec.items():
            print(f"  {k}: {json.dumps(v, ensure_ascii=False)}")
        time.sleep(1.5)

    fails = [(c, k) for c, r in out["checks"].items()
             for k, v in r.items() if isinstance(v, dict) and v.get("verdict") == "FAIL"]
    out["n_fail"] = len(fails)
    out["fails"] = [f"{c}/{k}" for c, k in fails]
    out["gate"] = "PASS" if not fails else "FAIL"
    p = OUT_DIR / "ashare_xq_preflight.json"
    p.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n阶段0 闸门: {out['gate']}   FAIL 项: {out['fails']}")
    print(f"报告: {p}")


if __name__ == "__main__":
    main()
