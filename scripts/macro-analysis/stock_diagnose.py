#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
单只股票诊断（Route S）· 2026-09-03 建立

回答"给我一只股票，量化 Agent 能提供什么"。
沿用宏观/A股 SOP 纪律：**描述性诊断 ≠ 策略验证**，N=1 不得声称 alpha。

产出：
  outputs/ashare_single_hfq_xq.sqlite  -> fundamental 表（东财业绩报表，含公告日）
  outputs/<date>/<code>_诊断.json
  outputs/<date>/<code>_诊断报告.md
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

import numpy as np

_HERE = Path(__file__).resolve().parent
# 与项目其余脚本保持一致：工作区根 = _HERE.parents[3]。
# 历史 bug（第廿七类 b）：曾用「就近查找含 outputs/ 的父目录」的启发式，
# 命中 my-deepseek-harness/ 下的残留 outputs 目录 ⇒ 策略注册表路径拼错
# ⇒ coverage_preflight() 静默降级为 N/A ⇒ 判决书 [0] 段 tier 门禁全程失效。
# 启发式只会引入不确定性，一律用固定的 parents[3]。
ROOT = _HERE.parents[3]

PROXY = "http://127.0.0.1:10809"
UA = {"User-Agent": "Mozilla/5.0"}
OUTDIR = ROOT / "outputs" / datetime.now().strftime("%Y-%m-%d")
OUTDIR.mkdir(parents=True, exist_ok=True)


def _opener():
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": PROXY, "https": PROXY}))


def em_report(code: str, size: int = 60) -> list[dict]:
    """东财业绩报表：营收/净利/同比/ROE/毛利率 + 公告日期（vintage 关键字段）"""
    params = {"reportName": "RPT_LICO_FN_CPD", "columns": "ALL",
              "filter": f'(SECURITY_CODE="{code}")', "pageNumber": "1",
              "pageSize": str(size), "sortColumns": "REPORTDATE", "sortTypes": "-1",
              "source": "WEB", "client": "WEB"}
    u = "https://datacenter-web.eastmoney.com/api/data/v1/get?" + urllib.parse.urlencode(params)
    d = json.loads(_opener().open(urllib.request.Request(u, headers=UA),
                                  timeout=45).read().decode("utf-8", "replace"))
    return ((d.get("result") or {}).get("data") or [])


def tx(symbol: str, a: str, b: str, fq: str) -> dict[str, float]:
    """腾讯日线（分段，规避 641 根硬上限）。fq in {'', 'hfq'}"""
    out: dict[str, float] = {}
    segs = _segments(a, b)
    for s, e in segs:
        url = (f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
               f"?param={symbol},day,{s},{e},640,{fq}")
        d = json.loads(_opener().open(urllib.request.Request(url, headers=UA),
                                      timeout=45).read().decode("utf-8", "replace"))
        node = (d.get("data") or {}).get(symbol) or {}
        key = "hfqday" if fq == "hfq" else "day"
        for r in (node.get(key) or []):
            try:
                out[r[0]] = float(r[2])   # close
            except (ValueError, IndexError):
                continue
    return out


def _segments(a: str, b: str, days: int = 548):
    """腾讯单请求 641 根硬上限 + 动态分段（SOP 坑②：开放末段窗口会增长）"""
    from datetime import date, timedelta
    y0, m0, d0 = (int(x) for x in a.split("-"))
    y1, m1, d1 = (int(x) for x in b.split("-"))
    cur, end = date(y0, m0, d0), date(y1, m1, d1)
    segs = []
    while cur < end:
        nxt = min(cur + timedelta(days=days), end)
        segs.append((cur.isoformat(), nxt.isoformat()))
        cur = nxt + timedelta(days=1)
    return segs


# ---------------------------------------------------------------- 画像指标

def perf_stats(px: dict[str, float]) -> dict:
    ds = sorted(px)
    v = np.array([px[d] for d in ds], float)
    r = v[1:] / v[:-1] - 1.0
    n = len(r)
    years = n / 244.0
    cum = v[-1] / v[0] - 1.0
    cagr = (v[-1] / v[0]) ** (1 / years) - 1.0 if years > 0 and v[0] > 0 else float("nan")
    vol = float(r.std(ddof=1) * np.sqrt(244))
    sharpe = (cagr - 0.02) / vol if vol > 0 else float("nan")
    peak = np.maximum.accumulate(v)
    dd = v / peak - 1.0
    mdd = float(dd.min())
    # 最大回撤区间
    i_end = int(np.argmin(dd))
    i_start = int(np.argmax(v[:i_end + 1])) if i_end > 0 else 0
    # 逐年
    by_year = {}
    for i in range(1, len(ds)):
        y = ds[i][:4]
        by_year.setdefault(y, []).append(v[i] / v[i - 1] - 1.0)
    yearly = {y: float(np.prod([1 + x for x in rs]) - 1) for y, rs in by_year.items()}
    # 涨跌分布
    return dict(
        n_days=len(ds), start=ds[0], end=ds[-1], years=round(years, 2),
        cum_return=float(cum), cagr=float(cagr), ann_vol=vol,
        sharpe=float(sharpe), max_drawdown=mdd,
        dd_start=ds[i_start], dd_end=ds[i_end],
        yearly=yearly,
        up_days=float((r > 0).mean()),
        limit_up=int((r > 0.1005).sum()), limit_down=int((r < -0.1005).sum()),
        ret_skew=float(_skew(r)), ret_kurt=float(_kurt(r)),
        var_95=float(np.percentile(r, 5)),
    )


