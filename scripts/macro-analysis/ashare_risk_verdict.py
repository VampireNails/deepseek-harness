#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""个股风险判决书（Route V）—— 把已验证的风险判据拼成一份单股输出。

定位：投资决策辅助的「回避 / 警惕」半边。**不产出买卖建议**（workflow §0）。

为什么是这个形态（不是选股）：
  * 三关过滤器已连续三次裁决「统计显著 ≠ 经济可行」，找 alpha 的路线全部卡在关卡③。
  * 唯一存活形态恒为**风控标签**；且**风险对单只股票有定义**，天然绕开
    「池级截面排序器对单一样本无定义」的原理性限制。

拼接的判据（每条都有实测依据，见 ashare-research-SOP.md）：

  [0] 参照系清单     stock_diagnose.coverage_preflight   → tier A/B/C/D/E
                     （非门禁；仅说明哪些池级标签可查。数据齐备性见
                      stock_diagnose.data_readiness_gate，两者语义不同）
  [1] 数据质量判决   hfq_qc 表（采集器口径）             → PASS / FAIL / N/A
  [2] 波动率状态     自身历史分位 + 当前回撤              → **描述性，非择时**
  [3] 两融拥挤标签   ashare_margin_neglist.diagnose      → CROWDED/ELEVATED/NORMAL
  [4] 板块基本面标签 农业池内净利同比横截面分位           → AVOID / NORMAL

红线：
  1. 缺数据一律 **fail-loud**：N/A 必须写明原因，不得静默当成「正常」。
  2. 波动率状态**只描述**，不得引申为仓位建议 —— 方向 1 关卡③ 已判死
     （换仓位后 +0.20~1.52%/年，|效应|/MDE 0.30~1.05，功效不足，不可交付）。
  3. 不输出评分/加权总分 —— 没有经过验证的合成权重，只给清单式命中情况。

用法：
  python ashare_risk_verdict.py --code 600519
  python ashare_risk_verdict.py --code 600519 --asof 2026-09-02 --json-only
