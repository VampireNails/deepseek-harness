#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""批量风险扫描（Route V-B）— 全池风险名单，不依赖任何个人账户。

定位（见 workflow.md §7 定位纪律，勿改）：
    本脚本的产物是「对任意标的可复用、可验证的风险判据 + 名单 + 边界」，
    **与任何人的持仓无关**。输入是「池」，不是「账户」。

与单只版 ashare_risk_verdict.py 的关系：
    判据（阈值 / 分位语义 / 标签名）100% 复用，不做任何重新定值；
    差异只在**批量化实现**（共享连接、向量化），并用 `--selfcheck` 做等价性对照。

判据清单（3 条，其中 2 条已验证、波动率为描述性）：
    [M] 两融拥挤    margin   CROWDED ≥90 分位 / ELEVATED 75~90   —— 已验证
    [A] 农业净利同比 agri     AVOID ≥80 分位 / COLLAPSE ≤−100%    —— 已验证（仅池内）
    [V] 波动率状态  vol      HIGH ≥90 / ELEVATED 75~90 + 深度回撤 —— 仅描述性

用法：
    python ashare_risk_scan.py --pool csi800            # 扫一个池
    python ashare_risk_scan.py --pool margin            # 扫全部两融标的
    python ashare_risk_scan.py --pool csi800 --selfcheck 30
    python ashare_risk_scan.py --pool agri --asof 2025-06-30

输出（默认 outputs/YYYY-MM-DD/）：
    risk_scan_{pool}_{dbstem}.json   全量明细（每只股票每个标签）
    risk_scan_{pool}_{dbstem}.csv    表格，便于排序筛选
    risk_scan_{pool}_{dbstem}.md     风险名单（按标签分组 + 边界声明）
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import sqlite3
import sys
from datetime import datetime, date
from pathlib import Path

import numpy as np

# --- 路径 -------------------------------------------------------------------
HERE = Path(__file__).resolve().parent
OUT = HERE.parent.parent.parent.parent / "outputs"      # <repo>/outputs
if not OUT.exists():                                     # 回退：脚本目录/outputs
    OUT = HERE / "outputs"
ALTDB = OUT / "ashare_altdata.sqlite"
FUNDDB = OUT / "ashare_fundamental.sqlite"
REGDB = OUT / "ashare_strategy_registry.sqlite"

sys.path.insert(0, str(HERE))

# --- 判据常量：全部从单只版 import，禁止在此重新定值 -------------------------
import ashare_risk_verdict as rv                          # noqa: E402
import ashare_margin_neglist as mn                        # noqa: E402

DEEP_DD = rv.DEEP_DD
NP_YOY_AVOID_PCT = rv.NP_YOY_AVOID_PCT
NP_YOY_COLLAPSE = rv.NP_YOY_COLLAPSE
RV_WINDOW = rv.RV_WINDOW
RV_MIN_OBS = rv.RV_MIN_OBS
DROP_FIRST_N = rv.DROP_FIRST_N

POOL_DBS = {
    "csi800": "ashare_csi800_hfq_xq.sqlite",
    "wide": "ashare_wide_hfq_xq.sqlite",
    "agri": "ashare_agri_hfq_xq.sqlite",
    "semi": "ashare_semi_hfq_xq.sqlite",
}
FUND_DBS = [FUNDDB, OUT / "ashare_csi800_fund.sqlite"]


class ScanError(RuntimeError):
    pass