def _skew(x):
    m = x.mean(); s = x.std(ddof=1)
    return ((x - m) ** 3).mean() / s ** 3 if s > 0 else float("nan")


def _kurt(x):
    m = x.mean(); s = x.std(ddof=1)
    return ((x - m) ** 4).mean() / s ** 4 - 3 if s > 0 else float("nan")


def beta_alpha(stock: dict[str, float], bench: dict[str, float]) -> dict:
    ds = sorted(set(stock) & set(bench))
    if len(ds) < 60:
        return {}
    rs = np.array([stock[ds[i]] / stock[ds[i - 1]] - 1 for i in range(1, len(ds))])
    rb = np.array([bench[ds[i]] / bench[ds[i - 1]] - 1 for i in range(1, len(ds))])
    X = np.column_stack([np.ones(len(rb)), rb])
    b = np.linalg.lstsq(X, rs, rcond=None)[0]
    resid = rs - X @ b
    n = len(rb)
    # Newey-West(L=3) 对 alpha 的 t 检验
    # 坑⑬（2026-09-03 湘电股份验证时 agent 发现、人工核实到根因）：
    #   原代码 u = X[:, 1:] * resid → 丢掉常数项列，S/G 退化为 1×1，无法与 2×2 的
    #   (X'X)^-1 相乘；作者遂用 `S.sum() * 0 + HC` 把 S 抹零掩盖维度崩溃，
    #   导致字段 t_alpha_nw 名不副实（实际只是 HC 异方差稳健 t）。
    #   修复：用完整设计矩阵（含常数项），并同时输出 NW 与 HC 两套，标签各自如实。
    u = X * resid[:, None]
    S = (u.T @ u)
    L = 3
    for l in range(1, L + 1):
        w = 1 - l / (L + 1)
        G = u[l:].T @ u[:-l]
        S += w * (G + G.T)
    XtX_inv = np.linalg.inv(X.T @ X)
    k = X.shape[1]
    V_hc = XtX_inv @ (X.T @ np.diag(resid ** 2) @ X) @ XtX_inv
    V_nw = XtX_inv @ S @ XtX_inv * (n / (n - k))  # 小样本自由度修正

    def _t(V):
        # NW 的 S 不保证正定，对角可能为负 → 截零，避免 sqrt 产生 nan
        se = np.sqrt(np.clip(np.diag(V), 0.0, None))
        return (float(b[0] / se[0]) if se[0] > 0 else float("nan")), float(se[0])

    t_nw, se_nw = _t(V_nw)
    t_hc, se_hc = _t(V_hc)
    n_eff = n / 244.0 * 2  # 独立观测近似：每年 ~2 个独立半年窗口
    return dict(beta=float(b[1]), alpha_daily=float(b[0]), alpha_ann=float(b[0] * 244),
                t_alpha_nw=t_nw, se_alpha_nw=se_nw,
                t_alpha_hc=t_hc, se_alpha_hc=se_hc,
                r2=float(np.corrcoef(rs, rb)[0, 1] ** 2),
                n_obs=n, n_eff_indep=round(n_eff, 1))


# ---------------------------------------------------------------- 数据质量

