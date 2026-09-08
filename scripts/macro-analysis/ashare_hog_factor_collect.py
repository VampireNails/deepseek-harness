#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ashare_hog_factor_collect.py — 生猪产业链基本面数据采集（供「猪价 beta × 猪价动量」因子）

为什么需要这些数据
------------------
农业池（BK0433，109 只）内不同个股对猪价的敏感度差异极大：牧原这类纯养殖对猪价
高敏感，海大集团（饲料）低敏感甚至反向。要构造池内选股因子，必须先估出**个股猪价
beta**，而估 beta 必须用**高频（日度）**猪价序列对日度股票收益做回归。
→ **频率优先于历史长度**：宁要 2021 至今的日度，也不要 2014 至今的月度。

实测结论（2026-09-02 实测，详见实测报告）
----------------------------------------
1. 【最佳序列】新浪期货 `LH0`（生猪**主力连续**）日线 1369 根，2021-01-08 → 至今。
   - 东财 push2his **只保留未到期合约**（实测 lh2101..lh2607 全部返回空），
     只能拿到最近 6 个活跃合约 ≈ 1 年 → **不能用东财估长周期 beta**。
   - 新浪 `InnerFuturesNewService.getDailyKLine` **保留已到期合约**（实测 LH2109、
     LH2201、LH2301、LH2401、LH2501 均有数据），`LH0` 为交易所/新浪口径主力连续。
2. 中国养猪网（玄田数据）`xt.yangzhu.vip/data/getzhujiahitsdata`（POST）
   - ptype: 1 外三元 / 2 内三元 / 3 土杂猪 / 4 玉米 / 5 豆粕
   - datetype=0 免登录，返回**近 367 天日度**；datetype>=1 需付费会员（code=0 拒绝）
   - → 现货日度只有 **1 年**窗口，这是**免费额度上限**，不可强求更长。
3. 农业农村部生猪专题月度数据 `moa.gov.cn/ztzl/szcpxx/jdsj/YYYY/YYYYMM/`
   - **数据在 HTML 表格里**（不是 JS 动态加载；此前"抓不到"的判断有误）：
     直接 GET 页面 + 解析 <table> 即可，无需浏览器自动化。
   - 含**能繁母猪存栏**（季度末口径）、生猪存栏/出栏、月度猪价、屠宰量、进出口。
   - 发布节奏：每年 1/20、4/20、7/20、10/20 → **季度发布**，页面覆盖 2022-01 起。
4. pfsc.agri.cn（全国农产品批发市场价格信息系统）为 Spring Boot + Vue SPA，
   数据接口 401 需鉴权 → **放弃**，不冒充官方一手源。

vintage 数据纪律（项目铁律）
---------------------------
1. **只增不修**：所有原始表为**纯追加**（append-only），无唯一约束覆盖历史；
   每次采集写入新 `collected_at` 批次，同一 (series, date) 的历史批次永久保留。
   消费侧通过 `v_*_latest` 视图取 `MAX(collected_at)` 的当前值。
2. **source 字段**：每条记录记录**真实抓取 URL**，第三方转载绝不冒充官方一手源。
3. **发布口径**：`freq_actual` 记录数据**真实发布频率**（日度/季度末/月度），
   严禁把周度/月度/季度数据当日度用。派生序列额外标 `freq_derived` 与 `formula`。
4. **只记实测**：抓不到就如实记 `collect_log.status='FAIL'`，不编造、不凑数。

用法
----
    python ashare_hog_factor_collect.py                # 全量采集
    python ashare_hog_factor_collect.py --only futures # 只采期货
    python ashare_hog_factor_collect.py --only spot,official
    python ashare_hog_factor_collect.py --report       # 只出统计报告（不抓）
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sqlite3
import sys
import time
import urllib3
from datetime import datetime, timedelta
from pathlib import Path

import requests

urllib3.disable_warnings()

_HERE = Path(__file__).resolve().parent
ROOT = _HERE.parents[3]
DB_PATH = ROOT / "outputs" / "ashare_industry.sqlite"
OUT_DIR = ROOT / "outputs" / "2026-09-02"

RUN_TS = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

# ---------------------------------------------------------------- 抓取基础设施

# 代理 127.0.0.1:64119 会间歇性断连 → 指数退避重试，且**绝不抛异常**（返回 None）
MAX_RETRIES = 12


def _sleep_backoff(i: int) -> None:
    time.sleep(min(0.6 + 0.9 * i, 6.0) + random.uniform(0, 0.5))


def safe_get(sess, url, params=None, retries: int = MAX_RETRIES, timeout: int = 25,
             accept_non200: bool = False):
    """accept_non200=True 时返回非 200 响应（用于区分「页面不存在」与「代理断连」，
    避免对 404 做无意义的 8 次重试）。"""
    for i in range(retries):
        try:
            r = sess.get(url, params=params, timeout=timeout)
            if r.status_code == 200 or accept_non200:
                return r
        except Exception:  # noqa: BLE001 代理断连/超时，全部吞掉后重试
            pass
        _sleep_backoff(i)
    return None


def safe_post(sess, url, data=None, retries: int = MAX_RETRIES, timeout: int = 25):
    for i in range(retries):
        try:
            r = sess.post(url, data=data, timeout=timeout)
            if r.status_code == 200:
                return r
        except Exception:  # noqa: BLE001
            pass
        _sleep_backoff(i)
    return None


