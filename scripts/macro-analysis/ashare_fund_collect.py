# -*- coding: utf-8 -*-
"""A股基本面采集器（候选B 工程落地）—— 东财业绩报表，按报告期批量拉取。

【为什么能做，以及和港股的根本差异】
港股基本面需要 LLM 逐份抽取 PDF 财报（95 只 × 51 期 = 4,845 次 LLM 调用），
成本高到必须先做功效评估；而 A 股有**结构化的财报数据接口**，一次请求就能
按报告期拿到全市场某一期的全部业绩 —— 51 期只需 ~1,200 次请求。
探针（ashare_fund_probe.py）已验证：全市场 11,492 条、农业池 95/95 全命中。

【★★ vintage 纪律：本脚本最重要的设计】
财报有两个日期，混淆它们是前视偏差的头号来源：

    REPORTDATE   报告期（如 2025-12-31）—— 描述的是「哪一段时间的业绩」
    NOTICE_DATE  公告日期（如 2026-03-28）—— 描述的是「市场什么时候知道的」

**因子只能用 NOTICE_DATE，绝不能用 REPORTDATE。**
用 REPORTDATE 会让策略在 2025-12-31 就"知道"要等到 2026-03-28 才公布的业绩 ——
这是凭空多出 3 个月的预知能力，回测必然虚高。

本脚本把两个日期都存下来，但**下游因子构造必须只用 notice_date**
（见 ashare_fund_validate.py 的生效日规则）。

【只增不修】
PRIMARY KEY (code, report_date)，重复采集用 INSERT OR REPLACE 覆盖同键记录；
但每次采集都记 collect_log，保留完整采集历史。不删任何历史记录。

【独立库】
写 outputs/ashare_fundamental.sqlite，与主库 ashare_agri.sqlite 完全隔离
（「学习产物只写独立库」纪律）。
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime
from pathlib import Path

_HERE = Path(__file__).resolve().parent
ROOT = _HERE.parents[3]
sys.path.insert(0, str(_HERE))

from ashare_agri_backtest import DB as PRICE_DB  # noqa: E402

OUT_DB = ROOT / "outputs" / "ashare_fundamental.sqlite"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0 Safari/537.36")
EM_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
EM_REPORT = "RPT_LICO_FN_CPD"

# 字段映射：东财返回名 → 本地列名
FIELD_MAP = {
    "WEIGHTAVG_ROE": "roe",            # 加权净资产收益率 %（主因子，水平值）
    "YSTZ": "rev_yoy",                 # 营业收入同比 %
    "SJLTZ": "np_yoy",                 # 净利润同比 %（⚠️ 需缩尾，见探针）
    "BPS": "bps",                      # 每股净资产
    "MGJYXJJE": "ocf_ps",              # 每股经营现金流
    "XSMLL": "gross_margin",           # 销售毛利率 %
    "TOTAL_OPERATE_INCOME": "revenue",
    "PARENT_NETPROFIT": "net_profit",
    "BASIC_EPS": "eps",
}

DDL = """
CREATE TABLE IF NOT EXISTS fund_reports (
    code            TEXT NOT NULL,
    report_date     TEXT NOT NULL,
    notice_date     TEXT,
    roe             REAL,
    rev_yoy         REAL,
    np_yoy          REAL,
    bps             REAL,
    ocf_ps          REAL,
    gross_margin    REAL,
    revenue         REAL,
    net_profit      REAL,
    eps             REAL,
    collected_at    TEXT NOT NULL,
    source          TEXT NOT NULL,
    PRIMARY KEY (code, report_date)
);
CREATE INDEX IF NOT EXISTS idx_fund_notice ON fund_reports(notice_date);
CREATE INDEX IF NOT EXISTS idx_fund_code ON fund_reports(code);

-- 每期的全市场截面中位数：用于把农业池因子做成「相对全市场」的口径，
-- 剥离猪周期这类全市场共同冲击（这是农业池基本面最需要的中性化）
CREATE TABLE IF NOT EXISTS market_median (
    report_date     TEXT NOT NULL,
    n_total         INTEGER,
    roe_med         REAL,
    rev_yoy_med     REAL,
    np_yoy_med      REAL,
    gross_margin_med REAL,
    collected_at    TEXT NOT NULL,
    PRIMARY KEY (report_date)
);