def hfq_quality(code: str, sym: str, a: str, b: str) -> dict:
    """SOP 坑⑨：个股级 hfq 数值错误 —— 未复权日收益必须落在涨跌停限内，
    hfq 若系统性超限则是数据源缺陷（不是市场事实）。"""
    raw = tx(sym, a, b, "")
    hfq = tx(sym, a, b, "hfq")
    ds = sorted(set(raw) & set(hfq))
    if len(ds) < 100:
        return {"ok": False, "note": "样本不足"}
    rr = np.array([raw[ds[i]] / raw[ds[i - 1]] - 1 for i in range(1, len(ds))])
    hh = np.array([hfq[ds[i]] / hfq[ds[i - 1]] - 1 for i in range(1, len(ds))])
    diff = hh - rr
    lim = 0.10 if not code.startswith(("300", "688")) else 0.20
    tol = lim + 0.005
    over = np.abs(hh) > tol
    n_raw_over = int((np.abs(rr) > tol).sum())
    n_hfq_over = int(over.sum())
    over_max = float(np.abs(hh[over]).max() * 100) if n_hfq_over else 0.0

    # 坑⑭（2026-09-03 用户质疑后实测）：原 PASS/FAIL 二元判决把「复权因子精度误差」
    # 与「数据自证错误」混为一谈，一律判 FAIL 并写「不宜用于日频回测」——这是过度声明。
    # 实测 600416：42 个超限日全部落在 10.51%~11.82%，无一日超 15%，属精度问题；
    # 20 日动量信号层污染仅 8.95%，EIV 衰减 lambda=0.85 → 效应被稀释、结果偏保守，
    # 正确结论是「回测不理想（易假阴性）」，不是「不能回测」。
    # 判据：超限幅度是否超过 15% —— 超过则是数据错误，未超过则只是精度误差。
    W = 20
    lam = float("nan")
    if len(rr) > W + 50:
        c_r = np.concatenate([[0.0], np.cumsum(rr)])
        c_h = np.concatenate([[0.0], np.cumsum(hh)])
        mr, mh = c_r[W:] - c_r[:-W], c_h[W:] - c_h[:-W]
        if mh.var() > 0:
            lam = float(mr.var() / mh.var())

    if n_hfq_over <= n_raw_over + 1:
        verdict = "PASS"
    elif over_max <= 15.0:
        verdict = "WARN_PRECISION"
    else:
        verdict = "FAIL"

    noise_ann = float(diff.std(ddof=1) * np.sqrt(244) * 100)
    if verdict == "PASS":
        remedy = ""
    elif verdict == "WARN_PRECISION":
        remedy = (f"复权因子精度误差（超限 {n_hfq_over} 天，最大 {over_max:.2f}%，均未超 15%）："
                  f"日频信号被稀释（EIV lambda={lam:.3f} < 1），回测效应偏保守（易假阴性），"
                  f"**并非不可回测**。注：正常股复权会剔除分红除权跳空，lambda 应 > 1"
                  f"（实测 600519/000001/601398/000651 为 1.16~2.16）；本股 lambda<1 "
                  f"说明噪声大到反转了该效应，实际稀释程度大于 {lam:.3f} 的字面值。"
                  f"日频回测建议改走未复权 + 自行按除权日修正（SOP §13.2 处置）；"
                  f"月/年频画像可容忍（噪声年化 {noise_ann:.1f}%）。")
    else:
        remedy = (f"存在超 15% 的超限日（最大 {over_max:.2f}%），属数据自证错误而非精度问题："
                  f"不可用，须换数据源或改走未复权 + 自行修正。")
    # 坑⑫（2026-09-03 湘电股份验证时发现）：字段名 ok 原为硬编码 True，真实语义
    # 是「算出了统计量」，与 verdict 直接矛盾；stdout 只打印 verdict 所以人看不出，
    # 但读 JSON 的 agent / 下游会把 ok=true 误读成「数据质量通过」。
    # 现拆为 computed（能否算出）与 ok（质量判定），两者语义各自归位。
    return dict(
        computed=True, ok=(verdict == "PASS"),
        usable=(verdict in ("PASS", "WARN_PRECISION")), n_days=len(ds),
        raw_over_limit=n_raw_over,
        hfq_over_limit=n_hfq_over,
        over_max_abs_pct=over_max,
        diff_mean_pct=float(diff.mean() * 100),
        diff_std_pct=float(diff.std(ddof=1) * 100),
        diff_max_abs_pct=float(np.abs(diff).max() * 100),
        pct_days_diff_gt_10bp=float((np.abs(diff) > 0.001).mean() * 100),
        noise_ann_pct=noise_ann,
        signal_dilution_lambda=lam,
        cum_raw=float(raw[ds[-1]] / raw[ds[0]] - 1),
        cum_hfq=float(hfq[ds[-1]] / hfq[ds[0]] - 1),
        verdict=verdict,
        remedy=remedy,
    )


# ------------------------------------------------- 参照系清单（原「覆盖前置闸门」）
# 2026-09-03 补建。教训：600416 演示时直接跑诊断流水线、未先判定「这只股有没有
# 已验证策略覆盖」，导致「策略没给结论」被误读为「策略对这只股没测好」。
# 真相：600416 不在任何已验证池（农业95 / 半导体79 / 中证800 780 全部 False）。
#
# ★ 2026-09-07 降级（用户质疑后定案）：本节**不再是门禁**。
#   ① 三池并集 909 只 / A 股约 5000 只 ⇒ **82% 个股都落 TIER_D** ⇒ 信息量≈0，
#      一个八成概率输出同一结论的判定不构成"判决"；
#   ② 单股诊断需要的是**数据**而非池资格（600416 未命中任何池，却有 5733 根 K 线
#      + 20 期财报 + 19 条龙虎榜）——「不在池内」既不该减少分析项，也不该成为结论；
#   ③ 实际伤害：agent 把「未命中」当成主结论写进摘要（0907 独立评审 52/100 头号扣分）。
#   ⇒ 门禁职责移交给 data_readiness_gate()（判数据齐备性）；
#     本节只回答「有哪些池级参照系可用」：命中=多一个对照，未命中=少一个（可补建）。
REGISTRY = ROOT / "outputs" / "ashare_strategy_registry.sqlite"