def mk_sess(referer: str) -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Referer": referer})
    return s


SESS_SINA = mk_sess("https://finance.sina.com.cn/futures/quotes/LH2609.shtml")
SESS_EM = mk_sess("https://quote.eastmoney.com/")
SESS_ZW = mk_sess("https://zhujia.zhuwang.com.cn/")
SESS_ZW.headers.update({"Origin": "https://zhujia.zhuwang.com.cn",
                        "X-Requested-With": "XMLHttpRequest"})
SESS_MOA = mk_sess("https://www.moa.gov.cn/")


# ---------------------------------------------------------------- 数据库

SCHEMA = """
-- 采集批次日志：成功/失败都如实记录
CREATE TABLE IF NOT EXISTS collect_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    collected_at TEXT NOT NULL,          -- 批次时间戳
    source_key   TEXT NOT NULL,          -- 逻辑源标识
    source_url   TEXT,                   -- 真实抓取 URL
    freq_actual  TEXT,                   -- 真实发布口径
    status       TEXT NOT NULL,          -- OK / FAIL / EMPTY
    n_rows       INTEGER DEFAULT 0,
    note         TEXT
);

-- 序列元数据：发布口径的唯一权威登记处
CREATE TABLE IF NOT EXISTS series_meta (
    series_id    TEXT NOT NULL,
    collected_at TEXT NOT NULL,
    name         TEXT,
    unit         TEXT,
    freq_actual  TEXT NOT NULL,          -- 日度 / 季度末 / 月度 / 日度(派生)
    freq_note    TEXT,                   -- 口径说明（如"交易所日频，主力连续拼接"）
    source_name  TEXT,
    source_url   TEXT,
    is_official  INTEGER,                -- 1=官方一手源 0=第三方
    is_derived   INTEGER DEFAULT 0,      -- 1=派生（自算）
    formula      TEXT,
    PRIMARY KEY (series_id, collected_at)
);

-- 生猪期货日线（追加式，无覆盖）
CREATE TABLE IF NOT EXISTS raw_hog_futures_daily (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    collected_at TEXT NOT NULL,
    series_id    TEXT NOT NULL,          -- LH0_MAIN / LH2609 / EM_LH2609 / EM_IDX980073
    trade_date   TEXT NOT NULL,
    open         REAL, high REAL, low REAL, close REAL,
    volume       REAL, amount REAL, settle REAL,
    source_url   TEXT NOT NULL,
    freq_actual  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_fut ON raw_hog_futures_daily(series_id, trade_date);

-- 生猪产业链现货日度（追加式）
CREATE TABLE IF NOT EXISTS raw_hog_spot_daily (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    collected_at TEXT NOT NULL,
    series_id    TEXT NOT NULL,          -- ZW_PIG_OUT / ZW_PIG_IN / ZW_PIG_LOCAL / ZW_CORN / ZW_SOYBEAN_MEAL
    trade_date   TEXT NOT NULL,
    value        REAL,
    unit         TEXT,
    source_url   TEXT NOT NULL,
    freq_actual  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_spot ON raw_hog_spot_daily(series_id, trade_date);

-- 官方产业指标（农业农村部，月/季，追加式）
CREATE TABLE IF NOT EXISTS raw_hog_official (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    collected_at  TEXT NOT NULL,
    period        TEXT NOT NULL,         -- 2026Q1 / 2026-03
    indicator     TEXT NOT NULL,
    value         REAL,
    unit          TEXT,
    mom           TEXT,
    yoy           TEXT,
    freq_actual   TEXT NOT NULL,         -- 季度末 / 月度
    release_page  TEXT NOT NULL,         -- 真实抓取 URL
    source_url    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_off ON raw_hog_official(indicator, period);

-- 派生序列（自算，明确标注非官方一手）
CREATE TABLE IF NOT EXISTS raw_hog_derived (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    collected_at TEXT NOT NULL,
    series_id    TEXT NOT NULL,          -- PIG_GRAIN_RATIO
    trade_date   TEXT NOT NULL,
    value        REAL,
    formula      TEXT NOT NULL,
    input_series TEXT NOT NULL,
    freq_actual  TEXT NOT NULL,
    source_url   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_der ON raw_hog_derived(series_id, trade_date);

-- 当前值视图：同一 (series, date) 取最近批次，历史批次仍在原表可回溯
CREATE VIEW IF NOT EXISTS v_futures_latest AS
SELECT series_id, trade_date, open, high, low, close, volume, amount, settle,
       source_url, freq_actual, collected_at
FROM (
  SELECT *, ROW_NUMBER() OVER (
      PARTITION BY series_id, trade_date ORDER BY collected_at DESC, id DESC) rn
  FROM raw_hog_futures_daily)
WHERE rn = 1;

CREATE VIEW IF NOT EXISTS v_spot_latest AS
SELECT series_id, trade_date, value, unit, source_url, freq_actual, collected_at
FROM (
  SELECT *, ROW_NUMBER() OVER (
      PARTITION BY series_id, trade_date ORDER BY collected_at DESC, id DESC) rn
  FROM raw_hog_spot_daily)
WHERE rn = 1;

CREATE VIEW IF NOT EXISTS v_official_latest AS
SELECT period, indicator, value, unit, mom, yoy, freq_actual, release_page, collected_at
FROM (
  SELECT *, ROW_NUMBER() OVER (
      PARTITION BY indicator, period ORDER BY collected_at DESC, id DESC) rn
  FROM raw_hog_official)
WHERE rn = 1;
"""


