#!/usr/bin/env python3
"""Cross-sectional quant engine: panel IC, group backtest, neutralization, benchmarks.

路线 B（真量化）的计算核心。在 quant_engine.py（单票原语）之上构建：
  1. 跨截面 IC：每期对所有标的计算因子-收益 rank IC → IC 序列
  2. IC 统计：均值、标准差、ICIR、IC>0 占比、IC 衰减
  3. 分层回测：按因子排序分 N 组 → 各组平均收益 → 多空组合
  4. 行业/市值中性化：截面回归去行业和市值效应
  5. 基准对比：超额收益、tracking error、information ratio
  6. 综合报告：一次性输出因子评估全貌

数据来源：equity_data_model.py 的 forward_returns + v_panel + benchmarks

CLI:
  ic --factor <key> --horizon fwd_20d    跨截面 IC 分析
  group --factor <key> --groups 5        分层回测
  report --factor <key>                  综合因子报告
  self-test                              已知答案自测
"""
from __future__ import annotations

import argparse
import math
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parents[4] / "outputs" / "equity_fundamental.sqlite"
TRADING_DAYS = 252


# ============================================================ Stats Primitives ===

def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _std(xs: list[float], ddof: int = 1) -> float:
    n = len(xs)
    if n <= ddof:
        return 0.0
    m = _mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (n - ddof))


def _rank(xs: list[float]) -> list[float]:
    """Average-rank normalization (handles ties)."""
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    ranks = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        avg = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def _spearman(xs: list[float], ys: list[float]) -> float:
    """Spearman rank correlation."""
    if len(xs) < 3:
        return 0.0
    rx, ry = _rank(xs), _rank(ys)
    mx, my = _mean(rx), _mean(ry)
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    vx = math.sqrt(sum((a - mx) ** 2 for a in rx))
    vy = math.sqrt(sum((b - my) ** 2 for b in ry))
    return cov / (vx * vy) if vx > 0 and vy > 0 else 0.0


def _percentile(xs: list[float], p: float) -> float:
    """p-th percentile (0-1)."""
    s = sorted(xs)
    k = (len(s) - 1) * p
    lo, hi = int(math.floor(k)), int(math.ceil(k))
    if lo == hi:
        return s[lo]
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


# ============================================================ Panel Data Loading ===


def _load_panel_data(db: Path, factor_key: str, horizon: str,
                     min_stocks: int = 3) -> dict[str, list[tuple[str, float, float]]]:
    """加载面板数据：{trade_date: [(ticker, factor_value, forward_return), ...]}

    使用 forward-fill：因子值从生效日起持续有效，直到下一个更新。

    ⚠️ 生效日 = **财报实际发布日（release_date）**，不是报告期末（08-31 修复）。
    此前用 period-end（H1→7/1、H2→次年1/1）是**严重前视偏差**：
    港股中期报告 6/30 截止但 8-9 月才发布，年报 12/31 截止但次年 3-4 月才发布，
    用报告期末会让回测用到当时不存在的数据，系统性高估因子有效性。

    价格因子（price_computed）无发布日概念，生效日 = period 日期的次日。
    """
    # 只取 derived（基本面）与 price_computed（价格）因子。
    # **排除 macro_aligned**：宏观因子对齐到个股时同一时点所有股票同值
    # （横截面零区分度），参与个股 IC 是数学上无意义的冗余——宏观因子属
    # 时序/行业层面变量，正确用法是对齐到指数或行业做择时，而非个股横截面。
    con = sqlite3.connect(db)
    # 基本面因子按 (ticker, period) 关联 company_facts 取实际发布日
    factor_rows = con.execute(
        "SELECT d.ticker, d.period, d.value, MAX(c.release_date) AS release_date "
        "FROM derived_factors d "
        "LEFT JOIN company_facts c ON c.ticker=d.ticker AND c.period=d.period "
        "WHERE d.factor_key=? AND d.source='derived' "
        "GROUP BY d.ticker, d.period",
        (factor_key,)).fetchall()
    # 价格因子：period 本身就是日期
    price_rows = con.execute(
        "SELECT ticker, period, value, NULL FROM derived_factors "
        "WHERE factor_key=? AND source='price_computed' ORDER BY ticker, period",
        (factor_key,)).fetchall()
    con.close()
    factor_rows = list(factor_rows) + list(price_rows)

    # 生效日 = min(实际发布日, 该报告期的标准发布日)。
    #
    # 为什么取 min 而非直接用 release_date：回溯抽取时，prior 期的 release_date
    # 记的是**回溯当次的公告日**（如 2024H1 数据显示为 2025-09），而非该期原始
    # 发布日，会导致多个历史期的因子同时生效、forward-fill 互相覆盖。
    # 取 min 可让历史期回落到其自身的发布时点（2024H1→2024-10），
    # 而最新期若在 10 月前发布（如 2026H1 于 8/26），则采用真实的更早发布日。
    import datetime as _dt
    def _standard_release(period: str) -> str:
        """该报告期的保守标准发布日（港股法定最迟期限后）。"""
        if period.endswith("H1"):
            return f"{period[:4]}-10-01"   # 中期报告最迟 8/31
        if period.endswith("H2"):
            return f"{int(period[:4]) + 1}-05-01"  # 年报最迟次年 4/30
        return period

    def _effective(period: str, release_date: str | None) -> str:
        std = _standard_release(period)
        if release_date and std != period:
            return min(release_date, std)
        if release_date:
            return release_date
        if std != period:
            return std
        if "-" in period:  # 价格因子：period 即日期，次日起生效
            parts = period.split("-")
            try:
                d = _dt.date(int(parts[0]), int(parts[1]), int(parts[2]))
                return (d + _dt.timedelta(days=1)).isoformat()
            except ValueError:
                return period
        return period

    ticker_factors: dict[str, list[tuple[str, float]]] = {}
    for tk, period, val, rel in factor_rows:
        if val is None:
            continue
        eff = _effective(period, rel)
        ticker_factors.setdefault(tk, []).append((eff, val))

    # 对每个 ticker 按生效日排序
    for tk in ticker_factors:
        ticker_factors[tk].sort(key=lambda x: x[0])

    # 获取所有标的的前向收益
    con2 = sqlite3.connect(db)
    fwd_rows = con2.execute(
        f"SELECT ticker, trade_date, {horizon} FROM forward_returns "
        f"WHERE {horizon} IS NOT NULL ORDER BY trade_date, ticker"
    ).fetchall()
    con2.close()

    # Forward-fill：对每个 ticker 的每个交易日，取该日期之前最新的因子值
    by_date: dict[str, list[tuple[str, float, float]]] = {}
    for tk, td, fwd_val in fwd_rows:
        if tk not in ticker_factors:
            continue
        # 找该日期之前最新的因子值
        factor_val = None
        for eff, val in ticker_factors[tk]:
            if eff <= td:
                factor_val = val
            else:
                break
        if factor_val is not None:
            by_date.setdefault(td, []).append((tk, factor_val, fwd_val))

    # 过滤数据不足的日期
    return {d: items for d, items in by_date.items() if len(items) >= min_stocks}


def _load_panel_sector(db: Path, factor_key: str, horizon: str,
                       min_stocks: int = 3) -> dict[str, list[tuple[str, float, float, str]]]:
    """面板数据带行业：{trade_date: [(ticker, factor_value, fwd_return, sector), ...]}"""
    panel = _load_panel_data(db, factor_key, horizon, min_stocks)
    con = sqlite3.connect(db)
    sec = dict(con.execute("SELECT ticker, sector FROM sector_map").fetchall())
    con.close()
    return {d: [(tk, v, r, sec.get(tk, "unknown")) for tk, v, r in items]
            for d, items in panel.items()}


