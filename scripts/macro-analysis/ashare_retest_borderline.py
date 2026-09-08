#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ashare_retest_borderline.py — 换源后复测 3 个 borderline 因子（A/B 同口径对照）
==========================================================================
背景（为什么要做这件事）
----------------------
2026-09-05 第廿四类事故：腾讯 fqkline 后复权序列系统性污染，无除权日
hfq 收益 vs 本地 raw 收益 p50 差 0.2%、最大 3.6%、6% 的交易日差 >1%。
污染量级（0.68%/月）≥ 因子信号幅度（0.2~0.5%/月）。

结论影响的不对称性（决定了本次复测的范围）
----------------------------------------
- 加性噪声只稀释 IC ⇒ 已「判死」的结论不被推翻（死因是信号不足）
- 但【UNDERPOWERED】类判决不可靠：|IC|/MDE 在 0.5~1.0 边缘的因子，
  真实信号可能被噪声吃掉 ⇒ 必须复测
- PB 是【方向性偏差】（污染与分红相关 → 与 PB 截面排序相关），最危险

故复测对象 = 三个 borderline 因子（旧的 |IC|/MDE）：
    gm_change   毛利率变化·YoY     1.035  (IC +0.0199, t +2.07)
    rev_yoy_rel 营收同比·相对市场   0.614  (IC +0.0217, t +1.10)
    pb          估值 PB            0.489  (IC -0.0277, t -0.89)

设计原则：不重写检验逻辑，只切换数据源
--------------------------------------
三个因子分属三个脚本，口径（月度、h=60、NW lags、MCC、缩尾、生效日）
各不相同且都经过此前推敲。重写 = 引入口径漂移 = 对照失去意义。
故本脚本只是【驱动层】：用 subprocess 调原脚本，仅换 --price-db，
并对同一脚本同一参数跑 A/B 两遍，保证差异 100% 归因于数据源。

用法
----
    python ashare_retest_borderline.py                 # 全跑 A/B 两遍
    python ashare_retest_borderline.py --only-xq       # 只跑新源
输出：outputs/2026-09-05/ashare_retest_borderline.json
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
ROOT = _HERE.parents[3]
OUT_DIR = ROOT / "outputs" / "2026-09-05"
PY = sys.executable

TX_DB = ROOT / "outputs" / "ashare_csi800_hfq.sqlite"        # 旧源：腾讯（2014 起）
# 新源：雪球【切片到 2014 起】。必须用切片而非全量库 —— 雪球返回全历史
# （最早 1992），与腾讯库时间跨度不同会让面板 T 不一致，A/B 差异就不再
# 能 100% 归因于数据源。详见 ashare_xq_slice2014.py。
XQ_DB = ROOT / "outputs" / "ashare_csi800_hfq_xq_2014.sqlite"
FUND_DB = ROOT / "outputs" / "ashare_csi800_fund.sqlite"
RAW_DB = ROOT / "outputs" / "ashare_csi800_raw.sqlite"

# 三个因子 → (脚本, 结果里的 key 路径, 中文名, 旧基线 ratio)
TARGETS = {
    "gm_change": {"script": "ashare_csi800_multifactor.py",
                  "pick": ("factors", "gm_change"),
                  "label": "毛利率变化·YoY", "baseline_ratio": 1.035},
    "rev_yoy_rel": {"script": "ashare_fund_validate.py",
                    "pick": ("factors", "rev_yoy_rel"),
                    "label": "营收同比·相对市场中位数", "baseline_ratio": 0.614},
    "pb": {"script": "ashare_csi800_pb.py",
           "pick": ("__root__",),
           "label": "估值 PB", "baseline_ratio": 0.489},
}


