#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ashare_agri_validate.py — A 股农业池（农林牧渔）的三关验证

承接 equity_narrow_pool_power.py / equity_daily_ic_robust.py 的方法，
在新采集的 A 股农业池数据上运行同一套三关框架，以便与港股结果**同口径对比**。

相比港股验证，本脚本新增了此前因数据缺失而无法构造的因子：
  - turnover_level_20d  20 日平均换手率      （港股无换手率，无法构造）
  - turnover_ratio_20d  换手率近 20 日/前 20 日（比量比更干净：剔除股本变动）
  - amihud_20d          |ret|/成交额 非流动性 （港股 amount 全 NULL，无法构造）

中性化控制变量改用 log(20 日成交额)，比港股用 log(成交量) 更贴近规模/流动性。

三关
----
① 功效：置换检验实测噪声 → σ_true → MDE
② 显著性：中性化 + MCC + 非重叠子样本 + 分年度
③ 经济可行性：在 equity_layer_backtest.py 中单独跑（本脚本只出①、②）

用法
----
    python ashare_agri_validate.py
    python ashare_agri_validate.py --reps 20 --min-cross 30
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime
from pathlib import Path

import numpy as np
from scipy.stats import rankdata, norm

from equity_narrow_pool_power import (
    OUT_DIR, HOLDING_PERIODS, newey_west_t, _roll_mean, mde_of, verdict_for,
)
from ashare_hfq_access import qc_bad_codes, QcMissing

# 路径：先用 .parent 取到 macro-analysis 目录，再上溯 3 层到工作区根。
# ⚠️ 直接写 Path(__file__).resolve().parents[3] 会少一层（落在 my-deepseek-harness），
#    导致 DB 路径错误、报 "unable to open database file"，且错误信息不指向真实原因。
_HERE = Path(__file__).resolve().parent
ROOT = _HERE.parents[3]
DB = ROOT / "outputs" / "ashare_agri_hfq_xq.sqlite"   # 独立库（后复权 + 流动性）
HK_POWER = OUT_DIR / "narrow_pool_power.json"

FACTOR_DEFS = [
    ("momentum_20d",     "20日动量"),
    ("momentum_60d",     "60日动量"),
    ("reversal_5d",      "5日反转"),
    ("volatility_20d",   "20日波动"),
    ("price_to_ma20",    "价格/MA20"),
    ("volume_ratio_20d", "20日量比"),
    ("turnover_level_20d", "20日换手水平", ),
    ("turnover_ratio_20d", "20日换手比"),
    ("amihud_20d",       "Amihud非流动性"),
]


PRICE_TABLE = "daily_quotes_hfq"   # 后复权（腾讯 fqkline hfq），价格恒为正
LIQ_TABLE = "daily_liquidity"      # 成交额/换手率（东财，从主库复制）
QC_TABLE = "hfq_qc"                # 质检表：bad=1 的标的必须剔


def load_panel(conn: sqlite3.Connection, table: str = PRICE_TABLE,
               apply_qc: bool = True):
    """价格取后复权表；成交额/换手率从 daily_liquidity 关联（腾讯 hfq 无该字段）。

    ★ QC 闸门（2026-09-03 补建）：原实现**完全没有**过滤质检判坏的标的，是本
    项目 A 股在役链路上唯一一处真实漏网（农业/半导体/中证800/宽池的回测脚本均已
    过滤，只有本脚本没有）。治理方式不是就地补一行 SQL——那样下次还会有第二个
    脚本漏——而是统一走 ashare_hfq_access.qc_bad_codes()：QC 表缺失时抛
    QcMissing（fail-loud），绝不静默退化成「全量放行」。
    """
    rows = conn.execute(
        f"SELECT p.trade_date, p.code, p.close, l.volume, l.amount, l.turnover_pct "
        f"FROM {table} p "
        f"LEFT JOIN {LIQ_TABLE} l ON l.code = p.code AND l.trade_date = p.trade_date "
        f"WHERE p.close IS NOT NULL ORDER BY p.trade_date, p.code"
    ).fetchall()
    if not rows:
        raise SystemExit(f"{table} 为空，请先运行 ashare_agri_collect.py --only-badj")

    # QC 闸门：必须在 conn 关闭前查询
    bad = qc_bad_codes(None, QC_TABLE, conn) if apply_qc else set()

    dates = sorted({r[0] for r in rows})
    codes = sorted({r[1] for r in rows} - bad)
    di = {d: i for i, d in enumerate(dates)}
    ci = {c: j for j, c in enumerate(codes)}
    close = np.full((len(dates), len(codes)), np.nan)
    volume = np.full_like(close, np.nan)
    amount = np.full_like(close, np.nan)
    turn = np.full_like(close, np.nan)
    for d, c, cl, v, a, t in rows:
        if c in bad:          # QC 判坏：整只剔除，不进入任何截面
            continue
        i, j = di[d], ci[c]
        close[i, j] = cl
        if v is not None:
            volume[i, j] = v
        if a is not None:
            amount[i, j] = a
        if t is not None:
            turn[i, j] = t
    return np.array(dates), np.array(codes), close, volume, amount, turn


