#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""A股农业池【后复权】日线采集 —— 腾讯行情 (hfq)　　⛔ 已停用（2026-09-06）

⛔⛔ 本采集器的数据源已被证实污染，**禁止再用于生产采集** ⛔⛔
------------------------------------------------------------------
2026-09-05 跨源核对结论：腾讯 fqkline 的 **hfq 后复权**存在系统性污染——
无除权日应满足恒等式 `hfq 日收益 == raw 日收益`，实测腾讯偏差 max 3.57%、
p50 0.20%（对照组雪球 max 2.8e-6，即 4 位小数舍入噪声）。

本文件仅保留作**考古/对照**用途：
  - `ashare_crosscheck_data.py`（跨源核对）
  - `ashare_pollution_quantify.py`（污染量化）
  - `ashare_retest_borderline.py`（A/B 复测的旧源一侧）
  上述脚本需要旧源数据时，请显式传 `allow_deprecated=True` 并书面说明。

生产替代品：`ashare_xq_collect.py --db outputs/<池>_hfq_xq.sqlite`（雪球源）。
若你只是想「重新采一遍农业池」，请改用上面那条命令，不要跑本脚本——
跑它会把污染数据重新写回 `ashare_agri_hfq.sqlite`。

--------------------------------------------------------------------------------
以下为停用前的原始说明（保留以解释历史设计决策）

为什么必须有这张表
------------------
前复权（fqt=1）在长周期 + 高分红标的上会被扣成**负数或接近 0**，导致：
  - 日收益出现 ±833% 的荒谬值（实测 牧原股份 002714 前复权最低 -3.60 元，
    506 个交易日价格 <= 0.5，另有 23 只最低价被压到 0~4 元区间）
  - 这是**股票级**缺陷，截断时间区间无法规避
后复权（hfq）以上市首日为锚向后累加，价格恒为正、日收益必然落在涨跌停限内，
是唯一可用于收益率计算的口径。

为什么改用腾讯而不是东财
------------------------
1. 东财 push2his.eastmoney.com 在批量采集后被沙箱代理拒绝连接（实测
   ProxyError，curl 直接 000；同一时刻百度/代理均 200）→ 数据源单点故障
2. 腾讯 web.ifzq.gtimg.cn 的 fqkline 原生支持 hfq，且未被封锁
3. 【交叉校验】与东财 badj 在 000798 上重叠 2957 个交易日：
     价格比值恒定 1.364（仅锚点不同）
     日收益差异 中位数 2.8e-4、p99 1.3e-3、最大 1.8e-3
   → 日波动 2~3% 量级下，该差异远低于噪声地板，可忽略

接口约束（实测）
----------------
- 单请求最多 641 根 K 线，count 传更大值会返回 {"code":0,"msg":"param error","data":[]}
  → 必须按约 2 年一段分段抓取后合并
- 返回结构：data.<symbol>.hfqday = [[date, open, close, high, low, volume], ...]
  ⚠️ 键名是 **hfqday**（不是 hfq）；不复权为 day，前复权为 qfqday
- hfq 无成交额/换手率，volume 单位为手（与东财一致），成交额与换手率从
  daily_quotes_raw 按 (code, trade_date) 关联

用法
----
  python ashare_badj_collect.py                # 采集全部（自动跳过已完成的）
  python ashare_badj_collect.py --resume       # 只补采缺失的
  python ashare_badj_collect.py --codes 002714,000048
  python ashare_badj_collect.py --since 2018-01-01     # 只采近年（更快）