def open_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.executescript(SCHEMA)
    return c


def log(c: sqlite3.Connection, source_key: str, url, freq: str,
        status: str, n: int = 0, note: str = "") -> None:
    c.execute("INSERT INTO collect_log(collected_at,source_key,source_url,freq_actual,"
              "status,n_rows,note) VALUES(?,?,?,?,?,?,?)",
              (RUN_TS, source_key, url, freq, status, n, note))


def meta(c: sqlite3.Connection, series_id: str, name: str, unit: str,
         freq_actual: str, freq_note: str, source_name: str, source_url: str,
         is_official: int, is_derived: int = 0, formula: str = "") -> None:
    c.execute("INSERT OR REPLACE INTO series_meta VALUES(?,?,?,?,?,?,?,?,?,?,?)",
              (series_id, RUN_TS, name, unit, freq_actual, freq_note,
               source_name, source_url, is_official, is_derived, formula))


# ---------------------------------------------------------------- 1. 生猪期货（新浪 / 东财）

SINA_K = ("https://stock2.finance.sina.com.cn/futures/api/jsonp.php/"
          "var%20t=/InnerFuturesNewService.getDailyKLine?symbol=")
EM_KLINE = "https://push2his.eastmoney.com/api/qt/stock/kline/get"


def sina_daily(symbol: str):
    """新浪期货日线。返回 list[dict(d,o,h,l,c,v,p,s)]，失败返回 None。"""
    r = safe_get(SESS_SINA, SINA_K + symbol)
    if r is None:
        return None
    t = r.text.strip()
    m = re.search(r"=\s*\((\[.*\])\)\s*;?\s*$", t, re.S)
    try:
        return json.loads(m.group(1) if m else t)
    except Exception:  # noqa: BLE001
        return None