CREATE TABLE IF NOT EXISTS collect_log_fund (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    collected_at    TEXT NOT NULL,
    report_date     TEXT NOT NULL,
    n_pages         INTEGER,
    n_all_market    INTEGER,
    n_pool_hit      INTEGER,
    status          TEXT NOT NULL,
    note            TEXT
);
"""


def http_get_json(url: str, timeout: int = 30, retries: int = 3):
    """带重试的 JSON 拉取（指数退避）。"""
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": UA,
                              "Referer": "https://data.eastmoney.com/"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except Exception as e:          # noqa: BLE001
            last = e
            if i < retries - 1:
                time.sleep(1.5 * (2 ** i))
    raise last


def fetch_period(period: str, page_size: int = 500, max_pages: int = 40,
                 sleep: float = 0.2):
    """拉一期全市场业绩报表（分页直到拉完）。返回 (rows, n_pages)。"""
    rows, n_pages = [], 0
    total = None
    for p in range(1, max_pages + 1):
        params = {
            "reportName": EM_REPORT,
            "columns": "ALL",
            "filter": f"(REPORTDATE='{period}')",
            "pageNumber": str(p),
            "pageSize": str(page_size),
            "sortColumns": "SECURITY_CODE",
            "sortTypes": "1",
            "source": "WEB",
            "client": "WEB",
        }
        url = EM_URL + "?" + urllib.parse.urlencode(params)
        obj = http_get_json(url)
        res = obj.get("result") if isinstance(obj, dict) else None
        n_pages = p
        if not res:
            break
        data = res.get("data") or []
        rows.extend(data)
        if total is None:
            total = res.get("count") or res.get("total") or 0
        if not data or (total and len(rows) >= total):
            break
        time.sleep(sleep)
    return rows, n_pages


def pool_codes(price_db=None, codes_file=None):
    """池代码来源：优先 codes_file（可与价格采集并行），否则从价格库读。"""
    if codes_file:
        p = Path(codes_file)
        txt = p.read_text(encoding="utf-8")
        return [c.strip() for c in re.split(r"[,\s]+", txt) if c.strip()]
    conn = sqlite3.connect(str(price_db or PRICE_DB), timeout=30)
    codes = [r[0] for r in conn.execute(
        "SELECT DISTINCT code FROM daily_quotes_hfq ORDER BY code")]
    conn.close()
    return codes


def gen_periods(start_year: int, end_year: int):
    """生成季报报告期列表。顺序：从早到晚。"""
    out = []
    for y in range(start_year, end_year + 1):
        for md in ("03-31", "06-30", "09-30", "12-31"):
            out.append(f"{y}-{md}")
    return out


def _f(v):
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None      # 过滤 NaN


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start-year", type=int, default=2013,
                    help="起始报告期年份（2013年报在2014-04披露，覆盖2014起始）")
    # ★ 2026-09-06 修复（第卅三类近亲：默认参数 = 数据边界）：原硬编码 2025，
    #   导致 2026 年报告期（03-31/06-30/09-30/12-31）永不进入 periods 列表，
    #   fund_reports 停在 2025-12-31；判决书 label_agri_np_yoy 取
    #   「ORDER BY report_date DESC LIMIT 1」⇒ 一直用 18 个月前的年报打标签。
    #   未披露期返回 0 行会记 status='empty'（--resume 只跳 'ok'），故可安全取当前年。
    ap.add_argument("--end-year", type=int, default=date.today().year,
                    help="结束报告期年份（默认=当前年）")
    ap.add_argument("--resume", action="store_true", help="跳过已成功采集的期")
    ap.add_argument("--page-size", type=int, default=500)
    ap.add_argument("--sleep", type=float, default=0.2)
    # ★ 换池：池代码来源与输出库都可指向宽池，避免污染农业池的既有产物
    ap.add_argument("--price-db", default=str(PRICE_DB),
                    help="池代码来源库（读其中的 daily_quotes_hfq）")
    ap.add_argument("--out-db", default=str(OUT_DB), help="基本面输出库")
    ap.add_argument("--codes-file", default="",
                    help="池代码清单文件（逗号/空白分隔），优先于 --price-db")
    args = ap.parse_args()

    codes = pool_codes(args.price_db, args.codes_file or None)
    pool = set(codes)
    print("=" * 78)
    print("A股基本面采集（东财业绩报表）")
    print("=" * 78)
    print(f"标的池: {len(codes)} 只   （来源 {Path(args.price_db).name}）")
    print(f"目标库: {args.out_db}")

    conn = sqlite3.connect(str(args.out_db), timeout=30)
    conn.executescript(DDL)
    conn.commit()

    done = set()
    if args.resume:
        done = {r[0] for r in conn.execute(
            "SELECT report_date FROM collect_log_fund WHERE status='ok'")}
        if done:
            print(f"--resume: 跳过已采集 {len(done)} 期")

    periods = gen_periods(args.start_year, args.end_year)
    todo = [p for p in periods if p not in done]
    print(f"报告期: {periods[0]} ~ {periods[-1]}（共 {len(periods)} 期，待采 {len(todo)} 期）\n")

    ok = fail = empty = 0
    t_start = time.time()
    for i, period in enumerate(todo, 1):
        ts = datetime.now().isoformat(timespec="seconds")
        try:
            rows, n_pages = fetch_period(period, page_size=args.page_size,
                                         sleep=args.sleep)
        except Exception as e:                      # noqa: BLE001
            fail += 1
            conn.execute(
                "INSERT INTO collect_log_fund "
                "(collected_at,report_date,n_pages,n_all_market,n_pool_hit,status,note)"
                " VALUES (?,?,?,?,?,?,?)",
                (ts, period, 0, 0, 0, "fail", f"{type(e).__name__}: {e}"))
            conn.commit()
            print(f"  [{i}/{len(todo)}] {period}  ✗ {type(e).__name__}: {e}")
            continue

        if not rows:
            empty += 1
            conn.execute(
                "INSERT INTO collect_log_fund "
                "(collected_at,report_date,n_pages,n_all_market,n_pool_hit,status,note)"
                " VALUES (?,?,?,?,?,?,?)",
                (ts, period, n_pages, 0, 0, "empty", "该报告期无数据（可能未到披露期）"))
            conn.commit()
            print(f"  [{i}/{len(todo)}] {period}  — 空")
            continue

        # 农业池记录
        n_hit = 0
        for r in rows:
            code = r.get("SECURITY_CODE")
            if code not in pool:
                continue
            vals = {k: _f(r.get(k)) for k in FIELD_MAP}
            notice = r.get("NOTICE_DATE") or ""
            if notice and "T" in notice:
                notice = notice.split("T")[0]
            conn.execute(
                "INSERT OR REPLACE INTO fund_reports "
                "(code,report_date,notice_date,roe,rev_yoy,np_yoy,bps,ocf_ps,"
                " gross_margin,revenue,net_profit,eps,collected_at,source)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (code, period, notice or None,
                 vals["WEIGHTAVG_ROE"], vals["YSTZ"], vals["SJLTZ"], vals["BPS"],
                 vals["MGJYXJJE"], vals["XSMLL"], vals["TOTAL_OPERATE_INCOME"],
                 vals["PARENT_NETPROFIT"], vals["BASIC_EPS"], ts,
                 "eastmoney-RPT_LICO_FN_CPD"))
            n_hit += 1

        # 全市场截面中位数（用于中性化）
        def med(key):
            vs = [_f(r.get(key)) for r in rows]
            vs = [v for v in vs if v is not None]
            if len(vs) < 50:
                return None
            vs.sort()
            m = len(vs)
            return vs[m // 2] if m % 2 else 0.5 * (vs[m // 2 - 1] + vs[m // 2])

        conn.execute(
            "INSERT OR REPLACE INTO market_median "
            "(report_date,n_total,roe_med,rev_yoy_med,np_yoy_med,gross_margin_med,"
            " collected_at) VALUES (?,?,?,?,?,?,?)",
            (period, len(rows), med("WEIGHTAVG_ROE"), med("YSTZ"),
             med("SJLTZ"), med("XSMLL"), ts))

        conn.execute(
            "INSERT INTO collect_log_fund "
            "(collected_at,report_date,n_pages,n_all_market,n_pool_hit,status,note)"
            " VALUES (?,?,?,?,?,?,?)",
            (ts, period, n_pages, len(rows), n_hit, "ok", ""))
        conn.commit()
        ok += 1
        el = time.time() - t_start
        eta = el / i * (len(todo) - i)
        print(f"  [{i}/{len(todo)}] {period}  ✓ 全市场 {len(rows):>6} 条  "
              f"池内命中 {n_hit:>3}/{len(codes)}  "
              f"（已用 {el:.0f}s，预计还需 {eta:.0f}s）")

    conn.close()
    print(f"\n完成: ok={ok}  fail={fail}  empty={empty}  "
          f"耗时 {time.time() - t_start:.0f}s")

    # 汇总（⚠️ 必须连 args.out_db：连常量 OUT_DB 会把农业池的统计当成宽池结果）
    conn = sqlite3.connect(str(args.out_db), timeout=30)
    n = conn.execute("SELECT COUNT(*) FROM fund_reports").fetchone()[0]
    nc = conn.execute("SELECT COUNT(DISTINCT code) FROM fund_reports").fetchone()[0]
    nnd = conn.execute(
        "SELECT COUNT(*) FROM fund_reports WHERE notice_date IS NOT NULL").fetchone()[0]
    print(f"\n库统计: {n} 条记录 / {nc} 只 / 有公告日期 {nnd} 条 "
          f"({nnd / max(n, 1):.1%})")
    conn.close()


if __name__ == "__main__":
    main()