# ---------------------------------------------------------------- [M] 两融（批量）
def batch_margin(asof: str | None = None, codes: list | None = None) -> dict:
    """一次性算全部两融标的的 chg20 拥挤分位。

    口径与 mn.diagnose 逐行对齐（pct = 1 − #(v>v20)/n_cs），只把 N 次单点查询
    压成 2 次全表查询。返回 {code: {...}}，不可判定的标的一律保留并写明原因
    （fail-closed：无数据 ≠ 不拥挤）。

    **不丢行**：若传入 `codes`（池内全集），则两融库中查不到的标的也会补一条
    N/A 并写明「非两融标的」。否则批量版会静默遗漏整类股票，与单只版覆盖率不一致
    （2026-09-04 自检抓到：农业池 95 只中 9 只非两融标的被漏）。
    """
    if not ALTDB.exists():
        raise ScanError(f"另类数据库缺失：{ALTDB}（拒绝静默跳过）")

    c = sqlite3.connect(str(ALTDB))
    try:
        q = ("SELECT DISTINCT trade_date FROM margin_daily WHERE rzye>0 "
             + ("AND trade_date<=?" if asof else "") + " ORDER BY trade_date")
        cal = [r[0] for r in c.execute(q, ([asof] if asof else []))]
        if not cal:
            raise ScanError("两融库为空（或 asof 早于全部数据）")

        # 全局最新两融日 t（用于横截面）
        t = cal[-1]
        if len(cal) < 21:
            raise ScanError(f"两融交易日仅 {len(cal)} 个，无法构造 t−20 横截面")
        t20 = cal[-21]                                    # 日历索引 −20
        idx = {d: i for i, d in enumerate(cal)}

        rows = c.execute(
            "SELECT code, trade_date, rzye FROM margin_daily "
            "WHERE trade_date IN (?,?) AND rzye>0", (t, t20)).fetchall()
        now = {a: b for a, d, b in rows if d == t}
        past = {a: b for a, d, b in rows if d == t20}
        cs = {a: math.log(b) - math.log(past[a])
              for a, b in now.items() if past.get(a, 0) > 0 and b > 0}
        if not cs:
            raise ScanError("横截面为空：t 与 t−20 无共同标的")

        n_cs = len(cs)
        vals = np.array(sorted(cs.values()))

        # 每只股票自己的余额日（可能停牌/调出 → 用它自己最近一条 ≤t）
        latest = dict(c.execute(
            "SELECT code, MAX(trade_date) FROM margin_daily "
            "WHERE trade_date<=? AND rzye>0 GROUP BY code", (t,)).fetchall())

        out: dict[str, dict] = {}
        for code in sorted(set(now) | set(latest)):
            td = latest.get(code)
            if td is None:
                continue
            # 该股自己日历上的 t−20 余额
            ti = idx[td]
            d20 = cal[ti - 20] if ti - 20 >= 0 else None
            b_t = c.execute("SELECT rzye FROM margin_daily WHERE code=? AND "
                            "trade_date=? AND rzye>0", (code, td)).fetchone()
            if not b_t:
                out[code] = dict(verdict="N/A", level="unknown",
                                 reason="无 t 日余额记录")
                continue
            if d20 is None:
                out[code] = dict(verdict="N/A", level="unknown",
                                 reason="该股两融历史不足 20 个交易日 → chg20 不可计算")
                continue
            b20 = c.execute("SELECT rzye FROM margin_daily WHERE code=? AND "
                            "trade_date=? AND rzye>0", (code, d20)).fetchone()
            if not b20:
                out[code] = dict(verdict="N/A", level="unknown",
                                 reason="近 20 个交易日存在余额缺口（新入/停牌/移出）")
                continue
            v20 = math.log(b_t[0]) - math.log(b20[0])
            pct = float((vals <= v20).mean())             # 与 diagnose 的 1−#(v>v20)/n 等价

            if pct >= mn.TOP_DECILE:
                v, lv = "CROWDED", "bad"
                reason = (f"chg20 拥挤分位 {pct:.0%}（≥{mn.TOP_DECILE:.0%} 顶十分位）")
            elif pct >= mn.HIGH_DECILE:
                v, lv = "ELEVATED", "warn"
                reason = (f"chg20 拥挤分位 {pct:.0%}（{mn.HIGH_DECILE:.0%}~"
                          f"{mn.TOP_DECILE:.0%}）")
            else:
                v, lv = "NORMAL", "ok"
                reason = f"chg20 拥挤分位 {pct:.0%}（常态）"
            stale = (date.fromisoformat(asof or date.today().isoformat())
                     - date.fromisoformat(td)).days
            out[code] = dict(verdict=v, level=lv, reason=reason,
                             asof_margin_date=td, stale_days=stale,
                             chg20_ln=v20, crowding_percentile=pct,
                             cross_section_n=n_cs)
    finally:
        c.close()

    n_margin = len(out)
    if codes is not None:
        for code in codes:
            if code not in out:
                out[code] = dict(
                    verdict="N/A", level="unknown",
                    reason="非两融标的或无两融记录（无数据≠不拥挤，fail-closed）")

    meta = dict(margin_date=t, margin_date_t20=t20, cross_section_n=n_cs,
                n_codes=len(out), n_with_margin=n_margin)
    return dict(labels=out, meta=meta)