def em_kline(secid: str, fqt: int = 0):
    """东财日线。返回 (name, klines)，失败返回 (None, None)。"""
    r = safe_get(SESS_EM, EM_KLINE, {
        "secid": secid, "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        "klt": "101", "fqt": str(fqt), "beg": "20201201",
        "end": "20500101", "lmt": "100000"})
    if r is None:
        return None, None
    try:
        d = r.json().get("data") or {}
    except Exception:  # noqa: BLE001
        return "JSONERR", None
    return d.get("name"), d.get("klines") or []


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def collect_futures(c: sqlite3.Connection, do_contracts: bool = True) -> None:
    print("\n[1] 生猪期货日线")

    # --- 1a. 新浪 LH0 主力连续：估 beta 的主序列（2021-01 起，日度）---
    url = SINA_K + "LH0"
    d = sina_daily("LH0")
    if d:
        rows = [(RUN_TS, "LH0_MAIN", x["d"], _f(x["o"]), _f(x["h"]), _f(x["l"]),
                 _f(x["c"]), _f(x["v"]), _f(x["p"]), _f(x["s"]), url, "日度")
                for x in d if x.get("d") and _f(x["c"]) is not None]
        c.executemany("INSERT INTO raw_hog_futures_daily(collected_at,series_id,"
                      "trade_date,open,high,low,close,volume,amount,settle,"
                      "source_url,freq_actual) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        meta(c, "LH0_MAIN", "生猪期货主力连续(LH0)", "元/吨", "日度",
             "交易所日频；新浪口径主力连续拼接，已到期合约保留（东财不保留）",
             "新浪财经期货", url, 0)
        log(c, "sina:LH0", url, "日度", "OK", len(rows),
            f"{rows[0][2]} → {rows[-1][2]}" if rows else "")
        print(f"    LH0 主力连续      {len(rows):5d} 条  "
              f"{rows[0][2]} → {rows[-1][2]}" if rows else "    LH0 失败")
    else:
        log(c, "sina:LH0", url, "日度", "FAIL", 0, "代理断连或接口不可达")
        print("    LH0 主力连续      失败")

    # --- 1b. 新浪分合约（含已到期），用于自建连续/换月校验 ---
    if do_contracts:
        tot = 0
        ok = 0
        for yr in range(21, 27):
            for mm in ["01", "03", "05", "07", "09", "11"]:
                sym = f"LH{yr}{mm}"
                dd = sina_daily(sym)
                if not dd:
                    continue
                rows = [(RUN_TS, f"SINA_{sym}", x["d"], _f(x["o"]), _f(x["h"]),
                         _f(x["l"]), _f(x["c"]), _f(x["v"]), _f(x["p"]), _f(x["s"]),
                         SINA_K + sym, "日度")
                        for x in dd if x.get("d") and _f(x["c"]) is not None]
                if rows:
                    c.executemany(
                        "INSERT INTO raw_hog_futures_daily(collected_at,series_id,"
                        "trade_date,open,high,low,close,volume,amount,settle,"
                        "source_url,freq_actual) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", rows)
                    tot += len(rows)
                    ok += 1
                time.sleep(0.35)
        meta(c, "SINA_LH_CONTRACTS", "生猪期货分合约(新浪,含已到期)", "元/吨", "日度",
             "交易所日频；单合约原始成交价，未经复权，含已到期合约",
             "新浪财经期货", SINA_K + "LH{YYMM}", 0)
        log(c, "sina:contracts", SINA_K + "LH{YYMM}", "日度",
            "OK" if ok else "FAIL", tot, f"{ok} 个合约")
        print(f"    新浪分合约        {tot:5d} 条  ({ok} 个合约)")

    # --- 1c. 东财活跃合约（补充最近一日，东财常比新浪快一天）---
    tot = ok = 0
    for yr in range(25, 28):
        for mm in ["01", "03", "05", "07", "09", "11"]:
            code = f"lh{yr}{mm}"
            name, k = em_kline(f"114.{code}")
            if not k:
                continue
            rows = []
            for line in k:
                p = line.split(",")
                if len(p) < 6:
                    continue
                rows.append((RUN_TS, f"EM_{code.upper()}", p[0], _f(p[1]), _f(p[3]),
                             _f(p[4]), _f(p[2]), _f(p[5]),
                             _f(p[6]) if len(p) > 6 else None, None,
                             EM_KLINE + f"?secid=114.{code}", "日度"))
            if rows:
                c.executemany(
                    "INSERT INTO raw_hog_futures_daily(collected_at,series_id,"
                    "trade_date,open,high,low,close,volume,amount,settle,"
                    "source_url,freq_actual) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", rows)
                tot += len(rows)
                ok += 1
            time.sleep(0.45)
    meta(c, "EM_LH_CONTRACTS", "生猪期货活跃合约(东财,仅未到期)", "元/吨", "日度",
         "交易所日频；东财仅保留未到期合约，历史合约返回空 → 不可用于长周期 beta",
         "东方财富 push2his", EM_KLINE, 0)
    log(c, "em:contracts", EM_KLINE, "日度", "OK" if ok else "FAIL", tot,
        f"{ok} 个活跃合约（已到期合约东财不保留）")
    print(f"    东财活跃合约      {tot:5d} 条  ({ok} 个，仅未到期)")

    # --- 1d. 东财生猪指数（板块代理，注意只有 2024-07 起）---
    name, k = em_kline("0.980073")
    if k:
        rows = []
        for line in k:
            p = line.split(",")
            if len(p) < 6:
                continue
            rows.append((RUN_TS, "EM_IDX980073", p[0], _f(p[1]), _f(p[3]), _f(p[4]),
                         _f(p[2]), _f(p[5]), _f(p[6]) if len(p) > 6 else None, None,
                         EM_KLINE + "?secid=0.980073", "日度"))
        c.executemany("INSERT INTO raw_hog_futures_daily(collected_at,series_id,"
                      "trade_date,open,high,low,close,volume,amount,settle,"
                      "source_url,freq_actual) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        meta(c, "EM_IDX980073", "东财生猪指数(980073)", "点", "日度",
             "东财自编指数日频；实测仅覆盖 2024-07-12 起，早于该日无数据",
             "东方财富 push2his", EM_KLINE + "?secid=0.980073", 0)
        log(c, "em:idx980073", EM_KLINE + "?secid=0.980073", "日度", "OK", len(rows),
            f"{rows[0][2]} → {rows[-1][2]}")
        print(f"    东财生猪指数      {len(rows):5d} 条  {rows[0][2]} → {rows[-1][2]}")
    else:
        log(c, "em:idx980073", EM_KLINE + "?secid=0.980073", "日度", "FAIL", 0)
        print("    东财生猪指数      失败")


# ---------------------------------------------------------------- 2. 生猪现货日度（养猪网）

ZW_API = "https://xt.yangzhu.vip/data/getzhujiahitsdata"

# ptype 实测映射：1 外三元 2 内三元 3 土杂猪 4 玉米 5 豆粕（6+ 返回空/报错）
ZW_SERIES = {
    1: ("ZW_PIG_OUT", "生猪(外三元)全国均价", "元/公斤", "pigprice"),
    2: ("ZW_PIG_IN", "生猪(内三元)全国均价", "元/公斤", "pig_in"),
    3: ("ZW_PIG_LOCAL", "生猪(土杂猪)全国均价", "元/公斤", "pig_local"),
    4: ("ZW_CORN", "玉米全国均价", "元/吨", "maizeprice"),
    5: ("ZW_SOYBEAN_MEAL", "豆粕全国均价", "元/吨", "bean"),
}


