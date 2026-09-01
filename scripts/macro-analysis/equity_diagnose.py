#!/usr/bin/env python3
"""equity_diagnose.py — 量化因子归因诊断器（第十七/十八轮固化）。

把所有分散的中性化审视固化为一个权威 CLI：

  consolidate   输出全部因子的 raw/size/sector/size_sector 四口径期级 IC 表
  composite      多因子 z 分组合 + 中性化检验（闭合"组合或许有用"漏洞）

纪律链（10 次归因纠错沉淀）：
  期级口径 → 市值/行业中性化 → 符号一致 → LOO → 视距单调性。
凡 |t| 由少数时段贡献、或仅在单一视距出现的信号一律非稳定。
"""
from __future__ import annotations
import argparse
import json
import math
import sqlite3
import statistics as st
from pathlib import Path

import equity_quant as eq

DB_PATH = eq.DB_PATH

HALF_FACTORS = [
    "revenue_yoy", "gross_margin", "net_margin_proxy", "ocf_to_revenue",
    "asset_liability_ratio", "inventory_to_revenue",
    "net_margin_chg", "rd_intensity",
]
TTM_FACTORS = [
    "ttm_revenue_yoy", "ttm_net_profit_yoy", "ttm_ocf_yoy", "ttm_gross_margin",
    "ttm_net_margin", "ttm_ocf_to_revenue",
]


def _t_of(vals: list[float]) -> float:
    n = len(vals)
    if n < 2:
        return 0.0
    m = sum(vals) / n
    s = st.stdev(vals)
    return m / (s / math.sqrt(n)) if s > 0 else 0.0


def _loo_min(vals: list[float]) -> float:
    if len(vals) < 4:
        return float("nan")
    return min(_t_of([v for i, v in enumerate(vals) if i != j]) for j in range(len(vals)))


def consolidate(horizon: str = "fwd_20d") -> list[dict]:
    """全部因子 × 四口径期级 IC 表。"""
    rows = []
    for fk in HALF_FACTORS + TTM_FACTORS:
        r = eq.period_level_ic_neutralized(DB_PATH, fk, horizon)
        if "error" in r:
            rows.append({"factor": fk, "error": r["error"]})
            continue
        nper = len(r.get("periods", []))
        row = {"factor": fk, "n_periods": nper, "periods": r["periods"]}
        for k in ("raw", "size", "sector", "size_sector"):
            blk = r.get(k, {})
            if "error" in blk:
                row[f"{k}_t"] = None
                row[f"{k}_icir"] = None
                row[f"{k}_pos"] = None
                continue
            t = blk.get("ic_tstat", float("nan"))
            npk = blk.get("n_periods", nper)
            icir = (t / math.sqrt(npk)) if (isinstance(t, float) and npk > 0) else float("nan")
            row[f"{k}_t"] = round(t, 3)
            row[f"{k}_icir"] = round(icir, 3)
            row[f"{k}_pos"] = round(blk.get("ic_positive_pct", float("nan")), 3)
        # 最强口径的 LOO（用 size_sector，最严格）
        ss = r.get("size_sector", {}).get("ic_by_period")
        if ss:
            row["size_sector_loo_min"] = round(_loo_min([ss[p] for p in sorted(ss)]), 3)
        rows.append(row)
    return rows


