#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ashare_daily_topup.py — 投研数据每日增量补数（收盘后调度，建议 21:00）
======================================================================
为什么单独立脚本：automation prompt 里堆 6 条命令容易漂移（agent 每次要
重新理解池列表/参数），封装成单一 CLI 入口后 prompt 只剩一行调用，且本脚本
可离线单测。

职责（只做增量补数，不做预测/分析）：
  1. 交易日守卫：周末跳过（A 股/港股均无交易）；法定节假日会幂等重采，无害。
  2. 5 个雪球后复权池增量：ashare_xq_collect.py --since <T>
     （幂等替换，只补「库内最新 < T」的股票，杜绝每日全量重拉）
  3. 两融增量：ashare_altdata_collect.py --src margin --all --since <库内全局最新>
  4. 港股日K：equity_quotes.py collect，标的 = equity_fundamental.sqlite universe(110 只)
  5. 龙虎榜：ashare_altdata_collect.py --src lhb --start <库内最新+1>（按交易日全市场，成本低）
  6. 大宗交易：ashare_altdata_collect.py --src block --all --since <库内全局最新>（增量页）
  7. 解禁日程：ashare_altdata_collect.py --src lift --all --since <今天>（只拉未来解禁）
  8. 财报季频：ashare_fund_collect.py（两个库，--resume 只补未采报告期，幂等）
  9. 汇总一行一项，失败不中断其余项。

★ 口径：判死 ≠ 禁采（2026-09-06 定案）
  「判死」是**交易信号可用性**的裁决（三关过滤器：功效→显著性→经济可行性），
  含义仅限「不得据此产生买入/卖出/目标价/仓位/择时结论」。
  它**不是数据价值判决**：龙虎榜/大宗/解禁在投研助手语境下是有效的描述性
  （事实型）风险事件；港股是投研标的池（workflow §6）而非 alpha 线。
  ⇒ 判死条目不禁采、不禁查、不删脚本；只禁止引申为交易建议。

红线：
  - A 股行情只能走雪球源（ashare_xq_collect.py）；禁止用腾讯源写后复权库。
  - 港股 count 必须显式传 1600：equity_quotes.py collect 是**快照语义**
    （先 DELETE 该 ticker 全部旧行再插入），默认 --count 320 会把 1600 行历史砍成 320 行。
  - 不采证据层（新闻/公告/研报——可回溯，按需现拉即可）。
  - 本脚本只补数，不产出任何买入/目标价/仓位/择时结论。

用法：
  python ashare_daily_topup.py                 # 今天日期作为 since，周末自动跳过
  python ashare_daily_topup.py --since 2026-09-04
  python ashare_daily_topup.py --no-weekend-guard
  python ashare_daily_topup.py --skip-hk       # 跳过港股（调试用）
