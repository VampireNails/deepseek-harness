#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
方向 1 · 关卡③：经济可行性 —— 波动率预测能否变成**可执行的仓位决策**？

【前两关的结论与缺口】
  关卡① GO：波动率可预测且功效充足（|效应|/MDE 1.87~5.22）。
  关卡② 通过：HAR 模型显著打败 persistence（h=20：RMSE −11.9%、QLIKE −24.5%，
              CW p≈0、NW-DM p≈0、逐年 10/10）。
  ⇒ 缺口：**统计显著 ≠ 能赚钱**。关卡②的效应单位是「对数波动率的 MSE」，
     用户拿它做不了任何决策。关卡③把它翻译成仓位，并测风险调整后收益。

【为什么是"市场层"为主口径】
  关卡①发现②：ρ̄=0.17~0.32 ⇒ K_eff≈4/636，可预测性主要来自**市场共同波动**，
  而非个股特质 ⇒ 交付形态更可能是「市场波动状态 → 总仓位」，个股层增量须单独验。

【组合（块频，h=20 交易日）】
  EQ    等权满仓（基准）
  VT_P  市场层波动率择时：w_t = clip(target/σ̂_mkt,t, 0, cap)，σ̂ 用 persistence
  VT_M  同上，σ̂ 用 HAR 模型预测
  IV_P  个股层逆波动加权：w_i ∝ 1/σ̂_i,t（横截面归一化），σ̂ 用 persistence
  IV_M  同上，σ̂ 用模型
  现金收益取 0（保守：惩罚 VT，不给它白捡的无风险收益）。

【关键：波动匹配后的均值检验】
  VT 会降低波动，直接比**绝对收益**必然吃亏；直接比 **Sharpe** 又无法做 DM 检验。
  解法：把 A 序列按 s = sd(B)/sd(A) 放大到与 B 同波动，再对
       d_t = s·r_A,t − r_B,t
  做 NW(HAC) 单侧 t 检验。**这在数学上等价于比较 Sharpe，但可做推断。**
  ⚠️ 诚实边界：放大仓位需要加杠杆，A 股散户难以实现。所以
     「Sharpe 提升」的正确交付语义是**风险/回撤下降**，不是"收益提高"。

