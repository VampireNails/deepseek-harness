#!/usr/bin/env python3
"""A 股转向的功效外推：扩截面到底能不能救 MDE？

背景
----
用户选定主攻方向 = 转 A 股，隐含假设是「截面从 ~100 扩到 5000 能显著降 MDE」。
本脚本验证该假设，不做工程实现。

方法
----
观测到的 IC 时间序列标准差 σ_obs 由两部分构成：
    σ_obs² = σ_true² + SE²
  - σ_true：真实 IC 随时间的变异（因子本身的不稳定性，扩截面无法消除）
  - SE    ：单期截面的抽样误差 ≈ 1/sqrt(N_cross - 1)（Spearman 零相关近似）
先用港股实测值反解 σ_true，再代入 A 股配置预测新的 σ_new 与 MDE：
    MDE = (z_{1-α/2} + z_{power}) * σ_new / sqrt(N_eff)

⚠️ 退化解处理（2026-09-02）
当 σ_obs <= SE 时反解出 σ_true = 0，这是**统计假象**而非"真实 IC 恒定"：
σ_obs 仅由 8~12 个期估出，估计误差极大；两方差相减在小 N 下严重低估 σ_true。
若照 σ_true=0 外推会得到 MDE≈0.006、t≈59 这类极端乐观值，不可采信。
故采用双口径：
  - 乐观口径：σ_true 取逐因子分解值（可为 0）→ 仅作理论下界
  - 保守口径：σ_true 统一取「全部非零分解值的中位数」→ **判定以保守口径为准**

⚠️ source 陷阱（2026-09-02）
derived_factors 里基本面因子 source='derived'，但价格因子 source='price_computed'。
按 source='derived' 过滤会把 5 个价格因子整组静默跳过 —— 必须按因子自动识别。
"""
from __future__ import annotations

import json
import math
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]          # D:\tmp\deepseek-harness
DB = ROOT / "outputs" / "equity_fundamental.sqlite"
POWER = ROOT / "outputs" / "equity_power_20260901.json"
OUT = ROOT / "outputs" / "2026-09-02" / "ashare_mde_extrapolation.json"

ALPHA, TARGET_POWER = 0.05, 0.80
Z = 1.959964 + 0.841621              # z(1-α/2) + z(power) = 2.8006
PLAUSIBLE_IC = (0.02, 0.05)          # 用户既定：真实因子效应量级