vintage 纪律：只增不修；每条带 source + collected_at；失败记入 collect_log_badj。
"""
import argparse
import sqlite3
import sys
import time
from datetime import datetime, date
from pathlib import Path

import numpy as np
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from requests.adapters import HTTPAdapter

_HERE = Path(__file__).resolve().parent
ROOT = _HERE.parents[3]
# ⚠️ 写独立库，不写 outputs/ashare_agri.sqlite：
#    主库可能被另一会话的采集进程持有写锁（实测只读可用、写入必报
#    "database is locked"），且「学习产物只写独立库」本就是项目纪律。
#    成交额/换手率等流动性字段从主库以只读方式复制过来，保证分析自洽。
DB = ROOT / "outputs" / "ashare_agri_hfq.sqlite"
SRC_DB = ROOT / "outputs" / "ashare_agri.sqlite"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

# ⚠️ 域名级封锁：批量采集约 450 次请求后，web.ifzq.gtimg.cn 会返回 HTTP 501，
#    而 ifzq.gtimg.cn 与 proxy.finance.qq.com 仍可用（实测同一时刻三者对比：
#    web.* =501 / ifzq.* =200 / proxy.finance =200）。
#    → 必须多域名候选 + 失败自动切换，不能写死单一 host。
HOSTS = [
    "https://ifzq.gtimg.cn/appstock/app/fqkline/get",
    "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/fqkline/get",
    "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
]
SOURCE = "tencent-fqkline-hfq"

# 分段抓取：单请求上限 641 根。
# ★★ 2026-09-02 教训：末段不能是开放大窗口 —— 旧硬编码 CHUNKS 的末段
#    ("2024-01-01","2026-12-31") 随时间自然增长，2026-09 已 ~645 根 > 641，
#    腾讯【静默截掉响应头部】→ 全部存量库在 2024-01-02~01-10 一段系统性缺 bar
#    （实测 agri 92/95、semi 68/79、wide 944/996 只受染），且脚本 QC 不查缺口、
#    照常通过。改为动态生成固定 18 个月窗口（≈370 根，远离上限），永不增长。
CHUNK_YEARS = 1.5
CHUNK_CAP_WARN = 630   # 单段返回接近此数即告警（防御：未来口径变化再触发）


def build_chunks(since: str, today: str):
    """从 since 到 today 生成固定 18 个月（548 天）的抓取窗口（≈370 根/段）。"""
    from datetime import date, timedelta
    d0 = date.fromisoformat(since.replace("/", "-"))
    d1 = date.fromisoformat(today.replace("/", "-"))
    step = timedelta(days=548)
    chunks, a = [], d0
    while a <= d1:
        b = min(a + step - timedelta(days=1), d1)
        chunks.append((a.isoformat(), b.isoformat()))
        a = b + timedelta(days=1)
    return chunks


def board_of(code: str):
    """返回 (板块, 涨跌幅限制)。北交所 ±30%、创业板/科创板 ±20%、主板 ±10%。
    B 股（200/900 开头）为外币计价，不参与 A 股量化池。"""
    if code.startswith(("920", "430", "83", "87", "92")):
        return "BSE", 0.30
    if code.startswith(("200", "900")):
        return "BSHARE", 0.10
    if code.startswith(("300", "301", "302", "688", "689")):
        return "CREA/STAR", 0.20
    return "MAIN", 0.10


# 默认剔除：北交所（日成交额常不足千万，无法建仓；腾讯亦不提供后复权）
#            + B 股（外币计价，境内策略不涉及）
EXCLUDE_BOARDS = {"BSE", "BSHARE"}


def tx_symbol(code: str) -> str:
    """A股代码 → 腾讯 symbol。"""
    if code.startswith(("920", "430", "83", "87", "92")):
        return "bj" + code
    if code.startswith(("60", "68", "9", "5")):
        return "sh" + code
    return "sz" + code


def ensure_schema(conn: sqlite3.Connection):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS daily_quotes_hfq (
        code          TEXT NOT NULL,
        trade_date    TEXT NOT NULL,
        open          REAL, close REAL, high REAL, low REAL,
        volume        REAL,
        source        TEXT NOT NULL,
        collected_at  TEXT NOT NULL,
        PRIMARY KEY (code, trade_date)
    );
    CREATE INDEX IF NOT EXISTS ix_hfq_date ON daily_quotes_hfq(trade_date);
    CREATE INDEX IF NOT EXISTS ix_hfq_code ON daily_quotes_hfq(code);

    CREATE TABLE IF NOT EXISTS hfq_qc (
        code            TEXT NOT NULL,
        collected_at    TEXT NOT NULL,
        n_bars          INTEGER,
        nonpositive     INTEGER,     -- 价格 <= 0 的天数（后复权必须为 0）
        over_limit_days INTEGER,     -- 日收益超涨跌停限的天数
        ret_min         REAL, ret_max REAL,
        limit_used      REAL,
        bad             INTEGER,
        PRIMARY KEY (code, collected_at)
    );

    CREATE TABLE IF NOT EXISTS collect_log_badj (
        code TEXT, collected_at TEXT, status TEXT, bars INTEGER,
        date_start TEXT, date_end TEXT, error TEXT
    );

    CREATE TABLE IF NOT EXISTS daily_liquidity (
        code          TEXT NOT NULL,
        trade_date    TEXT NOT NULL,
        volume        REAL, amount REAL, turnover_pct REAL,
        source        TEXT, collected_at TEXT,
        PRIMARY KEY (code, trade_date)
    );
    CREATE INDEX IF NOT EXISTS ix_liq_code ON daily_liquidity(code);
    """)
    conn.commit()


