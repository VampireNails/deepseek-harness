#!/usr/bin/env python3
"""按 CSV 币种分组，批量 LLM 抽取（驱动 equity_batch.py，Windows 下规避 shell 中文参数转义）。

用法:
  run_expansion_extract.py <csv> <current> <prior> <bs_current> <bs_prior> [skip1,skip2,...]

CSV 列: ticker,name_zh,sector,currency[,fiscal_year_end[,reason]]
- 按 currency 分组，每组一次 equity_batch run（batch 内逐只串行）；
- skip 列表中的标的跳过（如已试点完成）；
- included=0（已退市）的标的请直接从 CSV 行加 skip 列或从 skip 参数排除。
"""
from __future__ import annotations

import csv
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
PY = sys.executable


def main() -> None:
    csv_path = Path(sys.argv[1])
    current, prior = sys.argv[2], sys.argv[3]
    bs_cur, bs_prv = sys.argv[4], sys.argv[5]
    done = set(sys.argv[6].split(",")) if len(sys.argv) > 6 and sys.argv[6] else set()
    # 公告窗口（YYYYMMDD）。回溯历史期时**必须显式传**，否则 batch 默认窗口
    # 20260701~today 会去抓当期公告，却按 current 期入库——期标签污染
    # （波司登案例同款：3 月财年年报被标成 2026H1）。
    from_date = sys.argv[7] if len(sys.argv) > 7 else ""
    to_date = sys.argv[8] if len(sys.argv) > 8 else ""

    rows = list(csv.DictReader(open(csv_path, encoding="utf-8-sig")))
    groups: dict[str, list[tuple[str, str]]] = {}
    skipped = 0
    for r in rows:
        tk = (r.get("ticker") or "").strip()
        if not tk or tk in done:
            continue
        if (r.get("skip") or "").strip().lower() in ("1", "true", "yes"):
            skipped += 1
            continue
        cur = (r.get("currency") or "kHKD").strip()
        groups.setdefault(cur, []).append((tk, (r.get("name_zh") or "").strip()))

    print(f"[driver] groups: " + ", ".join(f"{k}={len(v)}" for k, v in groups.items())
          + f", skipped={skipped}", flush=True)
    for cur, items in groups.items():
        tickers = ",".join(t for t, _ in items)
        names = ",".join(n for _, n in items)
        print(f"=== group {cur}: {len(items)} tickers ===", flush=True)
        cmd = [PY, str(SCRIPTS / "equity_batch.py"), "run", "--tickers", tickers,
               "--names", names, "--currency", cur, "--llm",
               "--current", current, "--prior", prior,
               "--bs-current", bs_cur, "--bs-prior", bs_prv]
        if from_date and to_date:
            cmd += ["--from-date", from_date, "--to-date", to_date]
        subprocess.run(cmd, check=False)
    print("[driver] ALL GROUPS DONE", flush=True)


if __name__ == "__main__":
    main()
