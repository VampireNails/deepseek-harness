# -*- coding: utf-8 -*-
"""
方向 2 · 另类数据采集器（东财 datacenter / push2 系）
================================================================
数据源（2026-09-04 探针验证可回溯性）：
  margin  两融日度明细  RPTA_WEB_RZRQ_GGMX     个股全历史（2010 起，两融标的内）
  lhb     全市场龙虎榜  RPT_DAILYBILLBOARD_DETAILSNEW  按交易日（2014 起）
  block   大宗交易      RPT_DATA_BLOCKTRADE    个股全历史（2005 起）
  lift    限售解禁      RPT_LIFT_STAGE         个股全历史

硬约束（来自 SOP 与探针）：
  1. 东财一律 em_get 串行限流（间隔≥1s+抖动，会话复用）；push2his 被代理拦，勿用。
  2. page_size ≤ 500（实测上限），翻页到空为止。
  3. 逻辑主键（fail-loud 去重）：
       margin (code, trade_date)；lhb (trade_date, code) 同日多原因聚合成一行；
       lift  (code, free_date, limited_type)；block 无唯一约束（同日多笔为事实）。
  4. 幂等：INSERT OR REPLACE（重跑不产生重复行）。
  5. 断点续采：collect_log 记录每只/每日完成态，重跑自动跳过；失败重试 2 次后记
     status=error 并继续（不中断整批），最后汇总失败清单。
  6. 空数据（count=0）是合法结果，不等于失败 —— 探针教训：600519 解禁 count=None
     是"近年无解禁"，必须与端点错误区分（success=False 才算失败）。

用法：
  python ashare_altdata_collect.py --src margin --all [--limit N] [--db ...]
  python ashare_altdata_collect.py --src margin --codes 600519,000858
  python ashare_altdata_collect.py --src lhb --start 2014-01-02 [--end 2026-09-02]
  python ashare_altdata_collect.py --src block --all
  python ashare_altdata_collect.py --src lift  --all
"""
import argparse
import os
import random
import sqlite3
import sys
import time
from datetime import datetime

import requests

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
DATACENTER_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
EM_SESSION = requests.Session()
EM_SESSION.headers.update({"User-Agent": UA})
EM_MIN_INTERVAL = 1.0  # 批量采集 1.0s + 抖动即可（QPS≤2 安全区）
_em_last = [0.0]

DB_DEFAULT = "D:/tmp/deepseek-harness/outputs/ashare_altdata.sqlite"
HFQ_DB = "D:/tmp/deepseek-harness/outputs/ashare_csi800_hfq_xq.sqlite"

# ------------------------------------------------------------------ HTTP


def em_get(url, params=None, headers=None, timeout=20, **kw):
    wait = EM_MIN_INTERVAL - (time.time() - _em_last[0])
    if wait > 0:
        time.sleep(wait + random.uniform(0.1, 0.35))
    try:
        return EM_SESSION.get(url, params=params, headers=headers, timeout=timeout, **kw)
    finally:
        _em_last[0] = time.time()


def dc_page(report_name, filter_str, page_number, page_size=500,
            sort_columns="", sort_types="-1"):
    """单页查询。返回 (ok, rows, count)。ok=False 仅当 HTTP/解析失败；
    空数据（东财语义：success=False + message='返回数据为空'）视为
    ok=True, rows=[], count=0 —— 必须与真错误区分（600519 无解禁=合法空）。"""
    params = {
        "reportName": report_name, "columns": "ALL",
        "filter": filter_str, "pageNumber": str(page_number),
        "pageSize": str(page_size), "sortColumns": sort_columns,
        "sortTypes": sort_types, "source": "WEB", "client": "WEB",
    }
    try:
        r = em_get(DATACENTER_URL, params=params, timeout=25)
        if r.status_code != 200:
            return False, [], None
        d = r.json()
        res = d.get("result") or {}
        if d.get("success"):
            return True, res.get("data") or [], res.get("count")
        msg = str(d.get("message") or "")
        if "返回数据为空" in msg:
            return True, [], 0  # 合法空数据，不是错误
        return False, [], None
    except Exception:  # noqa: BLE001 —— 采集器吞网络异常由调用方重试
        return False, [], None


