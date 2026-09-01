#!/usr/bin/env python3
"""行业异质性诊断：检验「因子→收益」关系是否跨公司类型同质（2026-09-01）。

背景：用户指出证劵/农业/银行/半导体等公司类型差异极大，一套因子+单一池化 IC
可能是严重归因错误。既有 `period_level_ic_neutralized` 的 sector 口径只是
「行业内去均值后池化求单一 IC」——它假设所有行业的因子→收益斜率相同，
从未检验这一假设。

本脚本做三件事：
  1. `matrix`  行业 × 因子 可计算性矩阵（哪些因子在哪些行业无定义）
  2. `sector-ic` 每个因子在每个行业的「行业内 IC」逐期计算 → 各行业 IC 分布
     （正负号一致性 + 量级离散度 = 同质性检验）
  3. `split`  金融 vs 非金融 拆分重跑关键因子期级 IC（Fama-French 传统：金融股
     另案处理），看负结论是否因混入金融股而被污染

用法:
  equity_sector_het.py matrix
  equity_sector_het.py sector-ic [--factor revenue_yoy]
  equity_sector_het.py split [--factor revenue_yoy]
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from equity_quant import _load_panel_sector, _load_panel_meta, _spearman, _mean, _std, _ols_residual  # noqa: E402

DB_PATH = Path(__file__).resolve().parents[4] / "outputs" / "equity_fundamental.sqlite"

# 金融类细分（细分程度不一致：银行/保险/券商/交易所的财报结构完全不同）
FIN_TYPES = {
    "bank": {"hk00005", "hk00939", "hk01288", "hk01398", "hk01988",
             "hk02388", "hk03328", "hk03968", "hk03988"},
    "insurance": {"hk01299", "hk01336", "hk02318", "hk02601", "hk02628"},
    "broker": {"hk06030", "hk06886"},
    "exchange": {"hk00388"},
}
FINANCIALS = set().union(*FIN_TYPES.values())

KEY_FACTORS = ["revenue_yoy", "gross_margin", "net_margin_proxy",
               "ocf_to_revenue", "asset_liability_ratio"]

# 类型化因子（2026-09-01 补位）：金融股无毛利、无经营现金流语义，
# ROE/ROA/权益比率/净利润同比才是其概念正确的核心因子。
TYPE_FACTORS = ["roe", "roa", "equity_ratio", "net_profit_yoy"]
ALL_FACTORS = KEY_FACTORS + TYPE_FACTORS

# 因子 → 适用公司类型（"不适用"的因子在该类型上测出来是噪声，不是信号）
APPLICABILITY: dict[str, str] = {
    "gross_margin": "non_financial",          # 金融/能源/公用无销售成本
    "ocf_to_revenue": "non_financial",        # 金融股现金流表口径为存贷/保费
    "inventory_to_revenue": "non_financial",
    "rd_intensity": "non_financial",
    "asset_liability_ratio": "non_financial",  # 对银行是资本结构，语义不同
    "net_margin_proxy": "non_financial",       # 对银行≈效率比，非毛利概念
    "roe": "all", "roa": "all", "equity_ratio": "all",
    "net_profit_yoy": "all", "revenue_yoy": "all",
}

# 多重比较校正（MCC）：11 行业 × N 因子 个格子同时检验，
# Bonferroni 下行业级 t 需远大于 1.96 才能谈 alpha。
N_SECTORS = 11
ALPHA = 0.05


def mcc_threshold(n_factors: int = len(ALL_FACTORS)) -> float:
    """Bonferroni 双尾 t 阈值（正态近似，无 scipy 依赖）。"""
    n_tests = max(1, N_SECTORS * n_factors)
    alpha_per = ALPHA / n_tests
    p = 1 - alpha_per / 2
    lo, hi = 0.0, 8.0
    for _ in range(60):
        mid = (lo + hi) / 2
        phi = 0.5 * (1 + math.erf(mid / math.sqrt(2)))
        if phi < p:
            lo = mid
        else:
            hi = mid
    return round((lo + hi) / 2, 2)


def _sector_of(db: Path) -> dict[str, str]:
    con = sqlite3.connect(db)
    m = dict(con.execute("SELECT ticker, sector FROM universe WHERE included=1").fetchall())
    con.close()
    return m


def _effective_dates(db: Path, factor_key: str) -> list[tuple[str, str]]:
    con = sqlite3.connect(db)
    rows = con.execute(
        "SELECT d.period, MAX(c.release_date) FROM derived_factors d "
        "LEFT JOIN company_facts c ON c.ticker=d.ticker AND c.period=d.period "
        "WHERE d.factor_key=? AND d.source='derived' GROUP BY d.period ORDER BY d.period",
        (factor_key,)).fetchall()
    con.close()
    out = []
    for period, rel in rows:
        if period.endswith("H1"):
            std = f"{period[:4]}-10-01"
        elif period.endswith("H2"):
            std = f"{int(period[:4]) + 1}-05-01"
        else:
            std = period
        out.append((period, min(rel, std) if rel else std))
    return out


def run_matrix(db: Path) -> dict:
    sec = _sector_of(db)
    con = sqlite3.connect(db)
    sectors = sorted({s for s in sec.values()})
    out = {}
    for s in sectors:
        tot = sum(1 for v in sec.values() if v == s)
        out[s] = {"total": tot, "factors": {}}
        for f in KEY_FACTORS:
            n = con.execute(
                "SELECT COUNT(DISTINCT d.ticker) FROM derived_factors d "
                "WHERE d.factor_key=? AND d.period='2026H1' AND d.value IS NOT NULL "
                "AND d.ticker IN (SELECT ticker FROM universe WHERE sector=?)",
                (f, s)).fetchone()[0]
            out[s]["factors"][f] = n
    con.close()
    return out


def run_sector_ic(db: Path, factor_key: str, horizon: str = "fwd_20d",
                  min_stocks: int = 4) -> dict:
    sec = _sector_of(db)
    panel = _load_panel_sector(db, factor_key, horizon, min_stocks)
    eff = _effective_dates(db, factor_key)
    all_dates = sorted(panel.keys())

    per_sector: dict[str, list[float]] = {}
    per_period: dict[str, dict[str, float]] = {}
    for period, e in eff:
        d = next((x for x in all_dates if x >= e), None)
        if d is None:
            continue
        items = [(tk, f, r, sec.get(tk, "unknown")) for tk, f, r, s in panel[d]]
        items = [x for x in items if x[3] != "unknown"]
        by_sector: dict[str, list[tuple]] = {}
        for tk, f, r, s in items:
            by_sector.setdefault(s, []).append((f, r))
        for s, lst in by_sector.items():
            if len(lst) < min_stocks:
                continue
            ic = _spearman([a for a, _ in lst], [b for _, b in lst])
            per_sector.setdefault(s, []).append(ic)
            per_period.setdefault(period, {})[s] = round(ic, 4)

    sector_stats = {}
    for s, ics in per_sector.items():
        n = len(ics)
        pos = round(sum(1 for x in ics if x > 0) / n, 4) if n else 0.0
        if n < 2:
            sector_stats[s] = {"n_periods": n, "ic_mean": round(_mean(ics), 4) if ics else None,
                               "ic_tstat": None, "ic_pos_pct": pos, "signs": [round(x, 4) for x in ics]}
            continue
        m, sd = _mean(ics), _std(ics)
        t = (m / (sd / math.sqrt(n))) if sd > 1e-12 else None
        sector_stats[s] = {"n_periods": n, "ic_mean": round(m, 4),
                           "ic_tstat": (round(t, 4) if t is not None else None),
                           "ic_pos_pct": pos, "signs": [round(x, 4) for x in ics]}

    return {"factor": factor_key, "horizon": horizon,
            "sector_stats": sector_stats, "per_period": per_period}


def run_split(db: Path, factor_key: str, horizon: str = "fwd_20d",
              min_stocks: int = 4) -> dict:
    """金融 vs 非金融 拆分：各算期内 IC（Fama-French 传统金融股另案）。"""
    sec = _sector_of(db)
    panel = _load_panel_sector(db, factor_key, horizon, min_stocks)
    eff = _effective_dates(db, factor_key)
    all_dates = sorted(panel.keys())

    def _ic(keep_fin: bool) -> dict:
        ics = []
        for period, e in eff:
            d = next((x for x in all_dates if x >= e), None)
            if d is None:
                continue
            items = [(tk, f, r) for tk, f, r, s in panel[d]
                     if (tk in FINANCIALS) == keep_fin and sec.get(tk)]
            if len(items) < min_stocks:
                continue
            ics.append(_spearman([a for _, a, _ in items], [b for _, _, b in items]))
        if len(ics) < 2:
            return {"n_periods": len(ics), "ic_mean": None, "ic_tstat": None,
                    "ic_pos_pct": None}
        m, sd = _mean(ics), _std(ics)
        t = (m / (sd / math.sqrt(len(ics)))) if sd > 1e-12 else None
        return {"n_periods": len(ics), "ic_mean": round(m, 4),
                "ic_tstat": (round(t, 4) if t is not None else None),
                "ic_pos_pct": round(sum(1 for x in ics if x > 0) / len(ics), 4)}

    return {"factor": factor_key, "horizon": horizon,
            "financials": _ic(True), "non_financials": _ic(False),
            "n_financials": len(FINANCIALS)}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("cmd", choices=["matrix", "sector-ic", "split"])
    ap.add_argument("--db", default=str(DB_PATH))
    ap.add_argument("--factor", default="revenue_yoy")
    ap.add_argument("--json", default=None)
    args = ap.parse_args()
    db = Path(args.db)

    if args.cmd == "matrix":
        res = run_matrix(db)
        print("== 行业 × 因子 可计算性（2026H1 有值标的数 / 行业总数）==")
        hdr = f"{'sector':22s}" + "".join(f"{f[:7]:>9s}" for f in KEY_FACTORS)
        print(hdr)
        for s, d in res.items():
            line = f"{s:22s}"
            for f in KEY_FACTORS:
                line += f"{d['factors'][f]:>4d}/{d['total']:<4d}".rjust(9)
            print(line)
    elif args.cmd == "sector-ic":
        res = run_sector_ic(db, args.factor)
        print(f"== {args.factor} 各行业内 IC（期级，min 4 只）==")
        print(f"{'sector':22s} {'n':>3s} {'ic_mean':>8s} {'tstat':>7s} {'IC>0%':>6s}  signs")
        fin_sectors = {"Financials"}
        print(f"MCC 阈值 t>={mcc_threshold()}（{N_SECTORS} 行业×{len(ALL_FACTORS)} 因子 Bonferroni）")
        print(f"{'sector':22s} {'n':>3s} {'ic_mean':>8s} {'tstat':>7s} {'IC>0%':>6s}  flags")
        for s, d in sorted(res["sector_stats"].items(), key=lambda kv: -(kv[1].get("ic_tstat") or 0)):
            t = d.get("ic_tstat")
            flags = []
            if APPLICABILITY.get(args.factor) == "non_financial" and s in fin_sectors:
                flags.append("N/A-类型不适用")
            if t is not None and abs(t) >= mcc_threshold():
                flags.append(f"通过MCC({'正' if t > 0 else '负'}信号)")
            elif t is not None and abs(t) >= 1.96:
                flags.append("边缘未过MCC")
            if (d.get("n_periods") or 0) < 5:
                flags.append(f"n={d.get('n_periods')}样本不足")
            print(f"{s:22s} {d['n_periods']:>3d} {d['ic_mean']:>8.4f} "
                  f"{(t if t is not None else float('nan')):>7.2f} {d.get('ic_pos_pct', 0):>6.0%}  "
                  f"{','.join(flags)}")
    elif args.cmd == "split":
        res = run_split(db, args.factor)
        print(f"== {args.factor} 金融 vs 非金融 ==")
        for k in ("financials", "non_financials"):
            d = res[k]
            print(f"  {k:15s} n={d['n_periods']} ic_mean={d['ic_mean']} "
                  f"t={d['ic_tstat']} IC>0={d['ic_pos_pct']}")

    if args.json:
        Path(args.json).write_text(json.dumps(res, ensure_ascii=False, indent=2, default=str),
                                   encoding="utf-8")
        print(f"[sector-het] 已落盘 {args.json}")


if __name__ == "__main__":
    main()
