#!/usr/bin/env python3
"""Minimal quant engine MVP: factor_ic / run_backtest / risk_metrics.

设计原则（对齐飞轮哲学）：
- 引擎是纯函数库 + 已知答案自测（self-test 用构造数据校验算法正确性，不采信运行时自述）；
- 无 numpy/scipy 依赖（纯 Python，n 小场景够用，后续可替换为 Qlib/向量化实现）；
- 数据消费端从 equity 库 daily_quotes 读取，引擎本身无状态。

CLI:
  self-test                          已知答案自测（exit 1 on FAIL）
  demo --ticker hk03738 --window 20  从库中读真实行情跑动量信号回测演示
"""
from __future__ import annotations

import argparse
import math
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parents[4] / "outputs" / "equity_fundamental.sqlite"
TRADING_DAYS = 252


# ---------------------------------------------------------------- stats ---

def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _std(xs: list[float], ddof: int = 1) -> float:
    n = len(xs)
    if n <= ddof:
        return 0.0
    m = _mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (n - ddof))


def _rank(xs: list[float]) -> list[float]:
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


# ------------------------------------------------------------- factor IC ---

def factor_ic(factor: list[float], fwd_returns: list[float], method: str = "spearman") -> float:
    """Rank IC (default) or Pearson IC between cross-sectional factor values and forward returns."""
    if len(factor) != len(fwd_returns) or len(factor) < 3:
        raise ValueError("need equal-length series with n>=3")
    xs, ys = factor, fwd_returns
    if method == "spearman":
        xs, ys = _rank(factor), _rank(fwd_returns)
    mx, my = _mean(xs), _mean(ys)
    cov = sum((a - mx) * (b - my) for a, b in zip(xs, ys))
    vx = math.sqrt(sum((a - mx) ** 2 for a in xs))
    vy = math.sqrt(sum((b - my) ** 2 for b in ys))
    if vx == 0 or vy == 0:
        return 0.0
    return cov / (vx * vy)


# --------------------------------------------------------------- backtest ---

def run_backtest(closes: list[float], signals: list[float], cost_bps: float = 0.0) -> dict:
    """Daily-frequency long/flat/short backtest.

    signals[t-1] applies to day t return; turnover cost charged on |Δsignal|.
    closes: n prices; signals: n-1 positions (one per holding day).
    """
    if len(signals) != len(closes) - 1:
        raise ValueError("signals must have n-1 entries for n closes")
    rets = [(closes[i + 1] / closes[i]) - 1 for i in range(len(closes) - 1)]
    strat = []
    prev_pos = 0.0
    turnover = 0.0
    for t, r in enumerate(rets):
        pos = signals[t]
        turnover += abs(pos - prev_pos)
        fee = abs(pos - prev_pos) * cost_bps / 10000
        strat.append(pos * r - fee)
        prev_pos = pos
    cum = 1.0
    peak, maxdd = 1.0, 0.0
    for r in strat:
        cum *= (1 + r)
        peak = max(peak, cum)
        maxdd = min(maxdd, cum / peak - 1)
    ann = (cum ** (TRADING_DAYS / len(strat)) - 1) if strat and cum > 0 else -1.0
    vol = _std(strat) * math.sqrt(TRADING_DAYS)
    sharpe = (_mean(strat) * TRADING_DAYS) / vol if vol > 0 else 0.0
    bh = closes[-1] / closes[0] - 1
    return {
        "cum_return": cum - 1, "annual_return": ann, "annual_vol": vol,
        "sharpe": sharpe, "max_drawdown": maxdd, "turnover": turnover,
        "buy_hold_return": bh, "n_days": len(strat),
    }


# ----------------------------------------------------------- risk metrics ---

def risk_metrics(returns: list[float]) -> dict:
    """Annualized return/vol/Sharpe, max drawdown, historical VaR/CVaR 95%."""
    if len(returns) < 2:
        raise ValueError("need >=2 return observations")
    cum = 1.0
    peak, maxdd = 1.0, 0.0
    for r in returns:
        cum *= (1 + r)
        peak = max(peak, cum)
        maxdd = min(maxdd, cum / peak - 1)
    sorted_r = sorted(returns)
    k = max(1, int(math.floor(0.05 * len(sorted_r))))
    tail = sorted_r[:k]
    return {
        "annual_return": (cum ** (TRADING_DAYS / len(returns)) - 1) if cum > 0 else -1.0,
        "annual_vol": _std(returns) * math.sqrt(TRADING_DAYS),
        "sharpe": (_mean(returns) * TRADING_DAYS) / (_std(returns) * math.sqrt(TRADING_DAYS))
                  if _std(returns) > 0 else 0.0,
        "max_drawdown": maxdd,
        "var_95": _mean(tail) if tail else sorted_r[0],
        "cvar_95": _mean(tail) if tail else sorted_r[0],
    }


# ------------------------------------------------------------- self test ---

def _lcg(seed: int):
    state = seed
    while True:
        state = (state * 1103515245 + 12345) % (2 ** 31)
        yield state / (2 ** 31)