# A 股配置场景：(名称, 截面宽度, 年化期数, 年数)
SCENARIOS = [
    ("A股·基本面季报·5年", 5000, 4, 5),
    ("A股·基本面季报·10年", 5000, 4, 10),
    ("A股·量价持有20日·5年", 5000, 252 // 20, 5),
    ("A股·量价持有5日·5年", 5000, 252 // 5, 5),
]


def se_ic(n_cross: int) -> float:
    """单期 IC 的抽样标准误（零相关近似）。"""
    return 1.0 / math.sqrt(max(n_cross - 1, 1))


def decompose(ic_std_obs: float, n_cross: int) -> tuple[float, float]:
    """σ_obs² = σ_true² + SE²  →  反解 σ_true。返回 (σ_true, SE)。"""
    se = se_ic(n_cross)
    var_true = ic_std_obs ** 2 - se ** 2
    return (math.sqrt(var_true) if var_true > 0 else 0.0), se


def mde(sigma: float, n_eff: int) -> float:
    return Z * sigma / math.sqrt(n_eff) if n_eff > 0 else float("inf")


def verdict(m: float) -> str:
    lo, hi = PLAUSIBLE_IC
    if m <= lo:
        return "功效充足"
    if m <= hi:
        return "部分可检出(仅0.05档)"
    if m <= 3 * hi:
        return "功效不足"
    return "功效严重不足"


def avg_cross(con: sqlite3.Connection, factor: str) -> tuple[int, str]:
    """因子的中位数截面宽度；自动识别 source，返回 (宽度, source)。"""
    for src in ("derived", "price_computed"):
        rows = sorted(r[0] for r in con.execute(
            "SELECT COUNT(DISTINCT ticker) FROM derived_factors "
            "WHERE factor_key=? AND source=? GROUP BY period", (factor, src)))
        if rows:
            return rows[len(rows) // 2], src
    rows = sorted(r[0] for r in con.execute(
        "SELECT COUNT(DISTINCT ticker) FROM derived_factors "
        "WHERE factor_key=? GROUP BY period", (factor,)))
    return (rows[len(rows) // 2] if rows else 0), "unknown"


def main() -> None:
    power = json.loads(POWER.read_text(encoding="utf-8"))
    con = sqlite3.connect(DB)

    groups = [("fundamentals", "n_periods"), ("prices", "n_windows")]
    raw: dict[str, dict] = {}

    for gname, nkey in groups:
        for fk, v in power.get(gname, {}).items():
            cross, src = avg_cross(con, fk)
            if not cross:
                continue
            s_true, se = decompose(v["ic_std"], cross)
            raw[f"{'F' if gname == 'fundamentals' else 'P'}:{fk}"] = {
                "group": gname, "cross": cross, "source": src,
                "n_hk": v[nkey], "ic_mean": v["ic_mean"], "ic_std": v["ic_std"],
                "se_hk": se, "sigma_true": s_true,
                "noise_share_pct": 100 * se ** 2 / v["ic_std"] ** 2,
                "mde_hk": v["mde"],
            }

    # 保守下界：全部非零 σ_true 的中位数
    non_zero = sorted(e["sigma_true"] for e in raw.values() if e["sigma_true"] > 0)
    sigma_floor = non_zero[len(non_zero) // 2] if non_zero else 0.15

    report = {
        "alpha": ALPHA, "power": TARGET_POWER,
        "plausible_ic": list(PLAUSIBLE_IC),
        "sigma_true_floor_conservative": round(sigma_floor, 4),
        "note": ("sigma_true=0 为退化解（σ_obs<=SE），非真实 IC 恒定；"
                 "判定一律采用保守口径 σ_true_floor"),
        "factors": {},
    }

    for fk, e in raw.items():
        entry = dict(e)
        entry["sigma_true"] = round(e["sigma_true"], 4)
        entry["se_hk"] = round(e["se_hk"], 4)
        entry["noise_share_pct"] = round(e["noise_share_pct"], 1)
        for name, cross_new, per_year, years in SCENARIOS:
            n_eff = per_year * years
            s_opt = math.sqrt(e["sigma_true"] ** 2 + se_ic(cross_new) ** 2)
            s_con = math.sqrt(sigma_floor ** 2 + se_ic(cross_new) ** 2)
            entry[name] = {
                "n_eff": n_eff,
                "mde_optimistic": round(mde(s_opt, n_eff), 4),
                "mde_conservative": round(mde(s_con, n_eff), 4),
                "verdict": verdict(mde(s_con, n_eff)),
            }
        report["factors"][fk] = entry

    con.close()
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    # ── 控制台摘要 ────────────────────────────────────────────────────────────
    print("=" * 118)
    print("A 股功效外推  |  判定采用保守口径：σ_true 统一取非零分解值中位数 = "
          f"{sigma_floor:.4f}")
    print("=" * 118)
    print(f"{'因子':<26}{'截面':>5}{'港股N':>6}{'σ_obs':>8}{'SE':>7}{'σ_true':>8}"
          f"{'噪声%':>7}{'港股MDE':>9}{'季报10y':>9}{'持有20d':>9}{'持有5d':>9}")
    print("-" * 118)
    for fk, e in report["factors"].items():
        g = lambda n: e[n]["mde_conservative"]
        flag = "  ←退化" if e["sigma_true"] == 0 else ""
        print(f"{fk:<26}{e['cross']:>5}{e['n_hk']:>6}{e['ic_std']:>8.3f}{e['se_hk']:>7.3f}"
              f"{e['sigma_true']:>8.3f}{e['noise_share_pct']:>6.1f}%{e['mde_hk']:>9.3f}"
              f"{g('A股·基本面季报·10年'):>9.4f}{g('A股·量价持有20日·5年'):>9.4f}"
              f"{g('A股·量价持有5日·5年'):>9.4f}{flag}")
    print("-" * 118)

    print("\n保守口径判定汇总（达标 = 功效充足 或 部分可检出）")
    for name, _, _, _ in SCENARIOS:
        ok = [fk for fk, e in report["factors"].items()
              if not e[name]["verdict"].startswith("功效严重")
              and not e[name]["verdict"].startswith("功效不足")]
        print(f"  {name:<24} {len(ok):>2}/{len(report['factors'])} 达标"
              f"   中位MDE={sorted(e[name]['mde_conservative'] for e in report['factors'].values())[len(report['factors'])//2]:.4f}")

    # ── 反解：达到目标 MDE 所需的期数与年限 ──────────────────────────────────
    # 保守口径下 σ_true 统一，MDE 仅由 N_eff 驱动：N = (Z·σ_true / MDE)²
    freqs = [("季报", 4), ("月频", 12), ("周频", 52), ("日频-持有5日", 50), ("日频-持有1日", 252)]
    print("\n反解：达到目标 MDE 所需期数 / 年限"
          f"（σ_true={sigma_floor:.4f}，保守口径）")
    print(f"  {'目标MDE':<10}{'所需期数':>10}" + "".join(f"{n:>16}" for n, _ in freqs))
    need_rows = {}
    for target in (0.05, 0.03, 0.02):
        n_need = (Z * sigma_floor / target) ** 2
        cells = "".join(f"{n_need / f:>15.1f}y" for _, f in freqs)
        print(f"  {target:<10.3f}{n_need:>10.0f}{cells}")
        need_rows[str(target)] = {
            "n_periods_needed": round(n_need),
            "years": {n: round(n_need / f, 1) for n, f in freqs},
        }
    print("  注：σ_true 取自港股实测；A 股真实 IC 时间变异可能不同，年限仅作量级参考。")

    report["need_n_table"] = {"sigma_true_used": round(sigma_floor, 4), "rows": need_rows}
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n报告已写入: {OUT}")


if __name__ == "__main__":
    main()
