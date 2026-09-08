#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
宏观指标 vintage 档案构建器（Route M / 宏观回溯与预判 Agent）

目的
----
合并 outputs/2026-08-19 .. 2026-09-01 的每日 macro_indicators.sqlite 快照，
重建 (indicator, country, period) 的"首发值 → 修订序列"时间线，落实用户
"每日自存档规避前视偏差"的设计意图，并固化修订证据（如 ppi_yoy 2025-01:
08-19=-1.9 -> 09-01=-2.3）。

设计纪律（对照 ashare-research-SOP.md §二/§十一）
- 去重键只用逻辑主键 (indicator,country,period,value_type,collected_at)
- 过滤脏周期 period < '2000-01'（08-19 实测出现 1939-02-01 垃圾行）
- collected_at 为 ISO 带时区，同格式下字典序可排序，直接用
- 落盘：outputs/macro_vintage.sqlite
  - raw_vintage : 全量原始行（含 snapshot 来源），as-of 查询在 Python 端做
  - series_meta : 每 (indicator,country,period,value_type) 的首发/最终/修订数
  - revisions   : 首发之后的每一次变更（is_revision=1）
  - latest      : 最终值宽表（分析用，零修订偏置控制见 harness 文档）
"""
import sqlite3, glob, os, json
from pathlib import Path

# 向上找首个含 outputs/ 的祖先作为 ROOT（脚本位于 my-deepseek-harness/deepseek-harness/scripts/macro-analysis/，
# 比 SOP §十一 的 parents[3] 深一层，故此处稳健探测而非写死层数）
_HERE = Path(__file__).resolve().parent
ROOT = _HERE
while not (ROOT / "outputs").is_dir() and str(ROOT) != str(ROOT.parent):
    ROOT = ROOT.parent
SNAP_GLOB = sorted(glob.glob(str(ROOT / "outputs" / "2026-08-*" / "macro_indicators.sqlite")))
SNAP_GLOB += [str(ROOT / "outputs" / "2026-09-01" / "macro_indicators.sqlite")]
# CN 长历史回补库（macro_backfill_cn.py 产出，eastmoney 第三方源，2006-01 起）
BACKFILL = ROOT / "outputs" / "macro_cn_long.sqlite"
if BACKFILL.exists():
    SNAP_GLOB += [str(BACKFILL)]
OUT = ROOT / "outputs" / "macro_vintage.sqlite"

MIN_PERIOD = "2000-01"  # 脏周期过滤下界

# 权威度（越小越权威）。SOP 纪律：严禁第三方源冒充官方一手；
# 同一 (indicator,period) 多源冲突时，**官方一手优先**，不得以采集时间新为由让第三方源覆盖。
AUTHORITY = {"nbs": 0, "bls": 0, "eurostat": 0, "boj": 0,
             "fred_csv": 1, "nbsc": 1,
             "eastmoney": 2, "unknown": 3}
AUTH_LABEL = {0: "official_primary", 1: "official_secondary", 2: "third_party", 3: "unknown"}


def authority_of(source: str) -> int:
    return AUTHORITY.get((source or "unknown").lower(), 3)


def snap_date(path: str) -> str:
    p = Path(path)
    if p.name == "macro_cn_long.sqlite":
        return "backfill_cn"   # 长历史回补批次（终值，非逐日快照）
    return p.parent.name

def main():
    if OUT.exists():
        OUT.unlink()
    out = sqlite3.connect(str(OUT))
    out.execute("""CREATE TABLE raw_vintage (
        indicator TEXT, country TEXT, period TEXT, value_type TEXT,
        value REAL, release_date TEXT, collected_at TEXT, source TEXT,
        source_series TEXT, snapshot TEXT, authority INTEGER)""")
    out.execute("CREATE INDEX ix_raw ON raw_vintage(indicator,country,period,value_type,collected_at)")
    out.execute("""CREATE TABLE series_meta (
        indicator TEXT, country TEXT, period TEXT, value_type TEXT,
        first_value REAL, first_collected_at TEXT, final_value REAL,
        final_collected_at TEXT, n_obs INTEGER, n_revisions INTEGER,
        final_source TEXT, final_authority INTEGER,
        PRIMARY KEY (indicator,country,period,value_type))""")
    out.execute("""CREATE TABLE revisions (
        indicator TEXT, country TEXT, period TEXT, value_type TEXT,
        collected_at TEXT, value REAL, is_revision INTEGER,
        source TEXT, kind TEXT)""")   # kind: temporal(同源时间修订) | cross_source(换源口径差异)
    out.execute("""CREATE TABLE latest (
        indicator TEXT, country TEXT, period TEXT, value_type TEXT, value REAL,
        release_date TEXT, source TEXT, source_series TEXT, authority INTEGER,
        snapshot TEXT)""")
    # 跨源冲突：同一 key 不同源给出不同值（口径差异，**不是**时间序列修订，必须与 revisions 分开记账）
    out.execute("""CREATE TABLE cross_source_conflict (
        indicator TEXT, country TEXT, period TEXT, value_type TEXT,
        official_value REAL, official_source TEXT, third_value REAL, third_source TEXT,
        abs_diff REAL, PRIMARY KEY (indicator,country,period,value_type))""")

    raw = []
    n_skip = 0
    for f in SNAP_GLOB:
        sd = snap_date(f)
        c = sqlite3.connect(f)
        rows = c.execute("""SELECT indicator_name, country, period, value,
            value_type, release_date, collected_at, source, source_series
            FROM macro_indicators
            WHERE period >= ?""", (MIN_PERIOD,)).fetchall()
        for r in rows:
            # query order: indicator_name, country, period, value, value_type, release_date, collected_at, source, source_series
            try:
                v = float(r[3]) if r[3] is not None else None
            except (TypeError, ValueError):
                n_skip += 1
                continue
            if v is None:
                n_skip += 1
                continue
            # schema raw_vintage: indicator,country,period,value_type,value,release_date,collected_at,source,source_series,snapshot,authority
            raw.append((r[0], r[1] or "", r[2], r[4], v, r[5], r[6], r[7], r[8], sd,
                        authority_of(r[7])))
        c.close()
    print(f"[ingest] skipped {n_skip} non-numeric/empty value rows")
    out.executemany("""INSERT INTO raw_vintage VALUES (?,?,?,?,?,?,?,?,?,?,?)""", raw)
    out.commit()
    print(f"[raw_vintage] inserted {len(raw)} rows from {len(SNAP_GLOB)} sources")
    print(f"[sources] " + ", ".join(
        f"{s}(auth={authority_of(s)})" for s in sorted({r[7] for r in raw})))

    # group by logical key, sort by collected_at
    cur = out.execute("""SELECT indicator,country,period,value_type,value,collected_at,source
                         FROM raw_vintage ORDER BY indicator,country,period,value_type,collected_at""")
    groups = {}
    for ind, co, per, vt, val, ca, src in cur.fetchall():
        groups.setdefault((ind, co, per, vt), []).append((ca, val, src))

    meta_rows = []
    rev_rows = []
    conflict_rows = []
    for key, timeline in groups.items():
        timeline.sort(key=lambda x: x[0])                     # 时间正序 -> 首发值
        first_ca, first_val, first_src = timeline[0]
        # 终值口径：官方一手优先；同级取最晚采集（第三方源不得因"采集时间新"覆盖官方）
        final = min(timeline, key=lambda x: (authority_of(x[2]), -len(x[0]), x[0]))
        final_ca, final_val, final_src = final
        # 修订计数：只记**同源自洽的时间修订**；换源造成的差异计入 cross_source_conflict
        n_rev = 0
        seen = {first_val}
        for ca, val, src in timeline[1:]:
            if src != first_src:
                continue
            if abs(val - first_val) > 1e-9 and val not in seen:
                n_rev += 1
                seen.add(val)
        for ca, val, src in timeline[1:]:
            rev_rows.append((*key, ca, val, 1 if (src == first_src and abs(val - first_val) > 1e-9) else 0,
                             src, "temporal" if src == first_src else "cross_source"))
        # 跨源冲突：官方 vs 第三方在同一期给出的值不同
        by_auth = {}
        for ca, val, src in timeline:
            a = authority_of(src)
            if a not in by_auth or ca > by_auth[a][0]:
                by_auth[a] = (ca, val, src)
        if 0 in by_auth and 2 in by_auth:
            oc, ov, os_ = by_auth[0]
            tc, tv, ts = by_auth[2]
            if abs(ov - tv) > 1e-9:
                conflict_rows.append((*key, ov, os_, tv, ts, round(abs(ov - tv), 6)))
        meta_rows.append((*key, first_val, first_ca, final_val, final_ca,
                          len(timeline), n_rev, final_src, authority_of(final_src)))
    out.executemany("""INSERT OR REPLACE INTO series_meta VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""", meta_rows)
    out.executemany("""INSERT INTO revisions VALUES (?,?,?,?,?,?,?,?,?)""", rev_rows)
    out.executemany("""INSERT OR REPLACE INTO cross_source_conflict VALUES (?,?,?,?,?,?,?,?,?)""", conflict_rows)
    out.commit()

    # latest = 权威终值（官方一手优先；第三方源不得覆盖官方），每 key 单行
    out.execute("DELETE FROM latest")
    out.execute("""INSERT INTO latest
        (indicator,country,period,value_type,value,release_date,source,source_series,authority,snapshot)
        SELECT r.indicator,r.country,r.period,r.value_type,r.value,r.release_date,
               r.source,r.source_series,r.authority,r.snapshot
        FROM raw_vintage r
        JOIN series_meta m ON r.indicator=m.indicator AND r.country=m.country
            AND r.period=m.period AND r.value_type=m.value_type
            AND r.collected_at=m.final_collected_at AND r.source=m.final_source""")
    out.commit()

    n_rev_series = out.execute("SELECT COUNT(*) FROM series_meta WHERE n_revisions>0").fetchone()[0]
    n_meta = out.execute("SELECT COUNT(*) FROM series_meta").fetchone()[0]
    n_latest = out.execute("SELECT COUNT(*) FROM latest").fetchone()[0]
    n_conf = out.execute("SELECT COUNT(*) FROM cross_source_conflict").fetchone()[0]
    print(f"[series_meta] {n_meta} keys; [revisions] {n_rev_series} keys have >=1 同源时间修订")
    print(f"[cross_source_conflict] {n_conf} keys 官方 vs 第三方口径分歧")
    print(f"[latest] {n_latest} 权威终值行")

    # surface a few concrete revision examples (evidence for the user's design)
    ex = out.execute("""SELECT indicator,country,period,first_value,final_value,n_revisions
                        FROM series_meta WHERE n_revisions>0 ORDER BY n_revisions DESC LIMIT 10""").fetchall()
    print("\n=== revision evidence (同源时间修订, top) ===")
    for r in ex:
        print(r)

    # 跨源口径分歧（第三方源验源证据）
    cf = out.execute("""SELECT indicator,country,period,official_value,third_value,abs_diff
                        FROM cross_source_conflict ORDER BY abs_diff DESC LIMIT 15""").fetchall()
    if cf:
        print("\n=== cross-source conflict (官方 vs 第三方) ===")
        for r in cf:
            print(r)

    # 覆盖：回补后各序列的期数区间
    print("\n=== latest coverage (n>=30 的序列) ===")
    for r in out.execute("""SELECT indicator,country,COUNT(*) n,MIN(period) p0,MAX(period) p1
                            FROM latest GROUP BY indicator,country HAVING n>=30
                            ORDER BY country,n DESC"""):
        print(r)

    # data-quality: duplicates within same snapshot (same key, same collected_at)
    dup = out.execute("""SELECT COUNT(*) FROM (
        SELECT indicator,country,period,value_type,collected_at FROM raw_vintage
        GROUP BY indicator,country,period,value_type,collected_at HAVING COUNT(*)>1)""").fetchone()[0]
    print(f"\n[qc] within-snapshot duplicate (key,collected_at) groups: {dup}")

    out.close()
    print(f"\nWROTE {OUT}")

if __name__ == "__main__":
    main()
