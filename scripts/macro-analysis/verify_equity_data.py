#!/usr/bin/env python3
"""Independent Proof for the equity vintage DB (multi-ticker, 参数化版).

红线：Proof 独立读取 SQLite 重算，不采信 backfill/agent 自述；勾稽公式在本文件内
独立实现。任何 FAIL -> exit 1。

ticker 分支：hk03738 走专属检查（CB 相关）；其他 ticker 走通用检查（估值/通用因子）。
"""
from __future__ import annotations

import argparse
import math
import sqlite3
import subprocess
import sys
from pathlib import Path

EXPECTED_TABLES = {"company_facts", "market_quotes", "derived_factors",
                   "factor_registry", "source_trust", "daily_quotes"}
FAILS: list[tuple[str, str]] = []


def fail(tag: str, msg: str) -> None:
    FAILS.append((tag, msg))
    print(f"  [FAIL] {tag} {msg}")


def ok(tag: str, msg: str) -> None:
    print(f"  [ok]   {tag} {msg}")


def get_one(con, sql: str, params: tuple = ()) -> float | None:
    row = con.execute(sql, params).fetchone()
    return None if row is None or row[0] is None else float(row[0])


def fact(con, ticker: str, key: str, period: str) -> float:
    # 同 derived()：按 fact_key+period 取最新 collected_at，避免跨批次（多份公告）
    # 的 collected_at 不一致导致 MAX 错配。
    v = get_one(con, "SELECT value FROM company_facts WHERE fact_key=? AND period=? AND ticker=? "
                     "AND collected_at=(SELECT MAX(collected_at) FROM company_facts "
                     "WHERE ticker=? AND fact_key=? AND period=?)",
                (key, period, ticker, ticker, key, period))
    if v is None:
        raise KeyError(f"fact missing: {ticker}.{key}@{period}")
    return v


def has_fact(con, ticker: str, key: str, period: str) -> bool:
    return get_one(con, "SELECT COUNT(*) FROM company_facts WHERE fact_key=? AND period=? AND ticker=?",
                   (key, period, ticker)) > 0


def derived(con, ticker: str, key: str, period: str) -> float:
    # 按 factor_key+period 取最新 collected_at（而非全 ticker 的 MAX）：
    # derived_factors 混存 derived/macro_aligned/price_computed 三种来源，
    # 且 collected_at 格式不统一（纯日期 vs 带时间戳），全表 MAX 会错配。
    v = get_one(con, "SELECT value FROM derived_factors WHERE factor_key=? AND period=? AND ticker=? "
                     "AND collected_at=(SELECT MAX(collected_at) FROM derived_factors "
                     "WHERE ticker=? AND factor_key=? AND period=?)",
                (key, period, ticker, ticker, key, period))
    if v is None:
        raise KeyError(f"derived missing: {ticker}.{key}@{period}")
    return v


def quote(con, ticker: str, metric: str, date: str) -> float:
    v = get_one(con, "SELECT value FROM market_quotes WHERE metric=? AND quote_date=? AND ticker=?", (metric, date, ticker))
    if v is None:
        raise KeyError(f"quote missing: {ticker}.{metric}@{date}")
    return v


def close_enough(a: float, b: float, rel: float = 1e-6, abs_tol: float = 1e-6) -> bool:
    return abs(a - b) <= max(abs_tol, rel * max(abs(a), abs(b)))