def compute_factors(close, volume, amount, turn):
    f = {}

    def sdiv(cur, lag):
        out = np.full_like(cur, np.nan)
        if cur.shape[0] > lag:
            out[lag:] = cur[lag:] / cur[:-lag] - 1.0
        return out

    f["momentum_20d"] = sdiv(close, 20)
    f["momentum_60d"] = sdiv(close, 60)
    f["reversal_5d"] = -sdiv(close, 5)

    r1 = sdiv(close, 1)
    var20 = _roll_mean(r1 ** 2, 20) - _roll_mean(r1, 20) ** 2
    f["volatility_20d"] = np.sqrt(np.where(var20 > 0, var20, np.nan))
    f["price_to_ma20"] = close / _roll_mean(close, 20) - 1.0

    v20 = _roll_mean(volume, 20)
    v20p = np.full_like(volume, np.nan)
    v20p[20:] = v20[:-20]
    f["volume_ratio_20d"] = np.where(v20p > 0, v20 / np.where(v20p > 0, v20p, 1.0), np.nan)

    t20 = _roll_mean(turn, 20)
    t20p = np.full_like(turn, np.nan)
    t20p[20:] = t20[:-20]
    f["turnover_level_20d"] = np.log(np.where(t20 > 0, t20, np.nan))
    f["turnover_ratio_20d"] = np.where(t20p > 0, t20 / np.where(t20p > 0, t20p, 1.0), np.nan)

    a20 = _roll_mean(amount, 20)
    ill = np.where((a20 > 0) & np.isfinite(r1), np.abs(r1) / np.where(a20 > 0, a20, 1.0), np.nan)
    f["amihud_20d"] = _roll_mean(ill, 20) * 1e8
    return f


def fwd(close, h):
    out = np.full_like(close, np.nan)
    if close.shape[0] > h:
        out[:-h] = close[h:] / close[:-h] - 1.0
    return out


def _rank_std(a):
    r = rankdata(a).astype(np.float64)
    r -= r.mean()
    s = r.std()
    return r / s if s > 0 else np.zeros_like(r)


def ic_with_null(fmat, rmat, ctrl, rng, reps, min_cross):
    """逐日 IC + 置换噪声 + 中性化 IC。ctrl 可为 None。"""
    obs, neu, widths, nvar, tvar = [], [], [], [], []
    for t in range(fmat.shape[0]):
        m = np.isfinite(fmat[t]) & np.isfinite(rmat[t])
        if ctrl is not None:
            m &= np.isfinite(ctrl[t])
        n = int(m.sum())
        if n < min_cross:
            continue
        f, r = fmat[t][m], rmat[t][m]
        rf, rr = _rank_std(f), _rank_std(r)
        if rf.std() == 0 or rr.std() == 0:
            continue
        obs.append(float((rf * rr).sum() / n))
        widths.append(n)

        R = np.array([rng.permutation(rr) for _ in range(reps)])
        R -= R.mean(axis=1, keepdims=True)
        Rn = np.sqrt((R * R).sum(axis=1))
        ok = Rn > 0
        v = np.zeros(reps)
        v[ok] = (R[ok] @ rf) / (Rn[ok] * np.sqrt((rf * rf).sum()))
        nvar.append(float((v ** 2).mean()))
        tvar.append(1.0 / (n - 1))

        if ctrl is not None:
            rc = _rank_std(ctrl[t][m])
            X = np.column_stack([np.ones(n), rc])
            try:
                beta, *_ = np.linalg.lstsq(X, rf, rcond=None)
                res = rf - X @ beta
            except np.linalg.LinAlgError:
                res = rf
            if res.std() > 0:
                rn = (res - res.mean()) / res.std()
                neu.append(float((rn * rr).sum() / n))
            else:
                neu.append(0.0)
        else:
            neu.append(obs[-1])
    return (np.asarray(obs), np.asarray(neu), np.asarray(widths),
            float(np.mean(nvar)) if nvar else np.nan,
            float(np.mean(tvar)) if tvar else np.nan)