def _panel_multi(factor_keys: list[str], horizon: str, min_stocks: int = 5):
    """多因子面板：{date: {ticker: {factor: val, 'ret': r, 'sec': s, 'lm': log_mcap}}}。

    行业/市值直接由 sector_map 与 daily_quotes×universe 构建（非依赖单因子 panel），
    覆盖全部日期，避免首因子未生效时漏掉其它因子已生效的截面。
    """
    # 各因子可用值（forward-fill 后每个 date 的截面）
    factor_panels = {}
    for fk in factor_keys:
        factor_panels[fk] = eq._load_panel_data(DB_PATH, fk, horizon, min_stocks)

    con = sqlite3.connect(DB_PATH)
    sec = dict(con.execute("SELECT ticker, sector FROM sector_map").fetchall())
    shares = dict(con.execute(
        "SELECT ticker, shares_outstanding FROM universe WHERE shares_outstanding IS NOT NULL").fetchall())
    closes: dict[str, dict[str, float]] = {}
    for tk, d, c in con.execute(
            "SELECT ticker, quote_date, close FROM daily_quotes WHERE close IS NOT NULL"):
        closes.setdefault(d, {})[tk] = c
    con.close()

    dates = sorted(set().union(*[set(p.keys()) for p in factor_panels.values()]))
    merged: dict[str, dict[str, dict]] = {}
    for d in dates:
        cell: dict[str, dict] = {}
        for fk in factor_keys:
            for tk, fv, rv in factor_panels.get(fk, {}).get(d, []):
                cell.setdefault(tk, {"ret": rv})
                cell[tk][fk] = fv
        for tk, c in cell.items():
            lm = None
            cl = closes.get(d, {}).get(tk)
            if cl is not None and tk in shares:
                lm = math.log(cl * shares[tk]) if cl * shares[tk] > 0 else None
            c["sec"] = sec.get(tk)
            c["lm"] = lm
        merged[d] = {tk: c for tk, c in cell.items()
                     if c.get("lm") is not None and c.get("sec") is not None}
    return merged


def _demean(vals: list[float], keys: list) -> list[float]:
    agg: dict = {}
    for v, k in zip(vals, keys):
        agg.setdefault(k, []).append(v)
    avg = {k: sum(v) / len(v) for k, v in agg.items()}
    return [v - avg.get(k, 0) for v, k in zip(vals, keys)]


def _residualize(vals: list[float], lm: list[float], secs: list) -> list[float]:
    """对 (log_mcap, 行业) 双向残差化：先去行业均值，再对 log_mcap 一元回归取残差。"""
    dv = _demean(vals, secs)
    if len(set(lm)) < 2:
        return dv
    mx = sum(lm) / len(lm)
    sxx = sum((x - mx) ** 2 for x in lm)
    mr = sum(dv) / len(dv)
    sxy = sum((a - mr) * (b - mx) for a, b in zip(dv, lm))
    beta = sxy / sxx if sxx > 0 else 0.0
    return [a - beta * (b - mx) for a, b in zip(dv, lm)]


def _zscore_cross(vals: list[float]) -> list[float]:
    """截面 z 分。"""
    n = len(vals)
    if n < 2:
        return [0.0] * n
    m = sum(vals) / n
    s = st.pstdev(vals)
    if s == 0:
        return [0.0] * n
    return [(v - m) / s for v in vals]


def _effective_dates(factor_keys: list[str]) -> list[str]:
    """所有因子报告期的生效日并集（min(发布日, 标准发布日)）。

    合成 IC 必须**期级**（每生效日一个截面），不能用逐日 IC 序列求 t——
    逐日 IC 高度自相关，直接求 t 会系统性高估（与单因子 Newey-West 同源）。
    生效日间隔 ~6 个月，期级 IC 间近似独立，plain t 即诚实口径。
    """
    con = sqlite3.connect(DB_PATH)
    effs: list[str] = []
    for fk in factor_keys:
        rows = con.execute(
            "SELECT d.period, MAX(c.release_date) FROM derived_factors d "
            "LEFT JOIN company_facts c ON c.ticker=d.ticker AND c.period=d.period "
            "WHERE d.factor_key=? AND d.source='derived' "
            "GROUP BY d.period ORDER BY d.period", (fk,)).fetchall()
        for period, rel in rows:
            std = (f"{period[:4]}-10-01" if period.endswith("H1")
                   else f"{int(period[:4]) + 1}-05-01" if period.endswith("H2") else period)
            if std == period:
                effs.append(rel or period)
            else:
                effs.append(min(rel, std) if rel else std)
    con.close()
    return sorted(set(effs))