【费率】沿用 SOP §二：往返 0.30 / 0.50 / 0.80%，cost = 换手 × 费率。
【判据】主检验 NW p<=0.10 且 |效应|/MDE>=1 且 逐年为正>=60% ⇒ 通过关卡③
"""
from __future__ import annotations

import argparse
import json
import sys
import warnings
from datetime import datetime
from pathlib import Path

import numpy as np
from scipy import stats as st

ROOT = Path(__file__).resolve().parents[4]
assert (ROOT / "outputs").is_dir(), f"ROOT 推算错误: {ROOT}"
sys.path.insert(0, str(Path(__file__).resolve().parent))

from ashare_hfq_access import load_hfq_panel                      # noqa: E402
from ashare_risk_preflight import build_blocks                    # noqa: E402
from ashare_vol_forecast import (build_feature_tensor, W_SHORT,   # noqa: E402
                                 W_LONG, SPEC, nw_lag_of)
from macro_predict import (nw_var, mde_of, ridge_fit, ridge_pred)  # noqa: E402

DEFAULT_DB = ROOT / "outputs" / "ashare_csi800_hfq_xq.sqlite"
OUT_DIR = ROOT / "outputs" / datetime.now().strftime("%Y-%m-%d")
FEE_TIERS = [0.0030, 0.0050, 0.0080]      # 往返
TRADING_DAYS = 252


# ---------------------------------------------------------------- 预测矩阵
def forecast_matrix(L: np.ndarray, mk: np.ndarray, cols: list,
                    min_train: int) -> np.ndarray:
    """走前（扩张窗）ridge 预测，返回 Yhat: (K, n_blocks)，仅 OOS 期有值。

    与 ashare_vol_forecast.run_horizon 同一规格（同特征、同 ridge、同扩张窗），
    只是把「每期聚合损失」换成「逐个股预测值」用于建仓。"""
    K, n_blocks = L.shape
    F = build_feature_tensor(L, mk)
    Yh = np.full((K, n_blocks), np.nan)
    s_lo, s_hi = W_LONG - 1, n_blocks - 2
    X_parts, y_parts = [], []
    for s in range(s_lo, s_hi + 1):
        X = F[:, s, cols]
        yv = L[:, s + 1]
        ok = np.isfinite(X).all(axis=1) & np.isfinite(yv)
        if ok.sum() < 50:
            X_parts.append(np.zeros((0, len(cols))))
            y_parts.append(np.zeros(0))
            continue
        X_parts.append(X[ok])
        y_parts.append(yv[ok])
    cum = np.cumsum([p.size for p in y_parts])
    Xall, yall = np.vstack(X_parts), np.concatenate(y_parts)

    for t in range(min_train, n_blocks - 1):
        j = (t - 1) - s_lo
        if j < 0:
            continue
        ntr = int(cum[j])
        if ntr < 200:
            continue
        mdl = ridge_fit(Xall[:ntr], yall[:ntr])
        Xt = F[:, t, cols]
        ok = np.isfinite(Xt).all(axis=1)
        if ok.sum() < 50:
            continue
        Yh[ok, t + 1] = ridge_pred(mdl, Xt[ok])
    return Yh


def forecast_series(lb: np.ndarray, min_train: int) -> np.ndarray:
    """对**单条**对数波动序列做走前 ridge(HAR)，返回 ŷ（仅 OOS 有值）。

    与个股面板版的区别：这里只有一条时序（篮子波动），
    不存在横截面相关 ⇒ K_eff 问题消失，n_eff = 期数，与关卡①的保守口径一致。"""
    n = len(lb)
    F = np.column_stack([lb,
                         _roll_mean(lb, W_SHORT),
                         _roll_mean(lb, W_LONG)])
    Yh = np.full(n, np.nan)
    for t in range(min_train, n - 1):
        xs, ys = [], []
        for s in range(W_LONG - 1, t):
            if np.isfinite(F[s]).all() and np.isfinite(lb[s + 1]):
                xs.append(F[s])
                ys.append(lb[s + 1])
        if len(ys) < 20:
            continue
        mdl = ridge_fit(np.asarray(xs), np.asarray(ys))
        if np.isfinite(F[t]).all():
            Yh[t + 1] = float(ridge_pred(mdl, F[t].reshape(1, -1))[0])
    return Yh


def _roll_mean(x: np.ndarray, w: int) -> np.ndarray:
    out = np.full(len(x), np.nan)
    for i in range(len(x)):
        seg = x[max(0, i - w + 1): i + 1]
        out[i] = np.nanmean(seg) if np.isfinite(seg).any() else np.nan
    return out


def basket_block_rv(close: np.ndarray, h: int, n_blocks: int) -> np.ndarray:
    """等权篮子自身的块已实现波动 —— **这才是要控制的量**。

    ⚠️ 不能用「个股波动的横截面中位数」代替：篮子因分散化，其波动远低于个股中位数波动，
    拿个股中位数去做篮子择时，控制对象从一开始就搞错了。"""
    with np.errstate(invalid="ignore", divide="ignore"):
        ret = close[:, 1:] / close[:, :-1] - 1.0
    num = np.nansum(np.where(np.isfinite(ret), ret, np.nan), axis=0)
    den = np.sum(np.isfinite(ret), axis=0)
    with np.errstate(invalid="ignore", divide="ignore"):
        rb = num / np.where(den > 0, den, np.nan)      # 长度 T-1，rb[d]=第 d 日收益
    T = close.shape[1]
    out = np.full(n_blocks, np.nan)
    for t in range(n_blocks):
        lo, hi = t * h, min((t + 1) * h, T - 1)
        seg = rb[max(0, lo - 1): hi - 1]               # 块内日收益
        seg = seg[np.isfinite(seg)]
        if seg.size >= max(3, h // 3):
            out[t] = float(np.sqrt(np.sum(seg ** 2)))
    return out


def block_simple_returns(close: np.ndarray, h: int, n_blocks: int) -> np.ndarray:
    """块简单收益 R: (K, n_blocks)。缺任一端点即为 NaN（面板是 NaN 不填充）。"""
    T = close.shape[1]
    beg = np.arange(n_blocks) * h
    end = np.minimum(beg + h, T - 1)
    with np.errstate(invalid="ignore", divide="ignore"):
        R = close[:, end] / close[:, beg] - 1.0
    R[:, end == beg] = np.nan                      # 末块长度为 0
    return np.where(np.isfinite(R), R, np.nan)


# ---------------------------------------------------------------- 绩效指标
def perf(r: np.ndarray, h: int, fee: float, turn: np.ndarray | None) -> dict:
    """r 为毛收益序列；turn 为同期换手序列（可选，NaN 视为 0）。"""
    py = TRADING_DAYS / h
    r = np.asarray(r, dtype=float)
    tv = (np.zeros_like(r) if turn is None
          else np.asarray(turn, dtype=float))
    n = min(len(r), len(tv))
    r, tv = r[:n], np.nan_to_num(tv[:n], nan=0.0)
    # ⚠️ 必须**先按同一 mask 过滤，再扣成本**。
    #    旧写法 `r = r[isfinite(r)]` 之后再 `turn[:n]*fee`：
    #    r 已丢掉 NaN 而 turn 没有 ⇒ 收益与换手错位（h=60 上把 VT_B 的 CAGR 打到 9.45%）。
    m = np.isfinite(r)
    r_net = (r - tv * fee)[m]
    tv = tv[m]
    turn_ann = float(np.mean(tv)) * py
    mu = float(np.mean(r_net))
    sd = float(np.std(r_net, ddof=1))
    curve = np.cumprod(1.0 + r_net)
    peak = np.maximum.accumulate(curve)
    mdd = float(np.max(1.0 - curve / peak))
    cagr = float(curve[-1] ** (py / len(r_net)) - 1.0) if curve[-1] > 0 else float("nan")
    return {
        # ⚠️ 必须是 len(r_net)（过滤后的实际样本数），不是 len(r)。
        #    旧写法返回切片后的未过滤长度 ⇒ 报表显示 n=26 而统计量只用了 19 期。
        "n": int(len(r_net)),
        "cagr": cagr,
        "ann_vol": sd * float(np.sqrt(py)),
        "sharpe": (mu / sd * float(np.sqrt(py))) if sd > 0 else float("nan"),
        "max_drawdown": mdd,
        "calmar": (cagr / mdd) if mdd > 0 else float("nan"),
        "turnover_ann_x": turn_ann,
        "cost_drag_ann": turn_ann * fee,
    }


def lev_matched_test(r_a: np.ndarray, r_b: np.ndarray, h: int,
                     years: np.ndarray) -> dict:
    """把 A 放大到与 B 同波动，对 d = s·r_A − r_B 做 NW 单侧检验（A 优于 B）。"""
    a, b = np.asarray(r_a, float), np.asarray(r_b, float)
    m = np.isfinite(a) & np.isfinite(b)
    a, b, yv = a[m], b[m], np.asarray(years)[m]
    sa, sb = float(np.std(a, ddof=1)), float(np.std(b, ddof=1))
    if sa <= 0 or len(a) < 12:
        return {"error": "样本不足或零波动"}
    s = sb / sa
    d = s * a - b
    n = len(d)
    lag = nw_lag_of(n)
    dbar = float(d.mean())
    se = float(np.sqrt(nw_var(d, lag) / n))
    if se <= 0:
        return {"error": "se=0"}
    stat = dbar / se
    p = float(1.0 - st.t.cdf(stat, df=n - 1))
    mde, _ = mde_of(d)
    py = TRADING_DAYS / h
    per_year = {}
    for yy in np.unique(yv):
        mm = yv == yy
        per_year[str(int(yy))] = bool(d[mm].mean() > 0)
    n_pos = sum(1 for v in per_year.values() if v)
    return {
        "scale_a": s, "n": n, "nw_lag": int(lag),
        "mean_diff_ann_pct": dbar * py * 100.0,
        "stat": float(stat), "p_nw_one_sided": p,
        "mde_ann_pct": (mde * py * 100.0) if mde else float("nan"),
        "effect_over_mde": (abs(dbar) / mde) if mde else float("nan"),
        "year_positive": f"{n_pos}/{len(per_year)}",
        "year_ratio": n_pos / max(1, len(per_year)),
        "yearly": per_year,
    }


# ---------------------------------------------------------------- 主流程
def run(h: int, R: np.ndarray, L: np.ndarray, mk: np.ndarray,
        Yh_m: np.ndarray, years: np.ndarray, cap: float,
        min_train: int, rv_b: np.ndarray | None = None,
        yh_b: np.ndarray | None = None, test_fee: float = 0.0050) -> dict:
    K, n_blocks = L.shape
    py = TRADING_DAYS / h

    # ---- 目标波动：扩张窗均值（无前视）----
    sig_mkt = np.exp(mk)                                   # 市场已实现波动（中位数）
    target = np.full(n_blocks, np.nan)
    for t in range(1, n_blocks):
        seg = sig_mkt[:t]
        seg = seg[np.isfinite(seg)]
        if seg.size >= max(8, min_train // 2):
            target[t] = float(np.mean(seg))

    # ---- ⚠️ persistence 基准必须"滞后一块"，否则是前视 ----
    #      L[:, t] = 第 t 块的已实现波动，**块末才可知**；
    #      而 Yh[:, t] = t−1 时刻对第 t 块的预测。若两侧直接用同一个 t 索引，
    #      模型侧用的是 t−1 信息、persistence 侧用的是 t 的同期实现值
    #      ⇒ 信息集不对齐，任何"模型增量"都会被这个不对称污染。
    #      （2026-09-04 实测：修正前 IV_M vs IV_P 年化 +8.72%，修正后见最新日志。）
    persist = np.full_like(L, np.nan)
    persist[:, 1:] = L[:, :-1]

    # ---- 各组合的权重/敞口 ----
    out = {}
    ret, turn = {}, {}

    # EQ：等权，无换手成本（块间个股进出会带来换手，故按成分变化计换手）
    w_eq = np.where(np.isfinite(R), 1.0, np.nan)
    cnt = np.nansum(np.isfinite(R), axis=0)
    w_eq = w_eq / np.where(cnt > 0, cnt, np.nan)
    r_eq = np.nansum(w_eq * R, axis=0)
    ret["EQ"] = r_eq
    turn["EQ"] = np.zeros(n_blocks)          # 等权的换手在下方统一按成分变化计

    # VT_*：市场层敞口（两侧 σ̂ 均为 t−1 时刻对第 t 块的估计）
    for tag, sig_hat in (("VT_P", np.exp(persist)), ("VT_M", np.exp(Yh_m))):
        # 用横截面中位数作为"市场预测波动"，与关卡②的 mk 定义一致
        s_hat = np.nanmedian(sig_hat, axis=0)
        with np.errstate(invalid="ignore", divide="ignore"):
            w = np.clip(target / s_hat, 0.0, cap)
        w[~np.isfinite(w)] = np.nan
        r = w * r_eq
        tv = np.abs(np.diff(np.nan_to_num(w, nan=0.0), prepend=0.0))
        ret[tag], turn[tag] = r, tv

    # ---- VT_B*：篮子层敞口（**这才是正确的控制对象**）----
    #      VT_M 用「个股波动的横截面中位数」去缩放**篮子**敞口，控制对象错了：
    #      篮子因分散化，其波动远低于个股中位数波动。改预测篮子自身波动。
    if rv_b is not None:
        tg_b = np.full(n_blocks, np.nan)
        for t in range(1, n_blocks):
            seg = rv_b[:t]
            seg = seg[np.isfinite(seg)]
            if seg.size >= max(8, min_train // 2):
                tg_b[t] = float(np.mean(seg))
        pers_b = np.full(n_blocks, np.nan)          # 同样滞后一块，防前视
        pers_b[1:] = rv_b[:-1]
        cands = [("VT_BP", pers_b)]
        if yh_b is not None:
            cands.append(("VT_B", np.exp(yh_b)))
        for tag, sh in cands:
            with np.errstate(invalid="ignore", divide="ignore"):
                w = np.clip(tg_b / sh, 0.0, cap)
            w[~np.isfinite(w)] = np.nan
            ret[tag] = w * r_eq
            turn[tag] = np.abs(np.diff(np.nan_to_num(w, nan=0.0), prepend=0.0))

    # IV_*：个股层逆波动加权（横截面归一化，总和=1）
    for tag, sig_hat in (("IV_P", np.exp(persist)), ("IV_M", np.exp(Yh_m))):
        inv = 1.0 / np.where(sig_hat > 0, sig_hat, np.nan)
        inv = np.where(np.isfinite(R), inv, np.nan)
        den = np.nansum(inv, axis=0)
        w = inv / np.where(den > 0, den, np.nan)
        r = np.nansum(w * R, axis=0)
        tv = np.nansum(np.abs(np.diff(np.nan_to_num(w, nan=0.0),
                                      prepend=0.0, axis=1)), axis=0)
        ret[tag], turn[tag] = r, tv

    # EQ 的真实换手：等权篮子在块间的成分进出
    tv_eq = []
    prev = None
    for t in range(n_blocks):
        w = np.where(np.isfinite(R[:, t]), 1.0, 0.0)
        c = w.sum()
        w = w / c if c > 0 else w
        tv_eq.append(float(np.abs(w - prev).sum()) if prev is not None else 0.0)
        prev = w
    turn["EQ"] = np.asarray(tv_eq)

    # ---- 共同 OOS 样本（★ 不这么做绩效表就是误导）----
    #    各组合的**有效起期不同**：模型侧要训练热身（h=60 上 Yh 前 7 个 OOS 块为 NaN），
    #    persistence 侧从第 1 块就有值。若各自按自己的有效样本汇报，
    #    绩效表实际是在**不同时间窗**上比较不同组合 ——
    #    实测 h=60：VT_B 只覆盖块 32~50（19 期），恰好漏掉块 25~31 的大涨段
    #    （+2.2%/+24.6%/+2.3%/+2.2%/+5.7%/+10.1%/+4.6%），
    #    CAGR 被压到 9.42% vs VT_BP 15.19%，**看起来像策略差，其实是样本窗口差**。
    #    ⇒ 一律取所有组合都有效的交集。
    oos = np.zeros(n_blocks, dtype=bool)
    oos[min_train + 1:] = True
    oos &= np.isfinite(r_eq)
    for k in ret:
        oos &= np.isfinite(ret[k])
    yv = years[oos]

    # ---- 推断一律用**扣费后**净收益 ----
    #    ⚠️ 旧版检验用毛收益（ret），绩效表用净收益 ⇒ 检验完全忽略交易成本。
    #      在 h=5 上这是致命的：VT_BP 换手 3.92x/年 vs VT_B 0.66x/年，
    #      毛收益口径下两者几乎无差，扣费后差距才是真实可得的。
    net = {k: ret[k] - np.nan_to_num(turn[k], nan=0.0) * test_fee
           for k in ret}
    for fee in FEE_TIERS:
        for k in ret:
            out[f"{k}|fee{fee:.4f}"] = perf(ret[k][oos], h, fee, turn[k][oos])

    # 预注册主检验：★VT_B vs EQ（篮子层波动率择时是否提升风险调整收益）。
    #   ⚠️ 主检验曾在 v1 定为 VT_M vs EQ，后经**推理**（非看结果）发现控制对象写错
    #      （用个股波动中位数缩放篮子敞口），在读取 VT_B 结果之前即改为主检验。
    #      其余为二级/探索性，只用于定位"增量来自哪一层"，二级显著不算成功。
    tests = {}
    pairs = {
        "VT_M vs EQ（个股中位数波动择时，控制对象有缺陷）":      ("VT_M", "EQ"),
        "VT_P vs EQ（persistence 市场层择时是否已足够）":       ("VT_P", "EQ"),
        "VT_M vs VT_P（增量：模型是否优于 persistence）":       ("VT_M", "VT_P"),
        "IV_M vs IV_P（个股层增量：模型 vs persistence）":      ("IV_M", "IV_P"),
        "IV_M vs EQ（个股层逆波动加权是否优于等权）":           ("IV_M", "EQ"),
        "IV_P vs EQ（persistence 逆波动加权是否优于等权）":     ("IV_P", "EQ"),
    }
    if rv_b is not None:
        pairs = {
            "★VT_B vs EQ（主：篮子层波动择时是否提升风险调整收益）": ("VT_B", "EQ"),
            "VT_B vs VT_BP（篮子层增量：模型 vs persistence）":     ("VT_B", "VT_BP"),
            "VT_BP vs EQ（篮子层 persistence 择时是否已足够）":      ("VT_BP", "EQ"),
            **pairs,
        }
    for name, (a, b) in pairs.items():
        if a in ret and b in ret:
            tests[name] = lev_matched_test(net[a][oos], net[b][oos], h, yv)

    return {"perf": out, "tests": tests, "oos_periods": int(oos.sum()),
            "test_fee": test_fee,
            "target_vol_mean": float(np.nanmean(target)),
            "target_vol_basket_mean": (float(np.nanmean(rv_b))
                                       if rv_b is not None else None)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--since", default="2014-01-02")
    ap.add_argument("--horizons", default="20")
    ap.add_argument("--min-train", type=int, default=40)
    ap.add_argument("--min-valid-frac", type=float, default=0.25)
    ap.add_argument("--cap", type=float, default=1.0,
                    help="敞口上限（1.0=不加杠杆，A 股不可做空 ⇒ 下限 0）")
    ap.add_argument("--model", default="M2_HAR_mkt")
    ap.add_argument("--test-fee", type=float, default=0.0050,
                    help="检验所用的往返费率（默认 0.50%%，中档）")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    panel = load_hfq_panel(Path(args.db), since=args.since, apply_qc=True,
                           drop_first_n_days=5)
    print("=" * 78)
    print("方向 1 · 关卡③：波动率预测 → 仓位（经济可行性）")
    print("=" * 78)
    print(f"库: {Path(args.db).name}   QC 剔除 {panel.n_bad} 只   "
          f"面板 {len(panel.codes)} 只 × {len(panel.dates)} 日")
    print(f"敞口上限 cap={args.cap}（不加杠杆、不可做空）  "
          f"费率往返 {[f'{f:.2%}' for f in FEE_TIERS]}")

    all_res = {}
    for h in [int(x) for x in args.horizons.split(",")]:
        rv, _ = build_blocks(panel.close, h, panel.codes, False)
        n_blocks = rv.shape[1]
        ok = np.mean(np.isfinite(rv), axis=1) >= args.min_valid_frac
        rv, close = rv[ok], panel.close[ok]
        with np.errstate(divide="ignore", invalid="ignore"):
            L = np.log(rv)
        L[~np.isfinite(L)] = np.nan
        mk = np.nanmedian(L, axis=0)
        R = block_simple_returns(close, h, n_blocks)
        rv_b = basket_block_rv(close, h, n_blocks)
        with np.errstate(divide="ignore", invalid="ignore"):
            lb = np.log(rv_b)
        lb[~np.isfinite(lb)] = np.nan
        yh_b = forecast_series(lb, args.min_train)
        bd = [panel.dates[min((i + 1) * h, len(panel.dates) - 1)]
              for i in range(n_blocks)]
        years = np.array([int(d[:4]) for d in bd])

        print(f"\n{'='*78}\n[h={h} 日]  {rv.shape[0]} 只 × {n_blocks} 块")
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            Yh = forecast_matrix(L, mk, SPEC[args.model], args.min_train)
        res = run(h, R, L, mk, Yh, years, args.cap, args.min_train,
                  rv_b, yh_b, args.test_fee)
        all_res[f"h{h}"] = res

        print(f"  OOS 期数 {res['oos_periods']}   "
              f"目标波动 个股中位数={res['target_vol_mean']:.4f}  "
              f"篮子={res['target_vol_basket_mean']:.4f}")
        print(f"\n  绩效（fee=0.50% 往返）")
        print(f"    {'组合':<7s}{'CAGR':>9s}{'年化波动':>10s}{'Sharpe':>8s}"
              f"{'最大回撤':>10s}{'Calmar':>8s}{'年换手':>9s}{'费拖累':>9s}")
        for k in ["EQ", "VT_BP", "VT_B", "VT_P", "VT_M", "IV_P", "IV_M"]:
            key = f"{k}|fee0.0050"
            if key not in res["perf"]:
                continue
            p = res["perf"][key]
            print(f"    {k:<7s}{p['cagr']:>8.2%}{p['ann_vol']:>10.2%}"
                  f"{p['sharpe']:>8.3f}{p['max_drawdown']:>10.2%}"
                  f"{p['calmar']:>8.3f}{p['turnover_ann_x']:>8.2f}x"
                  f"{p['cost_drag_ann']:>9.2%}")
        print(f"\n  波动匹配后的 NW 单侧检验"
              f"（等价于比较 Sharpe，但可做推断；净收益 @ 往返 {args.test_fee:.2%}）")
        for name, t in res["tests"].items():
            if "error" in t:
                print(f"    {name}: {t['error']}")
                continue
            print(f"    {name}")
            print(f"      放大系数 {t['scale_a']:.3f}   "
                  f"年化超额 {t['mean_diff_ann_pct']:+.2f}%   "
                  f"stat={t['stat']:+.3f}  p={t['p_nw_one_sided']:.4f}")
            print(f"      MDE={t['mde_ann_pct']:.2f}%   "
                  f"|效应|/MDE={t['effect_over_mde']:.2f}   "
                  f"逐年为正 {t['year_positive']} = {t['year_ratio']:.0%}")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = Path(args.out) if args.out else OUT_DIR / f"vol_targeting_{Path(args.db).stem}.json"
    out.write_text(json.dumps({
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "db_snapshot": Path(args.db).name,
        "qc": {"applied": True, "n_excluded": panel.n_bad},
        "params": {"horizons": args.horizons, "min_train": args.min_train,
                   "cap": args.cap, "model": args.model,
                   "min_valid_frac": args.min_valid_frac,
                   "fee_tiers_roundtrip": FEE_TIERS, "cash_return": 0.0},
        "results": all_res,
        "note": "关卡③：波动匹配后的均值检验 = 比较 Sharpe 的可推断版本。"
                "敞口上限 1.0（不加杠杆、不可做空），现金收益 0。",
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n产物: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