def self_test() -> bool:
    results: list[tuple[str, bool, str]] = []

    def check(name, cond, detail=""):
        results.append((name, bool(cond), detail))

    # 1) perfect monotonic factor -> IC = ±1
    ic = factor_ic([1, 2, 3, 4, 5], [10, 20, 30, 40, 50])
    check("IC perfect monotonic = +1", abs(ic - 1.0) < 1e-9, f"ic={ic:.6f}")
    ic2 = factor_ic([1, 2, 3, 4, 5], [50, 40, 30, 20, 10])
    check("IC reversed = -1", abs(ic2 + 1.0) < 1e-9, f"ic={ic2:.6f}")

    # 2) noisy factor (fixed LCG seed) -> IC in (0.8, 1.0]
    g = _lcg(42)

    def rnd() -> float:
        return next(g)

    f = [i + (rnd() - 0.5) * 2 for i in range(50)]
    fwd = [i + (rnd() - 0.5) * 0.5 for i in range(50)]
    ic3 = factor_ic(f, fwd)
    check("IC noisy in (0.8, 1.0]", 0.8 < ic3 <= 1.0, f"ic={ic3:.4f}")

    # 3) backtest: rising prices + full long, zero cost == buy&hold
    bt = run_backtest([100.0, 110.0, 121.0], [1.0, 1.0], cost_bps=0)
    check("BT full-long == buy&hold", abs(bt["cum_return"] - 0.21) < 1e-9
          and abs(bt["buy_hold_return"] - 0.21) < 1e-9, f"cum={bt['cum_return']:.6f}")

    # 4) backtest: flat signal -> zero gross, cost drags negative
    bt2 = run_backtest([100.0, 110.0, 121.0], [0.0, 0.0], cost_bps=10)
    check("BT flat signal = 0 return", bt2["cum_return"] == 0.0, f"cum={bt2['cum_return']:.6f}")

    # 5) backtest: cost model — one entry with 10bps on 100% position
    bt3 = run_backtest([100.0, 101.0], [1.0], cost_bps=10)
    expected = 0.01 - 0.001
    check("BT cost charged on entry", abs(bt3["cum_return"] - expected) < 1e-9,
          f"cum={bt3['cum_return']:.6f} expected={expected:.6f}")

    # 6) risk_metrics on hand-computed series
    rm = risk_metrics([0.01, -0.01, 0.02, -0.02, 0.03, -0.03, 0.01, 0.01])
    check("RM mean/std finite", math.isfinite(rm["sharpe"]) and rm["annual_vol"] > 0,
          f"sharpe={rm['sharpe']:.4f} vol={rm['annual_vol']:.4f}")
    dd = risk_metrics([0.10, -0.10])["max_drawdown"]
    check("RM maxDD on +10%/-10% = -10%", abs(dd - (-0.10)) < 1e-9,
          f"dd={dd:.6f} (cum path 1.0->1.1->0.99, dd=0.99/1.1-1=-0.10)")

    ok = all(c for _, c, _ in results)
    for name, passed, detail in results:
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}  {detail}")
    print(f"[self-test] {'ALL PASS' if ok else 'FAILED'} ({sum(c for _, c, _ in results)}/{len(results)})")
    return ok


def demo(db: Path, ticker: str, window: int) -> None:
    con = sqlite3.connect(db)
    rows = con.execute(
        "SELECT quote_date, close FROM daily_quotes WHERE ticker=? ORDER BY quote_date",
        (ticker,)).fetchall()
    con.close()
    closes = [r[1] for r in rows]
    dates = [r[0] for r in rows]
    if len(closes) < window + 30:
        print(f"[demo] not enough bars ({len(closes)}) for window={window}")
        return
    # 20日动量信号：动量>0 持有，否则空仓（示例策略，不构成任何投资建议）
    signals = [1.0 if closes[i] / closes[i - window] - 1 > 0 else 0.0 for i in range(window, len(closes) - 1)]
    bt = run_backtest(closes, [0.0] * window + signals, cost_bps=15)
    rm = risk_metrics([bt["cum_return"] and (1 + bt["cum_return"]) ** (1 / max(bt["n_days"], 1)) - 1]
                      * max(bt["n_days"], 1))
    print(f"[demo] {ticker} momentum({window}d) long/flat, cost 15bps")
    print(f"  bars={len(closes)} ({dates[0]}..{dates[-1]}) n_days={bt['n_days']}")
    for k in ("cum_return", "annual_return", "sharpe", "max_drawdown", "turnover", "buy_hold_return"):
        print(f"  {k:16s} {bt[k]:.4f}")
    print(f"  (per-day vol proxy) {rm['annual_vol']:.4f}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=str(DB_PATH))
    ap.add_argument("--ticker", default="hk03738")
    ap.add_argument("--window", type=int, default=20)
    ap.add_argument("cmd", choices=["self-test", "demo"])
    args = ap.parse_args()
    if args.cmd == "self-test":
        raise SystemExit(0 if self_test() else 1)
    demo(Path(args.db), args.ticker, args.window)


if __name__ == "__main__":
    main()