def composite_ic(factor_keys: list[str], horizon: str = "fwd_20d",
                 min_factors: int = 3, min_stocks: int = 5) -> dict:
    """多因子合成 IC（点-in-time 安全，期级诚实口径）：

      1. 在每个**因子生效日**（union），对当日截面每只股票：

         composite = mean over available factors of z-score(factor value)

         z 分在**截面内**计算（无前视）；可选 neutralized：先对
         (log_mcap, 行业) 残差化再 z 分。
      2. 每生效日一个截面 IC（Spearman composite vs 前向收益）。
      3. 按期数（生效日数）算 plain t（期级 IC 间隔 ~6 月，近似独立，
         无需 Newey-West；NW 反而在 N<2·lags+1 时失灵）。

    闭合"组合或许有用"漏洞：若单因子皆噪声，组合仍噪声；若存弱而一致信号，
    组合可放大。结论以最严格（neutralized）口径为准。
    """
    merged = _panel_multi(factor_keys, horizon, min_stocks)
    all_dates = sorted(merged.keys())
    eff_dates = _effective_dates(factor_keys)

    out = {}
    for mode in ("raw", "neutralized"):
        ics: dict[str, float] = {}
        for eff in eff_dates:
            d = next((x for x in all_dates if x >= eff), None)
            if d is None:
                continue
            cell = merged[d]
            stocks = [tk for tk, c in cell.items()
                      if c.get("ret") is not None
                      and sum(1 for fk in factor_keys if fk in c) >= min_factors]
            if len(stocks) < min_stocks:
                continue
            zcols: dict[str, dict[str, float]] = {}
            for fk in factor_keys:
                present = [tk for tk in stocks if fk in cell[tk]]
                if len(present) < min_stocks:
                    continue
                rawv = [cell[tk][fk] for tk in present]
                if mode == "neutralized":
                    lms = [cell[tk]["lm"] for tk in present]
                    secs = [cell[tk]["sec"] for tk in present]
                    proc = _residualize(rawv, lms, secs)
                else:
                    proc = rawv
                zs = _zscore_cross(proc)
                zcols[fk] = {tk: z for tk, z in zip(present, zs)}
            if len(zcols) < min_factors:
                continue
            comp, ret = [], []
            for tk in stocks:
                zs = [zcols[fk][tk] for fk in zcols if tk in zcols[fk]]
                if len(zs) < min_factors:
                    continue
                comp.append(sum(zs) / len(zs))
                ret.append(cell[tk]["ret"])
            if len(comp) < min_stocks:
                continue
            ics[d] = eq._spearman(comp, ret)
        vals = list(ics.values())
        n = len(vals)
        out[mode] = {
            "n_periods": n, "periods": sorted(ics.keys()),
            "ic_tstat": round(_t_of(vals), 3),
            "icir": round(sum(vals) / n / (st.pstdev(vals) or 1), 3) if n else 0.0,
            "ic_positive_pct": round(sum(1 for x in vals if x > 0) / n, 3) if n else 0.0,
            "ic_by_period": {p: round(v, 4) for p, v in ics.items()},
        }
    out["factors"] = factor_keys
    out["horizon"] = horizon
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="量化因子归因诊断器")
    ap.add_argument("cmd", choices=["consolidate", "composite"])
    ap.add_argument("--horizon", default="fwd_20d")
    ap.add_argument("--factors", default=None,
                    help="composite 用因子列表，逗号分隔；缺省=全部基本面+TTM")
    ap.add_argument("--out", default=None, help="结果 JSON 输出路径")
    args = ap.parse_args()

    if args.cmd == "consolidate":
        rows = consolidate(args.horizon)
        print(json.dumps(rows, indent=2, ensure_ascii=False))
    else:
        fks = args.factors.split(",") if args.factors else (HALF_FACTORS + TTM_FACTORS)
        r = composite_ic(fks, args.horizon)
        print(json.dumps(r, indent=2, ensure_ascii=False))
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(rows if args.cmd == "consolidate" else r, fh,
                      ensure_ascii=False, indent=2)
        print(f"[diagnose] 已写 {args.out}")


if __name__ == "__main__":
    main()