def copy_liquidity(conn: sqlite3.Connection) -> int:
    """从主库（只读）复制成交量/成交额/换手率到独立库。

    腾讯 hfq 接口只给成交量（手），没有成交额与换手率；而 amihud、换手率等
    因子必须用到成交额。主库 daily_quotes_raw 里已有东财的这三个字段，
    按 (code, trade_date) 对齐即可。
    """
    n = conn.execute("SELECT COUNT(*) FROM daily_liquidity").fetchone()[0]
    if n:
        return 0
    src = sqlite3.connect(f"file:{SRC_DB}?mode=ro", uri=True, timeout=30)
    rows = src.execute(
        "SELECT code, trade_date, volume, amount, turnover_pct, source, collected_at "
        "FROM daily_quotes_raw").fetchall()
    src.close()
    conn.executemany(
        "INSERT OR REPLACE INTO daily_liquidity "
        "(code,trade_date,volume,amount,turnover_pct,source,collected_at) "
        "VALUES (?,?,?,?,?,?,?)", rows)
    conn.commit()
    return len(rows)


def _fetch_chunk(sym: str, a2: str, b: str, session, sleep: float):
    """抓【一个分段】。返回 (rows, err)。

    ⚠️ 晚于 2014 年上市的次新股，早期分段会返回
       data.<sym> = {"day":[], "qt":{...}} —— 没有 hfqday 键，数组为空。
       这是**正常情况**，不是限流，绝不能当成致命错误，否则整只股票会被
       放弃（实测 601952/603151/688098 等 12 只次新股因此全部误判失败）。
    """
    prm = f"{sym},day,{a2},{b},640,hfq"
    last = None
    for host in HOSTS:                       # 域名级封锁 → 逐个候选重试
        for attempt in range(3):
            try:
                r = session.get(host, params={"param": prm},
                                headers={"User-Agent": UA}, timeout=30)
                j = r.json()
                d = j.get("data")
                if not isinstance(d, dict):
                    raise RuntimeError(f"param error / empty: {str(j)[:80]}")
                rows = d.get(sym, {}).get("hfqday")
                if rows is not None and len(rows) >= CHUNK_CAP_WARN:
                    # ★ 静默截断前哨：641 为硬上限，返回接近上限说明窗口过大，
                    #   响应头部可能已被裁掉（2026-09-02 存量库 2024-01 缺口教训）
                    print(f"      ⚠ {sym} {a2}~{b} 返回 {len(rows)} 根接近上限，"
                          "窗口可能过大，建议缩小分段")
                return (rows if rows is not None else []), None
            except Exception as e:           # 网络抖动 / 限流，退避后换域名
                last = e
                time.sleep(sleep * (2 ** attempt))
    return None, last