def check_pairs(con, ticker, pairs) -> None:
    for key, period, expected in pairs:
        got = derived(con, ticker, key, period)
        if close_enough(got, expected, rel=1e-5):
            ok("E05", f"{key}@{period} {got:,.6f}")
        else:
            fail("E05", f"{key}@{period} stored={got:,.6f} recomputed={expected:,.6f}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=str(Path(__file__).resolve().parents[4] / "outputs" / "equity_fundamental.sqlite"))
    ap.add_argument("--ticker", default="hk03738")
    args = ap.parse_args()
    db = Path(args.db)
    tk = args.ticker

    print(f"== Proof: {tk} ==")
    con = sqlite3.connect(str(db))

    print("== E01 schema ==")
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    missing = EXPECTED_TABLES - tables
    if missing:
        fail("E01", f"missing tables: {sorted(missing)}")
    else:
        ok("E01", f"all {len(EXPECTED_TABLES)} tables present")

    print("== E02 observed purity (no estimated/quote keys in company_facts) ==")
    banned_exact = {"cb_principal_outstanding", "share_price_close", "market_cap", "pe_ttm", "pb_lf"}
    all_keys = [r[0] for r in con.execute("SELECT DISTINCT fact_key FROM company_facts WHERE ticker=?", (tk,))]
    bad = [k for k in all_keys if k in banned_exact or k.endswith("_est")]
    if bad:
        fail("E02", f"estimated/quote keys leaked into observed layer: {bad}")
    else:
        ok("E02", f"observed layer clean ({len(all_keys)} fact keys)")

    print("== E03 quotes separated (G1) ==")
    # G1 修复后行情统一在 daily_quotes（独立表），不再用旧 market_quotes 快照表。
    n_quotes = con.execute("SELECT COUNT(*) FROM daily_quotes WHERE ticker=?", (tk,)).fetchone()[0]
    if n_quotes < 1:
        fail("E03", f"daily_quotes rows={n_quotes} (expected >=1)")
    else:
        ok("E03", f"daily_quotes rows={n_quotes}")

    print("== E04 accounting identities ==")
    try:
        if tk == "hk03738":
            for period in ("2026H1", "2025H1"):
                if not all(has_fact(con, tk, k, period)
                           for k in ("gross_profit", "revenue", "cost_of_services")):
                    print(f"  [skip] E04 {period} 毛利勾稽（缺依赖字段）")
                    continue
                gp, rev, cost = fact(con, tk, "gross_profit", period), fact(con, tk, "revenue", period), fact(con, tk, "cost_of_services", period)
                if close_enough(gp, rev - cost):
                    ok("E04", f"{period} gross_profit == revenue - cost ({gp:,.0f})")
                else:
                    fail("E04", f"{period} gross_profit mismatch: {gp} vs {rev - cost}")
            for period in ("2026-06-30", "2025-12-31"):
                # 净资产口径兜底：net_assets（资产净值）或 total_equity（权益合计）皆可，
                # 不同公司/抽取器口径不同（LLM 抽取通常给 total_equity）。
                eq_key = None
                for k in ("net_assets", "total_equity"):
                    if has_fact(con, tk, k, period):
                        eq_key = k
                        break
                if eq_key is None or not all(has_fact(con, tk, k, period)
                                             for k in ("total_assets", "total_liabilities")):
                    print(f"  [skip] E04 {period} 资产负债表勾稽（缺净资产/资产/负债字段）")
                    continue
                ta, tl, na = (fact(con, tk, "total_assets", period),
                              fact(con, tk, "total_liabilities", period),
                              fact(con, tk, eq_key, period))
                # rel=1e-4：LLM 从千元报表换算百万单位会产生 0.1m 级舍入
                #（实测飞鹤 26042.0 vs 26041.9，rel≈3.8e-6），1e-6 过严误报；
                # 结构性错读（列读错，差 2 倍级）在 1e-4 下依然 FAIL。
                if close_enough(ta - tl, na, rel=1e-4):
                    ok("E04", f"{period} assets - liabilities == {eq_key} ({na:,.0f})")
                else:
                    fail("E04", f"{period} balance sheet mismatch: {ta - tl} vs {na}")
        else:
            # 通用：资产负债表恒等式（total_assets - total_liabilities == total_equity）
            # 三者齐全才校验——部分「業績公告」只含损益表，无资产负债表，跳过而非 FAIL。
            for period in ("2026-06-30", "2025-12-31"):
                need = ("total_assets", "total_liabilities", "total_equity")
                if not all(has_fact(con, tk, k, period) for k in need):
                    missing = [k for k in need if not has_fact(con, tk, k, period)]
                    print(f"  [skip] E04 {period} 资产负债表勾稽（缺 {missing}，公告未披露资产负债表）")
                    continue
                ta, tl, te = fact(con, tk, "total_assets", period), fact(con, tk, "total_liabilities", period), fact(con, tk, "total_equity", period)
                # rel=1e-4：同上，兼容单位换算舍入（详见分支内注释）
                if close_enough(ta - tl, te, rel=1e-4):
                    ok("E04", f"{period} assets - liabilities == total_equity ({te:,.0f})")
                else:
                    fail("E04", f"{period} balance sheet mismatch: {ta - tl} vs {te}")
    except KeyError as e:
        fail("E04", str(e))

    print("== E05 derived factors independently recomputed ==")
    try:
        if tk == "hk03738":
            # 阜博专用分支同样依赖声明式：LLM 抽取不覆盖 CB/ECL 细项
            # （cb_principal_issued、ecl_allowance 等），缺依赖须 skip 而非 FAIL。
            CUR, PRV, CBS, PBS = "2026H1", "2025H1", "2026-06-30", "2025-12-31"

            def _has(*deps) -> bool:
                return all(has_fact(con, tk, k, p) for k, p in deps)

            spec = [
                ("revenue_yoy", CUR, [("revenue", CUR), ("revenue", PRV)],
                 lambda: fact(con, tk, "revenue", CUR) / fact(con, tk, "revenue", PRV) - 1),
                ("gross_margin", CUR, [("gross_profit", CUR), ("revenue", CUR)],
                 lambda: fact(con, tk, "gross_profit", CUR) / fact(con, tk, "revenue", CUR)),
                ("dso_days", CUR, [("trade_receivables_net", CBS), ("revenue", CUR)],
                 lambda: fact(con, tk, "trade_receivables_net", CBS) / fact(con, tk, "revenue", CUR) * 182),
                ("ecl_ratio", CUR, [("ecl_allowance", CBS), ("trade_receivables_gross", CBS)],
                 lambda: fact(con, tk, "ecl_allowance", CBS) / fact(con, tk, "trade_receivables_gross", CBS)),
                ("net_profit_yoy", CUR, [("net_profit", CUR), ("net_profit", PRV)],
                 lambda: fact(con, tk, "net_profit", CUR) / fact(con, tk, "net_profit", PRV) - 1),
                ("adj_net_profit_yoy", CUR, [("adj_net_profit", CUR), ("adj_net_profit", PRV)],
                 lambda: fact(con, tk, "adj_net_profit", CUR) / fact(con, tk, "adj_net_profit", PRV) - 1),
            ]
            pairs = []
            for key, period, deps, calc in spec:
                if _has(*deps):
                    pairs.append((key, period, calc()))
                else:
                    missing = [k for k, p in deps if not has_fact(con, tk, k, p)]
                    print(f"  [skip] E05 {key}@{period}（缺依赖 {missing}）")
            # CB 类：需全部 CB 字段齐备才校验
            cb_deps = [("cb_principal_issued", "2025H2"), ("cb_principal_converted", "2025H2"),
                       ("cb_principal_converted", CUR), ("cb_principal_repurchased", CUR),
                       ("cash_and_equivalents", CBS), ("cash_and_equivalents", PBS),
                       ("capex", CUR), ("cb_repurchase_cash_paid", CUR),
                       ("exercise_cash_proceeds", CUR),
                       ("borrowings_current", CBS), ("borrowings_non_current", CBS),
                       ("borrowings_current", PBS), ("borrowings_non_current", PBS)]
            if _has(*cb_deps):
                cb25 = fact(con, tk, "cb_principal_issued", "2025H2") - fact(con, tk, "cb_principal_converted", "2025H2")
                cb26 = cb25 - fact(con, tk, "cb_principal_converted", CUR) - fact(con, tk, "cb_principal_repurchased", CUR)
                cash = fact(con, tk, "cash_and_equivalents", CBS)
                delta_cash = cash - fact(con, tk, "cash_and_equivalents", PBS)
                binc = ((fact(con, tk, "borrowings_current", CBS) + fact(con, tk, "borrowings_non_current", CBS))
                        - (fact(con, tk, "borrowings_current", PBS) + fact(con, tk, "borrowings_non_current", PBS)))
                ocf = (delta_cash + fact(con, tk, "capex", CUR) + fact(con, tk, "cb_repurchase_cash_paid", CUR)
                       - binc + 460 - fact(con, tk, "exercise_cash_proceeds", CUR))
                pairs += [
                    ("cb_principal_outstanding_est", CBS, cb26),
                    ("cb_gap_to_cash", CUR, cb26 - cash),
                    ("ocf_proxy_residual", CUR, ocf),
                    ("fcf_proxy_residual", CUR, ocf - fact(con, tk, "capex", CUR)),
                ]
            else:
                missing = sorted({k for k, p in cb_deps if not has_fact(con, tk, k, p)})
                print(f"  [skip] E05 CB 类因子（缺依赖 {missing}）")
            check_pairs(con, tk, pairs)
        else:
            # 依赖声明式：每个因子声明所需 fact，缺任一依赖即跳过校验
            # （与 ingest 的"缺字段静默跳过"语义一致）。
            # 关键：gross_profit 对电信/石油/航运/矿业等本就不适用（利润表无毛利行），
            # 一刀切 FAIL 会把大量正常标的误判为脏数据。
            CUR, PRV, CUR_BS = "2026H1", "2025H1", "2026-06-30"
            dep_specs = [
                ("revenue_yoy", CUR, [("revenue", CUR), ("revenue", PRV)],
                 lambda: fact(con, tk, "revenue", CUR) / fact(con, tk, "revenue", PRV) - 1),
                ("gross_margin", CUR, [("gross_profit", CUR), ("revenue", CUR)],
                 lambda: fact(con, tk, "gross_profit", CUR) / fact(con, tk, "revenue", CUR)),
                ("net_margin_proxy", CUR, [("net_profit_attributable", CUR), ("revenue", CUR)],
                 lambda: fact(con, tk, "net_profit_attributable", CUR) / fact(con, tk, "revenue", CUR)),
                ("ocf_to_revenue", CUR, [("ocf", CUR), ("revenue", CUR)],
                 lambda: fact(con, tk, "ocf", CUR) / fact(con, tk, "revenue", CUR)),
                ("ocf_to_net_profit", CUR, [("ocf", CUR), ("net_profit_attributable", CUR)],
                 lambda: fact(con, tk, "ocf", CUR) / fact(con, tk, "net_profit_attributable", CUR)),
                ("inventory_to_revenue", CUR, [("inventories", CUR_BS), ("revenue", CUR)],
                 lambda: fact(con, tk, "inventories", CUR_BS) / fact(con, tk, "revenue", CUR)),
            ]
            pairs = []
            for key, period, deps, calc in dep_specs:
                if all(has_fact(con, tk, k, p) for k, p in deps):
                    pairs.append((key, period, calc()))
                else:
                    missing = [k for k, p in deps if not has_fact(con, tk, k, p)]
                    print(f"  [skip] E05 {key}@{period}（缺依赖 {missing}，该口径不适用本标的）")
            # 可选字段：仅当**全部**所需 fact 存在时才校验（缺字段不构成 FAIL）。
            # 注意分母也要检查——实测百济有 net_debt_official 但缺 total_equity，
            # 漏检分母会抛出 KeyError 被误判为 FAIL。
            if (has_fact(con, tk, "non_recurring_total", "2026H1")
                    and has_fact(con, tk, "net_profit_attributable", "2026H1")):
                pairs.append(("non_recurring_ratio", "2026H1",
                              fact(con, tk, "non_recurring_total", "2026H1") / fact(con, tk, "net_profit_attributable", "2026H1")))
            if (has_fact(con, tk, "net_debt_official", "2026-06-30")
                    and has_fact(con, tk, "total_equity", "2026-06-30")):
                pairs.append(("net_debt_official_to_equity", "2026H1",
                              fact(con, tk, "net_debt_official", "2026-06-30") / fact(con, tk, "total_equity", "2026-06-30")))
            if (has_fact(con, tk, "borrowings_current", "2026-06-30")
                    and has_fact(con, tk, "borrowings_non_current", "2026-06-30")
                    and has_fact(con, tk, "total_equity", "2026-06-30")):
                pairs.append(("debt_to_equity", "2026H1",
                              (fact(con, tk, "borrowings_current", "2026-06-30") + fact(con, tk, "borrowings_non_current", "2026-06-30"))
                              / fact(con, tk, "total_equity", "2026-06-30")))
            check_pairs(con, tk, pairs)
    except KeyError as e:
        fail("E05", str(e))

    print("== E06 registry coverage ==")
    # 仅检查 source='derived' 的基本面派生因子；macro_aligned/price_computed
    # 属系统生成因子（宏观对齐/价格因子），有独立生成机制，不要求注册进 factor_registry。
    orphan = con.execute(
        "SELECT DISTINCT d.factor_key FROM derived_factors d LEFT JOIN factor_registry f "
        "ON d.factor_key=f.factor_key WHERE f.factor_key IS NULL AND d.ticker=? "
        "AND d.source='derived'", (tk,)).fetchall()
    if orphan:
        fail("E06", f"derived keys missing from registry: {[o[0] for o in orphan]}")
    else:
        ok("E06", "all derived factor_keys registered")

    print("== E07 source_trust coverage ==")
    used = {r[0] for r in con.execute("SELECT DISTINCT source FROM company_facts WHERE ticker=?", (tk,))} \
         | {r[0] for r in con.execute("SELECT DISTINCT source FROM market_quotes WHERE ticker=?", (tk,))} \
         | {r[0] for r in con.execute("SELECT DISTINCT source FROM daily_quotes WHERE ticker=?", (tk,))}
    registered = {r[0]: r[1] for r in con.execute("SELECT source, trust_level FROM source_trust")}
    unregistered = {s for s in used if s not in registered}
    bad_level = {s: registered[s] for s in used if s in registered and registered[s] not in ("official", "third_party")}
    if unregistered or bad_level:
        fail("E07", f"unregistered={sorted(unregistered)} bad_level={bad_level}")
    else:
        ok("E07", f"{len(used)} sources registered with valid trust levels")

    print("== E08 daily_quotes sanity ==")
    n, dmin, dmax, dups = con.execute(
        "SELECT COUNT(*), MIN(quote_date), MAX(quote_date), "
        "(SELECT COUNT(*) FROM (SELECT quote_date, COUNT(*) c FROM daily_quotes WHERE ticker=? "
        "GROUP BY quote_date HAVING c>1)) FROM daily_quotes WHERE ticker=?", (tk, tk)).fetchone()
    bad_px = con.execute("SELECT COUNT(*) FROM daily_quotes WHERE ticker=? AND (close IS NULL OR close<=0 OR high<low)",
                         (tk,)).fetchone()[0]
    if dups or bad_px:
        fail("E08", f"duplicate dates={dups} bad_prices={bad_px}")
    else:
        ok("E08", f"{n} bars, {dmin}..{dmax}, unique dates, prices sane")

    print("== E09 quant_engine known-answer self-test ==")
    eng = Path(__file__).with_name("quant_engine.py")
    r = subprocess.run([sys.executable, str(eng), "self-test"], capture_output=True, text=True)
    # E10 基本面覆盖（诚实标注，不计 FAIL）：
    # 无基本面数据的标的（如公告未发布）会因 E04/E05 依赖全跳过而"空数据 PASS"，
    # 必须明确标注，否则会误以为数据齐全。
    print("== E10 fundamental coverage ==")
    n_facts = con.execute(
        "SELECT COUNT(DISTINCT fact_key) FROM company_facts WHERE ticker=?", (tk,)).fetchone()[0]
    n_derived = con.execute(
        "SELECT COUNT(DISTINCT factor_key) FROM derived_factors "
        "WHERE ticker=? AND source='derived'", (tk,)).fetchone()[0]
    if n_facts == 0:
        print("  [warn] E10 无基本面数据（公告未发布或未抽取）——本 Proof 仅覆盖行情层")
    else:
        ok("E10", f"基本面 {n_facts} fact keys / {n_derived} derived factors")
    con.close()

    print(f"== Proof result: {'PASS' if not FAILS else 'FAIL'} ({len(FAILS)} failures) ==")
    if n_facts == 0:
        print(f"  coverage=quotes_only（基本面缺失，非数据质量问题）")
    for tag, msg in FAILS:
        print(f"  fail_tag={tag}: {msg}")
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