# ---------------------------------------------------------------- [V] 波动率（批量）
def batch_vol(db: Path, codes: list, asof: str | None = None) -> dict:
    """一次性加载全池后复权面板，向量化算 rv20 历史分位 + 当前回撤。

    判据与 rv.state_vol 完全一致（同阈值、同分位语义、缺样本同 N/A 规则）。

    **不丢行**：被 QC 剔除、或面板中无有效序列的标的，一律补 N/A 条目并写明原因，
    与单只版覆盖率一致（2026-09-04 自检抓到 000750 因 QC 剔除而漏行）。
    """
    from ashare_hfq_access import load_hfq_panel, qc_bad_codes

    bad = qc_bad_codes(db)                       # 缺表 → 抛 QcMissing（fail-loud）
    panel = load_hfq_panel(db, codes=codes, apply_qc=True,
                           drop_first_n_days=DROP_FIRST_N)
    dates = list(panel.dates)
    close = np.asarray(panel.close, dtype=float)

    if asof:
        keep = [j for j, d in enumerate(dates) if d <= asof]
        close = close[:, keep]
        dates = [dates[j] for j in keep]

    out: dict[str, dict] = {}
    for i, code in enumerate(panel.codes):
        if code in bad:
            out[code] = dict(verdict="N/A", level="unknown",
                             reason="QC 判坏，已从面板剔除 → 拒绝输出波动率状态")
            continue
        x = close[i]
        m = np.isfinite(x) & (x > 0)
        x = x[m]
        if x.size < RV_MIN_OBS:
            out[code] = dict(verdict="N/A", level="unknown",
                             reason=f"有效样本 {int(x.size)} < {RV_MIN_OBS} → 分位不可靠")
            continue
        r = np.diff(np.log(x))
        if r.size < RV_WINDOW + 2:
            out[code] = dict(verdict="N/A", level="unknown", reason="收益序列过短")
            continue
        # 滚动 std（与 state_vol 同构：窗口 [j-RV_WINDOW, j)）
        rvh = np.array([r[j - RV_WINDOW:j].std(ddof=1)
                        for j in range(RV_WINDOW, r.size + 1)])
        rv_now = float(rvh[-1] * math.sqrt(252.0))
        pct = float((rvh <= rvh[-1]).mean())
        peak = np.maximum.accumulate(x)
        dd = float(x[-1] / peak[-1] - 1.0)
        rv_full = float(r.std(ddof=1) * math.sqrt(252.0))

        if pct >= 0.90:
            v, desc = "HIGH", "历史高位（≥90 分位）"
        elif pct >= 0.75:
            v, desc = "ELEVATED", "偏高（75~90 分位）"
        else:
            v, desc = "NORMAL", "常态（<75 分位）"
        out[code] = dict(verdict=v, level=v, desc=desc, asof=dates[-1],
                         n_obs=int(x.size), rv20_annual=rv_now,
                         rv_full_annual=rv_full, vol_percentile=pct,
                         drawdown_now=dd, deep_drawdown=bool(dd <= DEEP_DD),
                         reason=(f"近{RV_WINDOW}日年化波动 {rv_now:.1%}，自身历史 "
                                 f"{pct:.0%} 分位（{desc}）；当前回撤 {dd:.1%}"))

    for code in codes:                            # 不丢行：面板外的补 N/A 并写明原因
        if code not in out:
            out[code] = dict(
                verdict="N/A", level="unknown",
                reason=("被 QC 判坏已从面板剔除 → 拒绝输出波动率状态"
                        if code in bad else
                        "面板中无该股有效序列（数据缺失或全被剔除）"))
    return out


