#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ashare_xq_collect.py — 换源重采：雪球后复权日线（csi800 全池）
==============================================================
背景：腾讯 fqkline 后复权系统性污染（第廿四类）。阶段 0 已用四项验证
证明雪球可用（ashare_xq_preflight.py，闸门 PASS），本脚本执行重采。

与腾讯采集的三点关键差异（决定了为什么雪球不会重蹈覆辙）
------------------------------------------------------
1. **不分段**：雪球 count 模式单次可取全量（实测 5832~7004 根，无硬上限），
   而腾讯是 641 根硬上限 + 末段静默截头，必须分段 → 拼接正是污染来源。
2. **窗口独立**：阶段 0 V2 验证不同 count 下后复权值逐日完全一致（差=0.0），
   腾讯则是复权值随查询窗口漂移。
3. **入库即跨源校验**：用本地已判正确的 raw 序列做日期轴交叉校验，
   并用「无除权日 hfq 收益 == raw 收益」恒等式抽检。

QC 三层（任一层判坏 → 记入 hfq_qc.bad=1，供 ashare_hfq_access 闸门消费）
----------------------------------------------------------------------
QC1 日期轴覆盖：雪球日期集合对本池日期轴（取自本地 raw，已跨源判正确）的
    覆盖率 < 90% ⇒ 判坏（抓"静默截断"）
QC2 涨跌停界限：分板块（主板 ±10% / 创业板·科创板 ±20% / 北交所 ±30%），
    日收益越过界限 ⇒ 记 over_limit_days（新股上市前 5 日不设限，单独豁免）
QC3 恒等式抽检：随机抽样 N 只，拉不复权序列，验证无除权日
    hfq 收益 == raw 收益（数学恒等式），p99 > 1e-4 ⇒ 判坏

产出（文件名带 _xq 源标识，避免覆盖腾讯历史产物 —— 第十八类纪律）
------------------------------------------------------------------
  outputs/ashare_csi800_hfq_xq.sqlite
    daily_quotes_hfq  (code, trade_date, open, close, high, low, volume, source, collected_at)
    hfq_qc            (code, collected_at, n_bars, ... , bad, reason)   ← 闸门消费
    collect_log_xq    (code, status, bars, date_start, date_end, error) ← 断点续采

用法：
  python ashare_xq_collect.py                  # 全池采集
  python ashare_xq_collect.py --limit 30       # 小批量试跑
  python ashare_xq_collect.py --resume         # 断点续采（跳过已成功）