def fetch_all(report_name, filter_str, sort_columns="", sort_types="-1",
              retries=2):
    """翻页取全量。返回 (rows, error_or_None)。空数据 → ([], None)。"""
    rows = []
    for attempt in range(retries + 1):
        ok, page, count = dc_page(report_name, filter_str, 1,
                                  sort_columns=sort_columns, sort_types=sort_types)
        if not ok:
            if attempt < retries:
                time.sleep(2.5 * (attempt + 1))
                continue
            return [], f"page1 fail after {retries + 1} tries"
        rows = list(page)
        total = count if count is not None else len(page)
        page_no = 1
        while page_no * 500 < total:
            ok, page, _ = dc_page(report_name, filter_str, page_no + 1,
                                  sort_columns=sort_columns, sort_types=sort_types)
            if not ok:
                if attempt < retries:
                    break  # 内层翻页失败：整只重试
                return [], f"page{page_no + 1} fail"
            rows.extend(page)
            page_no += 1
            if not page:  # 保护：服务端 count 与实际页不符时防死循环
                break
        return rows, None
    return [], "unreachable"


# ------------------------------------------------------------------ DB

SCHEMA = """
CREATE TABLE IF NOT EXISTS margin_daily(
  code TEXT NOT NULL, trade_date TEXT NOT NULL,
  rzye REAL, rzmre REAL, rzche REAL, rqye REAL, rqmcl REAL, rqchl REAL, rzrqye REAL,
  collected_at TEXT NOT NULL,
  PRIMARY KEY(code, trade_date));
CREATE TABLE IF NOT EXISTS lhb_daily(
  trade_date TEXT NOT NULL, code TEXT NOT NULL, name TEXT,
  net_amt REAL, buy_amt REAL, sell_amt REAL,
  turnover_rate REAL, close REAL, change_rate REAL,
  reasons TEXT,   -- 同日多原因 ';' 合并
  collected_at TEXT NOT NULL,
  PRIMARY KEY(trade_date, code));
CREATE TABLE IF NOT EXISTS block_trade(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  code TEXT NOT NULL, trade_date TEXT NOT NULL,
  deal_price REAL, close_price REAL, premium_ratio REAL,
  deal_volume REAL, deal_amount REAL, buyer TEXT, seller TEXT,
  collected_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_block_code ON block_trade(code, trade_date);
CREATE TABLE IF NOT EXISTS lift_stage(
  code TEXT NOT NULL, free_date TEXT NOT NULL, limited_type TEXT,
  free_shares REAL, free_ratio REAL,
  collected_at TEXT NOT NULL,
  PRIMARY KEY(code, free_date, limited_type));
CREATE TABLE IF NOT EXISTS collect_log(
  src TEXT NOT NULL, key TEXT NOT NULL, status TEXT NOT NULL,
  n_rows INTEGER, detail TEXT, collected_at TEXT NOT NULL,
  PRIMARY KEY(src, key));
"""


def short_date(v):
    if not v:
        return ""
    return str(v)[:10]


def connect_db(path):
    c = sqlite3.connect(path)
    c.executescript(SCHEMA)
    return c


def log_done(conn, src, key, n, detail=""):
    conn.execute(
        "INSERT OR REPLACE INTO collect_log(src,key,status,n_rows,detail,collected_at)"
        " VALUES(?,?,?,?,?,?)",
        (src, key, "done", n, detail[:200], datetime.now().isoformat(timespec="seconds")))


