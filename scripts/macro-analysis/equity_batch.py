#!/usr/bin/env python3
"""Batch pipeline: 多股规模化采集 -> 抽取 -> 入库 -> Proof，输出批量报告。

每只股票串行执行（subprocess 复用既有脚本，保证幂等与语义一致）：
  1. 行情采集   equity_quotes.py collect
  2. 公告定位   hkexnews prefix.do(取 stockId) -> titleSearchServlet.do(找中期业绩/中期报告)
  3. 下载+提取  curl + pypdf -> announcement_text.txt
  4. 字段抽取   equity_extract.py extract -> facts.json（含人工复核队列）
  5. 入库       equity_ingest.py ingest
  6. Proof      verify_equity_data.py --ticker

设计要点（规模化）：
- 失败隔离：任一股票失败不影响其余，全部记录到批量报告；
- 幂等：所有子脚本同天批次幂等，可重跑；
- 成本控制：本流程**不调用 LLM**（纯规则抽取），LLM 深度分析只对"复核队列/异常信号"标的启用；
- 复核队列：抽取置信度 low 的项进 review，供人工或 dsh 补充。

用法: equity_batch.py run --tickers hk00175,hk00941 [--workers 1] [--skip-announcement]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
OUT_ROOT = SCRIPTS.parents[3] / "outputs"
DB = OUT_ROOT / "equity_fundamental.sqlite"
PY = sys.executable
HKEX = "https://www1.hkexnews.hk"
UA = "equity-batch-collector/1.0"

DOC_PATTERNS = ("中期業績", "中期业绩", "中期報告", "中期报告", "六個月業績", "六个月业绩",
                "六個月之業績", "六個月中期", "INTERIM RESULTS", "INTERIM REPORT")


def _get(url: str, timeout: int = 30, retries: int = 3) -> str:
    """GET 文本。**必须重试**：HKEX 间歇性失败（实测国药控股一次查询瞬时报错、
    重试即成功），单发失败会静默变成 NOT_FOUND —— 一个报告期凭空消失，
    且不会有任何告警（批量报告里只是一条无害的 NOT_FOUND）。
    """
    import time as _t
    last: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            return urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "ignore")
        except Exception as e:  # 网络/超时/5xx 一律重试
            last = e
            if attempt < retries:
                _t.sleep(1.5 * attempt)
    raise last  # type: ignore[misc]


def _iso_date(hkex_date: str) -> str:
    """HKEX 公告日期 '26/08/2026 12:04'（DD/MM/YYYY）-> ISO '2026-08-26'。

    必须规范为 ISO：release_date 会作为因子生效日参与日期比较，若存成
    DD-MM-YYYY 会与 YYYY-MM-DD 的标准发布日比较失效（min/max 全错），
    导致生效日错乱、前视偏差。
    """
    if not hkex_date:
        return ""
    day = hkex_date.strip().split(" ")[0]
    parts = day.split("/")
    if len(parts) == 3:
        a, b, c = parts
        if len(a) == 4:  # YYYY/MM/DD
            return f"{a}-{int(b):02d}-{int(c):02d}"
        return f"{c}-{int(b):02d}-{int(a):02d}"  # DD/MM/YYYY
    return day


def _lookup_stock_id(code: str, name_zh: str = "") -> int | None:
    """查 stockId。**股票代码优先**（稳定唯一标识），名称仅作回退。

    prefix.do 是前缀搜索接口：
    - 传【股票代码】→ 精确返回该正股（实测 30/30 成功，且自动给出官方繁体全称
      含 －Ｗ/－ＳＷ 后缀），不依赖繁简转换、不依赖名称映射表；
    - 传【名称】→ 按名称前缀搜索，返回前 10 条，正股常被权证（如「美團中銀七一購A」）
      淹没，且名称须与 HKEX 繁体全称一致，简体名匹配失败。
    故代码是唯一可靠的索引方式，名称仅作兜底。
    """
    # 主路径：代码查询
    try:
        raw = _get(f"{HKEX}/search/prefix.do?callback=cb&lang=ZH&type=A"
                   f"&name={urllib.parse.quote(code)}&market=SEHK")
        d = json.loads(raw[raw.index("(") + 1:raw.rindex(")")])
        sid = next((s["stockId"] for s in d.get("stockInfo", [])
                    if s.get("code") == code), None)
        if sid is not None:
            return sid
    except Exception:
        pass
    # 回退：名称查询（须为 HKEX 繁体全称，简体名会失败）
    if not name_zh:
        return None
    try:
        raw = _get(f"{HKEX}/search/prefix.do?callback=cb&lang=ZH&type=A"
                   f"&name={urllib.parse.quote(name_zh)}&market=SEHK")
        d = json.loads(raw[raw.index("(") + 1:raw.rindex(")")])
        return next((s["stockId"] for s in d.get("stockInfo", [])
                     if s.get("code") == code), None)
    except Exception:
        return None


def locate_announcement(ticker: str, name_zh: str, from_date: str, to_date: str,
                        current_period: str = "") -> tuple[str, str, str] | None:
    """返回 (title, date, pdf_url)。用 prefix.do(股票代码) 取 stockId，再检索公告。

    current_period（如 '2026H1'/'2024H2'）用于**季度制披露**定向放行：美股口径的
    双重上市公司（百度/携程/B站/网易）标题只写「第X季度業績公告」，没有
    「六個月/中期/年度」等半年度词，期间匹配会落空（实测百度 2026H1 NOT_FOUND）。
    放行规则严格按目标期次限定，避免 Q1/Q3 季报被误标成半年/全年数据：
      - 目标 H1 → 仅接受 第二季度 / Q2（对应截至 6-30 的六个月）
      - 目标 H2 → 仅接受 第四季度 / Q4 / 全年（对应截至 12-31 的十二个月）
    """
    code = ticker[2:]
    stock_id = _lookup_stock_id(code, name_zh)
    if stock_id is None:
        return None
    url = (f"{HKEX}/search/titleSearchServlet.do?sortDir=0&sortByOptions=DateTime&category=0&market=SEHK"
           f"&stockId={stock_id}&documentType=-1&fromDate={from_date}&toDate={to_date}&title=&searchType=1"
           f"&t1code=-2&t2Gcode=-2&t2code=-2&rowRange=100&lang=zh")
    # 注意：result 是 **JSON 字符串**（不是数组），需二次 json.loads；元素是 dict
    try:
        payload = json.loads(_get(url))
    except Exception:
        return None
    result = payload.get("result")
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except Exception:
            return None
    if not isinstance(result, list):
        return None
    # 宽松组合匹配（业绩词 × 期间词），覆盖各家措辞差异：
    #   中期業績 / 中期報告 / 六個月業績 / 六個月之業績（小米）/ 六個月的業績（快手）
    # 逐个加子串变体不可持续，改为组合判定；"報表"（如翌日披露報表/證券變動月報表）
    # 不含"報告"/"業績"，不会被误匹配。
    RESULT_KW = ("業績", "业绩", "報告", "报告", "RESULTS", "REPORT")
    # 期间词须覆盖年报（年度報告/年報/年度業績/ANNUAL REPORT）：
    # 仅含"六個月/中期"会漏掉全部年报，回溯历史期时报 NOT_FOUND。
    PERIOD_KW = ("六個月", "六个月", "中期", "年度", "年報", "年报",
                 "INTERIM", "ANNUAL")
    # 季度制披露定向放行（仅按目标期次开放，防止 Q1/Q3 季报污染半年/全年标签）
    if current_period.endswith("H1"):
        QUARTER_KW = ("第二季度", "第2季度", "Q2", "二季度")
    elif current_period.endswith("H2"):
        QUARTER_KW = ("第四季度", "第4季度", "Q4", "四季度", "全年", "全年度")
    else:
        QUARTER_KW = ()
    # 排除词：这些公告虽含"年度報告"等词，但不是财务报告，
    # 误匹配会导致抽到空数据（实测建行 2023H2 命中「獨立董事年度述職報告」，0 facts 入库）。
    EXCLUDE_KW = ("述職", "述职", "履職", "履职", "獨立董事", "独立董事",
                  "審計委員會", "审计委员会", "提名委員會", "薪酬委員會",
                  "ESG", "可持續發展", "可持续发展", "提名", "薪酬",
                  "股東週年大會", "股东周年大会", "投票表決", "投票表决",
                  "會議通告", "会议通告", "委任", "辭任", "辞任",
                  "關連交易", "关连交易", "須予披露", "须予披露",
                  "風險管理", "风险管理", "內部監控", "内部监控",
                  "內部控制", "内部控制", "評價報告", "评价报告",
                  "監事會", "监事会", "董事會報告", "董事会报告",
                  # 实测中国平安 2026H1（A+H）命中「海外監管公告…投資者保護工作報告」：
                  # 含"中期"+"報告"但不是财报。注意：A+H 公司的真财报也带
                  # 「海外監管公告」前缀，**不可整体排除**，只定向排治理/监管类。
                  "投資者保護", "投资者保护",
                  "工作報告", "工作报告", "提質增效", "提质增效",
                  "投資者關係", "投资者关系", "通函", "上市規則", "上市规则",
                  # 实测中信证券 2026H1 命中「參加…中期業績聯合發佈會」——發佈會通知非财报；
                  # 实测华润电力/国药控股命中「全資附屬公司…半年度報告」——子公司报表非上市主体。
                  "發佈會", "发布会", "附屬公司", "附属公司", "子公司", "聯營", "合營")
    # 两遍匹配（2026-08-31 平安案例）：先严格财报标题模式，避免"监管公告也含
    # 中期+報告"的误匹配；严格无命中再退回宽松组合（保住各家措辞差异的召回）。
    # 严格命中还须同时含结果词（業績/報告/RESULTS/REPORT）：实测
    # 「截至2026年6月30日止六個月中期股息」含"六個月中期"却只是股息公告。
    STRICT_PAT = ("中期業績", "中期业绩", "中期報告", "中期报告", "六個月業績",
                  "六个月业绩", "六個月之業績", "六個月中期業績", "年度業績", "年度业绩",
                  "年度報告", "年度报告", "年報", "年报",
                  "INTERIM RESULTS", "INTERIM REPORT", "ANNUAL RESULTS",
                  "ANNUAL REPORT")
    rows = []
    for row in result:
        if not isinstance(row, dict):
            continue
        title = row.get("TITLE", "") or ""
        link = row.get("FILE_LINK", "") or ""
        if not link.lower().endswith(".pdf"):
            continue
        t = title.replace(" ", "")  # 标题常带空格（如小米「2026 年6 月30 日」）
        if any(k in t for k in EXCLUDE_KW):
            continue
        hit = (title[:80], str(row.get("DATE_TIME", ""))[:16], HKEX + link)
        # 裸标题兜底（实测昆仑能源 2026H1 标题就叫「業績公佈」，无期间词可匹配）
        if t in ("業績公佈", "業績公布", "業績公告", "业绩公布", "业绩公告",
                 "中期業績公佈", "中期業績公告"):
            return hit
        has_result = any(k in t for k in RESULT_KW) or "REPORT" in t.upper() or "RESULTS" in t.upper()
        # 年度类严格模式须排除「半年度」：子串包含（"半年度報告"⊃"年度報告"）会让
        # A+H 母公司列表里的子公司 A 股半年报抢先严格命中（实测国药控股 2026H1
        # 命中子公司「一致藥業」的半年报——实体错配）。
        annual_hit = any(k in t for k in STRICT_PAT) and "半年度" not in t and "半年度报告" not in t
        if annual_hit and has_result:
            return hit  # 严格命中：财报标题模式，直接采信
        has_period = any(k in t for k in PERIOD_KW) or "INTERIM" in t.upper()
        # 季度制披露：标题只写「第X季度業績公告」时，按目标期次定向放行
        q_hit = any(k in t for k in QUARTER_KW) or any(
            k in t.upper() for k in QUARTER_KW if k.upper() == k and len(k) <= 2)
        if has_result and (has_period or q_hit):
            rows.append(hit)  # 宽松命中先攒着，严格模式可能出现在后面的行
    # 宽松命中排序：①業績类优先於報告类 ②半年/全年口径优先於纯季度口径
    #（Q2/Q4 季报含 YTD 列，但只有季度的不如同时披露六个月/全年者可靠）。
    rows.sort(key=lambda h: (
        0 if ("業績" in h[0] or "业绩" in h[0] or "RESULTS" in h[0].upper()) else 1,
        0 if any(k in h[0] for k in ("六個月", "六个月", "中期", "年度", "年報", "年报",
                                     "INTERIM", "ANNUAL")) else 1))
    return rows[0] if rows else None


def run_one(ticker: str, workdir: Path, from_date: str, to_date: str,
            name_zh: str, currency: str, skip_announcement: bool,
            llm: bool = False,
            periods: tuple[str, str, str, str] | None = None) -> dict:
    # periods = (current, prior, bs_current, bs_prior)，回溯历史报告期时显式传入
    cur, prv, bs_cur, bs_prv = periods or ("2026H1", "2025H1", "2026-06-30", "2025-12-31")
    workdir.mkdir(parents=True, exist_ok=True)
    res = {"ticker": ticker, "periods": {"current": cur, "prior": prv}, "steps": {}}

    def step(name: str, cmd: list[str], cwd: str | None = None) -> tuple[bool, str]:
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=600, cwd=cwd)
            out = (p.stdout or "") + (p.stderr or "")
            ok = p.returncode == 0
            res["steps"][name] = "ok" if ok else f"FAIL({p.returncode})"
            return ok, out[-400:]
        except Exception as e:
            res["steps"][name] = f"ERR:{e}"
            return False, str(e)

    # 风险规则（2026-08-31 事故）：count 必须 ≥1600 保住 2020-03 起的完整历史窗口。
    # collect 对同 source 是"最新快照"语义（先 DELETE 后 INSERT），
    # 若用小 count 重跑已采集标的会把 1600 bars 截断成 500。
    step("quotes", [PY, str(SCRIPTS / "equity_quotes.py"), "collect", "--symbols", ticker,
                    "--count", "1600"])

    if not skip_announcement:
        hit = locate_announcement(ticker, name_zh, from_date, to_date, cur)
        if not hit:
            res["steps"]["announcement"] = "NOT_FOUND"
        else:
            title, date, pdf = hit
            res["announcement"] = {"title": title, "date": date, "url": pdf}
            pdf_path = workdir / "announcement.pdf"
            # 公告文件 ID 作为 source（与规则抽取一致）：.../2026082701016_c.pdf -> 2026082701016。
            # 切勿用 ticker 作 source——否则同一份公告被规则/LLM 两次抽取会记成两个来源，
            # 旧批次无法覆盖（实测中芯：kUSD 旧数据与 mUSD 新数据混合致勾稽失败）。
            import re as _re
            _m = _re.search(r"/(\d{10,})_c?\.pdf", pdf)
            ann_source = f"hkex:{_m.group(1)}" if _m else f"hkex:{ticker}"
            step("download", ["curl", "-sL", "--max-time", "180", "-o", str(pdf_path), pdf])
            if pdf_path.exists():
                txt_path = workdir / "announcement_text.txt"
                # 提取引擎优先级：PyMuPDF（中文 CID 字体支持好）-> pypdf（回退）。
                # 实测吉利中文中期报告用 pypdf 提取为乱码，PyMuPDF 正常——规模化必须优先 PyMuPDF。
                extract_code = (
                    "import sys\n"
                    "try:\n"
                    "    import pymupdf as mupdf\n"
                    "except ImportError:\n"
                    "    import fitz as mupdf\n"
                    "d = mupdf.open(sys.argv[1])\n"
                    "parts = []\n"
                    "for i, page in enumerate(d):\n"
                    "    parts.append('\\n===== PAGE %d =====\\n' % (i + 1) + (page.get_text() or ''))\n"
                    "open(sys.argv[2], 'w', encoding='utf-8').write(''.join(parts))\n"
                )
                step("extract_pdf", [PY, "-c", extract_code, str(pdf_path), str(txt_path)])
                if txt_path.exists():
                    facts_path = workdir / ("facts_llm.json" if llm else "facts.json")
                    if llm:
                        # LLM 抽取：规则抽取在陌生财报格式上系统性失效（revenue 抓到 -10、
                        # 毛利率当毛利额等），LLM 语义抽取才是正确解。key 从 .env 或环境变量读。
                        step("extract_fields",
                             [PY, str(SCRIPTS / "equity_llm_extract.py"), "extract", "--ticker", ticker,
                              "--text", str(txt_path), "--out", str(facts_path),
                              "--source", ann_source, "--url", pdf,
                              "--current", cur, "--prior", prv,
                              "--bs-current", bs_cur, "--bs-prior", bs_prv,
                              "--currency", currency, "--release-date", _iso_date(date) if date else ""])
                    else:
                        step("extract_fields",
                             [PY, str(SCRIPTS / "equity_extract.py"), "extract", "--ticker", ticker,
                              "--text", str(txt_path), "--out", str(facts_path),
                              "--current", cur, "--prior", prv,
                              "--bs-current", bs_cur, "--bs-prior", bs_prv,
                              "--currency", currency, "--release-date", _iso_date(date) if date else ""])
                    if facts_path.exists():
                        # 空抽取不入库：误匹配非财报公告（如独董述职报告）会产出 0 facts，
                        # 若照常 ingest 会写入空数据、且 Proof 因依赖感知而静默 PASS。
                        try:
                            import json as _json
                            _n = len(_json.loads(facts_path.read_text(encoding="utf-8")).get("facts", []))
                        except Exception:
                            _n = -1
                        if _n == 0:
                            res["steps"]["ingest"] = "SKIPPED_EMPTY"
                            print(f"  [warn] {ticker} 抽取结果为空（疑似误匹配非财报公告：{title[:40]}），跳过入库")
                        else:
                            step("ingest", [PY, str(SCRIPTS / "equity_ingest.py"), "ingest",
                                            "--facts", str(facts_path)])

    ok, out = step("verify", [PY, str(SCRIPTS / "verify_equity_data.py"), "--ticker", ticker])
    res["proof"] = "PASS" if ok else "FAIL"
    if not ok:
        res["proof_tail"] = out
    return res


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tickers", required=True, help="逗号分隔，如 hk00175,hk00941")
    ap.add_argument("--names", default="", help="与 tickers 对应的中文名（用于公告定位），逗号分隔")
    ap.add_argument("--currency", default="kHKD", help="公告币种单位，如 kHKD/kUSD/kCNY")
    ap.add_argument("--from-date", default="20260701")
    ap.add_argument("--to-date", default=dt.date.today().strftime("%Y%m%d"))
    ap.add_argument("--skip-announcement", action="store_true", help="只采行情与 Proof，跳过公告流程")
    ap.add_argument("--llm", action="store_true", help="用 LLM 抽取（equity_llm_extract.py）替代规则抽取")
    # 报告期参数：回溯历史公告时必须显式指定，否则会误标成 2026H1。
    # 中期报告：--current 2025H1 --prior 2024H1 --bs-current 2025-06-30 --bs-prior 2024-12-31
    # 年度报表：--current 2025H2 --prior 2024H2 --bs-current 2025-12-31 --bs-prior 2024-12-31
    ap.add_argument("--current", default="2026H1")
    ap.add_argument("--prior", default="2025H1")
    ap.add_argument("--bs-current", default="2026-06-30")
    ap.add_argument("--bs-prior", default="2025-12-31")
    ap.add_argument("cmd", nargs="?", default="run", choices=["run"])
    args = ap.parse_args()

    periods = (args.current, args.prior, args.bs_current, args.bs_prior)
    tickers = [t.strip() for t in args.tickers.split(",") if t.strip()]
    names = [n.strip() for n in args.names.split(",")] if args.names else [""] * len(tickers)
    day = dt.date.today().isoformat()
    results = []
    for tk, nm in zip(tickers, names):
        # 目录含期次：回溯多个报告期时，同一标的若共用目录会互相覆盖 PDF/facts
        wd = OUT_ROOT / day / f"batch-{tk}-{args.current}"
        print(f"=== {tk} [{args.current}] ===")
        r = run_one(tk, wd, args.from_date, args.to_date, nm, args.currency,
                    args.skip_announcement, args.llm, periods)
        results.append(r)
        print(json.dumps({k: v for k, v in r.items() if k != "proof_tail"}, ensure_ascii=False)[:300])

    report = OUT_ROOT / day / "batch-report.md"
    report.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"# 批量采集报告 {day}", "",
             f"标的数：{len(results)}｜币种口径：{args.currency}｜跳过公告：{args.skip_announcement}", "",
             "| 标的 | 行情 | 公告 | 抽取 | 入库 | Proof |", "|---|---|---|---|---|---|"]
    for r in results:
        s = r.get("steps", {})
        lines.append(f"| {r['ticker']} | {s.get('quotes','-')} | {s.get('announcement') or s.get('download','-')} "
                     f"| {s.get('extract_fields','-')} | {s.get('ingest','-')} | {r.get('proof','-')} |")
    lines.append("")
    for r in results:
        if r.get("proof") == "FAIL":
            lines += [f"## {r['ticker']} Proof 失败摘要", "```", (r.get("proof_tail") or "")[-800:], "```"]
    report.write_text("\n".join(lines), encoding="utf-8")
    passed = sum(1 for r in results if r.get("proof") == "PASS")
    print(f"[batch] {passed}/{len(results)} Proof PASS；报告 -> {report}")


if __name__ == "__main__":
    main()