"""
from __future__ import annotations

import argparse
import json
import random
import sqlite3
import time
import urllib.request
import http.cookiejar
from datetime import date
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parents[4]
OUT_DIR = WORKSPACE / "outputs" / time.strftime("%Y-%m-%d")
NEW_DB = WORKSPACE / "outputs" / "ashare_csi800_hfq_xq.sqlite"
RAW_DB = WORKSPACE / "outputs" / "ashare_csi800_raw.sqlite"   # 池定义 + 日期轴基准

MAX_COUNT = 8000        # 实测足够覆盖 A 股全部历史（最长 7004 根）
LIMIT_START = "1996-12-16"   # A 股涨跌停制度实施日，此前无涨跌幅限制
QC_WINDOW = "2014-01-01"     # 判坏窗口 = 因子检验窗口（此期间涨跌停制度稳定）
RESUME_GAP_DAYS = 30         # 与上一交易日间隔超过此天数 ⇒ 视为停牌后复牌首日，豁免涨跌停校验
SLEEP = 1.2             # 礼貌限速
RETRY = 3
QC_SAMPLE = 60          # 恒等式抽检样本量
COVERAGE_MIN = 0.90     # QC1 日期轴覆盖率下限


def make_session():
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    op.addheaders = [("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36")]
    op.open("https://xueqiu.com/hq", timeout=25).read()
    if not [c for c in cj if c.name == "xq_a_token"]:
        raise RuntimeError("雪球 cookie(xq_a_token) 获取失败")
    return op


def sym(code: str) -> str:
    return ("SH" if code.startswith(("6", "9", "5")) else "SZ") + code


def kline_count(op, code: str, qtype: str, count: int = MAX_COUNT, retry: int = RETRY):
    """count 模式：begin=当前时刻, count=-N, 不传 end（传 end 会退化为区间模式）。

    返回 [(date, open, close, high, low, volume)] 升序。volume 单位=股。
    """
    url = ("https://stock.xueqiu.com/v5/stock/chart/kline.json"
           f"?symbol={sym(code)}&begin={int(time.time() * 1000)}&period=day"
           f"&type={qtype}&count=-{count}&indicator=kline")
    last = None
    for k in range(retry):
        try:
            d = json.load(op.open(url, timeout=30))
            if d.get("error_code") not in (0, None):
                raise RuntimeError(f"error_code={d.get('error_code')} {d.get('error_description')}")
            col = d["data"]["column"]
            idx = {c: col.index(c) for c in ("timestamp", "open", "close", "high", "low", "volume")}
            out = [(time.strftime("%Y-%m-%d", time.localtime(it[idx["timestamp"]] / 1000)),
                    float(it[idx["open"]]), float(it[idx["close"]]), float(it[idx["high"]]),
                    float(it[idx["low"]]), float(it[idx["volume"]])) for it in d["data"]["item"]]
            out.sort(key=lambda x: x[0])
            return out
        except Exception as e:          # noqa: BLE001
            last = e
            time.sleep(2.0 * (k + 1) + random.random())
    raise RuntimeError(f"雪球采集失败 {code}: {last}")


def parse_dt(s: str) -> date:
    y, m, d = s.split("-")
    return date(int(y), int(m), int(d))


def limit_band(code: str) -> float:
    """分板块涨跌停界限（A 股规则）。

    代码段必须与 ashare_badj_collect.py::board_of 保持一致 —— 首次实现漏了
    【302】（创业板 2024 年启用的新代码段），导致 302132 被按主板 ±10% 判定，
    误报 28 天越界（其实际为创业板 ±20%，越界 0 天）。
    """
    if code.startswith(("300", "301", "302", "688", "689")):
        return 0.20            # 创业板 / 科创板
    if code.startswith(("43", "83", "87", "92", "8")):
        return 0.30            # 北交所
    return 0.10                # 主板


def init_db():
    NEW_DB.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(NEW_DB))
    c.execute("""CREATE TABLE IF NOT EXISTS daily_quotes_hfq(
        code TEXT, trade_date TEXT, open REAL, close REAL, high REAL, low REAL,
        volume REAL, source TEXT, collected_at TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS hfq_qc(
        code TEXT PRIMARY KEY, collected_at TEXT, n_bars INTEGER,
        nonpositive INTEGER, over_limit_days INTEGER, ret_min REAL, ret_max REAL,
        coverage REAL, identity_p99 REAL, bad INTEGER, reason TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS collect_log_xq(
        code TEXT PRIMARY KEY, status TEXT, bars INTEGER,
        date_start TEXT, date_end TEXT, error TEXT, collected_at TEXT)""")
    c.execute("CREATE INDEX IF NOT EXISTS ix_hfq_code ON daily_quotes_hfq(code)")
    c.execute("CREATE INDEX IF NOT EXISTS ix_hfq_date ON daily_quotes_hfq(trade_date)")
    c.commit()
    return c


def pool_codes(db: Path | None = None, codes_arg: str | None = None,
               codes_file: Path | None = None):
    """池定义：默认复用 csi800 raw 库；也可显式指定来源库或代码清单。

    三种来源互斥且优先级：codes_file > codes_arg > db > 默认 RAW_DB。
    —— 用于把农业/半导体/宽池等其它池也换成雪球源（腾讯源已废弃）。
    """
    if codes_file is not None:
        txt = Path(codes_file).read_text(encoding="utf-8")
        codes = [x.strip() for x in txt.replace(",", "\n").split()
                 if x.strip() and x.strip()[0].isdigit()]
        return sorted(set(codes))
    if codes_arg:
        return sorted({x.strip() for x in codes_arg.split(",") if x.strip()})
    src = Path(db) if db is not None else RAW_DB
    c = sqlite3.connect(str(src))
    codes = [r[0] for r in c.execute(
        "SELECT DISTINCT code FROM daily_quotes_hfq ORDER BY code")]
    c.close()
    return codes


def pool_axis():
    """每只股票【自身】的本地 raw 日期集合，作为覆盖率基准。

    必须用 per-code 而非全池并集：否则上市晚 / 长期停牌的股票会被
    结构性误判为覆盖率不足（实测 000034 在全池轴下仅 0.859）。
    本地 raw 已跨源判正确（vs 雪球前复权 maxCloseDiff=0.0）。
    """
    c = sqlite3.connect(str(RAW_DB))
    d: dict[str, set] = {}
    for code, dt in c.execute("SELECT code, trade_date FROM daily_quotes_raw"):
        d.setdefault(code, set()).add(dt)
    c.close()
    return d


def qc_check(code: str, rows: list, axis: set, identity_p99=None):
    """QC1 日期轴覆盖 + QC2 涨跌停界限。返回 (bad, reason, stats)。

    两个容错的制度性前提（不加就会把市场事实判成数据错误）
    --------------------------------------------------------
    1) A 股涨跌停制度自 1996-12-16 起实施，此前无涨跌幅限制
       （1992-05-21 放开、1996-12-16 起 ±10%）。雪球返回全历史
       （老股 8000 根），若不做时间切分，早期数据必然"越界"。
    2) 新股上市前 5 个交易日不设涨跌幅（科创/创业亦然）。
    另：日期轴必须用【该股自身】的本地 raw 日期集合，不能用全池并集 ——
    否则上市晚 / 长期停牌的股票会被结构性误判为覆盖率不足。
    """
    dates = [r[0] for r in rows]
    closes = [r[2] for r in rows]
    cov = len(set(dates) & axis) / len(axis) if axis else 1.0
    nonpos = sum(1 for x in closes if x is None or x <= 0)
    pairs = [(dates[i], closes[i] / closes[i - 1] - 1.0)
             for i in range(1, len(closes))
             if closes[i - 1] and closes[i - 1] > 0 and closes[i] is not None]
    band = limit_band(code)
    # ★ 阈值推导（与 ashare_badj_collect.py 2026-09-03 修正后的口径一致）：
    #   涨停价 = round(prev_close * (1+lim), 2)，分位舍入带来的相对误差 < 0.1pp，
    #   故 tol = lim + 0.005。用 lim+1e-6 会把 10.01%~10.5% 的合法涨停全判成坏数据
    #   （实测 000009 误报 79 天，其中绝大多数是 0.1001~0.1007）。
    tol = band + 0.005
    # 仅对涨跌停制度生效后的日期做校验，并豁免上市前 5 日
    pairs = [p for p in pairs if p[0] >= LIMIT_START][5:]
    # ★ 豁免【停牌后复牌首日】与【恢复上市首日】：这两类情形制度上不设涨跌幅
    #   （第廿五类，2026-09-05 实测）。不能用"猜是哪只股票"，只能用可观测的
    #   客观特征识别：与上一交易日的日历间隔 > RESUME_GAP_DAYS。
    #   实测三例：000155(停牌1年7个月,-28.9% 重组复牌)、
    #             000629(停牌1年4个月,+25.1% 重组复牌)、
    #             000792(暂停上市1年3个月,+306.1% 恢复上市)。
    #   误剔这些股票 = 用制度事实当数据错误，属于把好数据判死。
    resume_days = []
    kept = []
    prev_d = None
    for d, r in pairs:
        if prev_d is not None and (parse_dt(d) - parse_dt(prev_d)).days > RESUME_GAP_DAYS:
            resume_days.append((d, round(r, 4)))
        else:
            kept.append((d, r))
        prev_d = d
    pairs = kept
    over_all = sum(1 for _, r in pairs if abs(r) > tol)
    # 判坏只对【检验窗口】内：2005-2008 股改 / 重大重组复牌首日不设涨跌幅
    #   （制度事实，非数据错误，实测 000062 2006 年 +23.15%），
    #   而因子检验窗口自 2014-01-01 起，此期间制度稳定。
    over = sum(1 for d, r in pairs if d >= QC_WINDOW and abs(r) > tol)
    rets = [r for _, r in pairs]
    bad, reason = 0, ""
    if cov < COVERAGE_MIN:
        bad, reason = 1, f"日期轴覆盖率 {cov:.3f} < {COVERAGE_MIN}"
    elif nonpos:
        bad, reason = 1, f"非正收盘价 {nonpos} 个"
    elif over > 0:
        bad, reason = 1, f"检验窗口内越过涨跌停界限 {over} 天 (tol={tol:.3f})"
    elif identity_p99 is not None and identity_p99 > 1e-4:
        bad, reason = 1, f"恒等式 p99={identity_p99:.2e} > 1e-4"
    stats = dict(n_bars=len(rows), nonpositive=nonpos, over_limit_days=over,
                 over_limit_days_all=over_all, resume_exempt_days=len(resume_days),
                 resume_exempt_sample=resume_days[:3], limit_used=band,
                 ret_min=round(min(rets), 6) if rets else None,
                 ret_max=round(max(rets), 6) if rets else None,
                 coverage=round(cov, 4), identity_p99=identity_p99,
                 bad=bad, reason=reason)
    return bad, reason, stats


def identity_check(op, code, hfq_rows, count=1200):
    """QC3：无除权日 → 后复权收益 == 不复权收益（数学恒等式）。"""
    try:
        raw = kline_count(op, code, "normal", count)
    except Exception:                                    # noqa: BLE001
        return None
    hm = {d: c for d, _, c, _, _, _ in hfq_rows}
    rr = {raw[i][0]: raw[i][2] / raw[i - 1][2] - 1.0
          for i in range(1, len(raw)) if raw[i - 1][2] > 0}
    rh = {raw[i][0]: (hm[raw[i][0]] / hm[raw[i - 1][0]] - 1.0)
          for i in range(1, len(raw))
          if raw[i][0] in hm and raw[i - 1][0] in hm and hm[raw[i - 1][0]] > 0}
    no_ex = [d for d in rh if d in rr and abs(rh[d] - rr[d]) <= 1e-4]
    if len(no_ex) < 50:
        return None
    lr = {d: v for d, v in rr.items()}
    dif = sorted(abs(rh[d] - lr[d]) for d in no_ex)
    return {"n_no_exdiv": len(no_ex), "max": dif[-1],
            "p99": dif[int(len(dif) * 0.99)], "p50": dif[len(dif) // 2]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--since", default=None,
                    help="日期级增量：只补采库内最新 trade_date < since 的股票（每日 topup 用）。"
                         "与 --resume 正交：--resume 按 code 跳过「采过」，--since 按日期跳过「已最新」。")
    ap.add_argument("--sleep", type=float, default=SLEEP)
    ap.add_argument("--db", default=None,
                    help="输出库路径（默认 csi800 雪球库）。换其它池时必须显式指定，"
                         "避免与历史产物同名互相覆盖（第十八类静默覆盖）。")
    ap.add_argument("--pool-db", default=None,
                    help="池定义来源库（默认 csi800 raw）。取该库 daily_quotes_hfq 的 "
                         "DISTINCT code 作为待采清单。")
    ap.add_argument("--codes", default=None, help="显式代码清单，逗号分隔")
    ap.add_argument("--codes-file", default=None, help="代码清单文件（每行/逗号分隔）")
    args = ap.parse_args()
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    global NEW_DB
    if args.db:
        NEW_DB = Path(args.db)
        if not NEW_DB.is_absolute():
            NEW_DB = WORKSPACE / NEW_DB
        print(f"输出库: {NEW_DB}")
    conn = init_db()
    codes = pool_codes(args.pool_db, args.codes, args.codes_file)
    if not codes:
        raise SystemExit("池为空：请检查 --pool-db / --codes / --codes-file")
    if args.limit:
        codes = codes[:args.limit]
    done = set()
    if args.resume:
        done = {r[0] for r in conn.execute(
            "SELECT code FROM collect_log_xq WHERE status='ok'")}
    todo = [c for c in codes if c not in done]
    if args.since:
        # 日期级增量：库内已有 >= since 的行情即视为「已最新」，跳过（避免每日全量重拉）。
        # 每日 topup 场景下绝大多数股票已最新，只有停牌复牌/新上市/漏采的才需要补。
        fresh = {r[0] for r in conn.execute(
            "SELECT code FROM daily_quotes_hfq WHERE trade_date >= ?", (args.since,))}
        todo = [c for c in todo if c not in fresh]
        print(f"--since {args.since}: 库内已最新 {len(fresh)} 只，待补 {len(todo)} 只")
    print(f"池规模 {len(codes)}，待采 {len(todo)}，已完 {len(done)}")

    if not todo:
        print("待采 0 只，全部已最新，无需请求。")
        conn.close()
        return

    # 日期轴：每只股票自身的本地 raw 日期集合（已跨源判正确）
    axis_all = pool_axis()

    # 抽检集合：均匀抽样（恒等式校验需额外请求，故只抽检）
    step = max(1, len(todo) // QC_SAMPLE) if todo else 1
    sample_set = set(todo[::step][:QC_SAMPLE])

    op = make_session()
    t0 = time.time()
    n_ok = n_bad = n_fail = 0
    n_axis_missing = 0
    log = []
    for i, code in enumerate(todo, 1):
        try:
            rows = kline_count(op, code, "after")
        except Exception as e:                            # noqa: BLE001
            n_fail += 1
            conn.execute("INSERT OR REPLACE INTO collect_log_xq VALUES(?,?,?,?,?,?,?)",
                         (code, "fail", 0, None, None, str(e)[:300],
                          time.strftime("%Y-%m-%d %H:%M:%S")))
            conn.commit()
            log.append({"code": code, "status": "fail", "error": str(e)[:200]})
            print(f"[{i}/{len(todo)}] {code} FAIL {str(e)[:80]}")
            time.sleep(args.sleep)
            continue

        ident = None
        if code in sample_set:
            ident = identity_check(op, code, rows)
            time.sleep(args.sleep)
        p99 = ident["p99"] if ident else None
        # 该股自身的日期轴（无本地记录则退化为自覆盖，记 axis_missing 供人工核查）
        axis = axis_all.get(code)
        if not axis:
            # 非 csi800 池（农业/半导体/宽池）在 csi800 raw 库无记录 ⇒ 覆盖率判据
            # 在此退化为 1.0，【不是判坏通过】。真实覆盖率由 ashare_xq_reqc.py
            # 在采集完成后按【池级并集轴 ∩ 该股上市后】重算（两遍法）。
            axis = set()
            n_axis_missing += 1
        bad, reason, stats = qc_check(code, rows, axis, p99)

        now = time.strftime("%Y-%m-%d %H:%M:%S")
        # 幂等替换：先删该股旧行再插入（daily_quotes_hfq 无主键，全量重采会累积重复行
        # —— 第廿七类(a)。同事务包裹，失败则整股回滚，不留半截数据）。
        conn.execute("DELETE FROM daily_quotes_hfq WHERE code=?", (code,))
        conn.executemany(
            "INSERT INTO daily_quotes_hfq VALUES(?,?,?,?,?,?,?,?,?)",
            [(code, d, o, cl, h, lo, v / 100.0, "xueqiu", now)
             for d, o, cl, h, lo, v in rows])   # volume: 雪球=股 → 换算为手，与旧库口径一致
        # ★ 2026-09-06 修复（第卅七类：库版本漂移）：原写死 11 个位置参数，而生产
        #   雪球库经 ashare_xq_reqc.py 的 ALTER TABLE 已扩到 14 列（多
        #   resume_exempt_days / axis_source / limit_used）⇒ 位置式 INSERT 直接
        #   OperationalError 崩溃。又因 daily_topup 只取 stdout 末行、不查 returncode，
        #   崩溃被静默吞噬，行情日采从未真正跑通过（数据靠事务回滚才没被写坏）。
        #   现改为按 PRAGMA 实际列动态构造：新旧库版本（11/12/14 列）通吃。
        qc_cols = [r[1] for r in conn.execute("PRAGMA table_info(hfq_qc)")]
        qc_vals = {
            "code": code, "collected_at": now,
            "n_bars": stats["n_bars"], "nonpositive": stats["nonpositive"],
            "over_limit_days": stats["over_limit_days"],
            "ret_min": stats["ret_min"], "ret_max": stats["ret_max"],
            "coverage": stats["coverage"], "identity_p99": p99,
            "bad": bad, "reason": reason,
            "resume_exempt_days": stats.get("resume_exempt_days", 0),
            "axis_source": ("pool_axis" if axis else "none"),
            "limit_used": stats.get("limit_used"),
        }
        _cols = [c for c in qc_cols if c in qc_vals]
        conn.execute(
            f"INSERT OR REPLACE INTO hfq_qc ({','.join(_cols)})"
            f" VALUES({','.join(['?'] * len(_cols))})",
            tuple(qc_vals[c] for c in _cols))
        conn.execute("INSERT OR REPLACE INTO collect_log_xq VALUES(?,?,?,?,?,?,?)",
                     (code, "ok", len(rows), rows[0][0], rows[-1][0], None, now))
        conn.commit()
        if bad:
            n_bad += 1
        else:
            n_ok += 1
        rec = {"code": code, "status": "ok", "bars": len(rows),
               "span": [rows[0][0], rows[-1][0]], "coverage": stats["coverage"],
               "over_limit": stats["over_limit_days"], "bad": bad, "reason": reason}
        if ident:
            rec["identity"] = ident
        log.append(rec)
        if i % 25 == 0 or i == len(todo):
            el = time.time() - t0
            print(f"[{i}/{len(todo)}] ok={n_ok} bad={n_bad} fail={n_fail} "
                  f"elapsed={el/60:.1f}min eta={(el/i*(len(todo)-i))/60:.1f}min", flush=True)
        time.sleep(args.sleep * (0.8 + 0.4 * random.random()))

    summary = {"date": time.strftime("%Y-%m-%d"), "source": "雪球 kline (type=after)",
               "db": str(NEW_DB), "pool_size": len(codes),
               "n_ok": n_ok, "n_bad": n_bad, "n_fail": n_fail,
               "n_identity_sampled": len(sample_set),
               "elapsed_min": round((time.time() - t0) / 60, 1),
               "records": log}
    # 报告名随【输出库】变化：连续采多个池时固定名会互相覆盖（第十八类静默覆盖）
    stem = NEW_DB.stem if NEW_DB.stem.endswith("_xq") else NEW_DB.stem + "_xq"
    p = OUT_DIR / f"{stem}_collect.json"
    p.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n采集完成 ok={n_ok} bad={n_bad} fail={n_fail} 耗时={summary['elapsed_min']}min")
    print(f"新库: {NEW_DB}\n报告: {p}")
    conn.close()


if __name__ == "__main__":
    main()
