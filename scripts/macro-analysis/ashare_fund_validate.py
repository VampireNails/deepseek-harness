# -*- coding: utf-8 -*-
"""A股基本面因子验证（候选B 第①②关：功效 + 显著性）。

【★ vintage 处理是本脚本的核心，做错了整个结论就废了】
财报有两个日期，必须只用【公告日期】：

    report_date  报告期（2025-12-31）—— 业绩所属期间
    notice_date  公告日期（2026-03-28）—— 市场得知的时点

**因子生效日 = notice_date 之后的第一个交易日（T+1）**。
用 report_date 会让策略提前 1~4 个月"知道"尚未公布的业绩 → 前视偏差 → 回测虚高。

公告日期缺失时的兜底（按 A 股法定披露截止日的次日）：
    一季报 → 05-01    半年报 → 09-01    三季报 → 11-01    年报 → 次年 05-01
（与港股纪律同源：因子生效日 = min(实际公告日期, 法定截止日)）

【IC 的口径：按报告期，不按日历月】
基本面因子在一期财报内是恒定的，按月算 IC 会让同一份财报被重复计入 2~3 次，
虚增 N_eff。正确做法是**每个报告期只取一个 IC 观测**（取该期生效日的中位数日），
N_eff = 报告期数 ≈ 50。这是最保守、最符合"独立观测"定义的口径。

【★ 中性化：农业池基本面必须做】
农业股受猪周期统一驱动，绝对 ROE 在全行业上行期集体升高、下行期集体转负 ——
这是【共同暴露】不是【截面差异】。因此主口径用**相对全市场中位数**的因子值：
    roe_rel = roe_stock − roe_market_median(同期)
同时报告绝对口径作对照，两者的差异本身就是"猪周期共同冲击有多强"的度量。

【三关只跑①②】
基本面因子低频（N_eff≈50），若第①关功效不足，第③关回测无意义 ——
这正是港股基本面走过的弯路（先投工程再发现功效不足）。
只有①②都通过，才值得投第③关（扣成本回测）。
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
from scipy.stats import norm, rankdata

_HERE = Path(__file__).resolve().parent
ROOT = _HERE.parents[3]
sys.path.insert(0, str(_HERE))

from ashare_agri_backtest import (  # noqa: E402
    DB as PRICE_DB, PRICE_TABLE,
    load_panel, build_tradable, board_of, fwd,
)

FUND_DB = ROOT / "outputs" / "ashare_fundamental.sqlite"
OUT_DIR = ROOT / "outputs" / "2026-09-02"

# 兜底生效日（月-日）：法定披露截止日的次日
FALLBACK_EFF = {
    "03-31": "05-01",    # 一季报
    "06-30": "09-01",    # 半年报
    "09-30": "11-01",    # 三季报
    "12-31": "05-01",    # 年报（次年 05-01）
}

FACTORS = [
    ("roe_rel", "ROE(相对全市场中位数)"),
    ("roe_abs", "ROE(绝对值)"),
    ("rev_yoy_rel", "营收同比(相对)"),
    ("gross_margin", "毛利率(绝对值)"),
    ("np_yoy_wins", "净利同比(缩尾)"),
    ("comp_roe_rev", "合成·ROE+营收同比"),
    # ★ 宽基池必测：规模是 A 股最强的横截面因子之一。把规模放进【被测列表】
    #   而不是只当控制变量，是为了看清「规模因子在本池的效应量处于什么量级」
    #   —— 这是判断其他因子是否值得继续的标尺（参照系）。
    ("size_logrev", "规模·log(营业收入)"),
]


def effective_date(report_date: str, notice_date):
    """因子生效日。优先用实际公告日期，缺失则按法定截止日兜底。"""
    if notice_date:
        return notice_date
    md = report_date[5:]
    fb = FALLBACK_EFF.get(md, "05-01")
    y = int(report_date[:4])
    if md == "12-31":          # 年报兜底到次年
        y += 1
    return f"{y}-{fb}"


def load_fund(conn):
    rows = conn.execute(
        "SELECT code, report_date, notice_date, roe, rev_yoy, np_yoy, bps,"
        "       ocf_ps, gross_margin, revenue "
        "FROM fund_reports ORDER BY code, report_date").fetchall()
    med = {r[0]: r for r in conn.execute(
        "SELECT report_date, roe_med, rev_yoy_med, np_yoy_med, gross_margin_med"
        " FROM market_median")}
    return rows, med


def build_panel(dates, codes, fund_rows, med, wins_q=0.05):
    """构造『交易日 × 股票』的因子面板，严格按生效日向前填充。

    返回 dict[因子名] = (T, M) 数组。
    ⚠️ 关键：按生效日升序依次赋值 F[idx:] = v，后披露的期自然覆盖先披露的期。
    """
    T, M = len(dates), len(codes)
    ci = {c: j for j, c in enumerate(codes)}
    raw = {k: np.full((T, M), np.nan) for k in
           ["roe_abs", "roe_rel", "rev_yoy_rel", "gross_margin",
            "np_yoy_wins", "comp_roe_rev", "size_logrev"]}

    # 先按 (股票, 生效日) 组织，并对净利同比做全样本缩尾
    np_yoy_all = [r[5] for r in fund_rows if r[5] is not None]
    lo = hi = None
    if len(np_yoy_all) > 100:
        lo, hi = np.percentile(np_yoy_all, [wins_q * 100, (1 - wins_q) * 100])

    by_code = {}
    for code, rd, nd, roe, ry, ny, bps, ocf, gm, rev in fund_rows:
        eff = effective_date(rd, nd)
        by_code.setdefault(code, []).append(
            (eff, roe, ry, ny, gm, med.get(rd), rev))

    for code, lst in by_code.items():
        j = ci.get(code)
        if j is None:
            continue
        lst.sort(key=lambda x: x[0])          # 按生效日升序
        for eff, roe, ry, ny, gm, mrow, rev in lst:
            idx = int(np.searchsorted(dates, eff, side="right"))   # T+1 生效
            if idx >= T:
                continue
            if roe is not None:
                raw["roe_abs"][idx:, j] = roe
                if mrow and mrow[1] is not None:
                    raw["roe_rel"][idx:, j] = roe - mrow[1]
            if ry is not None and mrow and mrow[2] is not None:
                raw["rev_yoy_rel"][idx:, j] = ry - mrow[2]
            if gm is not None:
                raw["gross_margin"][idx:, j] = gm
            if ny is not None and lo is not None:
                raw["np_yoy_wins"][idx:, j] = float(np.clip(ny, lo, hi))
            # ★ 规模因子：log(营业收入)。宽基池按营收排序取，跨度达 440 倍，
            #   规模是必须控制的变量；宽池无流动性数据，无法反推流通市值，
            #   故用营收对数做规模代理（与市值高度相关）。
            if rev is not None and rev > 0:
                raw["size_logrev"][idx:, j] = float(np.log(rev))
        # 合成：ROE(相对) 与 营收同比(相对) 的截面 z 均值
    # 合成：ROE(相对) 与 营收同比(相对) 的截面 z 均值
    a, b = raw["roe_rel"], raw["rev_yoy_rel"]
    with np.errstate(invalid="ignore", divide="ignore"):
        za = (a - np.nanmean(a, axis=1, keepdims=True)) / np.nanstd(a, axis=1, keepdims=True)
        zb = (b - np.nanmean(b, axis=1, keepdims=True)) / np.nanstd(b, axis=1, keepdims=True)
        raw["comp_roe_rev"] = np.nanmean(np.stack([za, zb]), axis=0)
    return raw


def cross_section_z(x, mask):
    """逐行截面 z-score（只在 mask 内）。"""
    out = np.full_like(x, np.nan)
    for t in range(x.shape[0]):
        m = mask[t] & np.isfinite(x[t])
        if m.sum() < 5:
            continue
        v = x[t][m]
        sd = v.std()
        if sd and np.isfinite(sd):
            out[t, m] = (v - v.mean()) / sd
    return out


def spearman_ic(z, r, mask):
    """逐期截面 Spearman IC（对 z 排序后算 Pearson，等价）。"""
    ics, ns = [], []
    for t in range(z.shape[0]):
        m = mask[t] & np.isfinite(z[t]) & np.isfinite(r[t])
        n = int(m.sum())
        if n < 10:
            continue
        a = rankdata(z[t][m])
        b = rankdata(r[t][m])
        sa, sb = a.std(), b.std()
        if sa <= 0 or sb <= 0:
            continue
        # ⚠️ rankdata 后的 std/mean 都是 population 口径（除以 n），
        #    所以相关系数 = cov/(sa*sb)，cov 已经含 1/n 了，【不能再除以 n】。
        #    第一版多除了一个 n，IC 均值被缩小 ~75 倍 → MDE/|IC|/MDE 全错。
        #    （注意：t 统计量不受影响，因为分子分母同除，所以这个 bug 很隐蔽。）
        ics.append(float(((a - a.mean()) * (b - b.mean())).mean() / (sa * sb)))
        ns.append(n)
    return np.array(ics), np.array(ns)


def newey_west_t(x, lags):
    x = np.asarray(x, float)
    x = x[np.isfinite(x)]
    n = len(x)
    if n < 8:
        return float("nan"), n
    mu = x.mean()
    d = x - mu
    g0 = float(d @ d) / n
    v = g0
    for L in range(1, min(lags, n - 1) + 1):
        gl = float(d[L:] @ d[:-L]) / n
        v += 2.0 * (1.0 - L / (lags + 1.0)) * gl
    v = max(v, 1e-18)
    return float(mu / math.sqrt(v / n)), n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--holding", type=int, default=60,
                    help="持有期（交易日）。60=到下期财报")
    ap.add_argument("--min-cross", type=int, default=20)
    ap.add_argument("--fund-db", default=str(FUND_DB))
    ap.add_argument("--obs-freq", choices=["nonoverlap", "monthly"],
                    default="nonoverlap",
                    help="IC 观测频率：nonoverlap=每个报告期一个（默认）；monthly=每月一个（允许重叠，配 Newey-West）")
    ap.add_argument("--nw-lags", type=int, default=0,
                    help="覆盖 Newey-West 滞后阶数（0=自动）；用于重叠自相关稳健性检验")
    ap.add_argument("--exclude-financial", action="store_true",
                    help="剔除毛利率全期缺失的金融/类金融股")
    ap.add_argument("--min-history", type=int, default=0,
                    help="点入时上市满 N 个有效交易日；宽基池必须设 250")
    ap.add_argument("--price-db", default=str(PRICE_DB))
    ap.add_argument("--start", default="2014-01-01")
    ap.add_argument("--min-periods", type=int, default=20)
    ap.add_argument("--out", default=None,
                    help="输出 JSON 文件名（默认 ashare_fund_validate_h{holding}.json；"
                         "跨池复用时必须显式指定，防覆写其它池的产物）")
    # 2026-09-05 新增：换源复测需指定产物目录（原硬编码 2026-09-02，
    # 直接覆盖会毁掉旧源结论，导致前后无法对照）
    ap.add_argument("--out-dir", default=str(OUT_DIR))
    args = ap.parse_args()

    print("=" * 78)
    print("A股基本面因子验证（候选B 第①②关）")
    print("=" * 78)

    # ---- 价格面板（后复权）----
    fconn = sqlite3.connect(str(args.fund_db), timeout=30)
    fund_rows, med = load_fund(fconn)
    fconn.close()
    if not fund_rows:
        raise SystemExit(f"{FUND_DB} 为空，请先运行 ashare_fund_collect.py")

    pconn = sqlite3.connect(str(args.price_db), timeout=30)
    try:
        qc_bad = {r[0] for r in pconn.execute("SELECT code FROM hfq_qc WHERE bad=1")}
    except sqlite3.OperationalError:
        qc_bad = set()
    dates, codes, _o, close, volume, amount, turn = load_panel(pconn, PRICE_TABLE)
    pconn.close()

    tradable = build_tradable(close, volume, amount, codes, qc_bad,
                              exclude_b=True, exclude_limit=False,
                              min_history=args.min_history)
    # ★ 剔除金融股（银行/券商/保险）
    #   宽基池按营收取 top1000，金融股天然占比高（实测 80 只）。它们的
    #   「营业收入」是利息收入+手续费、「无毛利率」、ROE 的杠杆含义也与实业
    #   完全不同 —— 混在一个截面里做 z-score 等于拿两套不可比的口径排序。
    #   识别方式：毛利率【全部报告期均缺失】＝无营业成本科目 ＝ 金融/类金融。
    #   （比代码前缀判断可靠：城商行 002xxx、券商 000xxx 用前缀会漏判。）
    n_fin = 0
    if args.exclude_financial:
        have_gm = {c for c, in
                   [(r[0],) for r in fund_rows if r[8] is not None]}
        fin = [j for j, c in enumerate(codes) if c not in have_gm]
        n_fin = len(fin)
        if fin:
            tradable[:, fin] = False
        print(f"已剔除金融/类金融股（毛利率全期缺失）: {n_fin} 只")

    t0 = int(np.where(dates >= args.start)[0][0]) if args.start else 0
    T, M = close.shape
    print(f"价格面板 {T} 天 × {M} 只   起始 {dates[t0]}")
    print(f"财报记录 {len(fund_rows)} 条   可交易单元 {tradable.mean():.1%}")

    # ---- 因子面板 ----
    F = build_panel(dates, codes, fund_rows, med)
    ret_h = fwd(close, args.holding)          # 未来 H 日收益

    # ---- 每个报告期取一个 IC 观测 ----
    # ⚠️ 修复（原 bug）：此前用 (report_date, effective_date) 元组去重，而每只股票的
    #    公告日期各不相同 → 同一报告期被拆成几十个"期"（实测 394 个 vs 真实 ~52 个），
    #    N_eff 被严重低估、MDE 被放大到 0.10~0.15，功效评估完全失真。
    #    正确做法：只按 report_date 去重，生效日取该期池内股票生效日的【中位数】。
    #    取中位数是安全的：晚于中位数披露的股票，其因子值在该日仍是上一期的旧值
    #    （build_panel 按个股生效日填充），不存在前视。
    by_period = {}
    for r in fund_rows:
        eff = effective_date(r[1], r[2])
        by_period.setdefault(r[1], []).append(eff)
    periods = []
    for rd in sorted(by_period):
        effs = sorted(e for e in by_period[rd] if e)
        if not effs:
            continue
        periods.append((rd, effs[len(effs) // 2]))   # 中位数生效日
    obs_idx = []
    for rd, eff in periods:
        idx = int(np.searchsorted(dates, eff, side="right"))
        if t0 <= idx < T - args.holding:
            obs_idx.append(idx)
    if args.obs_freq == "monthly":
        # ★ 月度口径（Fama-MacBeth 标准做法）：每月取一个观测，允许收益窗口重叠。
        #   为什么需要它：非重叠口径下 N_eff 被 A股披露日历锁死在 26，
        #   而 MDE 的【硬下限】= 2.8006 × σ_true / √26 ≈ 0.059（σ_true≈0.107 实测）。
        #   观测到的 IC 恰好 0.058 → 卡在下限上，扩截面永远救不回来。
        #   唯一能降 MDE 的是增加 N_eff —— 重叠观测合法地提供了这个可能，
        #   代价是序列相关，由 Newey-West 调整（见下方 lags）。
        by_month = {}
        for i in range(t0, T - args.holding):
            by_month[str(dates[i])[:7]] = i
        obs_idx = sorted(by_month.values())
        print(f"月度口径（允许重叠）→ 有效观测 {len(obs_idx)} 期")
    else:
        obs_idx = sorted(set(obs_idx))
        keep = []
        for i in obs_idx:
            if not keep or i - keep[-1] >= args.holding * 0.8:
                keep.append(i)
        obs_idx = keep
        print(f"报告期 {len(periods)} 个 → 非重叠有效观测 {len(obs_idx)} 期")

    # ⚠️ Newey-West 滞后阶数：
    #    非重叠口径用经验法则 n^0.25 即可；
    #    月度重叠口径的自相关会延续【整个持有期】，lags 必须 ≥ 持有月数，
    #    否则 se 被低估 → t 虚高 → 这是最容易自欺的地方，宁可保守。
    nw_lags = max(int(math.ceil(len(obs_idx) ** 0.25)), 1)
    if args.obs_freq == "monthly":
        nw_lags = max(nw_lags, int(math.ceil(args.holding / 21.0)) + 1)
    if args.nw_lags > 0:
        nw_lags = args.nw_lags
    print(f"Newey-West lags = {nw_lags}")

    if len(obs_idx) < args.min_periods:
        raise SystemExit(f"有效观测期数 {len(obs_idx)} < {args.min_periods}，无法评估")

    # ---- 逐因子评估 ----
    alpha = 0.05
    n_tests = len(FACTORS) * 1
    t_thr = float(norm.ppf(1 - alpha / (2 * max(n_tests, 1))))
    print(f"MCC 阈值（n_tests={n_tests}, α=0.05）: |t| ≥ {t_thr:.3f}\n")

    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "holding_days": args.holding,
        "start": dates[t0],
        "n_observations": len(obs_idx),
        "mcc_threshold": t_thr,
        "factors": {},
    }

    print(f"  {'因子':<24}{'N':>5}{'IC均值':>9}{'IC_std':>8}{'σ_true':>8}"
          f"{'MDE':>8}{'|IC|/MDE':>10}{'t(HAC)':>9}{'显著':>6}")
    print("  " + "-" * 88)

    n_pass = 0
    for fk, flabel in FACTORS:
        X = cross_section_z(F[fk], tradable)
        ics, ns = spearman_ic(X[obs_idx], ret_h[obs_idx], tradable[obs_idx])
        if len(ics) < 8:
            print(f"  {flabel:<24}  观测不足（{len(ics)}）")
            continue
        n_typ = int(np.median(ns))
        # IC 方差分解：σ_obs² = σ_true² + Var_noise，Var_noise ≈ 1/(N-1)
        var_noise = 1.0 / max(n_typ - 1, 1)
        var_obs = float(ics.var(ddof=1))
        sigma_true = math.sqrt(max(var_obs - var_noise, 0.0))
        n_eff = len(ics)
        mde = 2.8006 * math.sqrt(var_obs) / math.sqrt(n_eff)
        ic_mean = float(ics.mean())
        t_stat, _ = newey_west_t(ics, lags=nw_lags)
        ratio = abs(ic_mean) / mde if mde > 0 else float("nan")
        sig = bool(abs(t_stat) >= t_thr)
        if sig:
            n_pass += 1
        print(f"  {flabel:<24}{n_typ:>5}{ic_mean:>+9.4f}"
              f"{math.sqrt(var_obs):>8.4f}{sigma_true:>8.4f}{mde:>8.4f}"
              f"{ratio:>10.2f}{t_stat:>+9.2f}{'  ✓' if sig else '    '}")

        report["factors"][fk] = {
            "label": flabel,
            "n_periods": int(len(ics)),
            "n_cross_typical": n_typ,
            "ic_mean": round(ic_mean, 5),
            "ic_std": round(math.sqrt(var_obs), 5),
            "sigma_true": round(sigma_true, 5),
            "var_noise": round(var_noise, 5),
            "mde": round(mde, 5),
            "ratio_ic_mde": round(ratio, 3),
            "t_hac": round(t_stat, 3),
            "significant_mcc": sig,
            "ic_series": [round(float(v), 5) for v in ics],
        }

    print(f"\n  通过 MCC: {n_pass}/{len(FACTORS)}")

    # ---- 判定 ----
    print("\n" + "=" * 78)
    print("判定（第①关功效 + 第②关显著性）")
    print("=" * 78)
    best = max(report["factors"].items(),
               key=lambda kv: kv[1]["ratio_ic_mde"], default=(None, None))
    if best[0]:
        b = best[1]
        print(f"  最优因子: {b['label']}")
        print(f"    IC 均值 {b['ic_mean']:+.4f}   MDE {b['mde']:.4f}   "
              f"|IC|/MDE = {b['ratio_ic_mde']:.2f}   t(HAC) = {b['t_hac']:+.2f}")
        if b["ratio_ic_mde"] >= 1 and b["significant_mcc"]:
            print(f"\n  ✓ 候选B 通过第①②关 → 值得投第③关（扣成本回测）")
            verdict = "PASS_STAGE12"
        elif b["significant_mcc"] and b["ratio_ic_mde"] < 1:
            print(f"\n  △ 显著但观测值 < MDE → 疑似夸大，需更多期数确认")
            verdict = "SIGNIFICANT_BUT_UNDERPOWERED"
        elif b["ratio_ic_mde"] >= 1:
            print(f"\n  △ 效应量够但没过 MCC → 可能噪声，也可能功效边缘")
            verdict = "EFFECT_BUT_NOT_SIGNIFICANT"
        else:
            print(f"\n  ✗ 功效不足且不显著 → 候选B 判死，农业池基本面归档为零结果")
            verdict = "UNDERPOWERED"
        report["verdict"] = verdict

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    op = out_dir / (args.out or f"ashare_fund_validate_h{args.holding}.json")
    op.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n已写出: {op}")


if __name__ == "__main__":
    main()