def collect_spot(c: sqlite3.Connection) -> None:
    print("\n[2] 生猪/饲料现货日度（中国养猪网·玄田数据）")
    stored = {}
    for ptype, (sid, name, unit, field) in ZW_SERIES.items():
        r = safe_post(SESS_ZW, ZW_API, {"ptype": ptype, "areano": -1, "datetype": 0})
        if r is None:
            log(c, f"zhuwang:ptype{ptype}", ZW_API, "日度", "FAIL", 0, "连接失败")
            print(f"    {name:14s} 失败")
            time.sleep(0.8)
            continue
        try:
            j = r.json()
        except Exception:  # noqa: BLE001
            log(c, f"zhuwang:ptype{ptype}", ZW_API, "日度", "FAIL", 0, "非JSON响应")
            print(f"    {name:14s} 非 JSON")
            time.sleep(0.8)
            continue

        if j.get("code") != 200:
            msg = str(j.get("msg"))[:80]
            log(c, f"zhuwang:ptype{ptype}", ZW_API, "日度", "FAIL", 0,
                f"code={j.get('code')} {msg}")
            print(f"    {name:14s} 拒绝: {msg}")
            time.sleep(0.8)
            continue

        data = j.get("data") or []
        rows = []
        for it in data:
            dt = it.get("pricedate")
            v = _f(it.get(field))
            if dt and v is not None:
                rows.append((RUN_TS, sid, dt, v, unit, ZW_API, "日度"))
        if rows:
            c.executemany("INSERT INTO raw_hog_spot_daily(collected_at,series_id,"
                          "trade_date,value,unit,source_url,freq_actual) "
                          "VALUES(?,?,?,?,?,?,?)", rows)
            stored[sid] = {d: v for _, _, d, v, *_ in rows}
            meta(c, sid, name, unit, "日度",
                 "第三方商业平台日度；datetype=0 免费窗口仅近 367 天，更长历史需付费会员"
                 " → 不可外推为长周期日度",
                 "中国养猪网(玄田数据)", ZW_API, 0)
            log(c, f"zhuwang:ptype{ptype}", ZW_API, "日度", "OK", len(rows),
                f"{rows[0][2]} → {rows[-1][2]}")
            print(f"    {name:14s} {len(rows):5d} 条  {rows[0][2]} → {rows[-1][2]}")
        else:
            log(c, f"zhuwang:ptype{ptype}", ZW_API, "日度", "EMPTY", 0)
            print(f"    {name:14s} 空")
        time.sleep(0.9)

    # --- 派生猪粮比：明确标注为自算，非官方一手 ---
    pig = stored.get("ZW_PIG_OUT") or {}
    corn = stored.get("ZW_CORN") or {}
    if pig and corn:
        rows = []
        for d, pv in sorted(pig.items()):
            cv = corn.get(d)
            # 官方猪粮比 = 生猪价(元/kg) / 玉米价(元/kg)；玉米源为元/吨 → /1000
            if cv:
                rows.append((RUN_TS, "PIG_GRAIN_RATIO", d, round(pv / (cv / 1000.0), 4),
                             "生猪(外三元)元/公斤 ÷ 玉米(元/吨÷1000)",
                             "ZW_PIG_OUT,ZW_CORN", "日度(派生)", ZW_API))
        if rows:
            c.executemany("INSERT INTO raw_hog_derived(collected_at,series_id,"
                          "trade_date,value,formula,input_series,freq_actual,source_url)"
                          " VALUES(?,?,?,?,?,?,?,?)", rows)
            meta(c, "PIG_GRAIN_RATIO", "猪粮比(自算派生)", "比值", "日度(派生)",
                 "由养猪网日度猪价与玉米价自算，**非发改委官方周度口径**，"
                 "不可与官方猪粮比混用；官方周度猪粮比本次未取得",
                 "派生(中国养猪网)", ZW_API, 0, 1,
                 "生猪(外三元)元/公斤 ÷ 玉米(元/吨÷1000)")
            log(c, "derived:pig_grain_ratio", ZW_API, "日度(派生)", "OK", len(rows),
                "自算，非官方一手源")
            print(f"    猪粮比(自算派生)  {len(rows):5d} 条  {rows[0][2]} → {rows[-1][2]}")
    else:
        log(c, "derived:pig_grain_ratio", ZW_API, "日度(派生)", "FAIL", 0,
            "依赖的猪价/玉米序列缺失")


# ---------------------------------------------------------------- 3. 农业农村部官方月度/季度

MOA_BASE = "https://www.moa.gov.cn/ztzl/szcpxx/jdsj"


def _cell_clean(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", s)).strip()


def parse_moa_page(html: str):
    """解析月度数据页表格。返回 [(indicator, value_str, mom, yoy), ...]"""
    out = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S | re.I):
        tds = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.S | re.I)
        vals = [v for v in (_cell_clean(x) for x in tds) if v]
        # 期望 5 列：序号 / 指标 / 数值 / 环比 / 同比
        if len(vals) >= 5 and re.search(r"(\d{4}年|\d{4}-\d)", vals[1]):
            out.append((vals[1], vals[2], vals[3], vals[4]))
    # 页面含 PC/移动双份表格 → 去重
    seen, ded = set(), []
    for r in out:
        if r[0] not in seen:
            seen.add(r[0])
            ded.append(r)
    return ded


_NUM_RE = re.compile(r"-?\d[\d,]*\.?\d*")


def _num(s: str):
    if not s or s in ("—", "-", "－"):
        return None
    m = _NUM_RE.search(s.replace(",", ""))
    return float(m.group(0)) if m else None


def _unit_of(ind: str) -> str:
    m = re.search(r"（([^）]*)）", ind)
    return m.group(1) if m else ""