# verdict -> 该策略对单只股票能给出什么
_KIND_BY_VERDICT = {
    "risk_signal_not_alpha": "RISK_OVERLAY",      # 可回答「是否落入回避清单」
    "有效预判(可交付边界内)": "DELIVERABLE",
}
# 即使命中池，也必须讲清的限制（截面排序器对单一样本无定义）
_POOL_LIMITATION = (
    "已验证策略全部是**池级截面排序器**：输出的是「在某池内排第几分位」，"
    "单一样本无法构成排序 → 对单只股票在数学上无定义。命中池 ≠ 得到个股买卖信号。"
)


def coverage_preflight(code: str) -> dict:
    """Step 0：判定该股是否被任何已验证策略覆盖，返回三档 tier。

    TIER_C_NONE          不在任何已验证池 → 无池级参照系（**分析范围不变**；
                         真正的门禁是 data_readiness_gate()，不是本函数）
    TIER_B_RISK_OVERLAY  池内且被风控 overlay 覆盖 → 可回答「是否在回避清单」
    TIER_A_POOL_STRATEGY 池内且有可交付策略 → 仍只给分位排序（见 _POOL_LIMITATION）
    """
    now = datetime.now().isoformat(timespec="seconds")
    res = dict(code=code, checked_at=now, registry=str(REGISTRY),
               registry_ok=False, in_pools=[], pool_scan=[], usable=[],
               tier="TIER_C_NONE", limitation=_POOL_LIMITATION)
    if not REGISTRY.exists():
        res["note"] = "策略注册表缺失，无法判定覆盖（按 TIER_C_NONE 从严处理）"
        return res
    res["registry_ok"] = True
    conn = sqlite3.connect(str(REGISTRY))
    try:
        conn.row_factory = sqlite3.Row
        for r in conn.execute("SELECT pool_key,label,n_codes,codes FROM pool_registry"):
            codes = json.loads(r["codes"] or "[]")
            hit = code in codes
            res["pool_scan"].append(dict(pool=r["pool_key"], label=r["label"],
                                         n_codes=r["n_codes"], hit=hit))
            if hit:
                res["in_pools"].append(r["pool_key"])
        for r in conn.execute("SELECT strategy_key,label,pool_key,verdict "
                              "FROM strategy_registry"):
            if r["pool_key"] not in res["in_pools"]:
                continue
            res["usable"].append(dict(strategy_key=r["strategy_key"], label=r["label"],
                                      pool=r["pool_key"], verdict=r["verdict"],
                                      kind=_KIND_BY_VERDICT.get(r["verdict"],
                                                                "NOT_DELIVERABLE")))
        # ★ 风险标签独立成表（2026-09-04）：风控 overlay 不是"策略"，判死后
        #   的策略条目会从 strategy_registry 物理删除，但风险标签必须保留。
        #   若读不到这张表却静默跳过，本股会被误判成"该池策略全部证伪"——
        #   不报错、只是判定变差，属第十五类静默 bug，故缺表必须显式降级记账。
        has_rl = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='risk_label_registry'").fetchone() is not None
        if has_rl:
            for r in conn.execute(
                    "SELECT label_key,label,pool_key,verdict,label_kind,in_use "
                    "FROM risk_label_registry WHERE in_use=1"):
                if r["pool_key"] not in res["in_pools"]:
                    continue
                res["usable"].append(dict(
                    strategy_key=r["label_key"], label=r["label"],
                    pool=r["pool_key"], verdict=r["verdict"],
                    kind=("RISK_OVERLAY" if r["label_kind"] == "validated"
                          else "NOT_DELIVERABLE"),
                    source="risk_label_registry"))
        else:
            res["risk_label_table"] = "MISSING"
            res["note"] = ("⚠ risk_label_registry 表缺失 → 风控标签未纳入覆盖判定，"
                           "tier 可能偏低。请运行 ashare_registry_migrate.py。")
    finally:
        conn.close()
    # 去重：迁移过渡期内同一 key 可能两张表都有（risk_label 为准）。
    # 不去重会让判决书把同一个风控标签列两遍。
    seen, merged = {}, []
    for u in res["usable"]:
        k = u["strategy_key"]
        if k in seen:
            if u.get("source") == "risk_label_registry":
                merged[seen[k]] = u          # 新表覆盖旧表
            continue
        seen[k] = len(merged)
        merged.append(u)
    res["usable"] = merged
    kinds = {u["kind"] for u in res["usable"]}
    if not res["in_pools"]:
        # 不在任何已验证池 —— 与「在池但策略全部证伪」必须分开记账
        res["tier"] = "TIER_D_OUT_OF_POOL"
    elif "DELIVERABLE" in kinds:
        res["tier"] = "TIER_A_POOL_STRATEGY"
    elif "RISK_OVERLAY" in kinds:
        res["tier"] = "TIER_B_RISK_OVERLAY"
    elif res["usable"]:
        res["tier"] = "TIER_C_IN_POOL_ALL_FAILED"
    else:
        res["tier"] = "TIER_E_NO_STRATEGY_FOR_POOL"
    # ★ 2026-09-07 语义重构：tier 只描述「有哪些参照系可用」，
    #   不再作为「能不能分析 / 分析要打几折」的降级判决。见 data_readiness_gate()。
    res["verdict_text"] = {
        "TIER_A_POOL_STRATEGY": "池内有可交付策略 —— 但仅能提供分位排序，"
                                "不提供个股级买卖信号。",
        "TIER_B_RISK_OVERLAY": "被风控 overlay 覆盖 —— 只能回答「是否落入回避清单」，"
                               "不可作为买入依据。",
        "TIER_C_IN_POOL_ALL_FAILED": "在池内，但该池所有策略均已证伪 "
                                     "（无效/功效不足/真实但不可交付）→ 无策略输出。",
        "TIER_D_OUT_OF_POOL": "未命中任何已建参照系（农业/半导体/中证800）。"
                              "**这不构成降级**——仅表示暂无池级参照，"
                              "描述性体检与大盘基准对照照常全量执行；"
                              "如需池级对照，见 reference_frames 的补建建议。",
        "TIER_E_NO_STRATEGY_FOR_POOL": "在池内但该池尚未登记任何策略 → 无策略输出。",
    }[res["tier"]]
    # 参照系视角：命中=多一个对照维度；未命中=少一个，且给出补建路径
    res["reference_frames"] = [
        dict(pool=p["pool"], label=p["label"], n=p["n_codes"], available=p["hit"])
        for p in res["pool_scan"]
    ]
    res["missing_frames"] = [
        dict(pool=p["pool"], label=p["label"], n=p["n_codes"],
             remedy=f"用 ashare_pool_build.py 建 {p['pool']} 池，"
                    f"或改用申万行业池 / 中证全指作为对照")
        for p in res["pool_scan"] if not p["hit"]
    ]
    res["frame_note"] = (
        "三个池并集去重仅 909 只，A 股约 5000 只 ⇒ 约 82% 的个股都会落 "
        "TIER_D。该标签信息量≈0，**禁止**把它写成报告主结论——"
        "真正的门禁是 data_readiness_gate()。"
    )
    return res