def _load_panel_meta(db: Path, factor_key: str, horizon: str,
                     min_stocks: int = 3) -> dict[str, list[tuple[str, float, float, str, float]]]:
    """面板数据带行业+市值：{date: [(ticker, factor, fwd_ret, sector, log_mcap), ...]}

    时变市值 = 当日收盘价 × 推算股本（股本来自 qt 接口，假设稳定）。
    市值中性化是因子分析的标准步骤：小盘股波动/动量天然不同，不控制市值
    会让因子 IC 沦为市值的代理变量（审视发现的 P1 问题）。
    """
    panel = _load_panel_data(db, factor_key, horizon, min_stocks)
    con = sqlite3.connect(db)
    sec = dict(con.execute("SELECT ticker, sector FROM sector_map").fetchall())
    shares = dict(con.execute(
        "SELECT ticker, shares_outstanding FROM universe "
        "WHERE shares_outstanding IS NOT NULL").fetchall())
    closes: dict[str, dict[str, float]] = {}
    for tk, d, c in con.execute(
            "SELECT ticker, quote_date, close FROM daily_quotes WHERE close IS NOT NULL"):
        closes.setdefault(tk, {})[d] = c
    con.close()

    out: dict[str, list[tuple[str, float, float, str, float]]] = {}
    for date, items in panel.items():
        row = []
        for tk, v, r in items:
            sh = shares.get(tk)
            px = closes.get(tk, {}).get(date)
            lm = math.log(sh * px) if (sh and px and sh > 0 and px > 0) else None
            row.append((tk, v, r, sec.get(tk, "unknown"), lm))
        out[date] = row
    return out


def _ols_residual(xs: list[float], ys: list[float]) -> list[float] | None:
    """一元 OLS 残差 y - (a + b*x)。用于市值中性化（对 log 市值回归取残差）。"""
    n = len(xs)
    if n < 3:
        return None
    mx, my = _mean(xs), _mean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx <= 0:
        return None
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    a = my - b * mx
    return [y - (a + b * x) for x, y in zip(xs, ys)]


# ============================================================ Cross-Sectional IC ===


def _horizon_days(horizon: str) -> int:
    """fwd_20d -> 20。解析失败返回 1。"""
    import re
    m = re.match(r"fwd_(\d+)d", horizon)
    return int(m.group(1)) if m else 1


def newey_west_tstat(series: list[float], lags: int) -> float | None:
    """Newey-West HAC t 统计量：用于重叠窗口的 IC 序列。

    重叠收益的 IC 序列存在自相关，朴素 t（mean/(std/sqrt(n))）会系统性高估
    显著性；非重叠采样虽无偏，却丢弃 ~95% 样本（统计功效大幅下降）。
    Newey-West 是金融计量标准解法：用**全样本逐日 IC**，只修正自相关的方差
    估计，兼顾无偏与功效。lags 取持有期天数-1（H 期重叠的自相关阶数）。
    """
    n = len(series)
    if n < lags + 3:
        return None
    mean = _mean(series)
    e = [x - mean for x in series]
    var = sum(x * x for x in e) / n
    for j in range(1, lags + 1):
        gj = sum(e[i] * e[i - j] for i in range(j, n)) / n
        var += 2 * (1 - j / (lags + 1)) * gj  # Bartlett 核
    if var <= 0:
        return None
    se = math.sqrt(var / n)
    return mean / se if se > 0 else 0.0


def cross_sectional_ic(db: Path, factor_key: str, horizon: str = "fwd_20d",
                       min_stocks: int = 3, nonoverlap: bool = False) -> dict:
    """跨截面 IC 分析：每期 rank IC → IC 序列统计。

    nonoverlap=False（默认）：逐日建仓，相邻持有窗口重叠，t-stat 会因自相关偏高。
    nonoverlap=True：按持有期天数跳跃采样，窗口互不重叠，t-stat 无偏（保守口径）。
    结论判定应以 nonoverlap=True 为准。
    """
    """跨截面 IC 分析：每期 rank IC → IC 序列统计。

    返回:
    {
        "factor": str, "horizon": str, "n_dates": int,
        "ic_mean": float, "ic_std": float, "icir": float,
        "ic_positive_pct": float,
        "ic_series": [(date, ic), ...],
        "ic_tstat": float,
    }
    """
    panel = _load_panel_data(db, factor_key, horizon, min_stocks)
    if len(panel) < 3:
        return {"factor": factor_key, "horizon": horizon, "n_dates": len(panel),
                "error": f"insufficient cross-sections ({len(panel)} < 3)"}

    ic_series = []
    all_dates = sorted(panel.keys())
    step = _horizon_days(horizon) if nonoverlap else 1
    for date in all_dates[::step]:
        items = panel[date]
        factors = [v for _, v, _ in items]
        returns = [r for _, _, r in items]
        if len(factors) < 3:
            continue
        ic = _spearman(factors, returns)
        ic_series.append((date, round(ic, 6)))

    if not ic_series:
        return {"factor": factor_key, "horizon": horizon, "n_dates": 0,
                "error": "no valid IC observations"}

    ics = [v for _, v in ic_series]
    ic_mean = _mean(ics)
    ic_std = _std(ics)
    icir = ic_mean / ic_std if ic_std > 0 else 0.0
    ic_pos = sum(1 for x in ics if x > 0) / len(ics)
    # t-test: IC mean / (IC std / sqrt(n))
    t_stat = ic_mean / (ic_std / math.sqrt(len(ics))) if ic_std > 0 else 0.0

    # Newey-West：始终基于**逐日** IC 序列（全样本）计算，与 nonoverlap 无关。
    # 逐日序列自相关经 HAC 修正后无偏，且保留全部样本（功效最高）。
    daily_ic = []
    for date in all_dates:
        items = panel[date]
        if len(items) < 3:
            continue
        daily_ic.append(_spearman([v for _, v, _ in items], [r for _, _, r in items]))
    nw_t = newey_west_tstat(daily_ic, max(1, _horizon_days(horizon) - 1))

    return {
        "factor": factor_key, "horizon": horizon, "n_dates": len(ics),
        "ic_mean": round(ic_mean, 6), "ic_std": round(ic_std, 6),
        "icir": round(icir, 4), "ic_positive_pct": round(ic_pos, 4),
        "ic_tstat": round(t_stat, 4),
        "n_daily": len(daily_ic),
        "ic_tstat_nw": round(nw_t, 4) if nw_t is not None else None,
        "ic_series": ic_series,
    }


# ============================================================ Group Backtest ===


def group_backtest(db: Path, factor_key: str, horizon: str = "fwd_20d",
                   n_groups: int = 5, min_stocks: int = 3,
                   nonoverlap: bool = False) -> dict:
    """分层回测：按因子排序分 N 组 → 各组平均未来收益 → 多空组合。

    nonoverlap 语义同 cross_sectional_ic：True 时按持有期天数跳跃采样，
    窗口互不重叠，Sharpe/t 统计不被自相关高估（保守口径，结论以此为准）。

    返回:
    {
        "factor": str, "horizon": str, "n_groups": int, "n_dates": int,
        "groups": {group_id: {"mean_return": float, "cumulative": float}},
        "long_short": {"mean_return": float, "cumulative": float, "sharpe": float},
    }
    """
    panel = _load_panel_data(db, factor_key, horizon, min_stocks)
    if len(panel) < 3:
        return {"factor": factor_key, "error": f"insufficient data ({len(panel)} dates)"}

    group_returns: dict[int, list[float]] = {g: [] for g in range(n_groups)}
    ls_returns = []

    step = _horizon_days(horizon) if nonoverlap else 1
    for date in sorted(panel.keys())[::step]:
        items = panel[date]
        if len(items) < n_groups:
            continue
        # 按因子值排序
        sorted_items = sorted(items, key=lambda x: x[1])
        chunk_size = len(sorted_items) // n_groups
        for g in range(n_groups):
            start = g * chunk_size
            end = start + chunk_size if g < n_groups - 1 else len(sorted_items)
            group_rets = [r for _, _, r in sorted_items[start:end]]
            group_returns[g].append(_mean(group_rets))
        # 多空：top group - bottom group
        bottom = [r for _, _, r in sorted_items[:chunk_size]]
        top = [r for _, _, r in sorted_items[-chunk_size:]]
        ls_returns.append(_mean(top) - _mean(bottom))

    if not ls_returns:
        return {"factor": factor_key, "error": "no valid group observations"}

    groups = {}
    for g in range(n_groups):
        gr = group_returns[g]
        cum = 1.0
        for r in gr:
            cum *= (1 + r)
        groups[g + 1] = {
            "mean_return": round(_mean(gr), 6),
            "cumulative": round(cum - 1, 6),
            "n_periods": len(gr),
        }

    ls_cum = 1.0
    for r in ls_returns:
        ls_cum *= (1 + r)
    h_days = _horizon_days(horizon)
    ls_vol = _std(ls_returns) * math.sqrt(TRADING_DAYS / max(1, h_days))
    ls_sharpe = (_mean(ls_returns) * TRADING_DAYS / 20) / ls_vol if ls_vol > 0 else 0.0

    return {
        "factor": factor_key, "horizon": horizon, "n_groups": n_groups,
        "n_dates": len(ls_returns),
        "groups": groups,
        "long_short": {
            "mean_return": round(_mean(ls_returns), 6),
            "cumulative": round(ls_cum - 1, 6),
            "sharpe": round(ls_sharpe, 4),
        },
    }