def _period_of(ind: str) -> tuple:
    """从指标名解析 (period, freq_actual)。返回 (None,None) 表示无法识别。"""
    m = re.search(r"(\d{4})年(第?[一二三四1-4])?季度末", ind)
    if m:
        q = {"一": 1, "二": 2, "三": 3, "四": 4}
        n = m.group(2)
        if n:
            n = n.lstrip("第")
            qi = q.get(n, int(n) if n.isdigit() else 0)
        else:
            qi = 0
        return (f"{m.group(1)}Q{qi}" if qi else f"{m.group(1)}Q?", "季度末")
    m = re.search(r"(\d{4})年(\d{1,2})月份", ind)
    if m:
        return (f"{m.group(1)}-{int(m.group(2)):02d}", "月度")
    m = re.search(r"(\d{4})年(\d{1,2})-(\d{1,2})月", ind)
    if m:
        return (f"{m.group(1)}-{int(m.group(3)):02d}", "月度累计")
    m = re.search(r"(\d{4})年(\d{1,2})季度", ind)
    if m:
        q = int(m.group(2))
        return (f"{m.group(1)}Q{q}", "季度")
    return (None, None)


def collect_official(c: sqlite3.Connection) -> None:
    print("\n[3] 农业农村部生猪专题（官方，季度发布/月度数据）")
    tot = ok_page = 0
    # 逐月遍历：能繁母猪存栏为季末口径（跨月重复，视图按 period 去重），
    # 而猪价/屠宰量等为月度口径 —— 只有逐月采集才能拿到**完整**月度序列。
    for y in range(2022, 2027):
        for m in [f"{i:02d}" for i in range(1, 13)]:
            if y == 2026 and int(m) > 8:
                continue
            url = f"{MOA_BASE}/{y}/{y}{m}/"
            r = safe_get(SESS_MOA, url, retries=8, timeout=30, accept_non200=True)
            if r is None:
                log(c, f"moa:{y}{m}", url, "季度末/月度", "FAIL", 0, "连接失败")
                continue
            if r.status_code != 200:
                # 404 = 该月页面尚未发布（如 2026-08 需待 10/20 发布），非抓取故障
                log(c, f"moa:{y}{m}", url, "季度末/月度", "FAIL", 0,
                    f"HTTP{r.status_code} 页面未发布")
                continue
            r.encoding = r.apparent_encoding
            recs = parse_moa_page(r.text)
            rows = []
            for ind, val, mom, yoy in recs:
                period, freq = _period_of(ind)
                if not period:
                    continue
                v = _num(val)
                if v is None:
                    continue
                rows.append((RUN_TS, period, ind, v, _unit_of(ind), mom, yoy,
                             freq, url, url))
            if rows:
                c.executemany("INSERT INTO raw_hog_official(collected_at,period,"
                              "indicator,value,unit,mom,yoy,freq_actual,release_page,"
                              "source_url) VALUES(?,?,?,?,?,?,?,?,?,?)", rows)
                tot += len(rows)
                ok_page += 1
                print(f"    {y}-{m}  {len(rows):3d} 条")
            else:
                log(c, f"moa:{y}{m}", url, "季度末/月度", "EMPTY", 0, "页面无表格数据")
            time.sleep(0.6)

    meta(c, "MOA_HOG_OFFICIAL", "农业农村部生猪专题指标集", "多单位",
         "季度末/月度",
         "官方一手源；发布节奏每年 1/20、4/20、7/20、10/20（**季度发布**），"
         "能繁母猪存栏为**季度末存量**口径，严禁插值为日度使用",
         "农业农村部", f"{MOA_BASE}/YYYY/YYYYMM/", 1)
    log(c, "moa:official", f"{MOA_BASE}/YYYY/YYYYMM/", "季度末/月度",
        "OK" if ok_page else "FAIL", tot, f"{ok_page} 个页面")
    print(f"    合计 {tot} 条（{ok_page} 个页面）")


# ---------------------------------------------------------------- 4. 发改委官方周度猪粮比

NDRC_LIST = "https://www.jgjcndrc.org.cn/list?clmId=1836667772799598593"
NDRC_ROOT = "https://www.jgjcndrc.org.cn"
# Nuxt SSR 使用 devalue 扁平化 payload：对象后紧跟其值，字段数不固定
# （linkUrl 为空时被引用的空串复用，值个数从 5 变 4）→ 按下一段 `{` 切分后逐项识别
NDRC_OBJ = (r'\{"articleId":\d+,"articleTitle":\d+,"articleType":18,"linkUrl":\d+,'
            r'"articleUrl":\d+,"isTop":\d+,"pubDate":\d+\},((?:"[^"]*",?)+?)(?=\{|$)')


def parse_ndrc_list(html):
    """解析 SSR payload 里的文章数组，返回 [{tId,title,articleUrl,pubDate}]。"""
    out = []
    for chunk in re.findall(NDRC_OBJ, html):
        vals = re.findall(r'"([^"]*)"', chunk)
        if len(vals) < 3:
            continue
        art_url = next((v for v in vals if v.startswith("/detail?")), None)
        pub = next((v for v in vals if re.match(r"^\d{4}-\d{2}-\d{2}T", v)), None)
        if not art_url or not pub:
            continue
        out.append({"tId": vals[0], "title": vals[1], "articleUrl": art_url,
                    "pubDate": pub})
    return out


def parse_ndrc_detail(html):
    """解析周报表：返回 (日期月日, 生猪价, 玉米价, 猪粮比) 或 None。"""
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S | re.I):
        tds = [re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", x)).strip()
               for x in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.S | re.I)]
        if len(tds) >= 4 and re.match(r"^\d{1,2}月\d{1,2}日$", tds[0].replace(" ", "")):
            return tds[0].replace(" ", ""), _f(tds[1]), _f(tds[2]), _f(tds[3])
    return None