"""
from __future__ import annotations

import argparse
import sqlite3
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parents[4]
SC = Path(__file__).resolve().parent
OUT = WORKSPACE / "outputs"

# 5 个雪球后复权池：池定义 = 该库自身 daily_quotes_hfq 的 DISTINCT code
POOLS = [
    "ashare_csi800_hfq_xq.sqlite",
    "ashare_agri_hfq_xq.sqlite",
    "ashare_semi_hfq_xq.sqlite",
    "ashare_single_hfq_xq.sqlite",
    "ashare_wide_hfq_xq.sqlite",
]

# 财报季频补采：(池代码来源库, 基本面输出库)
# ★ 判决书 FUND_DBS 按序取第一个可读库，农业股命中 ashare_fundamental（95 只全在
#   农业池内 → 池内分位正确）；csi800 库供宽池/其他用途。两个都要补，不能只补一个。
FUND_POOLS = [
    ("ashare_agri_hfq_xq.sqlite", "ashare_fundamental.sqlite"),
    ("ashare_csi800_hfq_xq.sqlite", "ashare_csi800_fund.sqlite"),
]


def run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", cwd=str(SC))


def tail_of(r: subprocess.CompletedProcess) -> str:
    """取子进程 stdout 末行；★ 非零退出一律显式报 FAIL（2026-09-06 修复）。

    原实现只取 stdout 末行、不看 returncode ⇒ 子进程 traceback 写 stderr 被完全
    吞掉，行情采集崩溃也显示成一行正常输出（「池规模 780，待采 780」），
    日采静默空转数日无人知晓。调度器绝不能吞返回码。
    """
    if r.returncode != 0:
        err = (r.stderr or "").strip().splitlines()
        msg = err[-1] if err else (r.stdout or "").strip().splitlines()[-1:] or [""]
        msg = err[-1] if err else (msg[0] if isinstance(msg, list) else msg)
        return f"*** FAIL exit={r.returncode} :: {str(msg)[:200]}"
    lines = (r.stdout or "").strip().splitlines()
    if lines:
        return lines[-1]
    return f"exit=0（无 stdout）"


def _next_day(d: str) -> str:
    """ISO 日期 +1 天（用于增量起点：库内已有 T 日，则从 T+1 开始补）。"""
    try:
        return (date.fromisoformat(d) + timedelta(days=1)).isoformat()
    except ValueError:
        return d


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default=None,
                    help="期望补到的交易日（缺省=今天 ISO 日期）")
    ap.add_argument("--no-weekend-guard", action="store_true",
                    help="关闭周末守卫（调试用）")
    ap.add_argument("--skip-hk", action="store_true", help="跳过港股日K")
    ap.add_argument("--skip-quote", action="store_true", help="跳过 A 股 5 池行情")
    ap.add_argument("--skip-margin", action="store_true", help="跳过两融")
    ap.add_argument("--skip-lhb", action="store_true", help="跳过龙虎榜")
    ap.add_argument("--skip-block", action="store_true", help="跳过大宗交易")
    ap.add_argument("--skip-lift", action="store_true", help="跳过解禁日程")
    ap.add_argument("--force-lift", action="store_true",
                    help="强制跑解禁（默认只在周一跑，见下方说明）")
    ap.add_argument("--skip-fund", action="store_true", help="跳过财报季频补采")
    ap.add_argument("--python", default=None,
                    help="采集用的 python 解释器（缺省=sys.executable）")
    args = ap.parse_args()

    today = date.today()
    if not args.no_weekend_guard and today.weekday() >= 5:
        print(f"[topup] {today.isoformat()} 是周末，A 股无交易，跳过。")
        return 0

    since = args.since or today.isoformat()
    py = args.python or sys.executable

    report: list[str] = [f"[topup] since={since} python={py}"]

    if args.skip_quote:
        report.append("A股行情: --skip-quote 已跳过")
        print(report[-1], flush=True)
    else:
        for db in POOLS:
            dbpath = OUT / db
            if not dbpath.exists():
                report.append(f"行情 {db}: 库缺失，跳过")
                print(report[-1])
                continue
            cmd = [py, str(SC / "ashare_xq_collect.py"),
                   "--db", str(dbpath), "--pool-db", str(dbpath),
                   "--since", since, "--sleep", "1.0"]
            r = run(cmd)
            report.append(f"行情 {db}: {tail_of(r)}")
            print(report[-1], flush=True)

    # 两融：--since = 库内全局最新（上次采到的最大 trade_date），无条件拉新增。
    # 不用「今天」：两融数据 T 日晚间才发布，若传今天而数据未发布，次日 since 前移
    # 会永久漏掉 T 日缺口。用全局最新做起点可兜底（幂等覆盖 + 拉之后新增）。
    altdb = OUT / "ashare_altdata.sqlite"
    if args.skip_margin:
        report.append("两融 margin: --skip-margin 已跳过")
        print(report[-1], flush=True)
    else:
        margin_since = None
        if altdb.exists():
            _c = sqlite3.connect(f"file:{altdb}?mode=ro", uri=True)
            try:
                margin_since = _c.execute(
                    "SELECT MAX(trade_date) FROM margin_daily").fetchone()[0]
            except Exception:  # noqa: BLE001 —— 表缺失等，退化为全历史起点
                margin_since = None
            _c.close()
        margin_since = margin_since or "2010-01-01"
        cmd = [py, str(SC / "ashare_altdata_collect.py"),
               "--src", "margin", "--all", "--since", margin_since]
        r = run(cmd)
        report.append(f"两融 margin (since {margin_since}): {tail_of(r)}")
        print(report[-1], flush=True)

    # ---- 港股日K（投研标的池，非 alpha 线）----
    # 标的 = universe(included=1)；count 必须 1600：collect 是快照语义，
    # 默认 320 会把现存 1600 行历史截断为 320 行。
    if args.skip_hk:
        report.append("港股日K: --skip-hk 已跳过")
        print(report[-1], flush=True)
    else:
        hkdb = OUT / "equity_fundamental.sqlite"
        hk_syms: list[str] = []
        if hkdb.exists():
            _c = sqlite3.connect(f"file:{hkdb}?mode=ro", uri=True)
            try:
                hk_syms = [r[0] for r in _c.execute(
                    "SELECT ticker FROM universe WHERE COALESCE(included,1)=1 "
                    "ORDER BY ticker")]
            except Exception as e:  # noqa: BLE001
                report.append(f"港股日K: universe 读取失败 {e}")
            _c.close()
        if hk_syms:
            cmd = [py, str(SC / "equity_quotes.py"), "--db", str(hkdb),
                   "--symbols", ",".join(hk_syms), "--count", "1600", "collect"]
            r = run(cmd)
            ok = (r.stdout or "").count("[collect]")
            report.append(f"港股日K ({len(hk_syms)} 只, count=1600): "
                          f"{ok} 只成功 / {tail_of(r)}")
        else:
            report.append("港股日K: universe 为空，跳过")
        print(report[-1], flush=True)

    # ---- 龙虎榜（按交易日全市场，日增量成本 = 每天 1 次请求）----
    # 判死的是「用龙虎榜做交易信号」，不是「龙虎榜数据无价值」：
    # 它是描述性风险事件（异动/资金博弈事实），投研助手需按日留存。
    lhb_since = None
    if altdb.exists():
        _c = sqlite3.connect(f"file:{altdb}?mode=ro", uri=True)
        try:
            lhb_since = _c.execute(
                "SELECT MAX(trade_date) FROM lhb_daily").fetchone()[0]
        except Exception:  # noqa: BLE001
            lhb_since = None
        _c.close()
    lhb_start = _next_day(lhb_since) if lhb_since else since
    if args.skip_lhb:
        report.append("龙虎榜 lhb: --skip-lhb 已跳过")
    elif lhb_start > since:
        report.append(f"龙虎榜 lhb: 库内最新 {lhb_since} 已 >= 目标 {since}，跳过")
    else:
        cmd = [py, str(SC / "ashare_altdata_collect.py"),
               "--src", "lhb", "--start", lhb_start, "--end", since]
        r = run(cmd)
        report.append(f"龙虎榜 lhb ({lhb_start}..{since}): {tail_of(r)}")
    print(report[-1], flush=True)

    # ---- 大宗交易 block（描述性风险事件，判死≠禁采）----
    # 起点同 margin 用库内全局 MAX 兜底；--since 时只拉新增页，780 只约几分钟。
    if args.skip_block:
        report.append("大宗 block: --skip-block 已跳过")
        print(report[-1], flush=True)
    else:
        block_since = None
        if altdb.exists():
            _c = sqlite3.connect(f"file:{altdb}?mode=ro", uri=True)
            try:
                block_since = _c.execute(
                    "SELECT MAX(trade_date) FROM block_trade").fetchone()[0]
            except Exception:  # noqa: BLE001
                block_since = None
            _c.close()
        block_since = block_since or since
        cmd = [py, str(SC / "ashare_altdata_collect.py"),
               "--src", "block", "--all", "--since", block_since]
        r = run(cmd)
        report.append(f"大宗 block (since {block_since}): {tail_of(r)}")
        print(report[-1], flush=True)

    # ---- 解禁 lift（未来事件日程表）----
    # 起点用「今天」而非库内 MAX：解禁日期跨度到 2034，MAX 无意义；要补的是
    # 新公告的未来解禁。接口支持 FREE_DATE 过滤 ⇒ 只拉未来。
    # ★ 降频：实测 780 只耗时 1012s（≈17 分钟，受 fetch_all 节流 sleep 支配），
    #   而解禁是低频事件日程（新公告月频量级），日采性价比低 ⇒ 默认只在周一跑。
    #   需要立即刷新时加 --force-lift。
    lift_due = (today.weekday() == 0) or args.force_lift or bool(args.since)
    if args.skip_lift:
        report.append("解禁 lift: --skip-lift 已跳过")
        print(report[-1], flush=True)
    elif not lift_due:
        report.append(f"解禁 lift: 今日周{today.weekday() + 1}，非周一跳过"
                      f"（--force-lift 可强制）")
        print(report[-1], flush=True)
    else:
        cmd = [py, str(SC / "ashare_altdata_collect.py"),
               "--src", "lift", "--all", "--since", since]
        r = run(cmd)
        report.append(f"解禁 lift (since {since}): {tail_of(r)}")
        print(report[-1], flush=True)

    # ---- 财报季频补采（幂等：--resume 只补未采期）----
    # ★ 2026-09-06 修复：ashare_fund_collect.py 原 --end-year 默认硬编码 2025，
    #   2026 年报告期永不采集 ⇒ fund_reports 停在 2025-12-31 ⇒ 判决书
    #   label_agri_np_yoy 取「最新一期」拿到 18 个月前的年报（95 只中 47 只标签翻转）。
    #   现默认改为当前年；此处再显式传年份，空期记 status='empty'（--resume 会重试）。
    if args.skip_fund:
        report.append("财报 fund: --skip-fund 已跳过")
        print(report[-1], flush=True)
    else:
        y = date.today().year
        for price_db, out_db in FUND_POOLS:
            pdb, odb = OUT / price_db, OUT / out_db
            if not pdb.exists():
                report.append(f"财报 {out_db}: 价格库缺失，跳过")
                print(report[-1], flush=True)
                continue
            cmd = [py, str(SC / "ashare_fund_collect.py"),
                   "--price-db", str(pdb), "--out-db", str(odb),
                   "--start-year", str(y - 1), "--end-year", str(y), "--resume"]
            r = run(cmd)
            report.append(f"财报 {out_db}: {tail_of(r)}")
            print(report[-1], flush=True)

    print("\n".join(["\n[topup 汇总]"] + report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
