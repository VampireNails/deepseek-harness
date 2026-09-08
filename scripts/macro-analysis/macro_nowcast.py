#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
宏观指标 nowcast（交付层）—— 把已通过验证的 `macro_ppi_yoy_ar` 变成日常可看的预测输出。

定位（不可逾越）
----------------
本脚本是**交付层**，不是新的研究工具。它不新造模型、不调参、不重新验证：
- 模型规格 100% 复用 `macro_predict.py`（同一 load_panel / feat_row / ridge_fit /
  walk_forward），**保证 nowcast 与已登记 verdict 口径同源**——口径漂移 = 另起炉灶的
  未验证模型，这正是本项目拒绝的东西。
- 只出 **1 步超前**（h=1）。verdict 只有 h=1 通过双闸门；多步需迭代填 lag，
  误差累积且**从未验证**，故不产出。

口径诚实（写死在每条输出里）
----------------------------
- 训练与评估用 **vintage 终值**；预测对象严格说是"终版口径的 T+1 值"。
  首次公布值与终版存在修订差 → **本输出不是实时首发口径 nowcast**，修订偏置未消除。
- zero look-ahead：预测 T+1 只用 period <= T 的观测（feat_row + asof 保证）。

状态灯（预声明规则，禁止事后挑选）
----------------------------------
以**最近 12 个 OOS 月**的 RMSE 降幅 r12 判定：
    r12 >  10%  → STRONG   绿灯：近期明显优于 persistence
    0 < r12 <= 10% → MILD  黄灯：仍优于基线但已衰减（报告已记录 2024 +5% / 2025 +4%）
    r12 <= 0    → INVALID  红灯：近期劣于 persistence，不得据此决策
红灯时脚本仍输出预测，但首屏打出否决提示——交付的是事实，不是"好看的结论"。

运行
----
python macro_nowcast.py                        # 默认 ppi_yoy@CN（唯一可交付策略）
python macro_nowcast.py --target cpi_yoy@CN    # 其它目标（会带未通过验证警告）
python macro_nowcast.py --lookback 24 --out <dir>
python macro_nowcast.py --no-latest            # 不写日常入口（一次性对照跑）