def nonoverlap_t(ic, h):
    ts = []
    for off in range(h):
        sub = ic[off::h]
        if len(sub) >= 20:
            _, _, t = newey_west_t(sub, lags=0)
            ts.append(float(t))
    return (min(ts), float(np.mean(ts))) if ts else (np.nan, np.nan)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=40)
    ap.add_argument("--min-cross", type=int, default=30)
    ap.add_argument("--db", default=str(DB), help="数据库路径（建议用 snapshots/ 下的快照）")
    ap.add_argument("--price-table", default=PRICE_TABLE,
                    help=f"价格表：{PRICE_TABLE}(后复权,默认)")
    ap.add_argument("--ctrl", choices=("amount", "volume"), default="amount",
                    help="中性化控制变量：amount=log(20日均额,默认,需 daily_liquidity.amount)；"
                         "volume=log(20日均量)（用于无成交额的库，如 semi 派生换手率库；"
                         "与港股线 equity_narrow_pool_power 的 log 成交量控制口径一致）")
    ap.add_argument("--out", default=None,
                    help="输出 JSON 文件名（默认 ashare_agri_validate.json；跨库复用时必须显式指定，防覆盖）")
    ap.add_argument("--no-qc", action="store_true",
                    help="关闭质检闸门（默认开启）。仅在库本身无 hfq_qc 表且已书面说明"
                         "后果时使用；结论须标注 qc_applied=false，不得与开闸结果混用")
    args = ap.parse_args()
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    db = Path(args.db)
    if not db.exists():
        raise SystemExit(f"数据库不存在: {db}\n请先运行 ashare_badj_collect.py")
    conn = sqlite3.connect(str(db))
    try:
        dates, codes, close, volume, amount, turn = load_panel(
            conn, args.price_table, apply_qc=not args.no_qc)
    except QcMissing as e:
        raise SystemExit(
            f"{e}\n\n如需在无质检的库上继续，请显式加 --no-qc，"
            f"并在结论中标注 qc_applied=false（不得与开闸结果混用）。")
    # 股票名称仅在旧主库里有；独立库缺失时降级为「代码即名称」，不影响计算
    try:
        st = dict(conn.execute("SELECT code, name FROM universe_agri").fetchall())
    except sqlite3.OperationalError:
        st = {c: c for c in codes}
    conn.close()
    T, M = close.shape
    print(f"农业池面板: {T} 日 × {M} 只   {dates[0]} ~ {dates[-1]}   "
          f"质检闸门={'关闭(⚠ 未过滤坏数据)' if args.no_qc else '开启'}")

    factors = compute_factors(close, volume, amount, turn)
    if args.ctrl == "volume":
        # ⚠️ 半导体等库 daily_liquidity.amount 全 NULL（派生换手率路线，无原生成交额）。
        # 用 log(20日均量) 兜底（港股线同口径）。amount 缺失时若仍走 amount 分支，
        # ctrl 全 NaN → 每日截面被 min_cross 静默清零，输出"看似正常"的空表。
        ctrl = np.where(_roll_mean(volume, 20) > 0, np.log(np.where(
            _roll_mean(volume, 20) > 0, _roll_mean(volume, 20), 1.0)), np.nan)
    else:
        ctrl = np.where(_roll_mean(amount, 20) > 0, np.log(np.where(
            _roll_mean(amount, 20) > 0, _roll_mean(amount, 20), 1.0)), np.nan)
    rng = np.random.default_rng(20260902)

    n_tests = len(FACTOR_DEFS) * len(HOLDING_PERIODS)
    mcc_t = float(norm.ppf(1 - 0.05 / (2 * n_tests)))
    print(f"因子 {len(FACTOR_DEFS)} × 持有期 {len(HOLDING_PERIODS)} = {n_tests} 次检验 "
          f"→ MCC 阈值 |t| ≥ {mcc_t:.3f}\n")

    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "panel": {"days": int(T), "tickers": int(M), "start": str(dates[0]), "end": str(dates[-1])},
        "db": str(db), "price_table": args.price_table,
        "mcc_threshold_t": round(mcc_t, 3), "n_tests": n_tests,
        "notes": ["价格表 = 后复权(hfq, 腾讯 fqkline)：前复权在长周期下会被高分红扣成负数"
                  "（实测牧原 002714 前复权最低 -3.60 元、506 日价格≤0.5），"
                  "这是股票级缺陷，截断时间区间无法规避，必须禁用于收益率计算",
                  "成交额/换手率来自东财无复权表（腾讯 hfq 无该字段），按 (code,trade_date) 关联",
                  "标的池已剔除北交所(9只)与B股(3只)：流动性不足以建仓",
                  "中性化控制变量 = " + ("log(20日成交额)" if args.ctrl == "amount" else "log(20日成交量)（该库无成交额，降级口径）")],
        "factors": {},
    }

    print("【第一关：功效 + 第二关：显著性】")
    hdr = f"{'因子':<16}{'H':>4}{'期数':>7}{'截面':>6}{'原始IC':>9}{'中性IC':>9}{'NW-t':>8}{'非重t_min':>10}{'噪声实测':>10}{'噪声理论':>10}{'σ_true':>8}{'MDE':>8}  判定"
    print(hdr)
    print("-" * len(hdr))

    st_vals = []
    for fkey, flabel in FACTOR_DEFS:
        fmat = factors[fkey]
        report["factors"][fkey] = {"label": flabel, "by_holding": {}}
        for h in HOLDING_PERIODS:
            rmat = fwd(close, h)
            obs, neu, widths, nv, tv = ic_with_null(
                fmat, rmat, ctrl, rng, args.reps, args.min_cross)
            if len(obs) < 60:
                continue
            c = obs - obs.mean()
            var_obs = float((c ** 2).sum() / (len(c) - 1))
            sig = float(np.sqrt(max(var_obs - nv, 0.0)))

            mi_raw, _, t_raw = newey_west_t(obs, lags=max(h - 1, 0))
            mi_n, _, t_n = newey_west_t(neu, lags=max(h - 1, 0))
            tmin, tmean = nonoverlap_t(neu, h)
            md = mde_of(sig, int(np.median(widths)), T, h)

            if abs(t_n) >= mcc_t and abs(tmin) >= 1.96:
                verdict = "★ 通过"
            elif abs(t_n) >= mcc_t:
                verdict = "MCC通过/非重叠不稳"
            else:
                verdict = "未通过"

            print(f"{flabel:<16}{h:>4}{len(obs):>7}{int(np.median(widths)):>6}"
                  f"{mi_raw:>9.4f}{mi_n:>9.4f}{t_n:>8.2f}{tmin:>10.2f}"
                  f"{nv:>10.4f}{tv:>10.4f}{sig:>8.4f}{md['mde']:>8.4f}  {verdict}")

            report["factors"][fkey]["by_holding"][str(h)] = {
                "n_periods": int(len(obs)), "median_cross": int(np.median(widths)),
                "ic_raw": round(float(mi_raw), 5), "t_raw_nw": round(float(t_raw), 3),
                "ic_neutral": round(float(mi_n), 5), "t_neutral_nw": round(float(t_n), 3),
                "nonoverlap_t_min": round(float(tmin), 3),
                "nonoverlap_t_mean": round(float(tmean), 3),
                "var_noise_measured": round(nv, 5),
                "var_noise_theory": round(tv, 5),
                "sigma_true": round(sig, 5),
                "mde": md["mde"], "verdict": verdict,
                "ratio_ic_over_mde": round(abs(float(mi_n)) / md["mde"], 2) if md["mde"] else None,
            }
            if h == 5:
                st_vals.append(sig)

    smed = float(np.median(st_vals)) if st_vals else 0.0
    report["sigma_true_median_h5"] = round(smed, 5)

    print(f"\nσ_true 中位数(H=5): {smed:.4f}")

    # 与港股对比
    if HK_POWER.exists():
        hk = json.loads(HK_POWER.read_text(encoding="utf-8"))
        hk_st = hk.get("factors", {})
        print(f"\n{'='*70}\n港股 vs A股农业池 对比（H=5 口径）\n{'='*70}")
        print(f"{'因子':<16}{'港股σ_true':>12}{'A股σ_true':>12}{'港股MDE':>10}{'A股MDE':>10}")
        for fkey, flabel in FACTOR_DEFS:
            hkv = hk_st.get(fkey, {}).get("by_holding", {}).get("5")
            av = report["factors"].get(fkey, {}).get("by_holding", {}).get("5")
            if not av:
                continue
            hs = hkv["sigma_true"] if hkv else None
            hm = hkv["mde_current_pool"]["mde"] if hkv else None
            print(f"{flabel:<16}"
                  f"{(f'{hs:.4f}' if hs is not None else '—'):>12}"
                  f"{av['sigma_true']:>12.4f}"
                  f"{(f'{hm:.4f}' if hm is not None else '—'):>10}"
                  f"{av['mde']:>10.4f}")
        report["hk_comparison_available"] = True

    # ★ 坑⑱（2026-09-03）：原为 `OUT_DIR / (args.out or "ashare_agri_validate.json")`，
    #   用 --db 指向别的池时会覆盖另一份结论产物（agri_backtest 已实测发生并恢复）。
    #   修法：默认库保持原名兼容既有引用，非默认库自动加库名后缀。
    if args.out:
        out = OUT_DIR / args.out
    elif db.resolve() == Path(DB).resolve():
        out = OUT_DIR / "ashare_agri_validate.json"
    else:
        out = OUT_DIR / f"ashare_agri_validate_{db.stem}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n报告已写入: {out}")
    print(f"判定口径: |t_neutral| ≥ {mcc_t:.2f}(MCC) 且 非重叠 |t_min| ≥ 1.96")


if __name__ == "__main__":
    main()
