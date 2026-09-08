#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
宏观指标预测 + 有效性验证框架 v2（长历史版，Route M / 宏观回溯与预判 Agent）

相对 v1 的修正（v1 结论不可信，已作废）
--------------------------------------
1. **DM 统计量公式错误（严重）**：v1 用 se=sqrt((1/n+1)*gamma0)，正确应为
   se=sqrt(gamma0/n)；导致 stat 被系统性低估 sqrt(n+1) 倍（n=283 时 ~17 倍）。
   v1 的"疑似夸大/功效不足"是统计量算错，不是真没效应。
2. **ridge 未标准化**：v1 把 alpha=1.0 直接作用于原始量纲（非农跨度 25100 vs
   CPI 10.6，差 3 个数量级），收缩强度极不均匀。v2 在**训练集内** z-score。
3. **嵌套备择未校正**：persistence 是 ridge 的嵌套特例（ridge 含 y(t-1) 特征），
   DM 对嵌套备择有偏，须用 **Clark-West (2007)** 校正作为主判据。
4. **滞后用位置索引**：v1 用 grid[ti-L]，序列有缺口时 t-1 并非日历 t-1。
   v2 改用**日历算术 + as-of 回填**（季度序列/缺口均可，且保证 period<=t-L）。

设计纪律（对照 ashare-research-SOP.md）
------------------------------------
- **zero look-ahead**：预测 Y(t) 只用 period <= t-1 的观测；lead 项 lag>=1；
  标准化统计量只从训练窗估计（严格避免前视）。
- **口径诚实**：vintage 库终值口径，修订偏置未消除；不冒充实时 nowcast。
- **SOP 三件套**：MDE + 效应量(RMSE 降幅%) + |效应|/MDE，缺一不可。
- **regime 稳健性**：A股教训（效应集中在特定年份）→ 全样本 + 2015+ + 2020+ 子样本
  一致性检查；只在部分子样本显著者不得判"可交付"。
- 四级结论：无效/无增量｜功效不足｜信息性零结果｜有效预判(可交付)｜真实但不可交付。