def collect_ndrc_weekly(c: sqlite3.Connection, max_items: int = 10) -> None:
    """
    发改委价格监测中心周报（官方猪粮比）。

    实测限制：列表分页为**前端分页**（&pageNum=N 等参数均返回同一页），
    SSR 单页只含**最新 10 篇** → 纯 HTTP 最多拿到近 10 周官方周度值。
    用途定位为**校验自算猪粮比的锚点**，而非 beta 回归序列（周度不可用日度回归）。
    """
    print("\n[4] 发改委价格监测中心 官方周度猪粮比")
    SESS_NDRC = mk_sess("https://www.jgjcndrc.org.cn/")
    r = safe_get(SESS_NDRC, NDRC_LIST, retries=8, timeout=30)
    if r is None:
        log(c, "ndrc:list", NDRC_LIST, "周度·当周", "FAIL", 0, "连接失败")
        print("    列表页失败")
        return
    r.encoding = r.apparent_encoding
    items = [x for x in parse_ndrc_list(r.text) if "生猪出场价格" in x["title"]]
    if not items:
        log(c, "ndrc:list", NDRC_LIST, "周度·当周", "EMPTY", 0, "payload 未解析到文章")
        print("    列表页未解析到文章")
        return
    print(f"    列表页解析到 {len(items)} 篇（分页为前端分页，仅最新 {len(items)} 篇）")

    rows = []
    ok = 0
    for it in items[:max_items]:
        url = NDRC_ROOT + it["articleUrl"]
        rr = safe_get(SESS_NDRC, url, retries=8, timeout=30)
        if rr is None:
            continue
        rr.encoding = rr.apparent_encoding
        parsed = parse_ndrc_detail(rr.text)
        if not parsed:
            continue
        md, pig, corn, ratio = parsed
        ym = re.search(r"(\d{4})年", it["title"])
        mm = re.match(r"(\d{1,2})月(\d{1,2})日", md)
        if not (ym and mm):
            continue
        d = f"{ym.group(1)}-{int(mm.group(1)):02d}-{int(mm.group(2)):02d}"
        if pig is not None:
            rows.append((RUN_TS, d, "全国生猪出场价格(发改委周度)", pig, "元/公斤",
                         "", "", "周度·当周", url, url))
        if corn is not None:
            rows.append((RUN_TS, d, "主要批发市场玉米价格(发改委周度)", corn, "元/公斤",
                         "", "", "周度·当周", url, url))
        if ratio is not None:
            rows.append((RUN_TS, d, "猪粮比价(发改委官方周度)", ratio, "比值",
                         "", "", "周度·当周", url, url))
        ok += 1
        print(f"    {d}  猪价 {pig}  玉米 {corn}  猪粮比 {ratio}")
        time.sleep(0.7)

    if rows:
        c.executemany("INSERT INTO raw_hog_official(collected_at,period,indicator,"
                      "value,unit,mom,yoy,freq_actual,release_page,source_url)"
                      " VALUES(?,?,?,?,?,?,?,?,?,?)", rows)
        meta(c, "NDRC_PIG_GRAIN_RATIO", "猪粮比价(发改委官方周度)", "比值", "周度·当周",
             "官方一手源；发改委价格监测中心周报，**周度**，不可用于日度回归。"
             "列表分页为前端分页，SSR 仅含最新 10 篇 → 本序列仅近 10 周，"
             "用于校验自算日度猪粮比，不作 beta 回归输入",
             "国家发改委价格监测中心", NDRC_LIST, 1)
        log(c, "ndrc:weekly", NDRC_LIST, "周度·当周", "OK", len(rows),
            f"{ok} 篇周报；仅最新 {len(items)} 篇可控（前端分页）")
    else:
        log(c, "ndrc:weekly", NDRC_LIST, "周度·当周", "FAIL", 0, "详情页解析失败")


# ---------------------------------------------------------------- 统计报告