def log_err(conn, src, key, detail=""):
    conn.execute(
        "INSERT OR REPLACE INTO collect_log(src,key,status,n_rows,detail,collected_at)"
        " VALUES(?,?,?,?,?,?)",
        (src, key, "error", 0, detail[:200], datetime.now().isoformat(timespec="seconds")))


def already_done(conn, src, key):
    row = conn.execute(
        "SELECT status FROM collect_log WHERE src=? AND key=?", (src, key)).fetchone()
    return row is not None and row[0] == "done"


# ------------------------------------------------------------------ 各源

def collect_margin(conn, codes, args):
    now = datetime.now().isoformat(timespec="seconds")
    done = err = 0
    # 日期级增量（--since）：无条件拉 DATE >= since 的新增 + 幂等覆盖，不跳过。
    # 为什么不用 fresh 跳过：东财「是否已有 since 之后的新数据」无法从库内判断；
    # 若数据发布晚于调度时点，次日 since 前移会把旧缺口永久漏掉。无条件拉（返回空
    # = 无新增）才是稳健的日常增量。缺省（无 --since）仍走 code 粒度断点续采。
    since = getattr(args, "since", None)
    if since:
        print(f"[margin] --since {since}: 无条件拉 DATE >= since 的新增（幂等覆盖）")
    for code in codes:
        key = code
        if not since and already_done(conn, "margin", key):
            continue
        # 增量模式（--since）只拉 since 之后的日期：margin 全历史约 3800 行=8 页，
        # 每日补数若全量重拉会 780×9s≈2 小时 + 大量请求（封 IP 风险）。margin 是
        # 绝对数值（融资/融券余额），非复权，无「历史值随除权漂移」问题，可安全增量。
        flt = f'(SCODE="{code}")(DATE>=\'{since}\')' if since else f'(SCODE="{code}")'
        rows, e = fetch_all("RPTA_WEB_RZRQ_GGMX", flt,
                            sort_columns="DATE", sort_types="-1")
        if e:
            log_err(conn, "margin", key, e)
            err += 1
            conn.commit()
            continue
        recs = []
        seen = set()
        dup = 0
        for x in rows:
            d = short_date(x.get("DATE"))
            if d in seen:
                dup += 1
                continue
            seen.add(d)
            recs.append((code, d,
                         x.get("RZYE"), x.get("RZMRE"), x.get("RZCHE"),
                         x.get("RQYE"), x.get("RQMCL"), x.get("RQCHL"),
                         x.get("RZRQYE"), now))
        conn.executemany(
            "INSERT OR REPLACE INTO margin_daily VALUES(?,?,?,?,?,?,?,?,?,?)", recs)
        # ★ 增量模式不写断点续采表：本轮只拉了 since 之后的片段，若记 done=已采，
        #   日后全量重采会被 already_done 全部跳过 ⇒ 数据永久残缺（第卅五类）。
        if not since:
            log_done(conn, "margin", key, len(recs), f"dup={dup}")
        conn.commit()
        done += 1
        if args.limit and done >= args.limit:
            break
    return done, err