# ------------------------------------------------ 数据齐备性门禁（2026-09-07 新建）
# 缘起（用户质疑）：「不在任何已验证池 → 仅描述性体检」这句没有用。
#   它把「池资格」当成诊断前置条件，但单股诊断需要的是**这只股的数据**，
#   而数据齐备性与池归属完全无关（600416 未命中任何池，却有 5733 根 K 线）。
#   更糟的是它误导 agent 把「未命中」当主结论写进摘要
#   （2026-09-07 独立评审 52/100 的头号扣分项）。
# 正解：门禁 = 数据齐备性。每项给 status + blocks（阻塞了什么）+ remedy（怎么补）。
#   READY/PARTIAL/BLOCKED 三档，且 PARTIAL 必须能说清「哪部分做不了」，
#   而不是笼统一句「仅描述性」。

def _scan_quotes(code: str) -> dict:
    """扫描全部雪球后复权库，取该股覆盖最好的一份。"""
    hits = []
    for p in sorted((ROOT / "outputs").glob("*_hfq_xq.sqlite")):
        try:
            con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
            row = con.execute(
                "select count(*),min(trade_date),max(trade_date) "
                "from daily_quotes_hfq where code=?", (code,)).fetchone()
            con.close()
        except sqlite3.Error:
            continue
        if row and row[0]:
            hits.append(dict(db=p.name, n=row[0], start=row[1], end=row[2]))
    hits.sort(key=lambda x: -x["n"])
    top = hits[0] if hits else None
    return dict(hits=hits, n=(top or {}).get("n", 0),
                start=(top or {}).get("start"), end=(top or {}).get("end"),
                db=(top or {}).get("db"))


def _scan_fundamentals(code: str) -> dict:
    """本地财报库覆盖；注意东财 API 可实时补，本地为空 ≠ 分析做不了。"""
    hits = []
    for p in sorted((ROOT / "outputs").glob("*.sqlite")):
        try:
            con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
            has = con.execute("select 1 from sqlite_master where type='table' "
                              "and name='fund_reports'").fetchone()
            if not has:
                con.close()
                continue
            row = con.execute(
                "select count(*),min(report_date),max(report_date) "
                "from fund_reports where code=?", (code,)).fetchone()
            con.close()
        except sqlite3.Error:
            continue
        if row and row[0]:
            hits.append(dict(db=p.name, n=row[0], start=row[1], end=row[2]))
    hits.sort(key=lambda x: -x["n"])
    top = hits[0] if hits else None
    return dict(hits=hits, n=(top or {}).get("n", 0),
                end=(top or {}).get("end"), db=(top or {}).get("db"))