def _freq_stats(dates):
    """由实际日期估算频率与缺失率。"""
    if len(dates) < 2:
        return "不足2点", None
    d = sorted(datetime.strptime(x, "%Y-%m-%d") for x in set(dates))
    gaps = [(d[i + 1] - d[i]).days for i in range(len(d) - 1)]
    gaps.sort()
    med = gaps[len(gaps) // 2]
    span = (d[-1] - d[0]).days + 1
    # 日度序列的缺失率：相对「自然日」的覆盖率（现货含周末，期货只算交易日）
    miss = 1 - len(d) / span
    label = {1: "日度", 7: "周度", 30: "月度", 90: "季度"}.get(med, f"间隔{med}天")
    return label, miss


def report(c: sqlite3.Connection) -> None:
    print("\n" + "=" * 96)
    print("采集统计（vintage 视图取当前值）")
    print("=" * 96)
    print(f"{'序列':<22}{'条数':>7}{'起':>12}{'止':>12}{'实测频率':>12}{'缺失率':>9}  口径")
    print("-" * 96)

    for sid, name in [
        ("LH0_MAIN", "生猪期货主力连续"),
        ("EM_LH2609", "生猪2609(东财)"),
        ("EM_IDX980073", "东财生猪指数"),
        ("SINA_LH2109", "LH2109(已到期)"),
    ]:
        rows = c.execute("SELECT trade_date FROM v_futures_latest WHERE series_id=? "
                         "ORDER BY trade_date", (sid,)).fetchall()
        if not rows:
            print(f"{sid:<22}{'0':>7}  —— 无数据")
            continue
        ds = [r[0] for r in rows]
        f, miss = _freq_stats(ds)
        print(f"{sid:<22}{len(ds):>7}{ds[0]:>12}{ds[-1]:>12}{f:>12}"
              f"{(f'{miss:.1%}' if miss is not None else '-'):>9}  日度·交易日")

    for sid in ["ZW_PIG_OUT", "ZW_PIG_IN", "ZW_PIG_LOCAL", "ZW_CORN", "ZW_SOYBEAN_MEAL"]:
        rows = c.execute("SELECT trade_date,value FROM v_spot_latest WHERE series_id=? "
                         "ORDER BY trade_date", (sid,)).fetchall()
        if not rows:
            print(f"{sid:<22}{'0':>7}  —— 无数据")
            continue
        ds = [r[0] for r in rows]
        f, miss = _freq_stats(ds)
        print(f"{sid:<22}{len(ds):>7}{ds[0]:>12}{ds[-1]:>12}{f:>12}"
              f"{(f'{miss:.1%}' if miss is not None else '-'):>9}  日度·含周末")

    for label, ind in [("能繁母猪存栏", "能繁母猪存栏"), ("生猪存栏", "生猪存栏"),
                       ("生猪出场价格", "全国生猪出场价格"), ("仔猪价格", "全国仔猪价格")]:
        # 限定 release_page 属农业农村部，避免与发改委同名指标串味
        rr = c.execute("SELECT period,value FROM v_official_latest WHERE indicator "
                       "LIKE ? AND release_page LIKE '%moa.gov.cn%' ORDER BY period",
                       (f"%{ind}%",)).fetchall()
        if not rr:
            print(f"{label:<22}{'0':>7}  —— 无数据")
            continue
        print(f"{'MOA:'+label:<22}{len(rr):>7}{rr[0][0]:>12}{rr[-1][0]:>12}"
              f"{'季度末' if '存栏' in ind else '月度':>12}{'-':>9}  "
              f"{'季度末存量' if '存栏' in ind else '月度'}")

    rr = c.execute("SELECT period,value FROM v_official_latest WHERE indicator=? "
                   "ORDER BY period", ("猪粮比价(发改委官方周度)",)).fetchall()
    if rr:
        ds = [x[0] for x in rr]
        f, _ = _freq_stats(ds)
        print(f"{'NDRC:猪粮比(官方)':<22}{len(ds):>7}{ds[0]:>12}{ds[-1]:>12}{f:>12}"
              f"{'-':>9}  周度·当周（仅最新10篇，前端分页）")
        print(f"{'':<22}  末值 {rr[-1][1]}")

    rows = c.execute("SELECT trade_date,value FROM raw_hog_derived WHERE series_id=? "
                     "ORDER BY trade_date", ("PIG_GRAIN_RATIO",)).fetchall()
    if rows:
        ds = [r[0] for r in rows]
        f, miss = _freq_stats(ds)
        print(f"{'PIG_GRAIN_RATIO':<22}{len(ds):>7}{ds[0]:>12}{ds[-1]:>12}{f:>12}"
              f"{(f'{miss:.1%}' if miss is not None else '-'):>9}  日度(自算派生)")
        print(f"{'':<22}  末值 {rows[-1][1]:.2f}")

    print("-" * 96)
    print("采集日志（失败如实列出）：")
    for k, url, st, n, note in c.execute(
            "SELECT source_key,source_url,status,n_rows,note FROM collect_log "
            "WHERE collected_at=? ORDER BY status,source_key", (RUN_TS,)):
        flag = "  " if st == "OK" else "!!"
        print(f"  {flag} {st:<6}{k:<28}{n:>6} 条  {(note or '')[:52]}")

    nbatch = c.execute("SELECT COUNT(DISTINCT collected_at) FROM collect_log").fetchone()[0]
    print("-" * 96)
    print(f"库: {DB_PATH}")
    print(f"历史批次: {nbatch} 个（只增不修，旧批次全部保留）")


# ---------------------------------------------------------------- main

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="",
                    help="逗号分隔：futures,spot,official,ndrc（默认全采）")
    ap.add_argument("--no-contracts", action="store_true", help="跳过新浪分合约")
    ap.add_argument("--report", action="store_true", help="只出统计报告，不抓取")
    args = ap.parse_args()

    c = open_db()
    if args.report:
        report(c)
        c.close()
        return

    only = {x.strip() for x in args.only.split(",") if x.strip()} or \
        {"futures", "spot", "official", "ndrc"}

    print(f"批次 {RUN_TS}   库 {DB_PATH}")
    if "futures" in only:
        collect_futures(c, do_contracts=not args.no_contracts)
    if "spot" in only:
        collect_spot(c)
    if "official" in only:
        collect_official(c)
    if "ndrc" in only:
        collect_ndrc_weekly(c)

    c.commit()
    report(c)
    c.close()
    print("\n完成。")


if __name__ == "__main__":
    main()