运行：python macro_predict.py
产物：outputs/2026-09-03/macro_predict_long.json + macro_strategy_registry.sqlite
"""
import sqlite3, json, math
from pathlib import Path
from datetime import datetime
import numpy as np
from scipy import stats

ROOT = Path(__file__).resolve().parent
while not (ROOT / "outputs").is_dir() and str(ROOT) != str(ROOT.parent):
    ROOT = ROOT.parent
VINTAGE = ROOT / "outputs" / "macro_vintage.sqlite"
OUTDIR = ROOT / "outputs" / "2026-09-03"
OUTDIR.mkdir(parents=True, exist_ok=True)
REG = ROOT / "outputs" / "macro_strategy_registry.sqlite"

MIN_TRAIN = 36              # 主口径：3 年训练窗
SELF_LAGS = [1, 2, 3]
RIDGE_ALPHA = 1.0           # 作用于 z-scored 特征
NW_LAG = 3                  # Newey-West 带宽（DM 稳健性）
ALPHA_GRID = [0.1, 1.0, 10.0]

# 目标 + 严格滞后特征（lead 项 lag>=1）
TARGETS = {
    "manufacturing_pmi@CN": dict(
        target=("manufacturing_pmi", "CN"),
        leads=[("nonmanufacturing_pmi", "CN", 1), ("ppi_yoy", "CN", 1), ("cpi_yoy", "CN", 1)]),
    "ppi_yoy@CN": dict(
        target=("ppi_yoy", "CN"),
        leads=[("manufacturing_pmi", "CN", 1), ("cpi_yoy", "CN", 1)]),
    "cpi_yoy@CN": dict(
        target=("cpi_yoy", "CN"),
        leads=[("ppi_yoy", "CN", 1), ("manufacturing_pmi", "CN", 1)]),
    "nonfarm_payroll_change@US": dict(
        target=("nonfarm_payroll_change", "US"),
        leads=[]),   # 库内无长历史 US 领先项（unemployment 仅 18 期）
}
# regime 子样本（起始期），用于一致性检查
SUBSAMPLES = {"full": None, "post2015": "2015-01", "post2020": "2020-01"}


# ---------------- 数据加载 ----------------
def load_panel():
    """(indicator,country) -> {period: value}，取权威终值（latest 已按官方一手优先）"""
    c = sqlite3.connect(str(VINTAGE))
    by_type = {}
    for ind, co, per, vt, val in c.execute(
            "SELECT indicator,country,period,value_type,value FROM latest"):
        by_type.setdefault((ind, co), {}).setdefault(vt, {})[per] = val
    c.close()
    data = {}
    for key, vts in by_type.items():
        if "reported" in vts:
            data[key] = vts["reported"]
        elif "percent_sa" in vts:
            data[key] = vts["percent_sa"]
        else:
            best = min(vts.items(), key=lambda kv: len(kv[1]))
            data[key] = best[1]
    return data


def pidx(p):
    y, m = p.split("-")
    return int(y) * 12 + int(m)


def pshift(p, k):
    """日历算术：p 往前 k 个月"""
    t = pidx(p) - 1 - k
    return f"{t // 12}-{t % 12 + 1:02d}"


def asof(series, limit_period, max_back=6):
    """返回 period <= limit_period 的最近一期的值（as-of 回填，保证无前视）。
    max_back 限制回溯月数：季度序列最多回 3 个月即可命中。"""
    p = limit_period
    for _ in range(max_back + 1):
        if p in series:
            return series[p]
        y, m = p.split("-")
        t = int(y) * 12 + int(m) - 2
        p = f"{t // 12}-{t % 12 + 1:02d}"
    return None


# ---------------- 特征 ----------------
def feat_row(data, t, tk, leads, back=6):
    """Y(t) 的严格滞后特征（全部 period <= t-1）。返回 (vec, ok)"""
    vec, ok = [], True
    for L in SELF_LAGS:
        v = asof(data[tk], pshift(t, L), back)
        if v is None:
            ok = False
            vec.append(0.0)
        else:
            vec.append(v)
    for ind, co, L in leads:
        lk = (ind, co)
        if lk not in data:
            ok = False
            vec.append(0.0)
            continue
        v = asof(data[lk], pshift(t, L), back)
        if v is None:
            ok = False
            vec.append(0.0)
        else:
            vec.append(v)
    return np.array(vec, dtype=float), ok


# ---------------- 模型（训练集内标准化，无前视） ----------------
def ridge_fit(Xtr, ytr, alpha=RIDGE_ALPHA):
    mu = Xtr.mean(axis=0)
    sd = Xtr.std(axis=0, ddof=1)
    sd[sd < 1e-12] = 1.0
    Xs = (Xtr - mu) / sd
    ybar = ytr.mean()
    A = Xs.T @ Xs + alpha * np.eye(Xs.shape[1])
    beta = np.linalg.solve(A, Xs.T @ (ytr - ybar))
    return dict(mu=mu, sd=sd, beta=beta, ybar=ybar)


def ridge_pred(m, X):
    return m["ybar"] + ((X - m["mu"]) / m["sd"]) @ m["beta"]


# ---------------- 检验 ----------------
def nw_var(d, lag):
    """Newey-West 长期方差（Bartlett 核）"""
    n = len(d)
    d = d - d.mean()
    g0 = float(np.dot(d, d) / n)
    s = g0
    for l in range(1, min(lag, n - 1) + 1):
        gl = float(np.dot(d[l:], d[:-l]) / n)
        s += 2.0 * (1.0 - l / (lag + 1.0)) * gl
    return max(s, 1e-18)


def dm_test(e_m, e_p, nw_lag=None):
    """Diebold-Mariano (HLN 小样本校正)，单侧：model 优于 persistence。
    正确公式：stat = dbar / sqrt(gamma/n)；HLN = DM * sqrt((n-1)/n)。"""
    d = (e_p ** 2) - (e_m ** 2)
    n = len(d)
    if n < 4:
        return float("nan"), float("nan")
    dbar = float(d.mean())
    g = nw_var(d, nw_lag) if nw_lag else float(np.var(d, ddof=1))
    se = math.sqrt(g / n)
    if se <= 0 or not np.isfinite(se):
        return float("nan"), float("nan")
    stat = dbar / se
    stat *= math.sqrt((n - 1) / n)          # HLN
    return float(stat), float(1.0 - stats.t.cdf(stat, df=n - 1))


def clark_west(e_m, e_p, yhat_m, yhat_p):
    """Clark-West (2007)：嵌套模型比较（persistence 嵌套于 ridge）。
    修正 DM 对嵌套备择的偏倚，是本框架的主判据。"""
    f = (e_p ** 2) - (e_m ** 2) + ((yhat_p - yhat_m) ** 2)
    n = len(f)
    if n < 4:
        return float("nan"), float("nan")
    fbar = float(f.mean())
    se = math.sqrt(float(np.var(f, ddof=1)) / n)
    if se <= 0:
        return float("nan"), float("nan")
    stat = fbar / se
    return float(stat), float(1.0 - stats.t.cdf(stat, df=n - 1))


def mde_of(d, alpha=0.05, power=0.80):
    """SOP 三件套之 MDE：在给定 n 与损失差波动下，80% 功效可检出的最小平均损失差"""
    n = len(d)
    if n < 4:
        return None, None
    se = math.sqrt(float(np.var(d, ddof=1)) / n)
    z = stats.norm.ppf(1 - alpha) + stats.norm.ppf(power)
    return float(z * se), float(se)


# ---------------- walk-forward ----------------
def walk_forward(data, tk, leads, subsample_start=None, window="expanding", roll=60,
                 winsor=None, train_exclude=None):
    """winsor=None 或 (lo_q, hi_q)：在**训练窗内**对 y 缩尾（稳健性变体，用于隔离
    COVID 类极端值；预声明的稳健性检查，**不作为主口径**）。
    train_exclude=(lo,hi)：**仅从训练集剔除**声明的结构断点区间（如 COVID 2020-03..08），
    测试集仍全量评估——允许稳健估计，但不允许跳过难题。"""
    series = data[tk]
    grid = sorted(series.keys(), key=pidx)
    if subsample_start:
        grid = [p for p in grid if p >= subsample_start]
    out = dict(t=[], y=[], mdl=[], ph=[], mn=[])
    for i in range(MIN_TRAIN, len(grid)):
        t = grid[i]
        if t not in series:
            continue
        lo = 0 if window == "expanding" else max(0, i - roll)
        Xtr, ytr = [], []
        for j in range(lo, i):
            pj = grid[j]
            if pj not in series:
                continue
            if train_exclude and train_exclude[0] <= pj <= train_exclude[1]:
                continue                      # 声明的结构断点不参与训练
            f, ok = feat_row(data, pj, tk, leads)
            if ok:
                Xtr.append(f)
                ytr.append(series[pj])
        if len(ytr) < MIN_TRAIN:
            continue
        Xtr, ytr = np.array(Xtr), np.array(ytr)
        if winsor:
            lo, hi = np.quantile(ytr, winsor[0]), np.quantile(ytr, winsor[1])
            ytr = np.clip(ytr, lo, hi)     # 缩尾在训练窗内估计，无前视
        m = ridge_fit(Xtr, ytr)
        f, ok = feat_row(data, t, tk, leads)
        if not ok:
            continue
        yhat = float(ridge_pred(m, f.reshape(1, -1))[0])
        yt = series[t]
        out["t"].append(t)
        out["y"].append(yt)
        out["mdl"].append(yhat)
        out["ph"].append(float(ytr[-1]))     # persistence = 训练窗最后一期（≈ y(t-1)）
        out["mn"].append(float(ytr.mean()))
    for k in ("y", "mdl", "ph", "mn"):
        out[k] = np.array(out[k], dtype=float)
    return out


def metrics(oos):
    y, mdl, ph, mn = oos["y"], oos["mdl"], oos["ph"], oos["mn"]
    n = len(y)
    if n < 8:
        return None
    rmse = lambda a: float(math.sqrt(np.mean((y - a) ** 2)))
    e_m, e_p = y - mdl, y - ph
    sse_m = float(np.sum(e_m ** 2))
    sse_p = float(np.sum(e_p ** 2))
    r2 = 1 - sse_m / sse_p if sse_p > 0 else float("nan")
    pred_chg, act_chg = mdl - ph, y - ph
    hit = float(np.mean(np.sign(pred_chg) == np.sign(act_chg)))
    n_dir = int(np.sum(np.sign(pred_chg) == np.sign(act_chg)))
    try:
        dir_p = float(stats.binomtest(n_dir, n, 0.5, alternative="greater").pvalue)
    except Exception:
        dir_p = float("nan")
    # 逐年分解（A股教训：效应若集中在少数年份 = fragile，不得判可交付）
    by_year = {}
    for yr in sorted({p[:4] for p in oos["t"]}):
        idx = [i for i, p in enumerate(oos["t"]) if p[:4] == yr]
        if len(idx) < 6:
            continue
        yy, mm, pp = y[idx], mdl[idx], ph[idx]
        rm_m = float(math.sqrt(np.mean((yy - mm) ** 2)))
        rm_p = float(math.sqrt(np.mean((yy - pp) ** 2)))
        by_year[yr] = dict(n=len(idx), rmse_model=rm_m, rmse_persist=rm_p,
                           reduction_pct=100.0 * (1 - rm_m / rm_p) if rm_p > 0 else float("nan"))
    d = (e_p ** 2) - (e_m ** 2)
    mde, se_d = mde_of(d)
    dbar = float(d.mean())
    dm_s, dm_p = dm_test(e_m, e_p)
    dm_s_nw, dm_p_nw = dm_test(e_m, e_p, nw_lag=NW_LAG)
    cw_s, cw_p = clark_west(e_m, e_p, mdl, ph)
    return dict(
        n_oos=n, oos_from=oos["t"][0], oos_to=oos["t"][-1],
        rmse_model=rmse(mdl), rmse_persist=rmse(ph), rmse_mean=rmse(mn),
        mae_model=float(np.mean(np.abs(e_m))), mae_persist=float(np.mean(np.abs(e_p))),
        rmse_reduction_pct=100.0 * (1 - rmse(mdl) / rmse(ph)) if rmse(ph) > 0 else float("nan"),
        oos_r2_vs_persist=r2, dir_hit=hit, dir_p=dir_p,
        dm_stat=dm_s, dm_p=dm_p, dm_stat_nw=dm_s_nw, dm_p_nw=dm_p_nw,
        cw_stat=cw_s, cw_p=cw_p,
        mean_loss_diff=dbar, mde_80=mde, effect_over_mde=(abs(dbar) / mde if mde else float("nan")),
        by_year=by_year,
        n_years_positive=sum(1 for v in by_year.values() if v["reduction_pct"] > 0),
        n_years=len(by_year))


def verdict_of(m, sub_sig):
    """四级结论 + regime 检查。sub_sig: {子样本名: 是否显著}"""
    if m is None:
        return "功效不足(OOS<8)", ""
    n = m["n_oos"]
    red = m["rmse_reduction_pct"]
    sig = (m["cw_p"] is not None and not math.isnan(m["cw_p"]) and m["cw_p"] <= 0.05) \
        and (m["dm_p_nw"] is not None and not math.isnan(m["dm_p_nw"]) and m["dm_p_nw"] <= 0.10)
    why = []
    if n < 30:
        why.append(f"OOS={n}<30 独立观测门槛")
    if sig:
        why.append(f"CW p={m['cw_p']:.4f}<=0.05 且 DM-NW p={m['dm_p_nw']:.4f}<=0.10")
    else:
        why.append(f"CW p={m['cw_p']:.4f} / DM-NW p={m['dm_p_nw']:.4f} 未达显著")
    eom = m["effect_over_mde"]
    if eom is not None and not math.isnan(eom):
        why.append(f"|效应|/MDE={eom:.2f}")

    if red <= 0:
        return "无效/无增量(相对persistence无改进)", "; ".join(why)
    if not sig:
        if eom is not None and not math.isnan(eom) and eom < 1:
            return "功效不足(效应低于MDE,样本不足以分辨)", "; ".join(why)
        return "信息性零结果(功效足够但无真实效应)", "; ".join(why)
    # 已显著 → 逐年一致性（A股教训：效应集中在少数年份 = fragile）
    ny, npos = m.get("n_years", 0), m.get("n_years_positive", 0)
    if ny >= 5:
        frac = npos / ny
        why.append(f"逐年为正 {npos}/{ny}={frac:.2f}")
        if frac < 0.60:
            return "真实但不可交付(效应集中在少数年份)", "; ".join(why)
    # 已显著 → 检查 regime 一致性
    tested = [k for k, v in sub_sig.items() if v is not None]
    if tested and not all(sub_sig[k] for k in tested):
        fail = [k for k in tested if not sub_sig[k]]
        return "真实但不可交付(regime依赖:子样本不一致)", "; ".join(why + [f"失效子样本={fail}"])
    if red < 5.0:
        return "真实但不可交付(显著但经济意义不足<5%)", "; ".join(why)
    if n < 30:
        return "真实但不可交付(样本不足30)", "; ".join(why)
    return "有效预判(可交付边界内)", "; ".join(why)


# ---------------- 主流程 ----------------
def main():
    data = load_panel()
    results = {}
    for name, spec in TARGETS.items():
        tk = spec["target"]
        if tk not in data:
            print(f"[skip] {name}: 无面板")
            continue
        res = dict(target=list(tk), n_periods=len(data[tk]), variants={})
        for leads, tag in [(spec["leads"], "composite"), ([], "ar_only")]:
            if tag == "ar_only" and not spec["leads"]:
                continue      # 无领先项时 composite == ar_only，跳过重复
            sub_metrics, sub_sig = {}, {}
            for sname, sstart in SUBSAMPLES.items():
                oos = walk_forward(data, tk, leads, subsample_start=sstart)
                m = metrics(oos)
                sub_metrics[sname] = m
                sub_sig[sname] = (None if m is None else
                                  (m["cw_p"] is not None and not math.isnan(m["cw_p"])
                                   and m["cw_p"] <= 0.05))
            m = sub_metrics["full"]
            v, why = verdict_of(m, {k: sub_sig[k] for k in ("post2015", "post2020")})
            # 滚动窗稳健性
            roll = metrics(walk_forward(data, tk, leads, window="rolling", roll=60))
            # 缩尾稳健性（预声明）：隔离极端值 regime 对线性模型的破坏；非主口径
            wz = metrics(walk_forward(data, tk, leads, winsor=(0.01, 0.99)))
            # 结构断点敏感性：COVID 期不参与训练，测试仍全量（预声明，非主口径）
            ex = metrics(walk_forward(data, tk, leads, train_exclude=("2020-03", "2020-08")))
            res["variants"][tag] = dict(model=f"ridge(alpha={RIDGE_ALPHA},训练集内标准化)",
                                        min_train=MIN_TRAIN, metrics_full=m,
                                        metrics_subsamples={k: sub_metrics[k] for k in ("post2015", "post2020")},
                                        metrics_roll60=roll, metrics_winsor99=wz, metrics_excl_covid=ex,
                                        verdict=v, verdict_basis=why)
            if m:
                print(f"\n=== {name} [{tag}] === 全样本期数={len(data[tk])} OOS={m['n_oos']} ({m['oos_from']}..{m['oos_to']})")
                print(f"  RMSE model={m['rmse_model']:.4f} persist={m['rmse_persist']:.4f} "
                      f"mean={m['rmse_mean']:.4f}  降幅={m['rmse_reduction_pct']:.2f}%")
                print(f"  OOS R^2={m['oos_r2_vs_persist']:.4f}  方向命中={m['dir_hit']:.3f} (p={m['dir_p']:.4f})")
                print(f"  CW stat={m['cw_stat']:.3f} p={m['cw_p']:.4f} | "
                      f"DM stat={m['dm_stat']:.3f} p={m['dm_p']:.4f} | DM-NW p={m['dm_p_nw']:.4f}")
                print(f"  MDE(80%)={m['mde_80']:.4f}  |效应|/MDE={m['effect_over_mde']:.2f}")
                for s in ("post2015", "post2020"):
                    sm = sub_metrics[s]
                    if sm:
                        print(f"    [{s}] OOS={sm['n_oos']:3d} 降幅={sm['rmse_reduction_pct']:6.2f}% "
                              f"CW p={sm['cw_p']:.4f} 命中={sm['dir_hit']:.2f}")
                if roll:
                    print(f"    [roll60] OOS={roll['n_oos']:3d} 降幅={roll['rmse_reduction_pct']:6.2f}% "
                          f"CW p={roll['cw_p']:.4f}")
                if wz:
                    print(f"    [winsor1-99] OOS={wz['n_oos']:3d} 降幅={wz['rmse_reduction_pct']:6.2f}% "
                          f"CW p={wz['cw_p']:.4f} 命中={wz['dir_hit']:.2f}")
                if ex:
                    print(f"    [excl-COVID训练] OOS={ex['n_oos']:3d} 降幅={ex['rmse_reduction_pct']:6.2f}% "
                          f"CW p={ex['cw_p']:.4f} DM-NW p={ex['dm_p_nw']:.4f} 命中={ex['dir_hit']:.2f}")
                if ny := m.get("n_years", 0):
                    print(f"    逐年降幅(为正 {m['n_years_positive']}/{ny}): " + "  ".join(
                        f"{y}:{v['reduction_pct']:+.0f}%" for y, v in sorted(m["by_year"].items())))
                if m and m["rmse_reduction_pct"] < 0:
                    oos = walk_forward(data, tk, leads)
                    err = np.abs(oos["y"] - oos["mdl"])
                    idx = np.argsort(-err)[:5]
                    print("    最大误差期(期,实际,预测,persist):")
                    for i in idx:
                        print(f"      {oos['t'][i]}  y={oos['y'][i]:10.2f}  "
                              f"yhat={oos['mdl'][i]:10.2f}  persist={oos['ph'][i]:10.2f}")
            print(f"  VERDICT: {v}\n    basis: {why}")
        results[name] = res

    out = dict(generated_at=datetime.now().isoformat(timespec="seconds"),
               framework="macro_predict v2 (长历史)",
               fixes_vs_v1=["DM公式修正(se=sqrt(g/n))", "ridge训练集内标准化",
                            "嵌套备择Clark-West主判据", "日历算术滞后+as-of回填"],
               discipline="zero look-ahead(特征 period<=t-1); 标准化统计量仅取训练窗; 终值口径(修订偏置未消除)",
               params=dict(min_train=MIN_TRAIN, self_lags=SELF_LAGS, ridge_alpha=RIDGE_ALPHA, nw_lag=NW_LAG),
               targets=results)
    fp = OUTDIR / "macro_predict_long.json"
    with open(fp, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2,
                  default=lambda x: None if (isinstance(x, float) and math.isnan(x)) else x)
    print(f"\nWROTE {fp}")

    # ---- 策略注册表 ----
    reg = sqlite3.connect(str(REG))
    for t in ("pool_registry", "strategy_registry", "strategy_evidence"):
        reg.execute(f"DROP TABLE IF EXISTS {t}")
    reg.execute("""CREATE TABLE pool_registry (
        pool_key TEXT PRIMARY KEY, label TEXT, n_indicators INTEGER,
        date_from TEXT, date_to TEXT, notes TEXT)""")
    reg.execute("""CREATE TABLE strategy_registry (
        strategy_key TEXT PRIMARY KEY, label TEXT, pool_key TEXT, target TEXT,
        model TEXT, min_train INTEGER, n_oos INTEGER, n_periods INTEGER,
        oos_from TEXT, oos_to TEXT,
        rmse_model REAL, rmse_persist REAL, rmse_reduction_pct REAL,
        oos_r2_vs_persist REAL, dir_hit REAL, dir_p REAL,
        cw_stat REAL, cw_p REAL, dm_stat REAL, dm_p REAL, dm_p_nw REAL,
        mde_80 REAL, effect_over_mde REAL,
        n_years INTEGER, n_years_positive INTEGER,
        verdict TEXT, verdict_basis TEXT, validated_at TEXT)""")
    reg.execute("CREATE TABLE strategy_evidence (strategy_key TEXT, gate TEXT, artifact TEXT, note TEXT)")
    reg.execute("INSERT OR REPLACE INTO pool_registry VALUES (?,?,?,?,?,?)",
                ("macro_cn_us_long", "宏观指标池(CN+US,月度,长历史)", 17, "2006-01", "2026-08",
                 "macro_vintage.sqlite: 每日快照(nbs/bls/fred_csv官方) + macro_backfill_cn.sqlite"
                 "(eastmoney第三方,2006-01起); latest 按权威度取官方一手优先; 终值口径"))
    now = datetime.now().isoformat(timespec="seconds")
    for name, r in results.items():
        base = "macro_" + name.split("@")[0]
        for tag, vr in r["variants"].items():
            m = vr["metrics_full"]
            if not m:
                continue
            sk = f"{base}_{'comp' if tag == 'composite' else 'ar'}"
            reg.execute("""INSERT OR REPLACE INTO strategy_registry VALUES
                (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (sk, f"宏观·{name} {tag}", "macro_cn_us_long", name, vr["model"],
                 vr["min_train"], m["n_oos"], r["n_periods"], m["oos_from"], m["oos_to"],
                 m["rmse_model"], m["rmse_persist"], m["rmse_reduction_pct"],
                 m["oos_r2_vs_persist"], m["dir_hit"], m["dir_p"],
                 m["cw_stat"], m["cw_p"], m["dm_stat"], m["dm_p"], m["dm_p_nw"],
                 m["mde_80"], m["effect_over_mde"],
                 m.get("n_years"), m.get("n_years_positive"),
                 vr["verdict"], vr["verdict_basis"], now))
            reg.execute("INSERT OR REPLACE INTO strategy_evidence VALUES (?,?,?,?)",
                        (sk, "oospredict", str(fp).replace("\\", "/"),
                         "walk-forward OOS(v2,DM修正+CW主判据+MDE)"))
    reg.commit()
    reg.close()
    print(f"WROTE {REG}")


if __name__ == "__main__":
    main()