# ============================================================ Benchmark Comparison ===


def benchmark_comparison(db: Path, ticker: str, horizon: str = "fwd_20d",
                         index_code: str = "HSI") -> dict:
    """个股 vs 基准的超额收益分析。"""
    con = sqlite3.connect(db)
    # 个股收益
    stock_rows = con.execute(
        f"SELECT trade_date, {horizon} FROM forward_returns "
        f"WHERE ticker=? AND {horizon} IS NOT NULL ORDER BY trade_date",
        (ticker,)).fetchall()
    # 基准收益
    bench_rows = con.execute(
        f"SELECT trade_date, close FROM benchmarks WHERE index_code=? ORDER BY trade_date",
        (index_code,)).fetchall()
    con.close()

    if not stock_rows or not bench_rows:
        return {"error": "insufficient data"}

    # 基准前向收益
    bench_dates = [r[0] for r in bench_rows]
    bench_closes = [r[1] for r in bench_rows]
    bench_fwd = {}
    horizon_days = {"fwd_1d": 1, "fwd_5d": 5, "fwd_20d": 20, "fwd_60d": 60}
    hd = horizon_days.get(horizon, 20)
    for i in range(len(bench_closes) - hd):
        bench_fwd[bench_dates[i]] = (bench_closes[i + hd] / bench_closes[i]) - 1

    # 对齐日期
    aligned = []
    for td, stock_ret in stock_rows:
        if td in bench_fwd:
            aligned.append((td, stock_ret, bench_fwd[td]))

    if len(aligned) < 3:
        return {"error": f"insufficient aligned data ({len(aligned)} dates)"}

    excess = [s - b for _, s, b in aligned]
    tracking_err = _std(excess)
    info_ratio = _mean(excess) / tracking_err if tracking_err > 0 else 0.0
    beta_num = sum((s - _mean([x[1] for x in aligned])) * (b - _mean([x[2] for x in aligned]))
                   for _, s, b in aligned)
    beta_den = sum((b - _mean([x[2] for x in aligned])) ** 2 for _, _, b in aligned)
    beta = beta_num / beta_den if beta_den > 0 else 0.0

    return {
        "ticker": ticker, "index": index_code, "horizon": horizon,
        "n_aligned": len(aligned),
        "stock_mean": round(_mean([s for _, s, _ in aligned]), 6),
        "bench_mean": round(_mean([b for _, _, b in aligned]), 6),
        "excess_mean": round(_mean(excess), 6),
        "tracking_error": round(tracking_err, 6),
        "information_ratio": round(info_ratio, 4),
        "beta": round(beta, 4),
    }


# ============================================================ Neutralization ===


def neutralized_ic(db: Path, factor_key: str, horizon: str = "fwd_20d",
                   min_stocks: int = 3, nonoverlap: bool = False) -> dict:
    """行业中性化后的跨截面 IC：对因子与收益**双向**做行业均值中心化，再算 rank IC。

    目的：剥离行业 beta 混杂，识别因子在"行业内相对强弱"上的真实预测力（纯 alpha）。
    单行业截面（无法中性化）跳过。判定口径同 cross_sectional_ic（nonoverlap 为准）。
    """
    panel = _load_panel_sector(db, factor_key, horizon, min_stocks)
    if len(panel) < 3:
        return {"factor": factor_key, "horizon": horizon, "error": "insufficient cross-sections"}

    ic_series = []
    step = _horizon_days(horizon) if nonoverlap else 1
    skipped_no_neutral = 0
    for date in sorted(panel.keys())[::step]:
        items = panel[date]
        # 剔除 <2 只的行业（行业内残差恒 0，无中性化信息），保留 >=2 只的行业
        per_sector: dict[str, int] = {}
        for _, _, _, s in items:
            per_sector[s] = per_sector.get(s, 0) + 1
        kept = [(tk, v, r, s) for tk, v, r, s in items if per_sector[s] >= 2]
        sectors = {s for _, _, _, s in kept}
        # 中性化需要 >=2 个行业、>=3 只股票
        if len(sectors) < 2 or len(kept) < 3:
            skipped_no_neutral += 1
            continue
        sector_vals: dict[str, list[float]] = {}
        sector_rets: dict[str, list[float]] = {}
        for _, v, r, s in kept:
            sector_vals.setdefault(s, []).append(v)
            sector_rets.setdefault(s, []).append(r)
        sector_avg = {s: _mean(vs) for s, vs in sector_vals.items()}
        ret_avg = {s: _mean(rs) for s, rs in sector_rets.items()}
        # 双向中性化：因子与收益均减行业均值
        neut = [(tk, v - sector_avg[s], r - ret_avg[s]) for tk, v, r, s in kept]
        factors = [v for _, v, _ in neut]
        returns = [r for _, _, r in neut]
        ic_series.append((date, round(_spearman(factors, returns), 6)))

    if not ic_series:
        return {"factor": factor_key, "horizon": horizon, "n_dates": 0,
                "error": "no neutralizable cross-sections"}

    ics = [v for _, v in ic_series]
    ic_mean = _mean(ics)
    ic_std = _std(ics)
    icir = ic_mean / ic_std if ic_std > 0 else 0.0
    ic_pos = sum(1 for x in ics if x > 0) / len(ics)
    t_stat = ic_mean / (ic_std / math.sqrt(len(ics))) if ic_std > 0 else 0.0
    return {
        "factor": factor_key, "horizon": horizon,
        "n_dates": len(ics), "skipped_no_neutral": skipped_no_neutral,
        "ic_mean": round(ic_mean, 6), "ic_std": round(ic_std, 6),
        "icir": round(icir, 4), "ic_positive_pct": round(ic_pos, 4),
        "ic_tstat": round(t_stat, 4), "ic_series": ic_series,
    }