def _scan_altdata(code: str) -> dict:
    p = ROOT / "outputs" / "ashare_altdata.sqlite"
    out = {k: 0 for k in ("margin_daily", "lhb_daily", "block_trade")}
    out["_db"] = p.name
    out["_ok"] = False
    if not p.exists():
        return out
    try:
        con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        for t in list(out):
            if t.startswith("_"):
                continue
            try:
                out[t] = con.execute(f"select count(*) from {t} where code=?",
                                     (code,)).fetchone()[0]
            except sqlite3.Error:
                out[t] = None
        con.close()
        out["_ok"] = True
    except sqlite3.Error:
        pass
    return out


def data_readiness_gate(code: str) -> dict:
    """真正的 Route S 门禁：判「数据够不够做分析」，不判「在不在池里」。

    返回 readiness ∈ {READY, PARTIAL, BLOCKED}，以及逐项
    status / blocks（阻塞了哪些分析）/ remedy（怎么补，可执行的命令）。
    """
    now = datetime.now().isoformat(timespec="seconds")
    q = _scan_quotes(code)
    f = _scan_fundamentals(code)
    a = _scan_altdata(code)
    items = []

    # —— 行情：一切的底座。无行情 = BLOCKED（连描述都做不了）
    if q["n"] >= 250:
        items.append(dict(item="后复权日线", status="OK",
                          detail=f"{q['n']} 根 {q['start']}~{q['end']} ({q['db']})",
                          blocks=[], remedy=""))
    elif q["n"] > 0:
        items.append(dict(item="后复权日线", status="PARTIAL",
                          detail=f"仅 {q['n']} 根（<1 年，{q['db']}）",
                          blocks=["年频收益/回撤", "跨年波动率", "牛熊分段对照"],
                          remedy="python ashare_hfq_access.py 补采该股全历史"))
    else:
        items.append(dict(item="后复权日线", status="EMPTY",
                          detail="所有 *_hfq_xq.sqlite 均无该股",
                          blocks=["价量画像", "收益/回撤/波动率", "基准对比",
                                  "技术形态描述"],
                          remedy="先采行情：python ashare_hfq_access.py --code "
                                 f"{code}（或加入某池后跑 ashare_daily_topup.py）"))

    # —— 财报：本地为空仍可走东财 API，故最高只到 PARTIAL
    if f["n"] > 0:
        items.append(dict(item="财报（本地库）", status="OK",
                          detail=f"{f['n']} 期，最新 {f['end']} ({f['db']})",
                          blocks=[],
                          remedy="注：本地库缺扣非字段，需扣非口径时另查东财 "
                                 "RPT_LICO_FN_CPD.DEDUCT_BASIC_EPS"))
    else:
        items.append(dict(item="财报（本地库）", status="EMPTY",
                          detail="本地无，但东财 API 可实时拉取",
                          blocks=["本地离线复算"],
                          remedy="东财 API 实时拉（em_report()），或跑 "
                                 "ashare_daily_topup.py 的财报幂等采集落库"))

    # —— 另类数据：缺失不阻塞主线，只缩小对照维度
    alt_detail = (f"两融 {a['margin_daily']} / 龙虎榜 {a['lhb_daily']} / "
                  f"大宗 {a['block_trade']}")
    if a["_ok"] and any(a[k] for k in ("margin_daily", "lhb_daily", "block_trade")):
        zero = [k for k in ("margin_daily", "lhb_daily", "block_trade") if not a[k]]
        # 部分子项为 0 必须显式提示，否则「0 条」会被读成「无风险」
        remedy = ("注意：" + "/".join(zero) + " 为 0 条 = 采集未覆盖，"
                  "**不等于**该股无此类事件/无风险，须在报告中显式标注"
                  if zero else "")
        items.append(dict(item="另类数据", status="OK", detail=alt_detail,
                          blocks=[], remedy=remedy))
    elif a["_ok"]:
        items.append(dict(item="另类数据", status="EMPTY",
                          detail=alt_detail + "（全 0）",
                          blocks=["两融拥挤度对照", "龙虎榜事件回溯",
                                  "大宗折溢价对照"],
                          remedy="确认该股是否被 altdata 采集覆盖；"
                                 "两融仅覆盖部分标的，0 条须显式标注为"
                                 "「未覆盖」而非「无风险」"))
    else:
        items.append(dict(item="另类数据", status="UNAVAILABLE",
                          detail="ashare_altdata.sqlite 不可用",
                          blocks=["两融/龙虎榜/大宗 全部对照"],
                          remedy="检查 outputs/ashare_altdata.sqlite 是否存在"))

    blocked = [b for it in items for b in it["blocks"]]
    if any(it["status"] == "EMPTY" and it["item"] == "后复权日线" for it in items):
        readiness = "BLOCKED"
    elif blocked:
        readiness = "PARTIAL"
    else:
        readiness = "READY"

    return dict(code=code, checked_at=now, readiness=readiness, items=items,
                blocked_analyses=blocked,
                summary=(f"{readiness}：{sum(1 for i in items if i['status']=='OK')}"
                         f"/{len(items)} 项数据齐备"
                         + (f"，受限分析 {len(blocked)} 项" if blocked else "")),
                note=("本门禁只回答「数据够不够」。「能不能给买卖建议」是恒定的"
                      "**否**（终态裁定，与池归属无关），不由本门禁决定。"))