def run(cmd, log_path: Path):
    t0 = time.time()
    r = subprocess.run([PY] + cmd, cwd=str(_HERE), capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    log_path.write_text((r.stdout or "") + "\n[STDERR]\n" + (r.stderr or ""),
                        encoding="utf-8")
    return {"rc": r.returncode, "sec": round(time.time() - t0, 1),
            "log": str(log_path)}


def base_args(tag: str, script: str):
    """构造某脚本在某一数据源下的完整命令行。"""
    db = TX_DB if tag == "tx" else XQ_DB
    out = OUT_DIR
    # ★ --exclude-financial 必须与基线一致：基线跑 pb / fund_validate 时带了该参数
    #   （剔除金融股，因其毛利率全期缺失会污染截面 z-score），未带时截面会多出
    #   ~50-60 只（实测 PB n_cross 557→609、rev_yoy 571→630），IC 随之改变。
    #   而 multifactor 基线未带该参数（gm_change 已逐位复现 1.035），故此处不加。
    #   教训：A/B 对照前必须先用旧源复现基线，否则无从判断差异来自数据还是口径。
    if script == "ashare_csi800_multifactor.py":
        return [script, "--price-db", str(db), "--out-dir", str(out),
                "--out", f"ashare_csi800_multifactor_{tag}.json"]
    if script == "ashare_fund_validate.py":
        return [script, "--price-db", str(db), "--fund-db", str(FUND_DB),
                "--obs-freq", "monthly", "--holding", "60",
                "--start", "2014-01-01", "--exclude-financial",
                "--out-dir", str(out),
                "--out", f"ashare_csi800_fund_validate_h60_monthly_{tag}.json"]
    if script == "ashare_csi800_pb.py":
        return [script, "--price-db", str(db), "--out-dir", str(out),
                "--exclude-financial",
                "--out", f"ashare_csi800_pb_{tag}.json"]
    raise ValueError(script)


def _metrics(node):
    return {
        "ic_mean": node.get("ic_mean"),
        "sigma_true": node.get("sigma_true"),
        "mde": node.get("mde"),
        "ratio_ic_mde": node.get("ratio_ic_mde"),
        "t_hac": node.get("t_hac"),
        "n_periods": node.get("n_periods"),
        "n_cross_typical": node.get("n_cross_typical"),
        "significant": node.get("significant") or node.get("significant_mcc"),
    }


def pick(node, path):
    if path == ("__root__",):
        return node
    for k in path:
        node = node[k]
    return node


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only-xq", action="store_true", help="只跑新源（旧源已有产物时）")
    ap.add_argument("--only-tx", action="store_true")
    args = ap.parse_args()
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    tags = ["xq"] if args.only_xq else (["tx"] if args.only_tx else ["tx", "xq"])
    res = {}
    # 始终遍历两个 tag：不在本次运行范围的走"复用已有产物"分支
    for tag in ["tx", "xq"]:
        db = TX_DB if tag == "tx" else XQ_DB
        if tag in tags and not db.exists():
            print(f"[skip] {tag} 库不存在: {db}")
            continue
        for key, spec in TARGETS.items():
            cmd = base_args(tag, spec["script"])
            jf = OUT_DIR / cmd[cmd.index("--out") + 1]
            # 不在本次运行范围的 tag：若产物已存在则直接复用（避免重复计算，
            # 例如先跑完 tx 再单独跑 xq 时，tx 结果不该重跑）
            if tag not in tags:
                if jf.exists():
                    d = json.loads(jf.read_text(encoding="utf-8"))
                    node = pick(d, spec["pick"])
                    res.setdefault(key, {})[tag] = {
                        "run": {"reused": True},
                        "metrics": _metrics(node)}
                continue
            lp = OUT_DIR / f"_retest_{tag}_{key}.log"
            print(f"\n>>> [{tag}] {key}  {' '.join(cmd[:1])} ...", flush=True)
            info = run(cmd, lp)
            print(f"    rc={info['rc']} {info['sec']}s", flush=True)
            res.setdefault(key, {})[tag] = {"run": info}
            if info["rc"] != 0:
                continue
            if jf.exists():
                d = json.loads(jf.read_text(encoding="utf-8"))
                node = pick(d, spec["pick"])
                res[key][tag]["metrics"] = _metrics(node)

    # ---- 对照汇总 ----
    print("\n" + "=" * 96)
    print("换源复测 A/B 对照（腾讯 → 雪球）")
    print("=" * 96)
    print(f"{'因子':<22}{'源':<6}{'IC均值':>10}{'σ_true':>9}{'MDE':>9}"
          f"{'|IC|/MDE':>10}{'t(HAC)':>9}{'翻转':>7}")
    print("-" * 96)
    summary = {}
    for key, spec in TARGETS.items():
        a = res.get(key, {}).get("tx", {}).get("metrics")
        b = res.get(key, {}).get("xq", {}).get("metrics")
        for tag, m in (("tx", a), ("xq", b)):
            if not m:
                print(f"{spec['label']:<22}{tag:<6}{'— 无结果 —':>40}")
                continue
            flip = ""
            if a and b and tag == "xq":
                # 判定翻转 = 是否越过关卡②（|t|>=2.8 for pb / MCC 阈值 for 多因子）
                # 统一用"|IC|/MDE 是否跨过 1.0"作为功效翻转的客观标志
                ra, rb = abs(a["ratio_ic_mde"]), abs(b["ratio_ic_mde"])
                if (ra < 1.0) != (rb < 1.0):
                    flip = "★功效"
                if (a["significant"] or False) != (b["significant"] or False):
                    flip += "★显著"
            print(f"{spec['label']:<22}{tag:<6}{m['ic_mean']:>+10.4f}"
                  f"{m['sigma_true']:>9.4f}{m['mde']:>9.4f}"
                  f"{abs(m['ratio_ic_mde']):>10.3f}{m['t_hac']:>+9.2f}{flip:>7}")
        summary[key] = {
            "label": spec["label"], "baseline_ratio": spec["baseline_ratio"],
            "tx": a, "xq": b,
            "delta_ratio": (abs(b["ratio_ic_mde"]) - abs(a["ratio_ic_mde"]))
                           if (a and b) else None,
            "delta_ic": (b["ic_mean"] - a["ic_mean"]) if (a and b) else None,
        }
        print("-" * 96)

    out = {"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
           "purpose": "换源（腾讯→雪球）复测 3 个 borderline 因子",
           "tx_db": str(TX_DB), "xq_db": str(XQ_DB), "summary": summary}
    p = OUT_DIR / "ashare_retest_borderline.json"
    p.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n已写出: {p}")


if __name__ == "__main__":
    main()