def collect_lhb(conn, dates, args):
    done = err = 0
    for d in dates:
        if already_done(conn, "lhb", d):
            continue
        rows, e = fetch_all("RPT_DAILYBILLBOARD_DETAILSNEW",
                            f"(TRADE_DATE>='{d}')(TRADE_DATE<='{d}')",
                            sort_columns="BILLBOARD_NET_AMT", sort_types="-1")
        if e:
            log_err(conn, "lhb", d, e)
            err += 1
            conn.commit()
            continue
        now = datetime.now().isoformat(timespec="seconds")
        agg = {}  # (code) -> rec；同日多原因合并为一行（逻辑主键）
        for x in rows:
            code = str(x.get("SECURITY_CODE", ""))
            rec = agg.setdefault(code, {
                "trade_date": d, "code": code,
                "name": x.get("SECURITY_NAME_ABBR", ""),
                "net_amt": 0.0, "buy_amt": 0.0, "sell_amt": 0.0,
                "turnover_rate": x.get("TURNOVERRATE"),
                "close": x.get("CLOSE_PRICE"), "change_rate": x.get("CHANGE_RATE"),
                "reasons": set()})
            rec["net_amt"] += (x.get("BILLBOARD_NET_AMT") or 0)
            rec["buy_amt"] += (x.get("BILLBOARD_BUY_AMT") or 0)
            rec["sell_amt"] += (x.get("BILLBOARD_SELL_AMT") or 0)
            if x.get("EXPLANATION"):
                rec["reasons"].add(str(x.get("EXPLANATION")))
        recs = []
        for r in agg.values():
            recs.append((r["trade_date"], r["code"], r["name"],
                         r["net_amt"], r["buy_amt"], r["sell_amt"],
                         r["turnover_rate"], r["close"], r["change_rate"],
                         ";".join(sorted(r["reasons"])), now))
        conn.executemany("INSERT OR REPLACE INTO lhb_daily VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                         recs)
        log_done(conn, "lhb", d, len(recs))
        conn.commit()
        done += 1
        if args.limit and done >= args.limit:
            break
    return done, err


def collect_block(conn, codes, args):
    done = err = 0
    # --since 日期级增量：无条件拉 TRADE_DATE >= since 的新增 + INSERT OR REPLACE 幂等
    # 覆盖。大宗是描述性风险事件（判死≠禁采），日采必须用增量否则 780 只全历史重拉。
    # 缺省 --since 时仍走 code 粒度断点续采（与 margin 同构）。
    since = getattr(args, "since", None)
    for code in codes:
        if not since and already_done(conn, "block", code):
            continue
        flt = (f'(SECURITY_CODE="{code}")(TRADE_DATE>=\'{since}\')'
               if since else f'(SECURITY_CODE="{code}")')
        rows, e = fetch_all("RPT_DATA_BLOCKTRADE", flt,
                            sort_columns="TRADE_DATE", sort_types="1")
        if e:
            log_err(conn, "block", code, e)
            err += 1
            conn.commit()
            continue
        now = datetime.now().isoformat(timespec="seconds")
        recs = [(code, short_date(x.get("TRADE_DATE")),
                 x.get("DEAL_PRICE"), x.get("CLOSE_PRICE"),
                 x.get("PREMIUM_RATIO"), x.get("DEAL_VOLUME"),
                 x.get("DEAL_AMT"), x.get("BUYER_NAME"), x.get("SELLER_NAME"), now)
                for x in rows]
        conn.executemany("INSERT OR REPLACE INTO block_trade"
                         "(code,trade_date,deal_price,close_price,premium_ratio,"
                         "deal_volume,deal_amount,buyer,seller,collected_at)"
                         " VALUES(?,?,?,?,?,?,?,?,?,?)", recs)
        if not since:      # 同 margin：增量模式不写断点表
            log_done(conn, "block", code, len(recs))
        conn.commit()
        done += 1
        if args.limit and done >= args.limit:
            break
    return done, err


def collect_lift(conn, codes, args):
    done = err = 0
    # --since 日期级增量：解禁是**未来事件日程表**（库内 2010~2034），日采只需补
    # 新增的未来解禁公告；历史解禁已入库且不变。接口实测支持 FREE_DATE 过滤
    # （002157 全历史 40 行 → FREE_DATE>=2026-09-01 剩 5 行），成本 1.2s → 0.1s/只。
    # 缺省 --since 仍走 code 粒度断点续采（与 margin/block 同构）。
    since = getattr(args, "since", None)
    for code in codes:
        if not since and already_done(conn, "lift", code):
            continue
        flt = (f'(SECURITY_CODE="{code}")(FREE_DATE>=\'{since}\')'
               if since else f'(SECURITY_CODE="{code}")')
        rows, e = fetch_all("RPT_LIFT_STAGE", flt,
                            sort_columns="FREE_DATE", sort_types="1")
        if e:
            log_err(conn, "lift", code, e)
            err += 1
            conn.commit()
            continue
        now = datetime.now().isoformat(timespec="seconds")
        recs = []
        for x in rows:
            lt = x.get("LIMITED_STOCK_TYPE")
            recs.append((code, short_date(x.get("FREE_DATE")),
                         str(lt) if lt else "",
                         x.get("FREE_SHARES_NUM"), x.get("FREE_RATIO"), now))
        conn.executemany("INSERT OR REPLACE INTO lift_stage VALUES(?,?,?,?,?,?)", recs)
        if not since:      # 同 margin/block：增量模式不写断点表
            log_done(conn, "lift", code, len(recs))
        conn.commit()
        done += 1
        if args.limit and done >= args.limit:
            break
    return done, err


# ------------------------------------------------------------------ 主入口

def load_csi800_codes():
    """从后复权库取成分代码（780 只，含采集失败的对照）。"""
    c = sqlite3.connect(HFQ_DB)
    codes = [r[0] for r in c.execute("SELECT DISTINCT code FROM daily_quotes_hfq")]
    c.close()
    return sorted(codes)


def trade_calendar(start, end):
    """从后复权库取交易日序列（成分股有报价的日期 = 交易日近似）。"""
    c = sqlite3.connect(HFQ_DB)
    rows = [r[0] for r in c.execute(
        "SELECT DISTINCT trade_date FROM daily_quotes_hfq"
        " WHERE trade_date>=? AND trade_date<=? ORDER BY trade_date",
        (start, end))]
    c.close()
    return rows


def _print_errors(conn, src):
    bad = conn.execute(
        "SELECT key,detail FROM collect_log WHERE src=? AND status='error'"
        " ORDER BY collected_at DESC LIMIT 10", (src,)).fetchall()
    if bad:
        print("  失败清单（最多 10 条）:")
        for k, d in bad:
            print(f"    {k}: {d}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True, choices=["margin", "lhb", "block", "lift"])
    ap.add_argument("--db", default=DB_DEFAULT)
    ap.add_argument("--codes", default="", help="逗号分隔；缺省用 --all")
    ap.add_argument("--all", action="store_true", help="CSI800 全部 780 只")
    ap.add_argument("--limit", type=int, default=0, help="pilot：最多处理 N 个 key")
    ap.add_argument("--start", default="2014-01-02")
    ap.add_argument("--end", default="2026-09-02")
    ap.add_argument("--since", default=None,
                    help="日期级增量（margin 用）：只补采库内最新 trade_date < since 的股票")
    args = ap.parse_args()

    t0 = time.time()
    conn = connect_db(args.db)

    if args.src == "lhb":
        # 龙虎榜按交易日回填，不需要股票清单
        dates = trade_calendar(args.start, args.end)
        print(f"[lhb] 交易日历 {args.start}~{args.end} 共 {len(dates)} 日")
        done, err = collect_lhb(conn, dates, args)
        print(f"[lhb] 完成 {done} 日，失败 {err} 日，耗时 {time.time() - t0:.0f}s")
        _print_errors(conn, "lhb")
        conn.close()
        return

    if args.codes:
        codes = [c.strip() for c in args.codes.split(",") if c.strip()]
    elif args.all:
        codes = load_csi800_codes()
    else:
        sys.exit("需指定 --codes 或 --all")

    if args.src == "margin":
        done, err = collect_margin(conn, codes, args)
    elif args.src == "block":
        done, err = collect_block(conn, codes, args)
    else:
        done, err = collect_lift(conn, codes, args)

    print(f"[{args.src}] 完成 {done} 个 key，失败 {err} 个，耗时 {time.time() - t0:.0f}s")
    _print_errors(conn, args.src)
    conn.close()


if __name__ == "__main__":
    main()