def fetch_one(code: str, since: str, session: requests.Session, sleep: float,
              workers: int = 6):
    """分段抓取一只股票的后复权日线。返回 {date: (o,c,h,l,v)} 或抛异常。

    ★ 6 个分段【并发】请求（默认 workers=6）
        实测单请求往返约 3 秒（纯网络延迟，非限流）—— 串行 6 段就是 18 秒/只，
        1000 只要 5.2 小时。分段之间彼此独立，完全可以重叠等待。
        并发后单只耗时 ≈ 3s + sleep，实测提速约 5 倍。
        ⚠️ 6 并发是刻意保守的取值：腾讯行情接口约 450 次请求后会对单个域名
           返回 HTTP 501，本脚本有 3 域名轮换兜底，不宜再提高并发度。
    """
    sym = tx_symbol(code)
    jobs = [(max(a, since), b)
            for a, b in build_chunks(since, date.today().isoformat())
            if not b < since]
    bars = {}
    n_err = 0
    last_err = None
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(jobs)))) as ex:
        futs = {ex.submit(_fetch_chunk, sym, a2, b, session, sleep): (a2, b)
                for a2, b in jobs}
        for f in as_completed(futs):
            a2, b = futs[f]
            rows, err = f.result()
            if rows is None:
                n_err += 1
                last_err = err
                # 分段级网络错误只记录，不中断：只要其它分段拿到了数据就还能用
                print(f"      ⚠ {a2}~{b} 请求失败（{err}），继续下一段")
                continue
            for row in rows:
                bars[row[0]] = (float(row[1]), float(row[2]), float(row[3]),
                                float(row[4]), float(row[5]))
    if not bars:
        raise RuntimeError(f"全部分段均无数据：{last_err}")
    return bars


