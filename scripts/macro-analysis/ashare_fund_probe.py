# -*- coding: utf-8 -*-
"""A股基本面数据源探针 —— 在投入全量采集前，用最低成本回答三个问题。

【为什么要先探针】
候选 B（A股基本面）的功效评估结论是「临界」：季频需要真实 IC ≥ 0.050，
而现实区间只有 0.02~0.05。在这个区间上投工程，很可能重演港股基本面的结局
（功效不足 → 信息性零结果，白白烧掉采集成本）。

但在放弃之前，有三个问题必须先用【最小成本】回答，因为它们能让候选 B 直接判死：

  Q1 数据源通不通？能不能按报告期【批量】拉全市场（一次请求拿一期全部股票）？
     → 若能，51 期只需 51 次请求，工程成本极低（对比港股要 LLM 逐份抽取 PDF）
     → 若不能（必须逐只股票请求），95 只 × 51 期 = 4845 次，成本不可接受

  Q2 有没有【公告日期】？没有它就无法建 vintage，前视偏差无解 → 直接判死

  Q3 农业池的【截面区分度】如何？猪周期下养殖股业绩高度同步，
     若净利润同比的截面 std 极小（全行业同涨同跌），则基本面因子在这个池子上
     根本没有区分度 → IC 必然接近 0 → 直接判死，不必采集

【为什么 Q3 最关键】
功效评估算的是「给定效应量需要多大的 IC」，但没回答「这个池子能不能产生
那么大的 IC」。农业股的核心是生猪养殖，全行业的净利润同比在猪价上行期
集体转正、下行期集体转负 —— 这是【共同暴露】不是【截面差异】。
这个问题用 1 期数据就能看出来，不需要采 51 期。

【纪律】
本脚本只读数据、不改主库。探针结论写进独立 JSON，不污染任何既有产物。
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

import numpy as np

# ⚠️ 路径必须写成 _HERE.parents[3]（_HERE = 脚本所在目录），
#    否则产物会落进 git 仓库（deepseek-harness/outputs/）污染版本库。
_HERE = Path(__file__).resolve().parent
ROOT = _HERE.parents[3]
OUT_DIR = ROOT / "outputs" / "2026-09-02"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0 Safari/537.36")

# 东财数据中心：业绩报表（按报告期批量拉全市场）
# 关键字段：SECURITY_CODE / REPORT_DATE / ANNOUNCE_DATE / WEIGHTAVG_ROE /
#          YSTZ(营收同比) / SJLTZ(净利润同比) / EPSJB / BPS
EM_REPORT = "RPT_LICO_FN_CPD"
EM_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"


def fetch_report(period: str, page_size: int = 500, page: int = 1, timeout: int = 30):
    """拉一期业绩报表的单页。返回 (obj, url)。"""
    params = {
        "reportName": EM_REPORT,
        "columns": "ALL",
        "filter": f"(REPORTDATE='{period}')",
        "pageNumber": str(page),
        "pageSize": str(page_size),
        "sortColumns": "SECURITY_CODE",
        "sortTypes": "1",
        "source": "WEB",
        "client": "WEB",
    }
    url = EM_URL + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               "Referer": "https://data.eastmoney.com/"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        txt = r.read().decode("utf-8", "replace")
    return json.loads(txt), url


def fetch_all_pages(period: str, page_size: int = 500, max_pages: int = 40,
                    sleep: float = 0.25):
    """⚠️ 必须分页拉全。东财单页上限 500 条，而 A 股全市场 5000+ 只。

    第一版只拉了第 1 页（按代码升序），导致农业 95 只只命中 15 只 ——
    基于这个偏样本算出的「截面区分度」毫无意义。
    """
    rows, url, total = [], None, None
    for p in range(1, max_pages + 1):
        obj, url = fetch_report(period, page_size, p)
        res = obj.get("result") if isinstance(obj, dict) else None
        if not res:
            break
        data = res.get("data") or []
        rows.extend(data)
        if total is None:
            total = res.get("count") or res.get("total") or 0
        if not data or (total and len(rows) >= total):
            break
        time.sleep(sleep)
    return rows, url, (total or len(rows))


def pool_codes():
    """从已建好的农业池库取股票代码列表。"""
    db = ROOT / "outputs" / "ashare_agri_hfq_xq.sqlite"
    conn = sqlite3.connect(str(db), timeout=30)
    codes = [r[0] for r in conn.execute(
        "SELECT DISTINCT code FROM daily_quotes_hfq ORDER BY code")]
    conn.close()
    return codes


def main():
    print("=" * 78)
    print("A股基本面数据源探针（候选B 可行性前置检验）")
    print("=" * 78)

    codes = pool_codes()
    print(f"\n农业池: {len(codes)} 只")

    # 用最近一个有完整数据的报告期做探针
    periods = ["2025-12-31", "2025-09-30", "2025-06-30", "2025-03-31",
               "2024-12-31", "2024-09-30"]
    obj = None
    used_period = None
    all_rows, url, total = [], None, 0
    for p in periods:
        try:
            print(f"\n尝试报告期 {p} ...")
            rows, url, tot = fetch_all_pages(p)
            print(f"  分页拉取 {len(rows)} 条（全市场 {tot} 条）")
            if len(rows) > 100:
                all_rows, used_period, total = rows, p, tot
                break
        except Exception as e:
            print(f"  ✗ 失败: {type(e).__name__}: {e}")
            continue

    if not all_rows:
        print("\n✗ 所有报告期均失败 → 候选B 数据源不可用（Q1 判死）")
        _write({"verdict": "Q1_FAIL", "note": "东财业绩报表接口不可用",
                "checked_at": datetime.now().isoformat(timespec="seconds")})
        return

    data = all_rows
    print(f"\n✓ Q1 通过：分页拉取拿到 {len(data)} 条（全市场 {total} 条）")
    print(f"    报告期 {used_period}")
    print(f"    URL: {url[:120]}...")

    # ---- Q2 公告日期 ----
    keys = list(data[0].keys())
    ann_key = next((k for k in keys if "ANNOUNCE" in k.upper() or "NOTICE" in k.upper()), None)
    print(f"\n字段清单（前 40 个）: {keys[:40]}")
    if ann_key:
        n_ann = sum(1 for r in data if r.get(ann_key))
        print(f"\n✓ Q2 通过：公告日期字段 = {ann_key}，{n_ann}/{len(data)} 条有值")
        q2 = True
    else:
        print(f"\n✗ Q2 失败：无公告日期字段 → 无法建 vintage，前视偏差无解")
        q2 = False

    # ---- Q3 截面区分度 ----
    pool = {r["SECURITY_CODE"]: r for r in data if r.get("SECURITY_CODE") in set(codes)}
    print(f"\n农业池命中: {len(pool)} / {len(codes)} 只")

    fields = {
        "WEIGHTAVG_ROE": "加权ROE",
        "YSTZ": "营收同比%",
        "SJLTZ": "净利润同比%",
        "EPSJB": "基本每股收益",
        "BPS": "每股净资产",
        "XSMLL": "销售毛利率%",
        "XSJLL": "销售净利率%",
        "ZCFZL": "资产负债率%",
    }

    print("\n" + "=" * 78)
    print("③ 截面区分度（核心判据）")
    print("=" * 78)
    # ⚠️ 农业股「净利润同比」会因盈转亏/亏转盈爆炸（实测 P10 = −344%、P90 = +104%），
    #    直接拿 std/IQR 当区分度判据会被极端值劫持 → 必须同时看 winsorize 后的口径。
    print(f"\n  {'字段':<16}{'命中':>6}{'均值':>11}{'中位':>11}{'std':>11}"
          f"{'IQR':>11}{'IQR(wins)':>11}")
    print("  " + "-" * 80)

    stats = {}
    for fk, flabel in fields.items():
        vals = []
        for c, r in pool.items():
            v = r.get(fk)
            if v is None:
                continue
            try:
                fv = float(v)
            except (TypeError, ValueError):
                continue
            if np.isfinite(fv):
                vals.append(fv)
        if len(vals) < 10:
            print(f"  {flabel:<16}{len(vals):>6}   —字段缺失或样本不足—")
            continue
        a = np.array(vals)
        q1, q3 = np.percentile(a, [25, 75])
        lo, hi = np.percentile(a, [5, 95])
        aw = np.clip(a, lo, hi)
        wq1, wq3 = np.percentile(aw, [25, 75])
        stats[fk] = {
            "label": flabel, "n": int(len(a)),
            "mean": float(a.mean()), "median": float(np.median(a)),
            "std": float(a.std(ddof=1)), "iqr": float(q3 - q1),
            "iqr_wins": float(wq3 - wq1),
            "p10": float(np.percentile(a, 10)), "p90": float(np.percentile(a, 90)),
        }
        s = stats[fk]
        print(f"  {flabel:<16}{s['n']:>6}{s['mean']:>11.2f}{s['median']:>11.2f}"
              f"{s['std']:>11.2f}{s['iqr']:>11.2f}{s['iqr_wins']:>11.2f}")

    # 主判据用【加权 ROE】（水平值，不会被同比基数效应劫持）；
    # 净利润同比只看 winsorize 后的 IQR。
    print("\n  判据（主：加权ROE 水平值）:")
    roe_ok = None
    if "WEIGHTAVG_ROE" in stats:
        s = stats["WEIGHTAVG_ROE"]
        print(f"    截面 std = {s['std']:.2f} pp    IQR = {s['iqr']:.2f} pp"
              f"    P10={s['p10']:.1f}  P90={s['p90']:.1f}")
        if s["iqr"] < 5:
            print(f"    ✗ ROE 的 IQR < 5pp → 全行业盈利高度同步，截面区分度不足")
            print(f"      → 基本面因子在这个池子上无法产生有效截面排序 → Q3 判死")
            roe_ok = False
        else:
            print(f"    ✓ ROE 的 IQR ≥ 5pp → 存在截面区分度")
            roe_ok = True
    else:
        print("    （字段缺失）")

    print("\n  判据（辅：净利润同比 winsorize 后）:")
    if "SJLTZ" in stats:
        s = stats["SJLTZ"]
        print(f"    原始 std = {s['std']:.1f}pp（被极端值劫持）"
              f"    winsorized IQR = {s['iqr_wins']:.1f}pp")
        if s["iqr_wins"] < 20:
            print(f"    ✗ winsorized IQR < 20pp → 业绩高度同步")
        else:
            print(f"    ✓ winsorized IQR ≥ 20pp → 有区分度（但需缩尾处理后才能用）")

    q3 = roe_ok

    verdict = "GO" if (q2 and q3) else ("PARTIAL" if q3 else "NO_GO")
    print("\n" + "=" * 78)
    print(f"探针结论: {verdict}")
    print("=" * 78)
    print(f"  Q1 批量可得（一次请求拿全市场）: ✓")
    print(f"  Q2 有公告日期（能建 vintage）  : {'✓' if q2 else '✗'}")
    print(f"  Q3 截面区分度足够              : {'✓' if q3 else ('✗' if q3 is False else '?')}")

    _write({
        "verdict": verdict,
        "checked_at": datetime.now().isoformat(timespec="seconds"),
        "probe_period": used_period,
        "q1_batch_fetch": {"ok": True, "n_all_market": len(data), "n_pool": len(pool),
                           "url": url},
        "q2_announce_date": {"ok": bool(q2), "field": ann_key,
                             "n_with_value": (sum(1 for r in data if r.get(ann_key))
                                              if ann_key else 0)},
        "q3_cross_section_dispersion": {"ok": q3, "stats": stats},
        "field_list": keys,
    })


def _write(payload):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    op = OUT_DIR / "ashare_fundamental_probe.json"
    op.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n已写出: {op}")


if __name__ == "__main__":
    main()