def size_neutralized_ic(db: Path, factor_key: str, horizon: str = "fwd_20d",
                        min_stocks: int = 3, nonoverlap: bool = False,
                        neutralize_sector: bool = True) -> dict:
    """市值中性化 IC（可叠加行业中性化），剥离市值混杂后的因子预测力。

    市值混杂是因子分析的经典陷阱：小盘股波动率/动量天然更高，若不控制市值，
    因子 IC 可能只是"小盘效应"的代理，而非因子本身的选股能力。

    每个截面：
      1. 因子值与收益各自对 log(市值) 做 OLS 回归，取残差（剔除市值线性影响）；
      2. （可选）再对残差做行业均值中心化，剔除行业 beta；
      3. 对中性化后的因子/收益算 Spearman rank IC。
    """
    panel = _load_panel_meta(db, factor_key, horizon, min_stocks)
    if len(panel) < 3:
        return {"factor": factor_key, "horizon": horizon,
                "error": "insufficient cross-sections"}

    ic_series: list[tuple[str, float]] = []
    step = _horizon_days(horizon) if nonoverlap else 1
    skipped = 0
    for date in sorted(panel.keys())[::step]:
        rows = [(tk, v, r, s, lm) for tk, v, r, s, lm in panel[date]
                if lm is not None]
        if len(rows) < 5:  # 回归至少需要若干有效市值样本
            skipped += 1
            continue
        lms = [x[4] for x in rows]
        f_res = _ols_residual(lms, [x[1] for x in rows])
        r_res = _ols_residual(lms, [x[2] for x in rows])
        if not f_res or not r_res:
            skipped += 1
            continue
        if neutralize_sector:
            per_sector: dict[str, int] = {}
            for x in rows:
                per_sector[x[3]] = per_sector.get(x[3], 0) + 1
            kept = [(i, x) for i, x in enumerate(rows) if per_sector[x[3]] >= 2]
            if len(kept) < 3 or len({x[3] for _, x in kept}) < 2:
                skipped += 1
                continue
            sv, sr = {}, {}
            for i, x in kept:
                sv.setdefault(x[3], []).append(f_res[i])
                sr.setdefault(x[3], []).append(r_res[i])
            sav = {k: _mean(v) for k, v in sv.items()}
            rav = {k: _mean(v) for k, v in sr.items()}
            fs = [f_res[i] - sav[x[3]] for i, x in kept]
            rs = [r_res[i] - rav[x[3]] for i, x in kept]
        else:
            fs, rs = f_res, r_res
        if len(fs) < 3:
            skipped += 1
            continue
        ic_series.append((date, round(_spearman(fs, rs), 6)))

    if not ic_series:
        return {"factor": factor_key, "horizon": horizon, "n_dates": 0,
                "skipped": skipped, "error": "no cross-sections with valid market cap"}

    ics = [v for _, v in ic_series]
    ic_mean, ic_std = _mean(ics), _std(ics)
    icir = ic_mean / ic_std if ic_std > 0 else 0.0
    ic_pos = sum(1 for x in ics if x > 0) / len(ics)
    t_stat = ic_mean / (ic_std / math.sqrt(len(ics))) if ic_std > 0 else 0.0
    # 逐日序列的 Newey-West（与主判定口径一致）
    daily_ic = []
    for date in sorted(panel.keys()):
        rows = [(tk, v, r, s, lm) for tk, v, r, s, lm in panel[date] if lm is not None]
        if len(rows) < 5:
            continue
        lms = [x[4] for x in rows]
        f_res = _ols_residual(lms, [x[1] for x in rows])
        r_res = _ols_residual(lms, [x[2] for x in rows])
        if not f_res or not r_res:
            continue
        if neutralize_sector:
            per_sector = {}
            for x in rows:
                per_sector[x[3]] = per_sector.get(x[3], 0) + 1
            kept = [(i, x) for i, x in enumerate(rows) if per_sector[x[3]] >= 2]
            if len(kept) < 3 or len({x[3] for _, x in kept}) < 2:
                continue
            sv, sr = {}, {}
            for i, x in kept:
                sv.setdefault(x[3], []).append(f_res[i])
                sr.setdefault(x[3], []).append(r_res[i])
            sav = {k: _mean(v) for k, v in sv.items()}
            rav = {k: _mean(v) for k, v in sr.items()}
            fs = [f_res[i] - sav[x[3]] for i, x in kept]
            rs = [r_res[i] - rav[x[3]] for i, x in kept]
        else:
            fs, rs = f_res, r_res
        if len(fs) >= 3:
            daily_ic.append(_spearman(fs, rs))
    nw_t = newey_west_tstat(daily_ic, max(1, _horizon_days(horizon) - 1))

    return {
        "factor": factor_key, "horizon": horizon,
        "n_dates": len(ics), "n_daily": len(daily_ic), "skipped": skipped,
        "neutralize_sector": neutralize_sector,
        "ic_mean": round(ic_mean, 6), "ic_std": round(ic_std, 6),
        "icir": round(icir, 4), "ic_positive_pct": round(ic_pos, 4),
        "ic_tstat": round(t_stat, 4),
        "ic_tstat_nw": round(nw_t, 4) if nw_t is not None else None,
        "ic_series": ic_series,
    }


def neutralize_cross_section(db: Path, factor_key: str, trade_date: str) -> dict:
    """截面中性化：对给定日期的因子值做行业+市值回归去效应。

    返回中性化后的因子值（截面残差）。
    简化实现：行业均值中心化（不引入 OLS 依赖）。
    """
    con = sqlite3.connect(db)
    # 获取该日期所有标的的因子值
    factor_rows = con.execute(
        "SELECT ticker, value FROM derived_factors "
        "WHERE factor_key=? AND period=? AND source='derived'",
        (factor_key, trade_date)).fetchall()
    # 获取行业映射
    sector_rows = con.execute("SELECT ticker, sector FROM sector_map").fetchall()
    con.close()

    sector_map = {tk: sec for tk, sec in sector_rows}
    items = [(tk, val, sector_map.get(tk, "unknown")) for tk, val in factor_rows if val is not None]

    if len(items) < 3:
        return {"error": "insufficient data", "neutralized": {}}

    # 行业均值中心化
    sector_means: dict[str, list[float]] = {}
    for _, val, sec in items:
        sector_means.setdefault(sec, []).append(val)
    sector_avg = {sec: _mean(vals) for sec, vals in sector_means.items()}

    neutralized = {}
    for tk, val, sec in items:
        neutralized[tk] = round(val - sector_avg.get(sec, 0), 6)

    return {
        "factor": factor_key, "date": trade_date,
        "n_stocks": len(items), "n_sectors": len(sector_means),
        "neutralized": neutralized,
    }


# ============================================================ Comprehensive Report ===