# ---------------------------------------------------------------- [A] 农业净利同比（批量）
def batch_agri(codes: list) -> dict:
    """池内分位：AVOID ≥80 分位 / COLLAPSE ≤−100%。仅对农业池成员适用。"""
    try:
        members = set(rv.pool_codes("ashare_agri"))
    except Exception as e:
        raise ScanError(f"农业池名单不可读：{e}（拒绝视为不适用）")
    if not members:
        raise ScanError("农业池名单为空 → 该标签无法判定（fail-closed）")

    target = [c for c in codes if c in members]
    out = {c: dict(applicable=False, verdict="N/A", level="unknown",
                   reason="非农业池成员 → 不适用（neglist 出板块即失效）")
           for c in codes}
    if not target:
        return out

    for db in FUND_DBS:
        if not db.exists():
            continue
        c = sqlite3.connect(str(db))
        try:
            tabs = {t for t, in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if "fund_reports" not in tabs:
                continue
            cols = {d[1] for d in c.execute("PRAGMA table_info(fund_reports)")}
            if "np_yoy" not in cols:
                continue
            rep = c.execute("SELECT MAX(report_date) FROM fund_reports "
                            "WHERE np_yoy IS NOT NULL").fetchone()[0]
            if not rep:
                continue
            q = ("SELECT code, np_yoy FROM fund_reports WHERE report_date=? "
                 "AND np_yoy IS NOT NULL")
            rows = [(a, b) for a, b in c.execute(q, (rep,)) if a in members]
            if not rows:
                continue
            arr = np.array([b for _, b in rows], dtype=float)
            for code, val in rows:
                pct = float((arr <= val).mean())
                if pct >= NP_YOY_AVOID_PCT:
                    v, lv = "AVOID", "bad"
                    why = (f"净利同比 {val:.1f}%，池内 {pct:.0%} 分位 ≥"
                           f"{NP_YOY_AVOID_PCT:.0%} → 回避组")
                elif val <= NP_YOY_COLLAPSE * 100:
                    v, lv = "COLLAPSE", "warn"
                    why = (f"净利同比 {val:.1f}% ≤{NP_YOY_COLLAPSE*100:.0f}% → 大幅转负"
                           f"（描述性：neglist 未检验低侧，不代表可买也不代表该卖）")
                else:
                    v, lv = "NORMAL", "ok"
                    why = f"净利同比 {val:.1f}%，池内 {pct:.0%} 分位"
                out[code] = dict(applicable=True, verdict=v, level=lv,
                                 reason=why, report_date=rep, np_yoy=val,
                                 pool_n=len(arr), percentile=pct)
            for code in target:
                if out.get(code, {}).get("verdict") == "N/A" and \
                        out[code].get("reason", "").startswith("非农业池"):
                    out[code] = dict(applicable=True, verdict="N/A",
                                     level="unknown",
                                     reason=f"池内成员但报告期 {rep} 无 np_yoy")
            return out
        finally:
            c.close()
    raise ScanError("未找到含 np_yoy 的基本面库 → 农业标签无法判定（fail-closed）")


# ---------------------------------------------------------------- 归类（与单只版同口径）
def classify(m: dict, v: dict, a: dict) -> tuple:
    """按单只版 ashare_risk_verdict.SECTION_LABELS 的 validated 标记归类。

    **已验证**（validated=True）：margin CROWDED、agri AVOID。
    **描述性**（validated=False）：margin ELEVATED、vol HIGH/ELEVATED、
    深度回撤、agri COLLAPSE。

    ⚠ 2026-09-04 曾在此把 margin ELEVATED 误归为已验证（121/780 只被错标）。
    单只版对 ELEVATED 显式 validated=False 且附
    `MARGIN_ELEVATED_CAVEAT`（75~90 分位区未单独检验，属边界外推）。
    **改动本函数前必须逐档对照 SECTION_LABELS。**
    """
    validated, descriptive, vkeys = [], [], []
    if m.get("verdict") == "CROWDED":
        validated.append("两融拥挤")
        vkeys.append("margin")
    elif m.get("verdict") == "ELEVATED":
        descriptive.append("两融拥挤度偏高(75~90分位)")
    if a.get("verdict") == "AVOID":
        validated.append("农业池净利同比回避组")
        vkeys.append("agri_np_yoy")
    elif a.get("verdict") == "COLLAPSE":
        descriptive.append("净利大幅转负")
    if v.get("verdict") in ("HIGH", "ELEVATED"):
        descriptive.append(f"波动率{v.get('desc')}")
    if v.get("deep_drawdown"):
        descriptive.append(f"深度回撤({v['drawdown_now']:.1%})")
    return validated, descriptive, vkeys


# ---------------------------------------------------------------- 一致性自检
def selfcheck(codes: list, n: int, db: Path, asof: str | None,
              bm: dict, vlab: dict, alab: dict) -> tuple:
    """批量结果 vs 单只版结果 逐只对照。不等价 = 批量化出错，必须报出来。

    比三层，缺一不可（第一版只比了前两层，漏掉了 ELEVATED 归类错误）：
      1. 单段 verdict 与分位数值
      2. **已验证标签集合**（margin/agri 的 validated 归类）
      单只侧一律走 `rv.*` 独立调用，批量侧复用主流程结果（避免 N 次重载面板）。
    """
    import random
    rng = random.Random(20260904)
    sample = rng.sample(codes, min(n, len(codes)))

    diffs = []
    for code in sample:
        try:
            s = rv.label_margin(code, asof=asof)
        except Exception as e:
            diffs.append((code, "margin", "EXC", str(e)))
            continue
        b = bm.get(code, {})
        if s.get("verdict") != b.get("verdict"):
            diffs.append((code, "margin", s.get("verdict"), b.get("verdict")))
            continue
        p1, p2 = s.get("crowding_percentile"), b.get("crowding_percentile")
        if p1 is not None and p2 is not None and abs(p1 - p2) > 1e-9:
            diffs.append((code, "margin_pct", f"{p1:.6f}", f"{p2:.6f}"))

        try:
            s2 = rv.state_vol(db, code, asof=asof)
            b2 = vlab.get(code, {})
            if s2.get("verdict") != b2.get("verdict"):
                diffs.append((code, "vol", s2.get("verdict"), b2.get("verdict")))
                continue
            q1, q2 = s2.get("vol_percentile"), b2.get("vol_percentile")
            if q1 is not None and q2 is not None and abs(q1 - q2) > 1e-9:
                diffs.append((code, "vol_pct", f"{q1:.6f}", f"{q2:.6f}"))
        except Exception as e:
            diffs.append((code, "vol", "EXC", str(e)))

        # ── 第 2 层：已验证标签集合（归类口径；只比 margin/agri，排除批量版没有的 quality）
        try:
            single = rv.verdict(code, asof=asof)
            sv = {h["key"] for h in single.get("hits", [])
                  if h.get("validated") and h["key"] in ("margin", "agri_np_yoy")}
            bv = set(classify(bm.get(code, {}), vlab.get(code, {}),
                              alab.get(code, {}))[2])
            if sv != bv:
                diffs.append((code, "validated_set",
                              ",".join(sorted(sv)) or "-",
                              ",".join(sorted(bv)) or "-"))
        except Exception as e:
            diffs.append((code, "validated_set", "EXC", str(e)))
    return sample, diffs


# ---------------------------------------------------------------- 装配
def scan(pool: str, asof: str | None = None, do_check: int = 0) -> dict:
    db = OUT / POOL_DBS[pool]
    if not db.exists():
        raise ScanError(f"行情库缺失：{db}")

    if pool == "margin":
        raise ScanError("pool=margin 需配合行情库做波动率段，请使用 csi800/wide/agri/semi")

    # 代码全集
    c = sqlite3.connect(str(db))
    codes = [r[0] for r in c.execute(
        "SELECT DISTINCT code FROM daily_quotes_hfq ORDER BY code")]
    c.close()
    if not codes:
        raise ScanError(f"{db} 中无任何代码")

    # ★ 必须传 codes：否则 batch_margin 只覆盖两融标的，池内非两融标的会丢行
    #   （margin 字段变 None 而不是 N/A，与单只版覆盖率不一致）。
    #   2026-09-04 自检抓到（exit=3，12 只抽检 11 只 margin 不等价）：
    #   「不丢行」最初只加在自检路径，生产路径漏了 —— 修复必须覆盖全部调用点。
    mres = batch_margin(asof, codes)
    mlab, mmeta = mres["labels"], mres["meta"]
    vlab = batch_vol(db, codes, asof=asof)
    alab = batch_agri(codes)

    rows = []
    for code in codes:
        m = mlab.get(code, dict(verdict="N/A", level="unknown",
                                reason="两融库无该股记录 → 非两融标的（无数据≠不拥挤）"))
        v = vlab.get(code, dict(verdict="N/A", level="unknown", reason="面板无该股"))
        a = alab.get(code, dict(applicable=False, verdict="N/A", level="unknown",
                                reason="非农业池成员 → 不适用"))
        validated, descriptive, _ = classify(m, v, a)
        rows.append(dict(code=code,
                         margin=m["verdict"], margin_pct=m.get("crowding_percentile"),
                         margin_stale=m.get("stale_days"),
                         margin_date=m.get("asof_margin_date"),
                         vol=v["verdict"], vol_pct=v.get("vol_percentile"),
                         dd=v.get("drawdown_now"),
                         agri=a["verdict"], np_yoy=a.get("np_yoy"),
                         n_validated=len(validated), n_descriptive=len(descriptive),
                         validated=";".join(validated),
                         descriptive=";".join(descriptive)))

    checks = None
    if do_check:
        sample, diffs = selfcheck(codes, do_check, db, asof, mlab, vlab, alab)
        checks = dict(n_checked=len(sample), n_diff=len(diffs),
                      diffs=[dict(code=a, field=b, single=str(cc), batch=str(d))
                             for a, b, cc, d in diffs])

    return dict(pool=pool, price_db=db.name, asof=asof or mmeta["margin_date"],
                generated_at=datetime.now().isoformat(timespec="seconds"),
                n_codes=len(codes), margin_meta=mmeta, rows=rows,
                selfcheck=checks)


# ---------------------------------------------------------------- 渲染
def render(res: dict) -> str:
    L = []
    rows = res["rows"]
    L.append(f"# 全池风险扫描 · {res['pool']}")
    L.append("")
    L.append(f"- 行情库：`{res['price_db']}`　覆盖 **{res['n_codes']}** 只")
    L.append(f"- 两融基准日：{res['margin_meta']['margin_date']}"
             f"（横截面 n={res['margin_meta']['cross_section_n']}，"
             f"对照日 {res['margin_meta']['margin_date_t20']}）")
    L.append(f"- 生成时间：{res['generated_at']}")
    L.append("")
    L.append("> **本名单与任何个人账户无关**。产物是可复用的判据与标签，"
             "不是针对特定持仓的建议。")
    L.append("")

    sc = res.get("selfcheck")
    if sc:
        L.append("## 一致性自检（批量 vs 单只）")
        L.append("")
        L.append(f"- 抽检 {sc['n_checked']} 只，差异 **{sc['n_diff']}** 项 "
                 + ("✅ 等价" if sc["n_diff"] == 0 else "❌ **不等价，批量结果不可信**"))
        if sc["diffs"]:
            L.append("")
            L.append("| 代码 | 字段 | 单只版 | 批量版 |")
            L.append("|---|---|---|---|")
            for d in sc["diffs"][:20]:
                L.append(f"| {d['code']} | {d['field']} | {d['single']} | {d['batch']} |")
        L.append("")

    L.append("## 汇总")
    L.append("")
    n_hit_v = sum(1 for r in rows if r["n_validated"] > 0)
    L.append("| 指标 | 数量 | 占比 |")
    L.append("|---|---|---|")
    L.append(f"| 命中**已验证**标签 | {n_hit_v} | {n_hit_v/max(1,len(rows)):.1%} |")
    for key, name in (("margin", "两融 CROWDED"), ("agri", "农业 AVOID")):
        if key == "margin":
            n = sum(1 for r in rows if r["margin"] == "CROWDED")
        else:
            n = sum(1 for r in rows if r["agri"] == "AVOID")
        L.append(f"| ├ {name} | {n} | {n/max(1,len(rows)):.1%} |")
    n_na = sum(1 for r in rows if r["margin"] == "N/A")
    L.append(f"| 两融不可判定（非标的/缺口） | {n_na} | {n_na/max(1,len(rows)):.1%} |")
    L.append("")

    L.append("## 风险名单：已验证标签")
    L.append("")
    hit = [r for r in rows if r["n_validated"] > 0]
    if hit:
        hit.sort(key=lambda r: (-(r["margin_pct"] or 0), r["code"]))
        L.append("| 代码 | 命中标签 | 两融拥挤分位 | 波动率 | 波动分位 | 当前回撤 |")
        L.append("|---|---|---|---|---|---|")
        for r in hit:
            mp = f"{r['margin_pct']:.0%}" if r["margin_pct"] is not None else "—"
            vp = f"{r['vol_pct']:.0%}" if r["vol_pct"] is not None else "—"
            dd = f"{r['dd']:.1%}" if r["dd"] is not None else "—"
            L.append(f"| **{r['code']}** | {r['validated']} | {mp} | {r['vol']} | "
                     f"{vp} | {dd} |")
    else:
        L.append("未命中（异常：两融顶十分位应恒有约 10% 命中，请核查横截面）")
    L.append("")
    L.append("### 边界（必须随名单一起读）")
    L.append("")
    L.append("- **两融拥挤** = risk_signal_not_alpha，不是 alpha："
             "CSI800 两融池 2014-2026，顶十分位群体 CAGR 6.0% vs 全池 13.5%、"
             "最大回撤 58.7% vs 39.5%。只说明该群体**脆弱**，不代表必然下跌。")
    L.append("- **两融拥挤度偏高（75~90 分位）属描述性、边界外推**，不进已验证计数："
             "回测口径是顶十分位（≥90 分位，D10），75~90 区间未单独检验，"
             "不得与 CROWDED 同等对待。")
    L.append("- **农业回避组** 仅在农业池内成立（IC −0.111 / t(HAC) −3.33）；"
             "CSI800 换池检验失败（月度口径符号翻转），**出板块即失效**。")
    L.append("- **未命中 ≠ 安全**：已验证清单只有两条，覆盖面极窄。")
    L.append("- 波动率 / 回撤 / 净利转负均为**描述性观察**，不进已验证计数："
             "波动率择时已判死（+0.20~1.52%/年，|效应|/MDE 0.30~1.05）。")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description="批量风险扫描（不依赖任何个人账户）")
    ap.add_argument("--pool", required=True, choices=sorted(POOL_DBS))
    ap.add_argument("--asof", default=None, help="YYYY-MM-DD 复现历史时点")
    ap.add_argument("--selfcheck", type=int, default=0, metavar="N",
                    help="抽检 N 只，比对单只版结果（建议 30）")
    ap.add_argument("--outdir", default=None)
    ap.add_argument("--json-only", action="store_true")
    a = ap.parse_args()

    try:
        res = scan(a.pool, a.asof, a.selfcheck)
    except Exception as e:
        print(f"[FATAL] {type(e).__name__}: {e}", file=sys.stderr)
        return 2

    outdir = Path(a.outdir) if a.outdir else (OUT / date.today().isoformat())
    outdir.mkdir(parents=True, exist_ok=True)
    stem = f"risk_scan_{a.pool}_{Path(res['price_db']).stem}"

    jp = outdir / f"{stem}.json"
    jp.write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    cp = outdir / f"{stem}.csv"
    with cp.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(res["rows"][0].keys()))
        w.writeheader()
        w.writerows(res["rows"])
    mp = outdir / f"{stem}.md"
    mp.write_text(render(res), encoding="utf-8")

    n_hit = sum(1 for r in res["rows"] if r["n_validated"] > 0)
    print(f"[OK] pool={a.pool} n={res['n_codes']} 命中已验证标签={n_hit}")
    print(f"     json: {jp}")
    print(f"     csv : {cp}")
    print(f"     md  : {mp}")
    if res.get("selfcheck"):
        sc = res["selfcheck"]
        print(f"     自检: 抽检 {sc['n_checked']} 只，差异 {sc['n_diff']} 项")
        if sc["n_diff"]:
            print("     ❌ 批量与单只不等价，结果不可信")
            return 3
    if not a.json_only:
        print()
        print(render(res))
    return 0


if __name__ == "__main__":
    sys.exit(main())