# ---------------------------------------------------------------- 主流程

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--code", required=True, help="6 位代码，如 600416")
    ap.add_argument("--name", default="")
    ap.add_argument("--since", default="2016-01-01")
    ap.add_argument("--db", default=str(ROOT / "outputs" / "ashare_single_hfq_xq.sqlite"))
    ap.add_argument("--bench", default="sh000300")
    args = ap.parse_args()

    code = args.code
    if not args.name:
        args.name = code
    sym = ("sh" if code.startswith(("6", "9")) else
           "sz" if code.startswith(("0", "2", "3")) else
           "bj" if code.startswith(("4", "8")) else "sh") + code

    today = datetime.now().strftime("%Y-%m-%d")
    print(f"=== 单股诊断 {code} {args.name} ===")

    # 0a. 数据齐备性门禁（2026-09-07 起为**真正的**门禁）
    print("[0/5] 数据齐备性门禁 ...")
    gate = data_readiness_gate(code)
    print(f"      {gate['summary']}")
    for it in gate["items"]:
        flag = {"OK": "✓", "PARTIAL": "~", "EMPTY": "✗",
                "UNAVAILABLE": "✗"}.get(it["status"], "?")
        print(f"        {flag} {it['item']}: {it['status']}  {it['detail']}")
        if it["blocks"]:
            print(f"            受限 → {'/'.join(it['blocks'])}")
        if it["remedy"]:
            print(f"            补法 → {it['remedy']}")
    if gate["readiness"] == "BLOCKED":
        print("      ✗ 行情缺失，后续步骤无意义 —— 请先按上方补法采数据。")
    print(f"      注: {gate['note']}")

    # 0b. 参照系清单（不是降级判决：命中=多一个对照维度，未命中=少一个）
    print("[0'/5] 参照系清单 ...")
    cov = coverage_preflight(code)
    scan_str = "  ".join(
        f"{p['pool']}={'可用' if p['hit'] else '未命中'}({p['n_codes']})"
        for p in cov["pool_scan"])
    print(f"      已建池扫描: {scan_str if scan_str else '(池注册表为空)'}")
    print(f"      tier: {cov['tier']}")
    for u in cov["usable"]:
        print(f"        - [{u['kind']}] {u['strategy_key']}  verdict={u['verdict']}")
    if cov["tier"] == "TIER_D_OUT_OF_POOL":
        print(f"      ⚠ {cov['frame_note']}")
        print("        → 仅表示缺少池级对照；描述性体检与大盘基准照常全量执行。")

    # 1. 数据质量
    print("[1/5] 后复权数据质量校验 ...")
    q = hfq_quality(code, sym, args.since, today)
    print(f"      未复权超限 {q.get('raw_over_limit')} 天 / hfq 超限 {q.get('hfq_over_limit')} 天 "
          f"(最大超限幅度 {q.get('over_max_abs_pct', 0):.2f}%) -> {q.get('verdict')}")
    if q.get("computed"):   # 坑⑫：这里是「算出了统计量」而非「质量通过」，FAIL 时更要看偏差
        print(f"      日收益偏差 hfq-raw: 标准差 {q['diff_std_pct']:.4f}%  "
              f"最大 {q['diff_max_abs_pct']:.2f}%")
        print(f"      累计收益 raw={q['cum_raw']:+.2%} hfq={q['cum_hfq']:+.2%} "
              f"(差 {(q['cum_hfq']-q['cum_raw'])*100:+.2f}pp)")
        lam = q.get("signal_dilution_lambda")
        if lam is not None and lam == lam:   # 非 NaN
            # ★ 坑⑳（2026-09-03，688615 诊断时发现）：原写法固定输出
            #   「效应低估 {(1-lam)*100:.0f}%」，而 lam>1 时该值为**负**
            #   （实测 688615 lam=1.184 → 打印「效应低估 -18%」），语义荒谬。
            #   根因与坑⑭ 同族：lam>1（正常）与 lam<1（稀释）**语义相反**，
            #   却用同一句模板描述 ⇒ 必然有一档是错的。
            #   基线：干净股复权剔除分红除权跳空 ⇒ 信号方差变小 ⇒ lam 应 >1
            #   （实测 600519/000001/601398/000651 = 1.16~2.16）。
            if lam >= 1.0:
                lam_txt = (f"lambda={lam:.3f} ≥1 → 属正常区间"
                           f"（干净股基线 1.16~2.16；复权剔除除权跳空使信号方差变小）")
            else:
                lam_txt = (f"lambda={lam:.3f} <1 → **异常**：噪声大到反转了上述正常效应，"
                           f"20日动量被稀释约 {(1-lam)*100:.0f}%，回测易假阴性")
            print(f"      噪声年化 {q['noise_ann_pct']:.2f}%  20日动量信号 {lam_txt}")
    if q.get("remedy"):
        print(f"      处置: {q['remedy']}")

    # 2. 行情画像
    print("[2/5] 价量画像 ...")
    px = tx(sym, args.since, today, "hfq")
    if len(px) < 250:
        print("      样本不足，终止")
        return 1
    st = perf_stats(px)
    bench = tx(args.bench, args.since, today, "")
    ba = beta_alpha(px, bench) if bench else {}
    bst = perf_stats(bench) if bench else {}

    # 3. 基本面
    print("[3/5] 基本面（东财业绩报表）...")
    rows = em_report(code)
    fund = []
    for r in rows:
        fund.append(dict(
            report_date=(r.get("REPORTDATE") or "")[:10],
            notice_date=(r.get("NOTICE_DATE") or "")[:10],
            revenue=r.get("TOTAL_OPERATE_INCOME"), revenue_yoy=r.get("YSTZ"),
            netprofit=r.get("PARENT_NETPROFIT"), netprofit_yoy=r.get("SJLTZ"),
            eps=r.get("BASIC_EPS"), roe=r.get("WEIGHTAVG_ROE"),
            gross_margin=r.get("XSMLL"), bps=r.get("BPS"),
        ))
    fund = [f for f in fund if f["report_date"]]
    print(f"      {len(fund)} 期，最新 {fund[0]['report_date']}（公告日 {fund[0]['notice_date']}）")

    # 4. 存库
    db = Path(args.db)
    conn = sqlite3.connect(str(db))
    conn.execute("""CREATE TABLE IF NOT EXISTS fundamental (
        code TEXT NOT NULL, report_date TEXT NOT NULL, notice_date TEXT,
        revenue REAL, revenue_yoy REAL, netprofit REAL, netprofit_yoy REAL,
        eps REAL, roe REAL, gross_margin REAL, bps REAL,
        source TEXT, collected_at TEXT,
        PRIMARY KEY (code, report_date))""")
    now = datetime.now().isoformat(timespec="seconds")
    for f in fund:
        conn.execute("INSERT OR REPLACE INTO fundamental VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (code, f["report_date"], f["notice_date"], f["revenue"],
                      f["revenue_yoy"], f["netprofit"], f["netprofit_yoy"], f["eps"],
                      f["roe"], f["gross_margin"], f["bps"], "eastmoney-RPT_LICO_FN_CPD", now))
    conn.execute("""CREATE TABLE IF NOT EXISTS stock_diag (
        code TEXT, name TEXT, diag_date TEXT, payload TEXT,
        PRIMARY KEY (code, diag_date))""")
    conn.commit()
    conn.close()

    # 5. 落盘
    out = dict(code=code, name=args.name, generated_at=now, since=args.since,
               coverage=cov, data_quality=q, price_stats=st, bench_stats=bst,
               beta_alpha=ba, fundamental=fund[:20],
               discipline=("描述性诊断，非策略验证；不得计算 IC 或声称 alpha。"
                           "N=1 不构成截面——池级截面排序器对单只股票在数学上无定义，"
                           "因此**本就不存在「对这一只股做回测」这回事**，这与数据质量无关；"
                           "不得把「无策略输出」归因为数据不好。"
                           "数据质量判决只影响效应估计的精度（稀释或污染），不改变上述结论。"),
               data_caveat=q.get("remedy", ""))
    jpath = OUTDIR / f"{code}_诊断.json"
    jpath.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=_nan), encoding="utf-8")
    conn = sqlite3.connect(str(db))
    conn.execute("INSERT OR REPLACE INTO stock_diag VALUES (?,?,?,?)",
                 (code, args.name, today, json.dumps(out, ensure_ascii=False, default=_nan)))
    conn.commit(); conn.close()

    print(f"[5/5] 已写入 {jpath}")
    print(f"\n  年化收益 {st['cagr']:+.2%}  年化波动 {st['ann_vol']:.2%}  "
          f"夏普 {st['sharpe']:.2f}  最大回撤 {st['max_drawdown']:.2%}"
          f"（{st['dd_start']}~{st['dd_end']}）")
    if ba:
        print(f"  vs 沪深300: beta={ba['beta']:.2f}  年化 alpha={ba['alpha_ann']:+.2%}  "
              f"t(NW)={ba['t_alpha_nw']:.2f}  R²={ba['r2']:.2f}  "
              f"N_eff(独立观测)≈{ba['n_eff_indep']}")
    if fund:
        f0 = fund[0]
        print(f"  最新财报 {f0['report_date']}: 营收 {_yi(f0['revenue'])}亿 "
              f"({f0['revenue_yoy']:+.2f}%)  归母净利 {_yi(f0['netprofit'])}亿 "
              f"({f0['netprofit_yoy']:+.2f}%)  ROE {f0['roe']}%")
    return 0


def _yi(x):
    return f"{x/1e8:.2f}" if isinstance(x, (int, float)) else "n/a"


def _nan(x):
    if isinstance(x, float) and (np.isnan(x) or np.isinf(x)):
        return None
    raise TypeError(str(type(x)))


if __name__ == "__main__":
    sys.exit(main())