"""

import argparse
import json
import math
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import numpy as np

_HERE = Path(__file__).resolve().parent
# 2026-09-06 修（第廿七类）：原为 ROOT = _HERE（脚本目录），拼出
# macro-analysis/outputs/... 不存在 → 候选库全部跳过 → "在全部候选行情库中均无数据"。
# 与项目其余脚本保持一致：工作区根 = _HERE.parents[3]。
ROOT = _HERE.parents[3]
# 2026-09-06 移除：原本在此有一段「就近查找含 outputs/ 的父目录」的启发式循环，
# 它会命中 my-deepseek-harness/outputs/（内含 0 字节假库）而把 ROOT 设错。
# 项目内 20+ 脚本统一用 _HERE.parents[3]，启发式只会引入不确定性。
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

OUT = ROOT / "outputs"
REGISTRY = OUT / "ashare_strategy_registry.sqlite"
ALTDB = OUT / "ashare_altdata.sqlite"

# 行情库候选顺序：池库优先（覆盖确定、体积小），宽库最后（1.2GB）
PRICE_DBS = [
    "ashare_csi800_hfq_xq.sqlite",
    "ashare_agri_hfq_xq.sqlite",
    "ashare_semi_hfq_xq.sqlite",
    "ashare_single_hfq_xq.sqlite",
    "ashare_wide_hfq_xq.sqlite",
]
FUND_DBS = ["ashare_fundamental.sqlite", "ashare_csi800_fund.sqlite"]

RV_WINDOW = 20          # 波动率窗口（交易日）
RV_MIN_OBS = 260        # 计算历史分位所需最小有效样本
DROP_FIRST_N = 5        # 剔上市前 N 日（新股不设涨跌幅，SOP §13.4.6）
DEEP_DD = -0.20         # 深度回撤阈值（描述用）
NP_YOY_AVOID_PCT = 0.80  # 净利同比 ≥80 分位 = 回避组（对齐 neglist「最高 20%」）
NP_YOY_COLLAPSE = -1.0   # 净利同比 ≤−100% = 大幅转负（描述性，非 neglist 覆盖范围）

# ★ neglist 只检验了**高侧**（回避净利同比最高 20%）。低侧没有任何已验证结论，
#   若不拆档就会被静默读成「低 = 安全」。故单列 COLLAPSE 档，标为描述性。
NP_YOY_COLLAPSE_NOTE = (
    "⚠ 该 neglist 只检验了高侧（回避最高 20%）；净利大幅转负不在其检验范围内，"
    "**这一格既不代表可买、也不代表该卖**，仅为事实提示")

TIER_TEXT = {
    "TIER_A_POOL_STRATEGY": "池内有可交付策略 —— 但仅能提供分位排序，不提供个股级买卖信号。",
    "TIER_B_RISK_OVERLAY": "被风控 overlay 覆盖 —— 只能回答「是否落入回避清单」，不可作为买入依据。",
    "TIER_C_IN_POOL_ALL_FAILED": "在池内，但该池所有策略均已证伪 → 无策略输出。",
    # 2026-09-07 改写：原措辞「仅描述性体检」把**池资格**误当成诊断降级条件。
    # 判决书场景下，池归属的真实含义只有一个：风控标签是按池定义的，
    # 不在池 ⇒ 无标签可查。这既不是降级，也不等于安全。
    "TIER_D_OUT_OF_POOL": "未命中任何已建风控标签的适用池 ⇒ **无已验证风险标签可查**。"
                          "这不构成降级，且**不等于无风险**——仅说明现有两条标签"
                          "（农业净利同比 / 中证800 两融拥挤度）的适用面未覆盖该股；"
                          "标签可查范围仅 909/约5000 只，覆盖盲区是常态而非常态之外。",
    "TIER_E_NO_STRATEGY_FOR_POOL": "在池内但该池尚未登记任何策略 → 无策略输出。",
}


class VerdictError(RuntimeError):
    pass


def _pct(v) -> str:
    """浮点收益 → 百分比串。None 显式显示为 N/A，不许静默当 0。"""
    return "N/A" if v is None else f"{float(v):.2%}"


# ---------------------------------------------------------------- 库解析

def resolve_price_db(code: str, db: str | None = None) -> Path:
    if db:
        p = Path(db)
        return p if p.is_absolute() else (ROOT / db)
    for name in PRICE_DBS:
        p = OUT / name
        if not p.exists():
            continue
        c = sqlite3.connect(str(p))
        try:
            tabs = {t for t, in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if "hfq_qc" not in tabs or "daily_quotes_hfq" not in tabs:
                continue
            hit = c.execute("SELECT 1 FROM daily_quotes_hfq WHERE code=? LIMIT 1",
                            (code,)).fetchone()
        finally:
            c.close()
        if hit:
            return p
    raise VerdictError(
        f"代码 {code} 在全部候选行情库中均无后复权数据：{PRICE_DBS}。"
        f"请先用 ashare_badj_collect.py 采集，或用 --price-db 显式指定。")


def pool_codes(pool_key: str) -> list:
    """读池成员。注册表缺失时返回 []，由调用方 fail-loud。"""
    if not REGISTRY.exists():
        return []
    c = sqlite3.connect(str(REGISTRY))
    try:
        r = c.execute("SELECT codes FROM pool_registry WHERE pool_key=?",
                      (pool_key,)).fetchone()
    finally:
        c.close()
    return json.loads(r[0]) if r and r[0] else []


# ------------------------------------------------- [0] 参照系清单（原「覆盖闸门」，非门禁）

def gate_coverage(code: str) -> dict:
    try:
        import stock_diagnose as sd
    except Exception as e:                                  # pragma: no cover
        return dict(tier="N/A", text=f"参照系清单不可用（registry 读取失败）：{e}",
                    in_pools=[], usable=[])
    r = sd.coverage_preflight(code)
    if not r.get("registry_ok"):
        return dict(tier="N/A",
                    text=f"策略注册表不可用（{r.get('note', '')}）→ 覆盖判定从严，"
                         f"不得据此认为「该股无风险标签」",
                    in_pools=[], usable=[])
    return dict(tier=r["tier"], text=TIER_TEXT.get(r["tier"], r.get("verdict_text", "")),
                in_pools=r.get("in_pools", []), usable=r.get("usable", []))


# ---------------------------------------------------------------- [1] 数据质量

def gate_quality(db: Path, code: str) -> dict:
    c = sqlite3.connect(str(db))
    try:
        row = c.execute(
            "SELECT collected_at,n_bars,nonpositive,over_limit_days,"
            "ret_min,ret_max,limit_used,bad FROM hfq_qc WHERE code=?",
            (code,)).fetchone()
    finally:
        c.close()
    if row is None:
        return dict(verdict="N/A", level="unknown",
                    reason="hfq_qc 中无该股质检记录（未采集或未质检）→ 从严处理，"
                           "不得视为 PASS")
    (collected, n_bars, nonpos, over_days, rmin, rmax, lim_used, bad) = row
    rng = f"[{_pct(rmin)}, {_pct(rmax)}]"
    if bad:
        return dict(verdict="FAIL", level="bad",
                    reason=f"采集器 QC 判坏（超限 {over_days} 日，收益区间 {rng}，"
                           f"限 {lim_used}）→ 数据自证错误，不可用",
                    n_bars=n_bars, over_limit_days=over_days,
                    ret_min=rmin, ret_max=rmax)
    return dict(verdict="PASS", level="ok",
                reason=f"采集器 QC 通过（{n_bars} 根，超限 {over_days} 日，"
                       f"收益区间 {rng}）",
                n_bars=n_bars, over_limit_days=over_days,
                ret_min=rmin, ret_max=rmax, collected_at=collected)


# ---------------------------------------------------------------- [2] 波动率状态

def state_vol(db: Path, code: str, asof: str | None = None) -> dict:
    from ashare_hfq_access import load_hfq_panel, qc_bad_codes

    bad = qc_bad_codes(db)          # 无 hfq_qc 表 → 抛 QcMissing（fail-loud）
    if code in bad:
        return dict(verdict="N/A", level="unknown",
                    reason="该股被 QC 判坏，已从面板剔除 → 拒绝输出波动率状态")

    panel = load_hfq_panel(db, codes=[code], apply_qc=True,
                           drop_first_n_days=DROP_FIRST_N)
    if code not in panel.codes:
        return dict(verdict="N/A", level="unknown",
                    reason="面板中无该股有效序列（数据缺失或全被剔除）")

    i = panel.codes.index(code)
    dates = panel.dates
    x = panel.close[i]
    if asof:
        keep = [j for j, d in enumerate(dates) if d <= asof]
        if len(keep) < RV_MIN_OBS:
            return dict(verdict="N/A", level="unknown",
                        reason=f"截至 {asof} 有效样本 {len(keep)} < {RV_MIN_OBS}，"
                               f"历史分位不可靠")
        x = x[keep]
        dates = [dates[j] for j in keep]

    m = np.isfinite(x) & (x > 0)
    x = x[m]
    if x.size < RV_MIN_OBS:
        return dict(verdict="N/A", level="unknown",
                    reason=f"有效后复权样本 {x.size} < {RV_MIN_OBS}，历史分位不可靠")

    r = np.diff(np.log(x))
    if r.size < RV_WINDOW + 2:
        return dict(verdict="N/A", level="unknown", reason="收益序列过短")

    rvh = np.array([r[j - RV_WINDOW:j].std(ddof=1)
                    for j in range(RV_WINDOW, r.size + 1)])
    rv_now = float(rvh[-1] * math.sqrt(252.0))
    pct = float((rvh <= rvh[-1]).mean())
    peak = np.maximum.accumulate(x)
    dd = float(x[-1] / peak[-1] - 1.0)
    rv_full = float(r.std(ddof=1) * math.sqrt(252.0))

    # ★ 回撤归因（2026-09-06 对比优化新增）：光给「跌了多少」无法回答「发生了什么」。
    #   记录历史高点日期与持续时长，供 [6] 证据区把跌幅对齐到同期的公告/新闻时间线。
    #   注意：这是**描述性归因**（同期发生了什么），不是因果推断，也不构成择时信号。
    _peak_idx = int(np.argmax(x))
    peak_date = dates[_peak_idx] if 0 <= _peak_idx < len(dates) else ""
    dd_days = int(len(x) - 1 - _peak_idx)

    if pct >= 0.90:
        level, desc = "HIGH", "历史高位（≥90 分位）"
    elif pct >= 0.75:
        level, desc = "ELEVATED", "偏高（75~90 分位）"
    else:
        level, desc = "NORMAL", "常态（<75 分位）"

    return dict(verdict=level, level=level, desc=desc,
                asof=dates[-1], n_obs=int(x.size),
                rv20_annual=rv_now, rv_full_annual=rv_full,
                vol_percentile=pct, drawdown_now=dd,
                drawdown_peak_date=peak_date, drawdown_days=dd_days,
                deep_drawdown=bool(dd <= DEEP_DD),
                reason=(f"近 {RV_WINDOW} 日年化波动 {rv_now:.1%}，"
                        f"处于自身历史 {pct:.0%} 分位（{desc}）；"
                        f"当前距历史高点回撤 {dd:.1%}"
                        + (f"（高点 {peak_date}，已 {dd_days} 个交易日）" if peak_date else "")
                        + f"；全样本年化波动 {rv_full:.1%}"),
                caveat="描述性统计，**不是择时信号**：波动率预测换仓位后仅 +0.20~1.52%/年、"
                       "|效应|/MDE 0.30~1.05，方向 1 关卡③ 已判死，不得据此加减仓。")


# ---------------------------------------------------------------- [3] 两融拥挤

def pool_equal_weight(db: Path, peak_date: str, asof: str, tol: int = 7) -> dict:
    """同期**池内等权**收益基准 —— 回答「是个股问题还是市场问题」。

    为什么用「池内等权」而不是沪深 300 指数：
      ① 腾讯源已进 `DEPRECATED_SOURCES` 黑名单（第廿四类：单源自洽但跨源矛盾），
         指数数据若取自另一源会引入口径不一致；池内等权与个股**同源同库同后复权口径**。
      ② 「差分类判据」对幸存者偏差大部分免疫（SOP 纪律）——本函数只用于**相对**比较。

    性能：直写相关子查询实测 21s；改为「只扫 [date-tol, date] 两段 + 窗口函数取每条最新」
         后 **0.09s**（tol=3）。tol 用于兼容停牌（第廿八类：分母须容忍停牌）。
    """
    q = f"""
    WITH a AS (SELECT code, close, trade_date,
                 ROW_NUMBER() OVER (PARTITION BY code ORDER BY trade_date DESC) rn
               FROM daily_quotes_hfq
               WHERE trade_date BETWEEN date(?, '-{tol} day') AND ?),
         b AS (SELECT code, close, trade_date,
                 ROW_NUMBER() OVER (PARTITION BY code ORDER BY trade_date DESC) rn
               FROM daily_quotes_hfq
               WHERE trade_date BETWEEN date(?, '-{tol} day') AND ?)
    SELECT COUNT(*), AVG(b.close / NULLIF(a.close, 0) - 1)
    FROM a JOIN b ON a.code = b.code
    WHERE a.rn = 1 AND b.rn = 1 AND a.close > 0
    """
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        n, avg = con.execute(q, (peak_date, peak_date, asof, asof)).fetchone()
        con.close()
        if not n:
            return {"error": "池内无有效样本"}
        return {"n": int(n), "ret": float(avg), "window": [peak_date, asof]}
    except Exception as e:                      # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}


def label_margin(code: str, asof: str | None = None) -> dict:
    if not ALTDB.exists():
        return dict(verdict="N/A", level="unknown",
                    reason=f"另类数据库缺失：{ALTDB}。无数据 ≠ 不拥挤（fail-closed）")
    try:
        import ashare_margin_neglist as mn
    except Exception as e:                                  # pragma: no cover
        return dict(verdict="N/A", level="unknown", reason=f"两融模块不可用：{e}")
    d = mn.diagnose(code, db=str(ALTDB), asof=asof)
    v = d.get("verdict")
    level = {"CROWDED": "bad", "ELEVATED": "warn", "NORMAL": "ok"}.get(v, "unknown")
    out = dict(verdict=v, level=level, reason=d.get("reason", ""),
               asof=d.get("asof_margin_date"), stale_days=d.get("stale_days"),
               crowding_pct=d.get("crowding_percentile"),
               cross_section_n=d.get("cross_section_n"),
               disclaimer=d.get("disclaimer", ""))
    if v in ("CROWDED", "ELEVATED"):
        out["basis"] = ("CSI800 两融池 2014-2026：chg20 顶十分位群体 CAGR 6.0% vs 全池 "
                        "13.5%、最大回撤 58.7% vs 39.5%（risk_signal_not_alpha）")
    return out


# ---------------------------------------------------------------- [4] 农业池净利同比

def label_agri_np_yoy(code: str) -> dict:
    members = pool_codes("ashare_agri")
    if not members:
        return dict(applicable=False, verdict="N/A", level="unknown",
                    reason="农业池成员名单不可读（注册表缺失）→ 该标签无法判定，"
                           "不得视为不适用")
    if code not in members:
        return dict(applicable=False, verdict="N/A", level="unknown",
                    reason="非农业池成员 → 该标签不适用"
                           "（neglist 出板块即失效，见 SOP §13.5）")

    for name in FUND_DBS:
        p = OUT / name
        if not p.exists():
            continue
        c = sqlite3.connect(str(p))
        try:
            tabs = {t for t, in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            tab = "fund_reports" if "fund_reports" in tabs else None
            if not tab:
                continue
            cols = {d[1] for d in c.execute("PRAGMA table_info(fund_reports)")}
            fld = "np_yoy" if "np_yoy" in cols else None
            if not fld:
                continue
            row = c.execute(
                f"SELECT report_date,{fld} FROM {tab} WHERE code=? AND {fld} IS NOT NULL "
                f"ORDER BY report_date DESC LIMIT 1", (code,)).fetchone()
            if not row:
                continue
            rep, val = row[0], float(row[1])
            cs = [(a, float(b)) for a, b in c.execute(
                f"SELECT code,{fld} FROM {tab} WHERE report_date=? AND {fld} IS NOT NULL",
                (rep,))]
        finally:
            c.close()

        if not cs:
            continue
        # 横截面**限定在池内** —— neglist 是池内分位，不是全市场分位
        pool = [v for a, v in cs if a in set(members)]
        if len(pool) < 10:
            return dict(applicable=True, verdict="N/A", level="unknown",
                        reason=f"报告期 {rep} 池内可比样本仅 {len(pool)} 只 <10，"
                               f"分位不可靠")
        arr = np.array(pool)
        pct = float((arr <= val).mean())
        avoid = pct >= NP_YOY_AVOID_PCT
        collapse = (not avoid) and (val <= NP_YOY_COLLAPSE * 100)
        if avoid:
            v_, lv = "AVOID", "bad"
            tail = f" ≥{NP_YOY_AVOID_PCT:.0%} → **落入回避组**"
        elif collapse:
            v_, lv = "COLLAPSE", "warn"
            tail = f" ≤{NP_YOY_COLLAPSE:.0%} → 净利大幅转负（描述性）"
        else:
            v_, lv = "NORMAL", "ok"
            tail = f" <{NP_YOY_AVOID_PCT:.0%} → 常态"
        return dict(
            applicable=True, db=name, report_date=rep, np_yoy=val,
            pool_n=len(arr), percentile=pct,
            verdict=v_, level=lv,
            reason=(f"报告期 {rep} 净利同比 {val:.1f}%，池内分位 {pct:.0%}{tail}"),
            basis=(("农业池 2014-2026：IC −0.111 / t(HAC) −3.33 / MDE 0.0877，"
                    "仅池内成立；CSI800 换池检验失败（月度口径符号翻转）")
                   if v_ != "COLLAPSE" else NP_YOY_COLLAPSE_NOTE),
        )
    return dict(applicable=True, verdict="N/A", level="unknown",
                reason="农业池成员，但基本面库中无该股净利同比 → 不得视为常态")


# ---------------------------------------------------------------- [5] 汇总

# ★ 每个 section 一张独立映射表，禁止跨 section 共用枚举。
#   教训（SOP 通律，已触发两次：verdict 三档 / signal_dilution_lambda）：
#   同一个枚举值在不同模块里含义不同时共用一张表，会把两融 ELEVATED
#   标成「波动率偏高」。
#   元组 = (标签名, 档位, 是否经过验证)
SECTION_LABELS = {
    "quality":     {"FAIL": ("数据质量判坏", "bad", True)},
    "vol":         {"HIGH": ("波动率历史高位", "warn", False),
                    "ELEVATED": ("波动率偏高", "warn", False)},
    "margin":      {"CROWDED": ("两融拥挤（顶十分位）", "bad", True),
                    "ELEVATED": ("两融拥挤度偏高（75~90 分位）", "warn", False)},
    "agri_np_yoy": {"AVOID": ("农业池净利同比回避组", "bad", True),
                    "COLLAPSE": ("农业池净利大幅转负", "warn", False)},
}
MARGIN_ELEVATED_CAVEAT = (
    "⚠ 回测口径为顶十分位（chg20 ≥90 分位，D10）；75~90 分位区未单独检验，"
    "属边界外推，不得与 CROWDED 同等对待")


def verdict(code: str, name: str = "", asof: str | None = None,
            price_db: str | None = None, with_evidence: bool = False) -> dict:
    """个股风险判决。

    with_evidence: 是否采集真实数据证据区（新闻 / 公告 / 研报）。
        **默认 False** —— 批量扫描（risk_scan，780 只）逐只走网络会慢到不可用；
        仅单只判决书 CLI 默认开启。
    """
    db = resolve_price_db(code, price_db)
    g0 = gate_coverage(code)
    g1 = gate_quality(db, code)
    g2 = state_vol(db, code, asof)
    # 同期池等权基准：仅在深度回撤时算（0.09s，可忽略），回答
    # 「是个股问题还是市场问题」。普通情形不浪费查询。
    if (g2 or {}).get("deep_drawdown") and (g2 or {}).get("drawdown_peak_date"):
        g2["bench_pool"] = pool_equal_weight(
            db, g2["drawdown_peak_date"], g2.get("asof") or "")
    g3 = label_margin(code, asof)
    g4 = label_agri_np_yoy(code)

    hits, misses = [], []
    for key, g, section in (
            ("quality", g1, "[1] 数据质量"),
            ("vol", g2, "[2] 波动率状态"),
            ("margin", g3, "[3] 两融拥挤"),
            ("agri_np_yoy", g4, "[4] 农业池净利同比")):
        v = g.get("verdict")
        meta = SECTION_LABELS.get(key, {}).get(v)
        if meta:
            title, sev, validated = meta
            basis = g.get("basis", "")
            if key == "margin" and v == "ELEVATED":
                basis = MARGIN_ELEVATED_CAVEAT
            hits.append(dict(key=key, label=title, severity=sev, section=section,
                             validated=validated, reason=g.get("reason", ""),
                             basis=basis or g.get("reason", "")))
        else:
            misses.append(dict(key=key, section=section, verdict=v,
                               reason=g.get("reason", "")))

    if g2.get("deep_drawdown"):
        hits.append(dict(key="drawdown", label="深度回撤中", severity="warn",
                         section="[2] 波动率状态", validated=False,
                         reason=f"当前距历史高点回撤 {g2['drawdown_now']:.1%}",
                         basis="描述性：阈值 −20%，非经验证信号"))
    if g1.get("verdict") == "N/A":
        hits.append(dict(key="qc_unknown", label="数据质量未判定", severity="warn",
                         section="[1] 数据质量", validated=False,
                         reason=g1.get("reason", ""),
                         basis="从严处理：未质检 ≠ 合格"))

    # 真实数据证据区（可选）：新闻 / 公告 / 研报，全部来自真实 API。
    # fail-loud：采集失败必须落进报告，绝不静默省略（否则「没拉到」会被读成「无消息」）。
    ev = None
    if with_evidence:
        try:
            import ashare_evidence
            # 回撤窗口取数：仅深度回撤且已知高点日期时才按年分段取（多花 6~8 次请求），
            # 普通情形不传 ⇒ 不多发请求。
            _pk = ((g2 or {}).get("drawdown_peak_date") or None
                   if (g2 or {}).get("deep_drawdown") else None)
            ev = ashare_evidence.collect_evidence(
                code, layers=("news", "announcement", "research"), limit=5,
                window_from=_pk)
        except Exception as e:                      # noqa: BLE001（有意兜底后进报告）
            ev = {"error": f"{type(e).__name__}: {e}", "layers": {}}

    n_val = sum(1 for h in hits if h["validated"])
    return dict(code=code, name=name, asof=asof or "", price_db=db.name,
                generated_at=datetime.now().isoformat(timespec="seconds"),
                coverage=g0, quality=g1, vol=g2, margin=g3, agri_np_yoy=g4,
                hits=hits, misses=misses, n_hits=len(hits),
                n_validated=n_val, n_descriptive=len(hits) - n_val,
                evidence=ev)


# ---------------------------------------------------------------- 报告渲染

def render(v: dict) -> str:
    L = []
    title = f"# 个股风险判决书 · {v['code']}{(' ' + v['name']) if v['name'] else ''}"
    L.append(title)
    L.append("")
    L.append(f"- 生成时间：{v['generated_at']}　行情库：`{v['price_db']}`"
             + (f"　截至：{v['asof']}" if v["asof"] else ""))
    L.append("- **本文件不产出买卖建议**。它只回答：这只股票当前挂着哪些**经过验证的风险标签**。")
    L.append("")

    g0 = v["coverage"]
    L.append(f"## [0] 参照系清单（非门禁）　→　`{g0['tier']}`")
    L.append("")
    L.append(g0["text"])
    if g0.get("in_pools"):
        L.append(f"- 命中池：{', '.join(g0['in_pools'])}")
    else:
        L.append("- 命中池：无")
    L.append("")
    L.append("> 已验证策略全部是**池级截面排序器**：输出「在某池内排第几分位」，"
             "单一样本无法构成排序 ⇒ 对单只股票在数学上无定义。")
    L.append("")
    L.append("> ⚠ **「未命中/0 项命中」是覆盖盲区，不是安全结论**。本报告只能证伪"
             "「已验证标签是否命中」，不能证明「该股无风险」。数据齐备性另见 "
             "`stock_diagnose.data_readiness_gate()`，两者语义不同，不可互换。")
    L.append("")

    g1 = v["quality"]
    L.append(f"## [1] 数据质量判决　→　`{g1['verdict']}`")
    L.append("")
    L.append(g1["reason"])
    L.append("")

    g2 = v["vol"]
    L.append(f"## [2] 波动率状态　→　`{g2['verdict']}`（描述性）")
    L.append("")
    if g2["verdict"] == "N/A":
        L.append(g2["reason"])
    else:
        L.append(f"- 数据截至 {g2['asof']}，有效样本 {g2['n_obs']} 日")
        L.append(f"- 近 {RV_WINDOW} 日年化波动：**{g2['rv20_annual']:.1%}**"
                 f"（全样本 {g2['rv_full_annual']:.1%}）")
        L.append(f"- 自身历史分位：**{g2['vol_percentile']:.0%}** → {g2['desc']}")
        L.append(f"- 当前距历史高点回撤：**{g2['drawdown_now']:.1%}**")
        L.append("")
        L.append(f"> ⚠ {g2['caveat']}")
    L.append("")

    g3 = v["margin"]
    L.append(f"## [3] 两融拥挤标签　→　`{g3['verdict']}`")
    L.append("")
    L.append(g3["reason"])
    if g3.get("asof"):
        L.append(f"- 两融数据日 {g3['asof']}，滞后 {g3.get('stale_days')} 天，"
                 f"横截面样本 {g3.get('cross_section_n')} 只")
    if g3.get("basis"):
        L.append(f"- 依据：{g3['basis']}")
    L.append("")

    g4 = v["agri_np_yoy"]
    L.append(f"## [4] 农业池净利同比标签　→　`{g4['verdict']}`")
    L.append("")
    L.append(g4["reason"])
    if g4.get("basis"):
        L.append(f"- 依据：{g4['basis']}")
    L.append("")

    L.append(f"## [5] 判决书：{v['n_validated']} 项已验证标签 / "
             f"{v['n_descriptive']} 项描述性观察")
    L.append("")

    val = [h for h in v["hits"] if h["validated"]]
    des = [h for h in v["hits"] if not h["validated"]]

    L.append("### 已验证风险标签（有回测依据，可作为回避/警惕的输入）")
    L.append("")
    if val:
        L.append("| 标签 | 档位 | 依据 | 来源 |")
        L.append("|---|---|---|---|")
        for h in val:
            L.append(f"| **{h['label']}** | {h['severity']} | {h['basis']} | {h['section']} |")
    else:
        L.append("无。")
        L.append("")
        L.append("> **无 ≠ 安全**：现有已验证清单只有两融拥挤、农业池净利同比两条，覆盖面极窄；"
                 "未命中只说明该股不在这些清单上。")
    L.append("")

    L.append("### 描述性观察（未经回测验证，不得当作信号使用）")
    L.append("")
    if des:
        L.append("| 观察 | 数值/依据 | 来源 |")
        L.append("|---|---|---|")
        for h in des:
            L.append(f"| {h['label']} | {h['reason']} | {h['section']} |")
    else:
        L.append("无。")
    L.append("")
    if v["misses"]:
        L.append("未命中 / 不适用项：")
        for m in v["misses"]:
            L.append(f"- `{m['key']}` → `{m['verdict']}`：{m['reason']}")
        L.append("")

    L.append("### 本判决书不能回答什么")
    L.append("")
    L.append("- **不回答买 / 卖 / 持有**，也不给目标价、止损位、仓位建议。")
    L.append("- 不预测未来收益：A 股 alpha 线已全部关闭（5 个价量因子为风格马甲、"
             "基本面换池检验失败、行业轮动需 147 年、宏观择时 MDE 结构性不匹配）。")
    L.append("- 不代表「未命中 = 安全」：现有风险清单只有两融拥挤与农业池净利同比两条，"
             "覆盖面极窄。")
    L.append("- 幸存者偏差：中证 800 为成分快照回看，**绝对收益水平不可信**；"
             "差分类判据（分位、相关性）大部分免疫。")
    L.append("")

    # ---------------------------------------------------------- [6] 真实数据证据区
    ev = v.get("evidence")
    if ev is not None:
        L.append("## [6] 真实数据证据区（新闻 / 公告 / 研报）")
        L.append("")
        L.append("> 全部来自**真实 API**（`ashare_evidence.py`），逐条可点回原文核实。"
                 "本区只回答「同期发生了什么」，**不回答该不该买卖**。")
        L.append("")
        if ev.get("error"):
            L.append(f"⚠ **证据采集失败**：{ev['error']}")
            L.append("")
            L.append("> 失败 ≠ 无消息。证据缺失时不得据此推断「没有利空」。")
            L.append("")
        else:
            layers = ev.get("layers") or {}
            _FLAG = {"OK": "✓", "EMPTY": "○", "UNAVAILABLE": "✗"}
            _TRUST = {"official": "一手", "third_party": "三方"}

            # ★ 回撤期事件归因（2026-09-06 对比开源新增）
            #   开源项目（TradingAgents / FinRobot 等）只给「当前情绪/新闻摘要」，
            #   不把价格回撤与同期披露做时间对齐。本段补上这个缺口：
            #   把 [2] 段的回撤幅度对齐到同期公告/新闻，回答「这段时间发生了什么」。
            #   纪律：是**时序对齐**，不是因果推断；不构成择时或买卖依据。
            g2v = v.get("vol") or {}
            pk = g2v.get("drawdown_peak_date") or ""
            if g2v.get("deep_drawdown") and pk:
                # ★ 窗口真取数（2026-09-06 二次优化）
                #   第一版是「从常规证据层里筛出落在窗口的条目」——常规层只取最近 N 条，
                #   对 5.6 年窗口等于没覆盖，只能靠「自曝不全」兜底（把手段当目的）。
                #   现改为按窗口真取数 + 按年分段，覆盖铺满整个窗口，并**精确**报告覆盖数字。
                wa = ev.get("window_announcements") or {}
                win: list[tuple[str, str, dict]] = []
                for _it in (wa.get("items") or []):
                    _t = (_it.get("time") or "")[:10]
                    if _t:
                        win.append((_t, "公告", _it))
                for _ln, _lbl in (("news", "新闻"), ("research", "研报")):
                    for _it in ((layers.get(_ln) or {}).get("items") or []):
                        _t = (_it.get("time") or "")[:10]
                        if _t and _t >= pk:
                            win.append((_t, _lbl, _it))
                _seen, _u2 = set(), []
                for _t, _lbl, _it in win:
                    _k = _it.get("url") or (_t, _it.get("title"))
                    if _k not in _seen:
                        _seen.add(_k); _u2.append((_t, _lbl, _it))
                win = _u2
                win.sort(key=lambda z: z[0])

                L.append(f"#### ⤵ 回撤期事件（自高点 {pk} 起 "
                         f"{g2v.get('drawdown_days', 0)} 个交易日）")
                L.append("")
                L.append(f"> 该窗口内回撤 **{g2v.get('drawdown_now', 0):.1%}**。"
                         f"以下为落入窗口的公开披露/报道，用于回答「这段时间发生了什么」"
                         f"——**是时序对齐，不是因果推断**。")
                L.append("")

                # ★ 同期池等权基准：回答「是个股问题还是市场问题」
                #   回撤幅度本身没有参照系——−40% 在普跌行情里可能是「跟跌」，
                #   在普涨行情里才是「个股暴雷」。没有基准就无法区分。
                _bp = g2v.get("bench_pool") or {}
                if _bp:
                    if _bp.get("error"):
                        L.append(f"> ⚠ 池等权基准不可用：{_bp['error']}")
                        L.append("")
                    else:
                        _br = float(_bp.get("ret") or 0.0)
                        _ex = float(g2v.get("drawdown_now") or 0.0) - _br
                        _w = _bp.get("window") or [pk, ""]
                        L.append(f"**同期参照**：{_w[0]} → {_w[1]}，"
                                 f"池内等权 **{_br:+.1%}**（n={_bp.get('n')}），"
                                 f"本股 **{g2v.get('drawdown_now', 0):+.1%}**"
                                 f"　⇒　**超额 {_ex:+.1%}**")
                        L.append("")
                        L.append("> 解读边界：①「池内等权」= 该池全部股票同权重，**不是沪深 300**"
                                 "（指数需跨源取数，会引入口径不一致）；"
                                 "② 池为成分快照回看，**绝对水平受幸存者偏差影响**，"
                                 "仅『超额』这类差分类判据可用；"
                                 "③ 这是**描述性对比**，不是归因结论——跑输不等于公司出了问题。")
                        L.append("")

                # 精确覆盖报告（取代原先模糊的「远非窗口全貌」）
                if wa:
                    _st = wa.get("status")
                    if _st == "UNAVAILABLE" or wa.get("error"):
                        L.append(f"> ⚠ **窗口取数失败**：{wa.get('error', _st)}"
                                 f"　—— 失败 ≠ 窗口内无事件。")
                        L.append("")
                    else:
                        _tt = int(wa.get("total") or 0)
                        _ff = int(wa.get("fetched") or 0)
                        _segs = wa.get("segments") or []
                        _yr = "　".join(f"{s['year']}:{s['fetched']}/{s['total']}"
                                        for s in _segs)
                        _pct = (_ff / _tt * 100) if _tt else 0.0
                        L.append(f"**窗口内公告覆盖**（巨潮口径）：共 **{_tt}** 条，"
                                 f"已取 **{_ff}** 条（{_pct:.0f}%），按年分段：")
                        L.append("")
                        L.append(f"> {_yr}")
                        L.append("")
                        if _pct < 100:
                            L.append("> ⚠ **覆盖粒度是「年」不是「条」**：每年取该年**最近** "
                                     f"{_segs[0]['fetched'] if _segs else 30} 条，"
                                     "非该年全部。要逐条还原须逐年翻页。")
                            L.append("")

                if win:
                    # 展示策略：**按年挑关键节点**，不是「取最近 8 条」。
                    #   后者对长窗口是灾难——5.6 年窗口只会看到起点附近的
                    #   「独立董事意见 / 监事会决议」这类噪音，看不到过程。
                    #   关键词命中是为了**降噪**，不是判断重要性，措辞不夸大。
                    _KEY = ("报告", "业绩", "预告", "快报", "重大", "重组", "收购",
                            "减持", "增持", "回购", "诉讼", "问询", "关注函", "停牌",
                            "风险", "预减", "预亏", "亏损", "辞职", "离任", "违规")
                    _by_year: dict[str, list] = {}
                    for _t, _lbl, _it in win:
                        _by_year.setdefault(_t[:4], []).append((_t, _lbl, _it))
                    _nodes, _ny = [], 0
                    for _y in sorted(_by_year, reverse=True):
                        _lst = _by_year[_y]
                        _hit = [x for x in _lst
                                if any(k in (x[2].get("title") or "") for k in _KEY)]
                        _nodes.extend((_hit[:2] if _hit else _lst[:1]))
                        _ny += 1
                    L.append(f"**年度关键节点**（{_ny} 个年度，每年最多 2 条，"
                             f"按关键词降噪后取该年条目）：")
                    L.append("")
                    for _t, _lbl, _it in _nodes:
                        _u = _it.get("url")
                        L.append(f"- `{_t}` 〔{_lbl}〕{_it.get('title', '')}")
                        if _u:
                            L.append(f"  - {_u}")
                    L.append("")
                    L.append(f"> 关键词降噪只用于**减少例行披露干扰**"
                             f"（监事会决议/独立董事意见等），"
                             f"不构成重要性判断。窗口内共 {len(win)} 条已取条目。")
                else:
                    L.append("- （窗口内无落入条目：现有证据均早于高点日期，或采集条数不足）")
                    L.append("")
                    L.append("> ⚠ 这不代表期间「什么都没发生」——很可能是证据采集条数有限，"
                             "**缺证据 ≠ 无事件**。")
                L.append("")

            # ★ 证据 ↔ 结论关联标注（2026-09-06 对比开源新增）
            #   开源 TradingAgents 的 TradeProposal 带 supporting_evidence 字段，
            #   把每条结论绑定到具体证据；我方原实现是结论（[5]）与证据（[6]）两张皮。
            #   此处补上关联，但**只做关键词命中提示，不做任何推断**——
            #   措辞固定为「含某字样 / 可能关联」，是否真相关由人工核实。
            # ⚠ 第廿二类教训（2026-09-06 当场复发一次）：key 必须来自 SECTION_LABELS /
            #   hits 的真实取值，不得凭标签名推断。实际取值 = quality/vol/margin/
            #   agri_np_yoy/drawdown/qc_unknown（此处曾误写 margin_crowding ⇒ 静默不触发）。
            _HINT_KW = {
                "margin": (("融资", "两融", "融券", "杠杆", "保证金"), "两融拥挤"),
                "agri_np_yoy": (("净利", "业绩预", "预亏", "预减", "亏损", "同比"), "农业池净利同比"),
                "drawdown": (("回撤", "创新低", "跌停", "减持", "下挫", "破位"), "深度回撤"),
                "vol": (("波动", "振幅", "异动", "涨停", "跌停"), "波动率状态"),
            }
            _hit_keys = {h.get("key") for h in (v.get("hits") or [])}
            _hint_used = 0

            def _hint_for(it: dict) -> str:
                nonlocal _hint_used
                txt = f"{it.get('title', '')} {it.get('summary', '')}"
                for k, (kws, lbl) in _HINT_KW.items():
                    if k in _hit_keys:
                        hit = next((w for w in kws if w in txt), None)
                        if hit:
                            _hint_used += 1
                            return f" ⟶ 含「{hit}」字样，可能关联 *{lbl}*（系统匹配，需人工核实）"
                return ""

            for lname, title in (("announcement", "交易所公告"),
                                 ("news", "个股新闻"),
                                 ("research", "券商研报")):
                d = layers.get(lname)
                if not d:
                    continue
                st = d.get("status", "?")
                L.append(f"### {_FLAG.get(st, '?')} {title}"
                         f"（{_TRUST.get(d.get('source_trust'), '-')} · {st} · "
                         f"{d.get('count', 0)} 条）")
                L.append("")
                if d.get("error"):
                    L.append(f"- ⚠ {d['error']}")
                for it in (d.get("items") or []):
                    extra = ""
                    if lname == "announcement" and it.get("type"):
                        extra = f"〔{it['type']}〕"
                    if lname == "research" and it.get("rating"):
                        extra = f"〔{it['rating']}〕"
                    L.append(f"- `{it.get('time', '')}` **{it.get('source', '')}**"
                             f"：{extra}{it.get('title', '')}{_hint_for(it)}")
                    if it.get("url"):
                        L.append(f"  - {it['url']}")
                if st == "EMPTY":
                    L.append("- （该层本次未取到条目）")
                L.append("")

            cons = (layers.get("research") or {}).get("consensus")
            if isinstance(cons, dict) and cons.get("items"):
                L.append("#### 机构一致预期 EPS（同花顺）")
                L.append("")
                L.append("| 年度 | 预测机构数 | 均值 | 最小值 | 最大值 |")
                L.append("|---|---|---|---|---|")
                for row in cons["items"][:4]:
                    L.append(f"| {row.get('年度', '')} | {row.get('预测机构数', '')} | "
                             f"{row.get('均值', '')} | {row.get('最小值', '')} | "
                             f"{row.get('最大值', '')} |")
                L.append("")
                if cons.get("note"):
                    L.append(f"> {cons['note']}")
                    L.append("")
            L.append("> ⚠ 新闻与研报评级属**第三方观点**，不等于本 agent 的结论；"
                     "研报评级（买入/增持）尤其不得当作买入依据。")
            L.append("")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--code", required=True, help="6 位代码")
    ap.add_argument("--name", default="")
    ap.add_argument("--asof", default=None, help="YYYY-MM-DD，用于复现历史时点")
    ap.add_argument("--price-db", default=None)
    ap.add_argument("--json-only", action="store_true")
    ap.add_argument("--no-evidence", action="store_true",
                    help="跳过 [6] 真实数据证据区（离线 / 加速用）")
    ap.add_argument("--out", default=None, help="JSON 输出路径")
    a = ap.parse_args()

    try:
        v = verdict(a.code, a.name, a.asof, a.price_db,
                    with_evidence=not a.no_evidence)
    except VerdictError as e:
        print(f"[FAIL] {e}", file=sys.stderr)
        return 2

    if not a.json_only:
        print(render(v))

    out = Path(a.out) if a.out else (
        OUT / datetime.now().strftime("%Y-%m-%d")
        / f"risk_verdict_{a.code}_{Path(v['price_db']).stem}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(v, ensure_ascii=False, indent=2, default=str),
                   encoding="utf-8")
    if not a.json_only:
        print(f"\n[JSON] {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