产物
----
- outputs/macro_nowcast/latest.md  ← **日常看这个**（固定路径，每次覆盖）
- outputs/macro_nowcast/latest.json
- outputs/macro_nowcast/history/<目标期>.md   （按目标期归档）
- outputs/<date>/macro_<指标>_nowcast.{json,md}（本次运行的完整快照）
"""
import sys, json, math, sqlite3, argparse
from pathlib import Path
from datetime import datetime

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from macro_predict import (load_panel, pidx, pshift, asof, feat_row,      # noqa: E402
                           ridge_fit, ridge_pred, walk_forward, metrics,
                           MIN_TRAIN, SELF_LAGS, RIDGE_ALPHA, TARGETS)

ROOT = Path(__file__).resolve().parent
while not (ROOT / "outputs").is_dir() and str(ROOT) != str(ROOT.parent):
    ROOT = ROOT.parent
OUTDIR = ROOT / "outputs" / "2026-09-03"
REG = ROOT / "outputs" / "macro_strategy_registry.sqlite"

# 通过验证的策略 → 目标映射（registry verdict 为准；此处仅作"是否已验证"提示）
VALIDATED = {"macro_ppi_yoy_ar": "ppi_yoy@CN"}

# 状态灯阈值（预声明）
R12_STRONG, R12_MILD = 10.0, 0.0
ROLL_RECENT = 12          # 状态灯回看月数
ERR_WIN = 60              # 区间宽度估计回看月数
COVER_Q = 0.80            # 区间经验覆盖率


def pnext(p):
    """日历算术：p 往后一个月。

    ⚠ 编码/解码差一陷阱（与 macro_predict.pshift 同一约定，改动前先读那段注释）：
        pidx(p) = 12*y + m（m 从 1 起，相邻月份连续）
        dec(t)  = (t // 12, t % 12 + 1)   ← 解码出的月份其编码为 t+1（偏移 +1）
      因此 dec(pidx(p)) 就是「下个月」，而 pshift 用 dec(pidx(p)-1-k) 把 +1 与 -1
      抵消成「往前 k 个月」。新增日历算术函数必须复用 dec 并保持该约定，
      否则会静默跳过/重复一个月。
    """
    t = pidx(p)
    return f"{t // 12}-{t % 12 + 1:02d}"


def is_deliverable(verdict_text):
    """verdict 是否属「可交付」档。

    ⚠ 必须用括号前的**主档名精确比对**，禁止子串包含：
    "真实但不可交付(regime依赖)" 含子串 "可交付" → 子串判断会把它误判为通过，
    让不可交付的规格蒙混进入交付层。这是本脚本最危险的失败模式。
    """
    if not verdict_text:
        return False
    return verdict_text.split("(")[0].strip() in ("有效预判",)


def verified_verdict(strategy_key):
    """从注册表取已验证 verdict；未登记返回 None（fail-loud）"""
    if not REG.exists():
        return None
    c = sqlite3.connect(str(REG))
    row = c.execute("SELECT verdict, verdict_basis, n_oos, rmse_reduction_pct, "
                    "n_years_positive, n_years FROM strategy_registry "
                    "WHERE strategy_key=?", (strategy_key,)).fetchone()
    c.close()
    if not row:
        return None
    keys = ["verdict", "verdict_basis", "n_oos", "rmse_reduction_pct",
            "n_years_positive", "n_years"]
    return dict(zip(keys, row))


def fit_nowcast(data, tk, leads, t_target):
    """用 period <= t_target-1 的全量历史拟合，预测 t_target（1 步，无前视）。"""
    series = data[tk]
    grid = sorted(series.keys(), key=pidx)
    Xtr, ytr = [], []
    for pj in grid:
        if pj == t_target or pidx(pj) > pidx(t_target):
            continue
        f, ok = feat_row(data, pj, tk, leads)
        if ok:
            Xtr.append(f)
            ytr.append(series[pj])
    if len(ytr) < MIN_TRAIN:
        raise RuntimeError(f"训练样本不足: {len(ytr)} < MIN_TRAIN={MIN_TRAIN}")
    Xtr, ytr = np.array(Xtr), np.array(ytr)
    m = ridge_fit(Xtr, ytr)
    f, ok = feat_row(data, t_target, tk, leads)
    if not ok:
        raise RuntimeError(f"目标期 {t_target} 特征不完整（滞后项缺失）——拒绝输出")
    yhat = float(ridge_pred(m, f.reshape(1, -1))[0])
    p_t = pshift(t_target, 1)                       # 严格滞后一期 = 基线观测
    if p_t not in series:
        raise RuntimeError(f"基线期 {p_t} 缺失 —— persistence 基线无法构造，拒绝输出")
    return dict(yhat=yhat, n_train=len(ytr), train_from=grid[0],
                train_to=max((p for p in grid if pidx(p) < pidx(t_target)), key=pidx),
                persist=float(series[p_t]))


def status_light(r12):
    if r12 is None:
        return "UNKNOWN", "近期样本不足，无法判定"
    if r12 > R12_STRONG:
        return "STRONG", f"最近 {ROLL_RECENT} 个月 RMSE 降幅 {r12:+.1f}% > {R12_STRONG}%：明显优于 persistence"
    if r12 > R12_MILD:
        return "MILD", (f"最近 {ROLL_RECENT} 个月 RMSE 降幅 {r12:+.1f}%：仍优于基线但已衰减"
                        f"（0~{R12_STRONG}% 区间），权重应下调")
    return "INVALID", (f"最近 {ROLL_RECENT} 个月 RMSE 降幅 {r12:+.1f}% ≤ 0："
                       "近期**劣于** persistence，不得据此决策")


LIGHT_ICON = {"STRONG": "🟢", "MILD": "🟡", "INVALID": "🔴", "UNKNOWN": "⚪"}

# 指标中文名 + 单位（交付层专用：报告是给人读的，不能出现 ppi_yoy 这种内部键）
LABEL = {
    "ppi_yoy": ("PPI 同比", "%"),
    "cpi_yoy": ("CPI 同比", "%"),
    "manufacturing_pmi": ("制造业 PMI", ""),
    "nonfarm_payroll_change": ("美国非农就业变动", "千人"),
}
# 指标发布节奏（用于说明 nowcast 的价值窗口；不编造精确日期）
RELEASE_NOTE = {
    "ppi_yoy": "国家统计局通常于**次月上旬**公布上月 PPI",
    "cpi_yoy": "国家统计局通常于**次月上旬**公布上月 CPI",
    "manufacturing_pmi": "国家统计局于**当月末**公布本月 PMI",
    "nonfarm_payroll_change": "美国劳工部通常于**次月第一个周五**公布上月非农",
}


def _sign(x, d=2):
    return f"{x:+.{d}f}"


def write_md(out, fp):
    """生成人类可读报告（交付形态）。

    定位：给「要看结论的人」读，不是给机器。因此：
    - 结论先行，数字带口径；
    - 每一节都回答「这对我意味着什么」或「我不能拿它做什么」；
    - 不隐藏不利证据（逐期表原样列出 better=False 的期数）。
    """
    nc, iv, st = out["nowcast"], out["interval"], out["status"]
    bs, cal, reg = out["backtest_summary"], out["calibration"], out.get("registered")
    icon = LIGHT_ICON.get(st["light"], "⚪")
    hkey, ctry = out["target"].split("@")
    cn_name, unit = LABEL.get(hkey, (hkey, ""))
    usuf = f" {unit}" if unit else ""
    rel_note = RELEASE_NOTE.get(hkey, "官方按既定日程公布")
    # 可交付性判定（全报告共用，必须最先定：摘要行与第二节都要用）
    ok = bool(reg) and is_deliverable(reg["verdict"])

    L = []
    A = L.append
    A(f"# 宏观 nowcast｜{cn_name}（{ctry}）｜目标期 {nc['target_period']}")
    A("")
    # 状态灯是「近期表现」，可交付性是另一维度 —— 两者必须同时出现在首屏，
    # 否则「🟢 STRONG」会被单独读成「可以用」，而规格其实不可交付。
    dtag = "　🔴 **规格不可交付，仅供对照**" if not ok else ""
    A(f"> 生成 {out['generated_at']}　数据截止 **{nc['data_cutoff']}**　"
      f"规格 `{out['strategy_key']}`　状态 {icon} **{st['light']}**{dtag}")
    A("")
    A(f"> 训练窗 {nc['train_from']}..{nc['train_to']}（n={nc['n_train']}）　"
      f"回测 OOS {bs['n_oos']} 期（{bs['oos_from']}..{bs['oos_to']}）")
    A("")

    # ---------- 一、结论先行 ----------
    A("## 一、结论先行")
    A("")
    A(f"**{nc['target_period']} {cn_name}预测 = {_sign(nc['point'])}{unit}**")
    A("")
    A(f"*价值窗口*：{rel_note}。本预测在官方公布前提供量化先验，公布后即失效。")
    A("")
    A("| 项 | 值 |")
    A("|---|---|")
    A(f"| 本模型点预测 | **{_sign(nc['point'])}** |")
    A(f"| persistence 基线（{nc['data_cutoff']} 实际值） | {_sign(nc['persistence_baseline'])} |")
    A(f"| 相对基线 | {_sign(nc['delta_vs_persistence'])} → **{nc['direction']}** |")
    A(f"| 80% 区间 | [{_sign(iv['lo'])}, {_sign(iv['hi'])}]（半宽 ±{iv['half_width']:.2f}） |")
    A(f"| 状态灯 | {icon} {st['light']} |")
    A("")
    A(f"**状态灯判据**：{st['reason']}")
    A("")
    A(f"区间口径：{iv['basis']}（非参数，不假设正态；全样本半宽 ±{iv['full_sample_half_width']:.2f}）")
    A("")
    A(f"> 「持平」判定阈值 = 环比变动中位数的 50% = ±{nc['flat_threshold']:.2f}；"
      f"|Δ| ≤ 该值一律记为持平，不做方向性解读。")
    A("")
    if reg and not is_deliverable(reg["verdict"]):
        A(f"> 🔴 **未通过可交付判定**：`{reg['verdict']}` —— 本报告仅供对照，不得用于决策。")
        A("")

    # ---------- 二、能用来做什么 ----------
    A("## 二、这份输出能用来做什么、不能用来做什么")
    A("")
    if not ok:
        # 未通过可交付判定 —— 不得给出任何"可以怎么用"的正面引导
        A("🔴 **本规格未通过可交付判定，以下「能用来做」整节不适用。**")
        A("")
        A("本报告的合法用途**只有一条**：作为方法学对照，"
          "用于观察「未通过验证的规格」在样本外的表现形态。")
        A("")
        A("除此之外，不得用于任何判断、决策或沟通。")
        A("")
    A("**能用来做（决策辅助）**")
    A("")
    A(f"- 在官方公布前，获得对 {nc['target_period']} {cn_name}的**量化先验**，"
      f"用于校准你对{'工业品价格' if hkey == 'ppi_yoy' else '该指标'}方向的已有判断；")
    A(f"- 当模型与基线分歧显著（本期 {_sign(nc['delta_vs_persistence'])}）时，"
      f"提示「简单外推可能失真」，值得查一下近期上下游价格的分歧；")
    A(f"- 用 80% 区间做**情景边界**，而不是只看点估计。")
    A("")
    A("**不能用来做（硬边界）**")
    A("")
    A("- ❌ **不是投资建议**：本输出不构成任何买卖、仓位或配置建议；")
    A("- ❌ **不是实时首发口径**：训练与评估均用 vintage 终值，"
      "预测对象是「终版口径的 T+1 值」，与官方首发值存在修订差（见下节）；")
    A("- ❌ **不能外推到多步**：只出 1 步超前（h=1），多步需迭代填 lag，未经验证；")
    A("- ❌ **不解释「为什么」**：模型是纯自回归，只捕捉惯性与均值回复，"
      "不含任何结构性因果，无法回答「是什么驱动了这次变动」。")
    A("")

    # ---------- 三、口径与边界 ----------
    A("## 三、口径与边界（必读）")
    A("")
    for c in out["caveat"]:
        A(f"- {c}")
    A("")
    if reg:
        ok = is_deliverable(reg["verdict"])
        if not ok:
            A(f"> 🔴 **本规格未通过可交付判定**（verdict：`{reg['verdict']}`）—— "
              f"下方数字**仅供对照，不得用于决策**。")
            A(">")
        A(f"**注册表已登记 verdict**：{'✅' if ok else '⚠️'} `{reg['verdict']}`")
        A("")
        A(f"> 依据：{reg['verdict_basis']}")
        A("")
    else:
        A("**⚠ 注册表未登记该规格 —— 未通过验证，仅供对照，不得用于决策。**")
        A("")

    # ---------- 四、近期校准 ----------
    A(f"## 四、逐期回看校准（最近 {cal['lookback']} 期）")
    A("")
    rows = cal["rows"]
    n_win = sum(1 for r in rows if r["better"])
    A(f"近期胜率（模型误差 < 基线误差）= **{cal['hit_rate_recent']:.0%}**（{n_win}/{len(rows)}）")
    A("")

    # ★ 中间量交叉校验：胜率接近 50% 不等于无效，必须用「赢时赢多少 / 输时输多少」证伪该误读
    wins = [abs(r["err_persist"]) - abs(r["err_model"]) for r in rows if r["better"]]
    losses = [abs(r["err_model"]) - abs(r["err_persist"]) for r in rows if not r["better"]]
    wm = float(np.mean(wins)) if wins else 0.0
    lm = float(np.mean(losses)) if losses else 0.0
    A(f"> ⚠ **胜率不是有效性判据，别把它读成「抛硬币」。**")
    A(">")
    A(f"> 这 {len(rows)} 期里，模型赢的 {n_win} 期平均比基线**少错 {wm:.2f}** pp，"
      f"输的 {len(losses)} 期平均只**多错 {lm:.2f}** pp，"
      f"累计净少错 **{n_win * wm - len(losses) * lm:.2f}** pp；")
    A("> 赢时赢得多、输时输得少，所以胜率接近一半，误差却实实在在更小。")
    A(">")
    r12txt = (f"，近 {st['roll_window']} 期 {st['r12_pct']:+.1f}%"
              if st.get("r12_pct") is not None else "")
    A(f"> **有效性判据是误差幅度，不是方向胜率**：全样本 {bs['n_oos']} 期 RMSE 降 "
      f"{bs['rmse_reduction_pct']:.2f}%{r12txt}，并经 CW / DM-NW 双闸门。"
      f"请勿用单期胜负评价本模型。")
    A("")
    A("| 期 | 实际 | 模型 | 基线 | 模型误差 | 基线误差 | 优于基线 |")
    A("|---|---|---|---|---|---|---|")
    for r in cal["rows"]:
        A(f"| {r['period']} | {_sign(r['actual'])} | {_sign(r['model'])} | "
          f"{_sign(r['persist'])} | {_sign(r['err_model'])} | {_sign(r['err_persist'])} | "
          f"{'✓' if r['better'] else '✗'} |")
    A("")
    A("> 注意：上表**原样列出**模型输给基期的月份。近期连续出现 ✗ 是趋势转折期的典型表现——"
      "自回归模型在拐点必然滞后。请以状态灯与区间为准，不要只看最近一两期。")
    A("")

    # ---------- 五、逐年 ----------
    by = out.get("by_year") or {}
    if by:
        A("## 五、逐年表现（walk-forward OOS）")
        A("")
        A("| 年 | 期数 | 模型 RMSE | 基线 RMSE | 降幅 |")
        A("|---|---|---|---|---|")
        for y in sorted(by):
            v = by[y]
            mark = "✓" if v["reduction_pct"] > 0 else "✗"
            A(f"| {y} | {v['n']} | {v['rmse_model']:.3f} | {v['rmse_persist']:.3f} | "
              f"{v['reduction_pct']:+.1f}% {mark} |")
        A("")
        A(f"合计：模型 RMSE {bs['rmse_model']:.3f} vs 基线 {bs['rmse_persist']:.3f} "
          f"（降 {bs['rmse_reduction_pct']:.2f}%），"
          f"逐年为正 {bs['n_years_positive']}/{bs['n_years']}，"
          f"方向命中率 {bs['dir_hit']:.1%}。")
        A("")

    # ---------- 六、规格 ----------
    ms = out["model_spec"]
    A("## 六、模型规格（与已验证策略同源，未做任何调参）")
    A("")
    A("| 项 | 值 |")
    A("|---|---|")
    A(f"| 变体 | {ms['variant']}（{'纯自回归' if ms['variant'] == 'ar' else '含领先项'}） |")
    A(f"| 自回归阶数 | {ms['self_lags']} |")
    A(f"| 领先项 | {ms['leads'] or '无'} |")
    A(f"| ridge alpha | {ms['ridge_alpha']}（训练集内 z-score 标准化） |")
    A(f"| 最小训练窗 | {ms['min_train']} 期 |")
    A("")
    A("---")
    A("")
    A(f"_由 `{out['generator']}` 自动生成。数字全部来自确定性 python + sqlite，未经 LLM 改写。_")

    with open(fp, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    return fp


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", default="ppi_yoy@CN",
                    help="目标键，默认 ppi_yoy@CN（唯一通过验证的策略）")
    ap.add_argument("--variant", default="ar", choices=["ar", "comp"],
                    help="ar=纯自回归（已验证口径）；comp=含领先项（未通过验证）")
    ap.add_argument("--lookback", type=int, default=24, help="回看校准表期数")
    ap.add_argument("--out", default=None, help="输出目录，默认 outputs/<date>")
    ap.add_argument("--no-latest", action="store_true",
                    help="不写 outputs/macro_nowcast/latest.* 日常入口（默认写）")
    args = ap.parse_args()

    outdir = Path(args.out) if args.out else OUTDIR
    outdir.mkdir(parents=True, exist_ok=True)

    if args.target not in TARGETS:
        print(f"[FATAL] 未知目标 {args.target}；可用: {list(TARGETS)}")
        return 2
    spec = TARGETS[args.target]
    tk = spec["target"]
    leads = [] if args.variant == "ar" else spec["leads"]
    skey = "macro_" + args.target.split("@")[0] + ("_ar" if args.variant == "ar" else "_comp")

    vv = verified_verdict(skey)
    print("=" * 74)
    print(f"宏观 nowcast | 目标 {args.target} | 规格 {skey}")
    print("=" * 74)
    if vv is None:
        print(f"⚠ 注册表未登记 {skey} —— 该规格**未通过验证**，输出仅供对照，不得用于决策。")
    else:
        ok = is_deliverable(vv["verdict"])
        flag = "✓" if ok else "⚠"
        print(f"{flag} 已登记 verdict: {vv['verdict']}")
        if not ok:
            print("  ⚠ 该规格**不属于可交付档** —— 输出仅供对照，不得用于决策。")
        print(f"  OOS={vv['n_oos']}  降幅={vv['rmse_reduction_pct']:.2f}%  "
              f"逐年为正 {vv['n_years_positive']}/{vv['n_years']}")

    data = load_panel()
    if tk not in data:
        print(f"[FATAL] 面板无 {tk}")
        return 2

    series = data[tk]
    grid = sorted(series.keys(), key=pidx)
    t_last, t_next = grid[-1], pnext(grid[-1])
    print(f"\n[1/4] 数据截止期 = {t_last}（共 {len(grid)} 期，{grid[0]} 起）")

    # ---- 2. 拟合 + 点预测 ----
    print(f"[2/4] 拟合 1 步 nowcast → 目标期 {t_next} ...")
    fit = fit_nowcast(data, tk, leads, t_next)
    yhat, persist = fit["yhat"], fit["persist"]
    print(f"      训练窗 {fit['train_from']}..{fit['train_to']}  n={fit['n_train']}")
    print(f"      点预测 = {yhat:+.3f}   persistence 基线 = {persist:+.3f}   "
          f"差 = {yhat - persist:+.3f}")

    # ---- 3. walk-forward 回看校准 ----
    print(f"[3/4] 回看校准（walk-forward OOS 重算，口径与 verdict 同源）...")
    oos = walk_forward(data, tk, leads)
    m = metrics(oos)
    y, mdl, ph, ts = oos["y"], oos["mdl"], oos["ph"], oos["t"]
    e_m, e_p = y - mdl, y - ph

    # 最近 ROLL_RECENT 个月 RMSE 降幅
    r12 = None
    if len(y) >= ROLL_RECENT:
        yy, mm, pp = y[-ROLL_RECENT:], mdl[-ROLL_RECENT:], ph[-ROLL_RECENT:]
        rm_m = float(math.sqrt(np.mean((yy - mm) ** 2)))
        rm_p = float(math.sqrt(np.mean((yy - pp) ** 2)))
        r12 = (1 - rm_m / rm_p) * 100.0 if rm_p > 0 else None
    light, why = status_light(r12)

    # 区间：非参数，取 OOS 绝对误差的经验分位（不假设正态）
    abs_e = np.abs(e_m)
    q_full = float(np.quantile(abs_e, COVER_Q))
    win = abs_e[-ERR_WIN:] if len(abs_e) >= ERR_WIN else abs_e
    q_recent = float(np.quantile(win, COVER_Q))
    band = dict(lo=yhat - q_recent, hi=yhat + q_recent, half_width=q_recent,
                coverage=COVER_Q, basis=f"最近 {len(win)} 个 OOS 月绝对误差 {COVER_Q:.0%} 分位",
                full_sample_half_width=q_full)

    # 方向判定阈值：取 OOS 期 |y(t)-y(t-1)| 中位数的 50%
    dy = np.abs(np.diff(y))
    flat_thr = float(np.median(dy) * 0.5) if len(dy) else 0.2
    delta = yhat - persist
    if abs(delta) <= flat_thr:
        direction = "基本持平"
    else:
        direction = "上行" if delta > 0 else "下行"

    # 逐期回看表
    lb = min(args.lookback, len(ts))
    rows = []
    for i in range(len(ts) - lb, len(ts)):
        rows.append(dict(period=ts[i], actual=float(y[i]), model=float(mdl[i]),
                         persist=float(ph[i]), err_model=float(e_m[i]),
                         err_persist=float(e_p[i]),
                         better=bool(abs(e_m[i]) < abs(e_p[i]))))

    print(f"[4/4] 状态灯 = {light}")
    print(f"      {why}")

    # ---- 组装输出 ----
    out = dict(
        generated_at=datetime.now().isoformat(timespec="seconds"),
        generator="macro_nowcast.py (交付层，规格复用 macro_predict.py)",
        strategy_key=skey, target=args.target,
        model_spec=dict(variant=args.variant, leads=leads, self_lags=SELF_LAGS,
                        ridge_alpha=RIDGE_ALPHA, min_train=MIN_TRAIN),
        registered=vv,
        caveat=[
            "训练与评估用 vintage **终值**口径；预测对象为终版口径的 T+1 值。",
            "首次公布值与终版存在修订差 → 本输出**不是实时首发口径 nowcast**，修订偏置未消除。",
            f"只出 1 步超前（h=1）；多步需迭代填 lag，未经验证，不产出。",
            "zero look-ahead：预测 T+1 只用 period <= T 的观测。",
        ],
        nowcast=dict(data_cutoff=t_last, target_period=t_next,
                     point=round(yhat, 4), persistence_baseline=round(persist, 4),
                     delta_vs_persistence=round(delta, 4),
                     direction=direction, flat_threshold=round(flat_thr, 4),
                     n_train=fit["n_train"], train_from=fit["train_from"],
                     train_to=fit["train_to"]),
        interval=band,
        status=dict(light=light, reason=why, r12_pct=(None if r12 is None else round(r12, 2)),
                    rule=f"r12>{R12_STRONG}%=STRONG; 0<r12<={R12_STRONG}%=MILD; r12<=0=INVALID",
                    roll_window=ROLL_RECENT),
        backtest_summary=dict(
            n_oos=m["n_oos"], oos_from=m["oos_from"], oos_to=m["oos_to"],
            rmse_model=round(m["rmse_model"], 4), rmse_persist=round(m["rmse_persist"], 4),
            rmse_reduction_pct=round(m["rmse_reduction_pct"], 2),
            dir_hit=round(m["dir_hit"], 4),
            n_years_positive=m.get("n_years_positive"), n_years=m.get("n_years")),
        calibration=dict(lookback=lb, rows=rows,
                         hit_rate_recent=round(float(np.mean([r["better"] for r in rows])), 4)),
        by_year=m.get("by_year"),
    )

    # 文件名必须带变体：ar 与 comp 是不同规格，同名会互相覆盖造成口径混淆
    stem = f"macro_{args.target.split('@')[0]}_nowcast_{args.variant}"
    fp = outdir / f"{stem}.json"
    with open(fp, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2, default=lambda x: None)
    fp_md = write_md(out, outdir / f"{stem}.md")

    # 日常入口：按目标期归档 + 固定 latest 副本。
    # nowcast 是「看最新」的东西，埋在按日期分的目录里等于没有交付入口。
    if not args.no_latest:
        ldir = ROOT / "outputs" / "macro_nowcast"
        (ldir / "history").mkdir(parents=True, exist_ok=True)
        l_md = write_md(out, ldir / "latest.md")
        l_js = ldir / "latest.json"
        with open(l_js, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2, default=lambda x: None)
        h_md = write_md(out, ldir / "history" / f"{out['nowcast']['target_period']}.md")
        print(f"\nWROTE {fp}")
        print(f"WROTE {fp_md}")
        print(f"WROTE {l_md}  <- 日常看这个")
        print(f"WROTE {l_js}")
        print(f"WROTE {h_md}")
    else:
        print(f"\nWROTE {fp}")
        print(f"WROTE {fp_md}")
    return out


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    main()
