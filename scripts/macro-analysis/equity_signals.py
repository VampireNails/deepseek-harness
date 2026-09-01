#!/usr/bin/env python3
"""Signal ledger + prediction verification loop (Flywheel 2, quant edition).

路线 B 的"越用越好"闭环：量化 agent 每期产出信号 → 落库（quant_signals）→
持有期满后回填实际收益 → 胜率/IC 复核报告 → 反馈修正因子权重。

三飞轮量化版映射：
  ① 数据厚度 = daily_quotes/forward_returns 持续 append（已有）
  ② 校验智慧 = 本模块：信号 open → resolved 的全生命周期，只增不改
  ③ 源可信度 = realized_return 来自 forward_returns（同一 vintage 管线）

红线：信号记录后不可修改（append-only）；回填只写 realized_return/resolved_at/
status 三个字段；报告必须区分 open/resolved。

CLI:
  record --factor volatility_20d --horizon fwd_20d --top 3 [--date D] [--invert]
  backfill                     # 回填所有已到期的 open 信号
  report [--factor K]          # 胜率/收益/IC 复核
  list [--status open|resolved] [--limit N]
  self-test
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from equity_quant import (  # noqa: E402
    DB_PATH as QUANT_DB,
    _load_panel_data,
    _mean,
    _rank,
    _spearman,
    _std,
)

DB_PATH = QUANT_DB
SCHEMA = """
CREATE TABLE IF NOT EXISTS quant_signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    ticker TEXT NOT NULL,
    factor_key TEXT NOT NULL,
    horizon TEXT NOT NULL,
    signal_value REAL,
    rank_pct REAL,
    direction TEXT NOT NULL,
    realized_return REAL,
    resolved_at TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    recorded_at TEXT NOT NULL,
    UNIQUE(trade_date, ticker, factor_key, horizon)
);
CREATE INDEX IF NOT EXISTS idx_signals_status ON quant_signals(status);
CREATE INDEX IF NOT EXISTS idx_signals_factor ON quant_signals(factor_key, horizon);
"""


def _conn(db: Path) -> sqlite3.Connection:
    c = sqlite3.connect(str(db))
    c.row_factory = sqlite3.Row
    return c


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _ensure_schema(con: sqlite3.Connection) -> None:
    con.executescript(SCHEMA)


# ============================================================ Record ===


def record_signals(db: Path, factor_key: str, horizon: str, top: int = 3,
                   trade_date: str | None = None, invert: bool = False,
                   min_stocks: int = 3) -> dict:
    """记录某截面日的 top-N/bottom-N 信号。

    invert=False: long = 因子值最高 N 只，short = 最低 N 只。
    invert=True: 反向（如波动率因子已知负向，long = 因子值最低 N 只）。
    """
    con = _conn(db)
    _ensure_schema(con)
    panel = _load_panel_data(db, factor_key, horizon, min_stocks)
    if not panel:
        con.close()
        return {"error": "no panel data"}

    if trade_date is None:
        trade_date = max(panel.keys())
    elif trade_date not in panel:
        con.close()
        return {"error": f"date {trade_date} not in panel (need >= {min_stocks} stocks)"}

    items = panel[trade_date]
    if len(items) < min_stocks:
        con.close()
        return {"error": f"only {len(items)} stocks on {trade_date}"}

    values = [v for _, v, _ in items]
    ranks = _rank(values)
    n = len(items)
    batch_id = f"{trade_date}-{factor_key}-{horizon}-{uuid.uuid4().hex[:6]}"

    inserts = []
    for (tk, val, _fwd), rk in zip(items, ranks):
        pct = (rk - 1) / (n - 1) if n > 1 else 0.5  # 0=最低, 1=最高
        high_side = pct >= 1 - top / n if n > top else pct >= 0.5
        low_side = pct <= top / n if n > top else pct < 0.5
        direction = None
        if high_side:
            direction = "short" if invert else "long"
        elif low_side:
            direction = "long" if invert else "short"
        if direction is None:
            continue
        inserts.append((batch_id, trade_date, tk, factor_key, horizon,
                        round(val, 8), round(pct, 4), direction, _now()))

    con.executemany(
        "INSERT OR IGNORE INTO quant_signals "
        "(batch_id, trade_date, ticker, factor_key, horizon, signal_value, "
        " rank_pct, direction, recorded_at) VALUES (?,?,?,?,?,?,?,?,?)", inserts)
    con.commit()
    out = {"batch_id": batch_id, "trade_date": trade_date,
           "factor": factor_key, "horizon": horizon,
           "recorded": len(inserts),
           "long": [i[2] for i in inserts if i[7] == "long"],
           "short": [i[2] for i in inserts if i[7] == "short"]}
    con.close()
    return out


# ============================================================ Backfill ===


def backfill_signals(db: Path) -> dict:
    """回填所有已到期的 open 信号：从 forward_returns 取实际收益。"""
    con = _conn(db)
    _ensure_schema(con)
    open_rows = con.execute(
        "SELECT id, trade_date, ticker, horizon FROM quant_signals "
        "WHERE status='open'").fetchall()
    filled, missing = 0, 0
    for r in open_rows:
        fwd = con.execute(
            f"SELECT {r['horizon']} FROM forward_returns "
            "WHERE ticker=? AND trade_date=?",
            (r["ticker"], r["trade_date"])).fetchone()
        if fwd and fwd[0] is not None:
            con.execute(
                "UPDATE quant_signals SET realized_return=?, resolved_at=?, "
                "status='resolved' WHERE id=? AND status='open'",
                (round(fwd[0], 8), _now(), r["id"]))
            filled += 1
        else:
            missing += 1
    con.commit()
    con.close()
    return {"open_scanned": len(open_rows), "filled": filled, "not_matured": missing}


# ============================================================ Report ===


def signal_report(db: Path, factor_key: str | None = None,
                  horizon: str | None = None) -> dict:
    """信号复核：胜率 / 均值收益 / rank IC（仅 resolved）。"""
    con = _conn(db)
    _ensure_schema(con)
    where, params = ["status='resolved'"], []
    if factor_key:
        where.append("factor_key=?")
        params.append(factor_key)
    if horizon:
        where.append("horizon=?")
        params.append(horizon)
    rows = con.execute(
        "SELECT direction, rank_pct, realized_return FROM quant_signals "
        f"WHERE {' AND '.join(where)}", params).fetchall()
    open_cnt = con.execute(
        "SELECT COUNT(*) FROM quant_signals WHERE status='open'").fetchone()[0]
    con.close()

    if not rows:
        return {"resolved": 0, "open": open_cnt, "message": "no resolved signals yet"}

    def _stats(sub: list) -> dict:
        rets = [r["realized_return"] for r in sub if r["realized_return"] is not None]
        wins = sum(1 for x in rets if x > 0)
        cum = 1.0
        for x in rets:
            cum *= (1 + x)
        return {"n": len(rets), "win_rate": round(wins / len(rets), 4) if rets else 0,
                "mean_return": round(_mean(rets), 6) if rets else 0,
                "cumulative": round(cum - 1, 4) if rets else 0}

    longs = [r for r in rows if r["direction"] == "long"]
    shorts = [r for r in rows if r["direction"] == "short"]
    # 信号 IC：rank_pct 与 realized_return 的 Spearman（正值=高因子分位→高收益）
    pcts = [r["rank_pct"] for r in rows]
    rets = [r["realized_return"] for r in rows]
    sig_ic = _spearman(pcts, rets) if len(rows) >= 3 else None

    return {
        "resolved": len(rows), "open": open_cnt,
        "long": _stats(longs), "short": _stats(shorts),
        "signal_ic": round(sig_ic, 4) if sig_ic is not None else None,
        "note": "signal_ic>0 = 高因子分位未来收益更高；若因子为负向（如波动率），"
                "预期 signal_ic<0 且 short 组胜率更高",
    }


def list_signals(db: Path, status: str | None = None, limit: int = 20) -> list[dict]:
    con = _conn(db)
    _ensure_schema(con)
    if status:
        rows = con.execute(
            "SELECT trade_date, ticker, factor_key, horizon, direction, "
            "rank_pct, realized_return, status FROM quant_signals "
            "WHERE status=? ORDER BY id DESC LIMIT ?", (status, limit)).fetchall()
    else:
        rows = con.execute(
            "SELECT trade_date, ticker, factor_key, horizon, direction, "
            "rank_pct, realized_return, status FROM quant_signals "
            "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    con.close()
    return [dict(r) for r in rows]


# ============================================================ Self Test ===


def self_test() -> bool:
    import tempfile
    checks = []

    def check(name, cond, detail=""):
        checks.append((name, cond))
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}  {detail}")

    tmp = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
    tdb = Path(tmp.name)
    tmp.close()

    try:
        # 构造模拟数据：3只股 × 40天，factor = momentum（G3 应跑赢 G1）
        con = sqlite3.connect(tdb)
        con.executescript("""
        CREATE TABLE daily_quotes (ticker TEXT, quote_date TEXT, open REAL,
            high REAL, low REAL, close REAL, volume REAL, amount REAL,
            currency TEXT, source TEXT, collected_at TEXT,
            PRIMARY KEY (ticker, quote_date, collected_at, source));
        CREATE TABLE forward_returns (ticker TEXT, trade_date TEXT,
            fwd_1d REAL, fwd_5d REAL, fwd_20d REAL, fwd_60d REAL,
            PRIMARY KEY (ticker, trade_date));
        CREATE TABLE derived_factors (ticker TEXT, factor_key TEXT, period TEXT,
            value REAL, unit TEXT, transform TEXT, transform_version TEXT,
            source TEXT, collected_at TEXT,
            PRIMARY KEY (ticker, factor_key, period, collected_at));
        -- 面板加载会 LEFT JOIN company_facts 取实际发布日（防前视偏差），
        -- 自测 schema 必须包含此表，否则报 no such table。
        CREATE TABLE IF NOT EXISTS company_facts (
            ticker TEXT NOT NULL, fact_key TEXT NOT NULL, period TEXT NOT NULL,
            freq TEXT NOT NULL DEFAULT 'H', value REAL, unit TEXT NOT NULL,
            source TEXT NOT NULL, source_url TEXT, release_date TEXT,
            collected_at TEXT NOT NULL,
            PRIMARY KEY (ticker, fact_key, period, freq, collected_at, source));
        """)
        # 价格路径：A 每日 +1%，B 持平，C 每日 -1%（factor=momentum 排序 A>B>C）
        for i in range(40):
            d = f"2026-01-{i+1:02d}" if i < 28 else f"2026-02-{i-27:02d}"
            rows = [("hkA", 100 * (1.01 ** i)), ("hkB", 100.0),
                    ("hkC", 100 * (0.99 ** i))]
            for tk, close in rows:
                con.execute(
                    "INSERT OR REPLACE INTO daily_quotes VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (tk, d, close, close, close, close, 1e6, 1e8, "HKD", "test", "2026-01-01"))
                # fwd_1d = 次日收益（最后一天为 NULL）
                con.execute(
                    "INSERT OR REPLACE INTO forward_returns (ticker, trade_date, fwd_1d) "
                    "VALUES (?,?,?)", (tk, d, None if i == 39 else close * (1.01 if tk == 'hkA' else 1.0 if tk == 'hkB' else 0.99) / close - 1))
            # factor: 用 momentum_1d 近似 = 当日收益
            for tk, close in rows:
                prev = 100 * (1.01 ** (i - 1)) if tk == 'hkA' else (100.0 if tk == 'hkB' else 100 * (0.99 ** (i - 1)))
                con.execute(
                    "INSERT OR REPLACE INTO derived_factors VALUES (?,?,?,?,?,?,?,?,?)",
                    (tk, "mom_test", d, close / prev - 1, "ratio", "rolling", "v1",
                     "price_computed", "2026-01-01"))
        con.commit()
        con.close()

        # patch _load_panel_data 的 horizon：只测 fwd_1d
        r1 = record_signals(tdb, "mom_test", "fwd_1d", top=1, trade_date="2026-01-10")
        check("record 3 signals (1 long 1 short)", r1.get("recorded", 0) >= 2,
              f"recorded={r1.get('recorded')} long={r1.get('long')} short={r1.get('short')}")

        # 再记录一个截面日，凑足 >=3 个信号可算 IC
        record_signals(tdb, "mom_test", "fwd_1d", top=1, trade_date="2026-01-13")

        r2 = backfill_signals(tdb)
        check("backfill resolves matured signals", r2["filled"] >= 3,
              f"filled={r2['filled']} not_matured={r2['not_matured']}")

        r3 = signal_report(tdb, "mom_test", "fwd_1d")
        check("report has signal IC", r3.get("signal_ic") is not None,
              f"ic={r3.get('signal_ic')}")
        # momentum 正向：long(hkA) 胜率应为 100%
        if r3.get("long") and r3["long"]["n"] > 0:
            check("long win_rate=1.0 (momentum up-stock)", r3["long"]["win_rate"] == 1.0,
                  f"win_rate={r3['long']['win_rate']} n={r3['long']['n']}")

        # 幂等：重复 record 同日不产生重复
        r4 = record_signals(tdb, "mom_test", "fwd_1d", top=1, trade_date="2026-01-10")
        con2 = sqlite3.connect(tdb)
        cnt = con2.execute(
            "SELECT COUNT(*) FROM quant_signals WHERE trade_date='2026-01-10'").fetchone()[0]
        con2.close()
        check("re-record same date idempotent", cnt == r1["recorded"],
              f"cnt={cnt} expected={r1['recorded']} re_recorded={r4['recorded']}")
    finally:
        try:
            tdb.unlink(missing_ok=True)
        except PermissionError:
            pass

    ok = all(c for _, c in checks)
    print(f"[self-test] {'ALL PASS' if ok else 'FAILURES'} "
          f"({sum(1 for _, c in checks if c)}/{len(checks)})")
    return ok


# ============================================================ CLI ===


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=str(DB_PATH))
    ap.add_argument("--factor", default="volatility_20d")
    ap.add_argument("--horizon", default="fwd_20d")
    ap.add_argument("--top", type=int, default=3)
    ap.add_argument("--date", default=None)
    ap.add_argument("--invert", action="store_true",
                    help="反向因子：long=因子值最低组（如波动率）")
    ap.add_argument("--status", default=None)
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("cmd", choices=["record", "backfill", "report", "list", "self-test"])
    args = ap.parse_args()
    db = Path(args.db)

    if args.cmd == "record":
        print(json.dumps(record_signals(db, args.factor, args.horizon, args.top,
                                        args.date, args.invert),
                         indent=2, ensure_ascii=False))
    elif args.cmd == "backfill":
        print(json.dumps(backfill_signals(db), indent=2, ensure_ascii=False))
    elif args.cmd == "report":
        print(json.dumps(signal_report(db, args.factor if args.factor != "all" else None,
                                       args.horizon),
                         indent=2, ensure_ascii=False))
    elif args.cmd == "list":
        for r in list_signals(db, args.status, args.limit):
            rr = f"{r['realized_return']:.4%}" if r["realized_return"] is not None else "-"
            print(f"{r['trade_date']} {r['ticker']:10s} {r['factor_key']:16s} "
                  f"{r['direction']:5s} pct={r['rank_pct']:.2f} ret={rr} [{r['status']}]")
    elif args.cmd == "self-test":
        raise SystemExit(0 if self_test() else 1)


if __name__ == "__main__":
    main()