def qc(code: str, bars: dict) -> dict:
    """后复权质量校验。后复权价格恒为正、日收益必然落在涨跌停限内 —— 超限即数据源异常。"""
    _, lim = board_of(code)
    dts = sorted(bars)
    px = np.array([bars[d][1] for d in dts], float)   # close
    r = px[1:] / px[:-1] - 1.0
    # 上市/恢复上市前 5 个交易日不设涨跌幅（注册制），不计入超限
    #
    # ★ 阈值修正（2026-09-03）：原为 lim * 1.15（主板即 11.5%），会放过
    #   10%~11.5% 区间的全部异常日 —— 实测 600416 因此只报 10 天、实则 86 天。
    #   正确上界：涨停价 = round(prev_close * (1+lim), 2)，分位舍入带来的
    #   相对误差 < 0.1pp，故取 lim + 0.005（主板 10.5%）即可覆盖合法情形。
    tol = lim + 0.005
    over = int((np.abs(r[5:]) > tol).sum()) if len(r) > 5 else 0
    nonpos = int((px <= 0).sum())
    bad = int(nonpos > 0 or over > 0)
    return {"n_bars": len(px), "nonpositive": nonpos, "over_limit_days": over,
            "ret_min": float(np.nanmin(r)) if len(r) else float("nan"),
            "ret_max": float(np.nanmax(r)) if len(r) else float("nan"),
            "limit_used": lim, "bad": bad}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DB))
    ap.add_argument("--codes", default="", help="逗号分隔，只采这些代码")
    ap.add_argument("--resume", action="store_true", default=True,
                    help="跳过已有数据的代码（默认开启）")
    ap.add_argument("--all", action="store_true", help="强制重采全部")
    ap.add_argument("--since", default="2014-01-01")
    ap.add_argument("--sleep", type=float, default=0.35, help="每段之间间隔秒")
    args = ap.parse_args()

    # ⚠️ PRAGMA journal_mode=WAL 需要独占锁，若另一会话正持有数据库会直接抛
    #    "database is locked" → 容错跳过（不改日志模式也能写，只是并发读稍弱）
    conn = sqlite3.connect(args.db, timeout=30)
    try:
        conn.execute("PRAGMA journal_mode=WAL")   # 允许采集进行中并发读
    except sqlite3.OperationalError as e:
        print(f"（跳过 WAL：{e}）")
    ensure_schema(conn)
    n_liq = copy_liquidity(conn)
    if n_liq:
        print(f"已从主库复制流动性字段: {n_liq} 行 → daily_liquidity")

    if args.codes:
        codes = [c.strip() for c in args.codes.split(",") if c.strip()]
    else:
        # 标的池以「主库成功采集的标的」为准（只读访问，不需要写锁）
        all_codes = [r[0] for r in conn.execute(
            "SELECT DISTINCT code FROM daily_liquidity ORDER BY code")]
        codes = [c for c in all_codes if board_of(c)[0] not in EXCLUDE_BOARDS]
        skipped = len(all_codes) - len(codes)
        if skipped:
            print(f"剔除北交所/B股 {skipped} 只 → 标的池 {len(codes)} 只"
                  f"（--include-bse 可强制保留）")
    if not codes:
        raise SystemExit(f"标的池为空：请确认主库可读 ({SRC_DB})")
    if args.resume and not args.all:
        done = {r[0] for r in conn.execute("SELECT DISTINCT code FROM daily_quotes_hfq")}
        codes = [c for c in codes if c not in done]
    print(f"待采集 {len(codes)} 只  起始 {args.since}  数据源 {SOURCE}")

    session = requests.Session()
    # ★ 并发请求需要足够大的连接池，否则会阻塞等待空闲连接，
    #   并发提速直接失效（默认 pool_maxsize 只有 10 且按 host 计）
    _ad = HTTPAdapter(pool_connections=16, pool_maxsize=16)
    session.mount('https://', _ad)
    session.mount('http://', _ad)
    ok = fail = 0
    bad_qc = []
    for i, code in enumerate(codes, 1):
        now = datetime.now().isoformat(timespec="seconds")
        try:
            bars = fetch_one(code, args.since, session, args.sleep)
            if not bars:
                raise RuntimeError("空数据")
            q = qc(code, bars)
            conn.executemany(
                "INSERT OR REPLACE INTO daily_quotes_hfq "
                "(code,trade_date,open,close,high,low,volume,source,collected_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                [(code, d, *bars[d], SOURCE, now) for d in sorted(bars)])
            conn.execute(
                "INSERT OR REPLACE INTO hfq_qc "
                "(code,collected_at,n_bars,nonpositive,over_limit_days,ret_min,ret_max,"
                "limit_used,bad) VALUES (?,?,?,?,?,?,?,?,?)",
                (code, now, q["n_bars"], q["nonpositive"], q["over_limit_days"],
                 q["ret_min"], q["ret_max"], q["limit_used"], q["bad"]))
            conn.execute(
                "INSERT INTO collect_log_badj (code,collected_at,status,bars,"
                "date_start,date_end,error) VALUES (?,?,?,?,?,?,?)",
                (code, now, "ok", q["n_bars"], min(bars), max(bars), None))
            conn.commit()
            ok += 1
            flag = f"  ⚠ QC异常 非正价={q['nonpositive']} 超限={q['over_limit_days']}" if q["bad"] else ""
            if q["bad"]:
                bad_qc.append(code)
            print(f"[{i}/{len(codes)}] {code} ✓ {q['n_bars']:>4}根 "
                  f"{min(bars)}~{max(bars)} ret[{q['ret_min']:+.2%},{q['ret_max']:+.2%}]{flag}")
        except Exception as e:
            conn.execute(
                "INSERT INTO collect_log_badj (code,collected_at,status,bars,"
                "date_start,date_end,error) VALUES (?,?,?,?,?,?,?)",
                (code, now, "fail", 0, None, None, str(e)[:300]))
            conn.commit()
            fail += 1
            print(f"[{i}/{len(codes)}] {code} ✗ {type(e).__name__}: {str(e)[:100]}")

    print("\n" + "=" * 60)
    print(f"成功 {ok}  失败 {fail}  QC异常 {len(bad_qc)}")
    if bad_qc:
        print("QC 异常代码:", ",".join(bad_qc))
    for t in ("daily_quotes_hfq", "hfq_qc"):
        n, m = conn.execute(
            f"SELECT COUNT(*), COUNT(DISTINCT code) FROM {t}").fetchone()
        print(f"{t}: {n} 行 / {m} 只")
    conn.close()


if __name__ == "__main__":
    main()