def event_study(db: Path, surprise_factor: str = "revenue_yoy",
                horizon: str = "fwd_20d", n_groups: int = 3,
                industry_neutralized: bool = True) -> dict:
    """PEAD 事件研究：以**精确发布日**为锚点的盈余漂移检验（与 IC 框架正交）。

    IC 框架的局限：因子生效后长期持有，混杂了估值/市场节奏；PEAD 聚焦
    「公告后 H 日」的窄窗口，检验市场对盈余惊喜的即时定价偏差——这是
    基本面信息最经典的 alpha 来源。

    每个事件 = (ticker, period)：
      1. 事件日 e = 发布日后第一个交易日（公告多在盘后，次日才可交易）；
      2. 异常收益 AR = 股票 e→e+H 收益 − HSI 同窗口收益（剥离市场）；
      3. 惊喜 = surprise_factor 该期值（同比增速作惊喜代理，无分析师预期数据）。

    行业中性化（默认开启）：AR 再减「同行业同期事件均值」，剥离行业 beta。
    ⚠️ 第 7 次归因纠错：gross_margin PEAD 信号完全是行业 beta——raw AR 高低组差
    -6.7%（t=-3.24）但行业中性化后归零（t=+0.06）。故默认以**中性化口径为结论**，
    raw 仅作对照并明确警示「raw 显著可能是行业/市场 beta 而非因子信号」。
    """
    h = _horizon_days(horizon)
    con = sqlite3.connect(db)
    events = con.execute(
        "SELECT c.ticker, c.period, MIN(substr(c.release_date,1,10)) FROM company_facts c "
        "WHERE c.release_date IS NOT NULL AND (c.period LIKE '%H1' OR c.period LIKE '%H2') "
        "GROUP BY c.ticker, c.period").fetchall()
    surprises: dict[tuple[str, str], float] = {
        (tk, p): v for tk, p, v in con.execute(
            "SELECT ticker, period, value FROM derived_factors "
            "WHERE factor_key=? AND source='derived'", (surprise_factor,))}
    fwd: dict[str, dict[str, float]] = {}
    for tk, d, f in con.execute(
            f"SELECT ticker, trade_date, {horizon} FROM forward_returns"):
        fwd.setdefault(tk, {})[d] = f
    hsi = dict(con.execute(
        "SELECT trade_date, close FROM benchmarks WHERE index_code='HSI' ORDER BY trade_date").fetchall())
    sec = dict(con.execute("SELECT ticker, sector FROM sector_map").fetchall())
    con.close()

    hsi_dates = sorted(hsi.keys())
    closes = [hsi[d] for d in hsi_dates]
    hsi_fwd = {d: (closes[i + h] / closes[i] - 1 if i + h < len(closes) else None)
               for i, d in enumerate(hsi_dates)}

    # by_period: (surprise, raw_AR, sector)   raw_AR = 个股收益 − HSI 同窗口
    by_period: dict[str, list[tuple[float, float, str]]] = {}
    for tk, p, rel in events:
        s = surprises.get((tk, p))
        if s is None:
            continue
        dts = sorted(fwd.get(tk, {}).keys())
        e = next((d for d in dts if d >= rel), None)  # 发布后首个交易日（盘后公告→次日可交易）
        if e is None:
            continue
        ar_s = fwd.get(tk, {}).get(e)
        hs = hsi_fwd.get(e)
        if ar_s is None or hs is None:
            continue
        by_period.setdefault(p, []).append((s, ar_s - hs, sec.get(tk, "unknown")))

    if len(by_period) < 3:
        return {"factor": surprise_factor, "horizon": horizon,
                "n_events": sum(len(v) for v in by_period.values()),
                "error": f"有效期数不足（{len(by_period)} < 3）"}

    def _period_stats(items: list[tuple[float, float, str]]) -> tuple[float, float]:
        """一期事件截面 → (IC, 高低组 AR 差)。

        industry_neutralized=True：AR 先减同行业同期均值（剥离行业 beta，
        第 7 次归因纠错的口径）。因子侧不调整（惊喜是横截面排序键）。
        """
        ss = [x[0] for x in items]
        ars = [x[1] for x in items]
        if industry_neutralized:
            per_sec: dict[str, list[float]] = {}
            for x in items:
                per_sec.setdefault(x[2], []).append(x[1])
            sec_avg = {k: _mean(v) for k, v in per_sec.items()}
            ars = [x[1] - sec_avg.get(x[2], 0.0) for x in items]
        ic = round(_spearman(ss, ars), 4)
        order = sorted(range(len(ss)), key=lambda i: ss[i])
        bins: list[list[float]] = [[] for _ in range(n_groups)]
        for rank, i in enumerate(order):
            bins[min(rank * n_groups // len(order), n_groups - 1)].append(ars[i])
        grp = {g: _mean(b) for g, b in enumerate(bins, 1)}
        return ic, grp[n_groups] - grp[1]

    per_period_ic: dict[str, float] = {}
    group_diff: dict[str, float] = {}
    n_events = 0
    for p in sorted(by_period.keys()):
        items = by_period[p]
        if len(items) < 8:  # 事件截面太小无意义
            continue
        n_events += len(items)
        ic, diff = _period_stats(items)
        per_period_ic[p] = ic
        group_diff[p] = round(diff, 4)

    def _period_t(d: dict[str, float]) -> tuple[float, float, int]:
        vals = list(d.values())
        n_p = len(vals)
        if n_p < 2:
            return 0.0, 0.0, n_p
        m, s = _mean(vals), _std(vals)
        t = m / (s / math.sqrt(n_p)) if s > 0 else 0.0
        return round(m, 4), round(t, 4), n_p

    ic_m, ic_t, n_p = _period_t(per_period_ic)
    diff_m, diff_t, _ = _period_t(group_diff)

    return {
        "factor": surprise_factor, "horizon": horizon,
        "industry_neutralized": industry_neutralized,
        "n_events": n_events, "n_periods": n_p,
        "periods": sorted(per_period_ic.keys()),
        "event_ic_mean": ic_m, "event_ic_t": ic_t,
        "high_minus_low_ar_mean": diff_m,
        "high_minus_low_ar_t": diff_t,
        "ic_by_period": per_period_ic,
        "group_diff_by_period": group_diff,
        "note": (
            "AR 剥离 HSI 同窗口；"
            + ("**已进一步行业中性化（AR−同行业同期均值），诚实口径**——"
               if industry_neutralized else
               "⚠️ 仅市场调整、**未行业中性化**：raw 显著可能是行业/市场 beta 而非因子信号"
               "（gross_margin 曾因此被误判，第 7 次归因纠错）。")
            + f"期级 t 自由度 {n_p-1}；惊喜用同比增速代理（无分析师预期数据）。"),
    }


def period_level_ic(db: Path, factor_key: str, horizon: str = "fwd_20d",
                    min_stocks: int = 5) -> dict:
    """**报告期级别** IC（Fama-MacBeth 式）：低频因子的诚实口径。

    为何需要它：基本面因子半年才更新，同一份因子值会对着几十个不同收益窗口
    算出几十个 IC。Newey-West 只修正了**收益窗口**的自相关，没处理**因子值
    重复**的问题——把 n=449 当样本量，t 值仍被系统性高估。

    本函数每个报告期只取**一个**截面 IC（因子生效日的横截面），再用
    N = 报告期数 计算 t 值（自由度 N-1），诚实反映真实样本量。
    """
    panel = _load_panel_data(db, factor_key, horizon, min_stocks)
    if not panel:
        return {"factor": factor_key, "error": "no panel data"}

    con = sqlite3.connect(db)
    # ⚠️ 期样本必须取**该因子实际存在的期**（derived_factors 的 DISTINCT period）。
    # 不能用 company_facts 的期：回溯时 current/prior 共用同一 release_date，
    # 较早期的生效日回落到标准发布日，但该期因子往往不存在（缺更早 prior），
    # 代码会跳到未来取到**另一个期的因子**——期标签错位、重复计数
    # （实测 revenue_yoy 实际 5 期却被算成 6 期，22H2/23H2 测的是同一份因子）。
    eff_rows = con.execute(
        "SELECT d.period, MAX(c.release_date) FROM derived_factors d "
        "LEFT JOIN company_facts c ON c.ticker=d.ticker AND c.period=d.period "
        "WHERE d.factor_key=? AND d.source='derived' "
        "GROUP BY d.period ORDER BY d.period", (factor_key,)).fetchall()
    con.close()

    def _eff(period: str, rel: str | None) -> str:
        std = (f"{period[:4]}-10-01" if period.endswith("H1")
               else f"{int(period[:4]) + 1}-05-01" if period.endswith("H2") else period)
        if std == period:
            return rel or period
        return min(rel, std) if rel else std

    all_dates = sorted(panel.keys())
    ics: list[tuple[str, float]] = []
    for period, rel in eff_rows:
        eff = _eff(period, rel)
        d = next((x for x in all_dates if x >= eff), None)
        if d is None:
            continue
        items = panel[d]
        if len(items) < min_stocks:
            continue
        ics.append((period, _spearman([v for _, v, _ in items],
                                      [r for _, _, r in items])))

    if len(ics) < 2:
        return {"factor": factor_key, "n_periods": len(ics),
                "error": f"报告期样本不足（{len(ics)} < 2），无法估计 t"}

    vals = [v for _, v in ics]
    m, s, n = _mean(vals), _std(vals), len(vals)
    t = m / (s / math.sqrt(n)) if s > 0 else 0.0
    return {
        "factor": factor_key, "horizon": horizon,
        "n_periods": n, "periods": [p for p, _ in ics],
        "ic_mean": round(m, 4), "ic_std": round(s, 4),
        "icir": round(m / s, 4) if s > 0 else 0.0,
        "ic_positive_pct": round(sum(1 for x in vals if x > 0) / n, 4),
        "ic_tstat": round(t, 4),
        "ic_by_period": {p: round(v, 4) for p, v in ics},
        "note": f"t 基于 {n} 个报告期（自由度 {n-1}）",
    }


def period_level_ic_neutralized(db: Path, factor_key: str, horizon: str = "fwd_20d",
                                min_stocks: int = 5) -> dict:
    """期级 IC 的中性化诊断（第 10 次归因纠错固化，2026-08-31）。

    第 9 次纠错修正 TTM 口径后，ttm_ocf_to_revenue 曾以 raw 期级 t=+2.94、
    LOO [+2.51,+3.60] 成为项目史上最强候选——但行业中性化后 t=+0.32 归零：
    高现金流质量集中在电信/能源等当期强势行业，是行业 beta 而非选股能力
    （与 volume_ratio=市值、gross_margin PEAD=行业同构，第 3 次同类假象）。

    每期截面分别计算：raw / size（因子与收益各自对 log 市值残差化）/
    sector（行业均值双向去均值）/ size_sector（先行业后市值），各按期数算 t。
    **结论以 size_sector 口径为准**：raw/size 显著而行业中性化后归零 = 行业 beta 假象。
    """
    panel = _load_panel_meta(db, factor_key, horizon, min_stocks)
    if not panel:
        return {"factor": factor_key, "error": "no panel data"}
    con = sqlite3.connect(db)
    eff_rows = con.execute(
        "SELECT d.period, MAX(c.release_date) FROM derived_factors d "
        "LEFT JOIN company_facts c ON c.ticker=d.ticker AND c.period=d.period "
        "WHERE d.factor_key=? AND d.source='derived' "
        "GROUP BY d.period ORDER BY d.period", (factor_key,)).fetchall()
    con.close()

    def _eff(period: str, rel: str | None) -> str:
        std = (f"{period[:4]}-10-01" if period.endswith("H1")
               else f"{int(period[:4]) + 1}-05-01" if period.endswith("H2") else period)
        if std == period:
            return rel or period
        return min(rel, std) if rel else std

    def _demean(vals: list[float], keys: list[str]) -> list[float]:
        agg: dict[str, list[float]] = {}
        for v, k in zip(vals, keys):
            agg.setdefault(k, []).append(v)
        avg = {k: _mean(v) for k, v in agg.items()}
        return [v - avg.get(k, 0.0) for v, k in zip(vals, keys)]

    all_dates = sorted(panel.keys())
    series: dict[str, list[float]] = {"raw": [], "size": [], "sector": [], "size_sector": []}
    periods_used: list[str] = []
    for period, rel in eff_rows:
        e = _eff(period, rel)
        d = next((x for x in all_dates if x >= e), None)
        if d is None:
            continue
        items = [(f, r, sec, lm) for tk, f, r, sec, lm in panel[d] if lm is not None]
        if len(items) < min_stocks:
            continue
        periods_used.append(period)
        fs = [x[0] for x in items]
        rs = [x[1] for x in items]
        secs = [x[2] for x in items]
        lms = [x[3] for x in items]
        series["raw"].append(_spearman(fs, rs))
        fr, rr = _ols_residual(lms, fs), _ols_residual(lms, rs)
        if fr and rr:
            series["size"].append(_spearman(fr, rr))
        fd, rd = _demean(fs, secs), _demean(rs, secs)
        series["sector"].append(_spearman(fd, rd))
        fr2, rr2 = _ols_residual(lms, fd), _ols_residual(lms, rd)
        if fr2 and rr2:
            series["size_sector"].append(_spearman(fr2, rr2))

    out: dict = {"factor": factor_key, "horizon": horizon, "periods": periods_used}
    for k, vals in series.items():
        n = len(vals)
        if n < 2:
            out[k] = {"n_periods": n, "error": "样本不足"}
            continue
        m, s = _mean(vals), _std(vals)
        t = m / (s / math.sqrt(n)) if s > 0 else 0.0
        # 功效标注（第 24 轮固化：每个结果必须自带 MDE，区分「信息性零」与「功效不足」；
        # 公式与 equity_power.mde 一致——power 自测 8/8 护栏覆盖该公式）
        if n >= 3 and s > 0:
            df = n - 1
            z975, z80 = 1.960, 0.842  # 大样本近似；期级 n<30 时用 CF 校正
            if df < 30:
                cf = lambda z: z + (z**3 + z) / (4*df) + (5*z**5 + 16*z**3 + 3*z) / (96*df**2)
                tcrit, tbeta = cf(1.960), cf(0.842)
            else:
                tcrit, tbeta = z975, z80
            mde = (tcrit + tbeta) * s / math.sqrt(n)
            gap = mde / 0.03  # 真实因子典型 IC=0.03
            vclass = ("检出信号" if abs(m) >= mde
                      else "信息性零结果" if mde <= 0.02
                      else "弱信息性" if mde <= 0.05
                      else "功效不足")
            out[k] = {"n_periods": n, "ic_mean": round(m, 4), "ic_tstat": round(t, 4),
                      "ic_positive_pct": round(sum(1 for x in vals if x > 0) / n, 4),
                      "mde": round(mde, 4), "power_gap_vs_ic0.03": round(gap, 2),
                      "verdict": vclass,
                      "ic_by_period": {p: round(v, 4) for p, v in zip(periods_used, vals)}}
        else:
            out[k] = {"n_periods": n, "ic_mean": round(m, 4), "ic_tstat": round(t, 4),
                      "ic_positive_pct": round(sum(1 for x in vals if x > 0) / n, 4),
                      "ic_by_period": {p: round(v, 4) for p, v in zip(periods_used, vals)}}
    out["note"] = ("期级中性化诊断：结论以 size_sector 口径为准——raw/size 显著而"
                   "行业中性化后归零 = 行业 beta 假象（第 10 次归因纠错）。"
                   "mde/power_gap 标注区分「信息性零」与「功效不足」（第 24 轮）。")
    return out


def factor_periodicity(db: Path, factor_key: str, source: str = "derived") -> dict:
    """因子的独立报告期数——低频因子的**真实样本量**。

    基本面因子半年/年度才更新：若用逐日 IC 序列算 t，n 看似几百，但这些
    交易日的因子值完全相同、IC 高度自相关，t 值被系统性高估
    （实测 revenue_yoy：逐日 NW t=-1.91 看似边缘显著，真实独立期仅 2 个）。
    判定低频因子必须看 n_periods，而非 n_daily。
    """
    con = sqlite3.connect(db)
    periods = [r[0] for r in con.execute(
        "SELECT DISTINCT period FROM derived_factors "
        "WHERE factor_key=? AND source=? ORDER BY period", (factor_key, source)).fetchall()]
    con.close()
    return {"factor": factor_key, "n_periods": len(periods), "periods": periods}


def factor_report(db: Path, factor_key: str, horizon: str = "fwd_20d",
                  n_groups: int = 5) -> str:
    """综合因子评估报告（Markdown 格式）。

    同时输出重叠（逐日建仓，n 大但自相关）与非重叠（跳跃采样，无偏保守）
    两种口径；因子有效性判定以非重叠口径为准。
    """
    ic = cross_sectional_ic(db, factor_key, horizon)
    gr = group_backtest(db, factor_key, horizon, n_groups)
    ic_no = cross_sectional_ic(db, factor_key, horizon, nonoverlap=True)
    gr_no = group_backtest(db, factor_key, horizon, n_groups, nonoverlap=True)

    lines = [
        f"# 因子评估报告: {factor_key}",
        f"horizon: {horizon} | n_groups: {n_groups}",
        "",
    ]

    # IC 分析
    if "error" in ic:
        lines.append(f"## IC 分析: {ic['error']}")
    else:
        verdict = "✅ 有效" if abs(ic["icir"]) >= 0.3 else ("⚠️ 边缘" if abs(ic["icir"]) >= 0.15 else "❌ 无效")
        lines += [
            "## IC 分析（逐日重叠口径）",
            f"- IC 均值: {ic['ic_mean']:.4f}",
            f"- IC 标准差: {ic['ic_std']:.4f}",
            f"- **ICIR: {ic['icir']:.4f}** (|ICIR|≥0.3 有效, ≥0.15 边缘)",
            f"- IC>0 占比: {ic['ic_positive_pct']:.1%}",
            f"- t-stat: {ic['ic_tstat']:.2f}（重叠窗口，自相关偏高）",
            f"- 评估: {verdict}",
            "",
        ]
        if "error" not in ic_no:
            lines += [
                "## IC 分析（非重叠口径）",
                f"- 样本数: {ic_no['n_dates']}（跳跃采样 step={_horizon_days(horizon)}，功效低）",
                f"- ICIR: {ic_no['icir']:.4f} | t-stat: {ic_no['ic_tstat']:.2f}",
                f"- IC>0 占比: {ic_no['ic_positive_pct']:.1%}",
                "",
            ]
        # Newey-West：全样本逐日 + HAC 修正，兼顾无偏与功效（主判定口径）
        nw_t = ic.get("ic_tstat_nw")
        if nw_t is not None:
            nw_sig = "✅ 显著" if abs(nw_t) >= 1.96 else ("⚠️ 边缘" if abs(nw_t) >= 1.64 else "不显著")
            lines += [
                "## IC 分析（Newey-West，**主判定口径**）",
                f"- 逐日样本 n={ic.get('n_daily', 0)}，lags={max(1, _horizon_days(horizon) - 1)}",
                f"- **NW t-stat: {nw_t:.2f}**（{nw_sig}）",
                "",
            ]
            # 低频因子警示：n_daily 虚高，真实样本是独立报告期数
            peri = factor_periodicity(db, factor_key)
            np_ = peri["n_periods"]
            if 0 < np_ <= 12:  # 年度/半年度/季度频率
                warn = ("🚨 **低频因子，t 值不可信**" if np_ < 5 else "⚠️ 低频因子")
                lines += [
                    f"### {warn}",
                    f"- 独立报告期数 **n_periods = {np_}**：{peri['periods']}",
                    f"- 上文的 n={ic.get('n_daily', 0)} 是**交易日数**，但因子半年/年度才更新一次，"
                    "这些交易日的因子值完全相同、IC 高度自相关。",
                    f"- **真实独立样本是 {np_} 个报告期，远非 {ic.get('n_daily', 0)} 个**；"
                    f"{'样本不足以支撑任何结论，NW t 被系统性高估。' if np_ < 5 else '结论需谨慎，建议积累更多报告期。'}",
                    "",
                ]
        # 分年度稳健性诊断：全样本显著 ≠ 稳定可用
        by_year: dict[str, list[float]] = {}
        for d, v in ic.get("ic_series", []):
            by_year.setdefault(d[:4], []).append(v)
        if len(by_year) >= 2:
            lines += ["## 稳健性诊断（分年度 IC）", "",
                      "| 年份 | IC均值 | n | NW t |", "|---|---|---|---|"]
            for y in sorted(by_year):
                vals = by_year[y]
                yt = newey_west_tstat(vals, max(1, _horizon_days(horizon) - 1))
                lines.append(f"| {y} | {_mean(vals):+.4f} | {len(vals)} | "
                             f"{yt:+.2f} |" if yt is not None else
                             f"| {y} | {_mean(vals):+.4f} | {len(vals)} | - |")
            pos_years = sum(1 for y in by_year if _mean(by_year[y]) > 0)
            lines += ["",
                      f"- 符号一致的年份占比: {pos_years}/{len(by_year)}"
                      f"（{'全部同号' if pos_years in (0, len(by_year)) else '**符号不稳定，慎用**'}）",
                      "- ⚠️ 若剔除个别强势年份后 t 值崩塌，说明显著性由少数时段驱动，"
                      "**不是稳定可用因子**（统计显著 ≠ 稳定可用）",
                      ""]

    # 分层回测
    if "error" in gr:
        lines.append(f"## 分层回测: {gr['error']}")
    else:
        ls = gr["long_short"]
        h_days = _horizon_days(horizon)
        lines += [
            "## 分层回测（逐日重叠口径）",
            f"- 分组数: {gr['n_groups']} | 回测期数: {gr['n_dates']}",
            "",
            "| 组别 | 单期均值 | 年化均值 |",
            "|---|---|---|",
        ]
        for g, data in gr["groups"].items():
            ann = data["mean_return"] * TRADING_DAYS / h_days
            lines.append(f"| G{g} | {data['mean_return']:.4%} | {ann:.2%} |")
        lines += [
            "",
            f"**多空组合**: 单期均值={ls['mean_return']:.4%} 年化={ls['mean_return'] * TRADING_DAYS / h_days:.2%} Sharpe={ls['sharpe']:.2f}（重叠口径）",
            "",
        ]
        if "error" not in gr_no:
            ls_no = gr_no["long_short"]
            lines += [
                f"**多空组合（非重叠口径，判定以此为准）**: n={gr_no['n_dates']} "
                f"单期均值={ls_no['mean_return']:.4%} 累计={ls_no['cumulative']:.2%} Sharpe={ls_no['sharpe']:.2f}",
                "",
            ]

    lines.append("---")
    # 动态标的数 + 样本量红线标注
    try:
        con = sqlite3.connect(str(db))
        n_universe = con.execute("SELECT COUNT(*) FROM universe").fetchone()[0]
        con.close()
    except Exception:
        n_universe = 0
    if n_universe < 30:
        lines.append(
            f"⚠️ 以上分析不构成投资建议。当前标的池 {n_universe} 只（<30，红线）："
            "结论仅为初步观察，不得作为'因子有效'依据。")
    else:
        lines.append("⚠️ 以上分析不构成投资建议，结论仅供研究参考。")
    return "\n".join(lines)


# ============================================================ Self Test ===


def self_test() -> bool:
    """已知答案校验。"""
    import tempfile
    results: list[tuple[str, bool, str]] = []

    def check(name, cond, detail=""):
        results.append((name, bool(cond), detail))

    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
        tdb = Path(f.name)

    try:
        # 创建完整 schema
        con = sqlite3.connect(tdb)
        con.executescript("""
        CREATE TABLE IF NOT EXISTS daily_quotes (
            ticker TEXT, quote_date TEXT, open REAL, high REAL, low REAL,
            close REAL, volume REAL, amount REAL, currency TEXT,
            source TEXT, collected_at TEXT,
            PRIMARY KEY (ticker, quote_date, collected_at, source));
        CREATE TABLE IF NOT EXISTS forward_returns (
            ticker TEXT, trade_date TEXT,
            fwd_1d REAL, fwd_5d REAL, fwd_20d REAL, fwd_60d REAL,
            PRIMARY KEY (ticker, trade_date));
        CREATE TABLE IF NOT EXISTS derived_factors (
            ticker TEXT, factor_key TEXT, period TEXT, value REAL,
            unit TEXT, transform TEXT, transform_version TEXT,
            source TEXT, collected_at TEXT,
            PRIMARY KEY (ticker, factor_key, period, collected_at));
        CREATE TABLE IF NOT EXISTS sector_map (
            ticker TEXT PRIMARY KEY, sector TEXT, industry TEXT,
            source TEXT, collected_at TEXT);
        CREATE TABLE IF NOT EXISTS benchmarks (
            index_code TEXT, trade_date TEXT, open REAL, high REAL,
            low REAL, close REAL, volume REAL, currency TEXT,
            source TEXT, collected_at TEXT,
            PRIMARY KEY (index_code, trade_date));
        -- _load_panel_data 会 LEFT JOIN company_facts 取实际发布日（防前视偏差），
        -- 自测 schema 必须包含此表，否则报 no such table。
        CREATE TABLE IF NOT EXISTS company_facts (
            ticker TEXT NOT NULL, fact_key TEXT NOT NULL, period TEXT NOT NULL,
            freq TEXT NOT NULL DEFAULT 'H', value REAL, unit TEXT NOT NULL,
            source TEXT NOT NULL, source_url TEXT, release_date TEXT,
            collected_at TEXT NOT NULL,
            PRIMARY KEY (ticker, fact_key, period, freq, collected_at, source));
        """)
        batch = "2026-01-01"

        # 构造 5 只股票，10 个交易日，因子值与收益完美正相关
        for tk_i in range(5):
            tk = f"hk{tk_i:05d}"
            sector = "A" if tk_i < 3 else "B"
            con.execute("INSERT OR REPLACE INTO sector_map VALUES (?,?,?, 'test', ?)",
                        (tk, sector, f"ind_{tk_i}", batch))
            for d in range(10):
                td = f"2026-01-{d+1:02d}"
                # 因子值 = tk_i * 10 + d（正相关于收益）
                factor_val = float(tk_i * 10 + d)
                con.execute("INSERT OR REPLACE INTO derived_factors VALUES (?,?,?,?,?,?,?,?,?)",
                            (tk, "test_factor", td, factor_val, "ratio",
                             "test", "v1", "derived", batch))
                # 收益 = factor_val / 1000（完美正相关）
                fwd_20d = factor_val / 1000
                con.execute("INSERT OR REPLACE INTO forward_returns VALUES (?,?,?,?,?,?)",
                            (tk, td, fwd_20d, fwd_20d, fwd_20d, fwd_20d))
        con.commit()
        con.close()

        # 1) 跨截面 IC：完美正相关 → IC ≈ 1.0
        ic_result = cross_sectional_ic(tdb, "test_factor", "fwd_20d")
        check("cross-sectional IC computed", "ic_mean" in ic_result, f"keys={list(ic_result.keys())}")
        check("IC near +1 (perfect correlation)",
              abs(ic_result.get("ic_mean", 0) - 1.0) < 0.01,
              f"ic_mean={ic_result.get('ic_mean')}")

        # 2) 分层回测：G5 (高因子) > G1 (低因子)
        gr_result = group_backtest(tdb, "test_factor", "fwd_20d", n_groups=5)
        check("group backtest computed", "groups" in gr_result, f"keys={list(gr_result.keys())}")
        if "groups" in gr_result:
            g1_mean = gr_result["groups"][1]["mean_return"]
            g5_mean = gr_result["groups"][5]["mean_return"]
            check("G5 > G1 (monotonic)", g5_mean > g1_mean,
                  f"G1={g1_mean:.4f} G5={g5_mean:.4f}")

        # 3) 中性化：行业内均值为 0
        neut = neutralize_cross_section(tdb, "test_factor", "2026-01-01")
        check("neutralization computed", "neutralized" in neut, f"keys={list(neut.keys())}")
        if "neutralized" in neut and len(neut["neutralized"]) == 5:
            vals = list(neut["neutralized"].values())
            check("neutralized values sum ~0 (within sectors)",
                  abs(sum(vals)) < 1.0, f"sum={sum(vals):.4f}")

        # 4) 行业中性化 IC：行业内因子仍正相关于收益 → IC ≈ 1.0
        nic = neutralized_ic(tdb, "test_factor", "fwd_20d")
        check("neutralized IC computed", "ic_mean" in nic,
              f"keys={list(nic.keys())} n_dates={nic.get('n_dates')}")
        check("neutralized IC near +1", abs(nic.get("ic_mean", 0) - 1.0) < 0.02,
              f"ic_mean={nic.get('ic_mean')} n={nic.get('n_dates')}")

    finally:
        try:
            tdb.unlink(missing_ok=True)
        except PermissionError:
            pass

    ok = all(c for _, c, _ in results)
    for name, passed, detail in results:
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}  {detail}")
    print(f"[self-test] {'ALL PASS' if ok else 'FAILED'} ({sum(c for _, c, _ in results)}/{len(results)})")
    return ok


# ============================================================ Factor Scan ===


def scan_all_factors(db: Path, horizon: str = "fwd_20d") -> list[dict]:
    """批量扫描所有可用因子的 IC 概览，按 |ICIR| 降序排列。"""
    con = sqlite3.connect(db)
    # 同 _load_panel_data：排除 macro_aligned（个股横截面无区分度，属时序/行业层变量）
    rows = con.execute(
        "SELECT DISTINCT factor_key FROM derived_factors "
        "WHERE source IN ('derived', 'price_computed') ORDER BY factor_key"
    ).fetchall()
    con.close()

    results = []
    for (fk,) in rows:
        ic = cross_sectional_ic(db, fk, horizon)
        if "error" in ic:
            results.append({
                "factor": fk, "ic_mean": None, "icir": None,
                "ic_positive_pct": None, "n_dates": ic.get("n_dates", 0),
                "verdict": f"❌ {ic['error']}",
            })
        else:
            abs_icir = abs(ic["icir"])
            verdict = "✅ 有效" if abs_icir >= 0.3 else ("⚠️ 边缘" if abs_icir >= 0.15 else "❌ 无效")
            results.append({
                "factor": fk,
                "ic_mean": ic["ic_mean"],
                "icir": ic["icir"],
                "ic_positive_pct": ic["ic_positive_pct"],
                "n_dates": ic["n_dates"],
                "verdict": verdict,
            })
    # 按 |ICIR| 降序
    results.sort(key=lambda x: abs(x["icir"]) if x["icir"] is not None else -1, reverse=True)
    return results


def ic_decay_analysis(db: Path, factor_key: str, horizons: list[str] | None = None) -> dict:
    """IC 衰减分析：同一因子在不同持有期的 IC 变化。"""
    if horizons is None:
        horizons = ["fwd_1d", "fwd_5d", "fwd_20d", "fwd_60d"]
    decay = []
    for h in horizons:
        ic = cross_sectional_ic(db, factor_key, h)
        if "error" in ic:
            decay.append({"horizon": h, "ic_mean": None, "icir": None, "error": ic["error"]})
        else:
            decay.append({
                "horizon": h,
                "ic_mean": ic["ic_mean"],
                "icir": ic["icir"],
                "ic_positive_pct": ic["ic_positive_pct"],
                "n_dates": ic["n_dates"],
            })
    return {"factor": factor_key, "decay": decay}


# ============================================================ CLI ===


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=str(DB_PATH))
    ap.add_argument("--factor", default="gross_margin")
    ap.add_argument("--horizon", default="fwd_20d",
                    choices=["fwd_1d", "fwd_5d", "fwd_20d", "fwd_60d"])
    ap.add_argument("--ticker", default="hk03738")
    ap.add_argument("--groups", type=int, default=5)
    ap.add_argument("cmd", choices=["ic", "group", "report", "benchmark",
                                     "neutralize", "ic-neutralized", "ic-size-neutralized",
                                     "ic-period-neu", "factors", "ic-decay", "event-study",
                                     "self-test"])
    ap.add_argument("--date", default="2026-06-30")
    ap.add_argument("--nonoverlap", action="store_true",
                    help="非重叠采样（按持有期天数跳跃），t/Sharpe 无偏保守口径")
    ap.add_argument("--surprise", default="revenue_yoy",
                    help="PEAD 事件研究的惊喜因子 factor_key（默认 revenue_yoy）")
    args = ap.parse_args()

    db = Path(args.db)
    if args.cmd == "ic":
        import json
        r = cross_sectional_ic(db, args.factor, args.horizon, nonoverlap=args.nonoverlap)
        r.pop("ic_series", None)
        print(json.dumps(r, indent=2, ensure_ascii=False))
    elif args.cmd == "group":
        import json
        r = group_backtest(db, args.factor, args.horizon, args.groups,
                           nonoverlap=args.nonoverlap)
        print(json.dumps(r, indent=2, ensure_ascii=False))
    elif args.cmd == "report":
        print(factor_report(db, args.factor, args.horizon, args.groups))
    elif args.cmd == "benchmark":
        import json
        r = benchmark_comparison(db, args.ticker, args.horizon)
        print(json.dumps(r, indent=2, ensure_ascii=False))
    elif args.cmd == "neutralize":
        import json
        r = neutralize_cross_section(db, args.factor, args.date)
        print(json.dumps(r, indent=2, ensure_ascii=False))
    elif args.cmd == "ic-neutralized":
        import json
        r = neutralized_ic(db, args.factor, args.horizon, nonoverlap=args.nonoverlap)
        r.pop("ic_series", None)
        print(json.dumps(r, indent=2, ensure_ascii=False))
    elif args.cmd == "ic-size-neutralized":
        import json
        r = size_neutralized_ic(db, args.factor, args.horizon, nonoverlap=args.nonoverlap)
        r.pop("ic_series", None)
        print(json.dumps(r, indent=2, ensure_ascii=False))
    elif args.cmd == "factors":
        results = scan_all_factors(db, args.horizon)
        print(f"{'Factor':25s} {'IC Mean':>8s} {'ICIR':>8s} {'IC>0%':>7s} {'N':>4s}  Verdict")
        print("-" * 75)
        for r in results:
            ic_m = f"{r['ic_mean']:.4f}" if r['ic_mean'] is not None else "N/A"
            icir = f"{r['icir']:.4f}" if r['icir'] is not None else "N/A"
            ic_p = f"{r['ic_positive_pct']:.0%}" if r['ic_positive_pct'] is not None else "N/A"
            nd = str(r['n_dates'])
            print(f"{r['factor']:25s} {ic_m:>8s} {icir:>8s} {ic_p:>7s} {nd:>4s}  {r['verdict']}")
    elif args.cmd == "ic-decay":
        import json
        r = ic_decay_analysis(db, args.factor)
        print(json.dumps(r, indent=2, ensure_ascii=False))
    elif args.cmd == "ic-period-neu":
        import json
        r = period_level_ic_neutralized(db, args.factor, args.horizon)
        print(json.dumps(r, indent=2, ensure_ascii=False))
    elif args.cmd == "event-study":
        import json
        neu = event_study(db, args.surprise, args.horizon, args.groups,
                          industry_neutralized=True)
        neu.pop("note", None)
        raw = event_study(db, args.surprise, args.horizon, args.groups,
                          industry_neutralized=False)
        raw.pop("note", None)
        print("=== PEAD 事件研究（行业中性化，诚实口径）===")
        print(json.dumps(neu, indent=2, ensure_ascii=False))
        print("\n=== PEAD 事件研究（仅市场调整，raw，对照/警示）===")
        print(json.dumps(raw, indent=2, ensure_ascii=False))
    elif args.cmd == "self-test":
        raise SystemExit(0 if self_test() else 1)


if __name__ == "__main__":
    main()
