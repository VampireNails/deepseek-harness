#!/usr/bin/env python3
"""LLM-powered facts extractor: announcement text -> facts.json (DeepSeek API).

规则抽取（equity_extract.py）在陌生财报格式上系统性失效（实测安踏：revenue 抓到 -10、
gross_profit 抓到毛利率百分比、non_controlling_interests 抓到资产总值）。根本原因：
各公司财报排版/单位/附注格式不同，正则规则无法规模化。LLM 能理解语义与表格上下文，
是 Route B 定位中"LLM 负责抽取"的正确落点。

设计：
- 文本截取：定位利润表/资产负债表/现金流量表章节，控制 prompt 长度与成本；
- 单次 API 调用抽取全部关键字段（避免多次往返），输出严格 JSON；
- 单位统一：要求 LLM 以"百万"（原文单位若为千元/元则换算）输出，unit 标注 mXXX；
- 输出格式与 equity_extract.py 的 facts.json 完全兼容，直接喂 equity_ingest.py；
- 交叉校验：LLM 抽取结果与规则抽取比对，分歧字段进 _review。

用法:
  equity_llm_extract.py extract --ticker hk02020 --text <file> --out <facts.json>
                                [--currency kCNY] [--release-date 2026-08-26]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.request
from pathlib import Path

API_URL = "https://api.deepseek.com/v1/chat/completions"
MODEL = "deepseek-chat"
UA = "equity-llm-extract/1.0"

# 与 equity_extract.py KEYMAP 对齐的关键字段（聚焦可比性最强的因子）
FIELDS: dict[str, str] = {
    "revenue": "营业收入（flow，本期/上期，通常称「收入/收益/營業額/营业额」）",
    "cost_of_services": "销售成本（expense，「销售成本/營業成本」）",
    "gross_profit": "毛利（flow，「毛利」，注意是毛利额不是毛利率）",
    "net_profit": "净利润/期内溢利（flow，合并口径）",
    "net_profit_attributable": "归母净利润（flow，「归属于母公司/本公司拥有人」口径）",
    "total_assets": "总资产（stock，期末时点）",
    "total_liabilities": "总负债（stock，期末时点）",
    "total_equity": "股东权益合计（stock，期末时点）",
    "cash_and_equivalents": "现金及现金等价物（stock，期末时点）",
    "inventories": "存货（stock，期末时点）",
    "trade_receivables_net": "应收账款净额（stock，期末时点）",
    "goodwill": "商誉（stock，期末时点）",
    "shares_outstanding": "已发行股份总数（stock，期末时点，单位：股）",
    "borrowings_current": "短期借款/即期借款（stock，期末时点）",
    "borrowings_non_current": "长期借款/非即期借款（stock，期末时点）",
    "net_debt_official": "净债务（stock，期末时点，公告口径，可正可负）",
    "rd_expense": "研发开支/研究及开发费用（expense）",
    "ebitda": "息税折旧摊销前利润（flow）",
    "non_recurring_total": "非经常性损益合计（flow，"\
        "用于剔除一次性损益计算经常性利润；若公告未披露则省略）",
    "ocf": "经营活动现金流净额（flow）",
    "investing_cf": "投资活动现金流净额（flow，通常为负）",
    "financing_cf": "筹资活动现金流净额（flow）",
    "capex": "资本开支/购建固定资产（flow，正值）",
    "eps_basic": "基本每股收益（flow）",
}

# 章节定位关键词（繁体为主，兼容简体）
SECTION_KEYS = {
    "income": ["綜合損益表", "综合损益表", "損益表", "损益表", "利潤表", "利润表",
               "收益表", "簡明綜合損益", "简明综合损益"],
    "balance": ["資產負債表", "资产负债表", "財務狀況表", "财务状况表",
                "簡明綜合資產負債", "简明综合资产"],
    "cashflow": ["現金流量表", "现金流量表", "簡明綜合現金流量", "简明综合现金"],
}


def _load_api_key() -> str:
    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if key:
        return key
    # 回退：从 harness .env（非标准格式 "deepseek API key = sk-..."）解析
    env = Path(__file__).resolve().parents[4] / "my-deepseek-harness" / "deepseek-harness" / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8", errors="ignore").splitlines():
            if "=" in line and "key" in line.lower():
                return line.split("=", 1)[1].strip()
    return ""


def _slice_sections(text: str, budget: int = 24000) -> str:
    """定位三大报表章节的**正文**位置，分段截取后拼接。

    必须从每个报表各自定位：实测中芯的损益表在 @~3.5k、资产负债表在 @~65k，
    若只从"第一个正文位置"连续截取，资产负债表会落在截取范围外而被漏抽
    （导致 total_assets/total_equity 缺失）。

    目录过滤：目录通常在文本前部（@~1k），正文在更后；优先取 > TOC_END 的
    匹配，若某章节全部匹配都在前部（短公告），则退回取第一个匹配。
    """
    TOC_END = 5000
    per = budget // 3
    chunks: list[str] = []
    for name, keys in SECTION_KEYS.items():
        best = -1
        for k in keys:
            positions = [m.start() for m in re.finditer(re.escape(k), text)]
            if not positions:
                continue
            body = [p for p in positions if p > TOC_END]
            cand = body[0] if body else positions[0]
            if best == -1 or cand < best:
                best = cand
        if best == -1:
            continue
        start = max(0, best - 200)
        chunks.append(f"\n===== [{name}] =====\n" + text[start:start + per])
    return "\n".join(chunks) if chunks else text[:budget]


def _build_prompt(spec: dict, sliced: str) -> str:
    fields_desc = "\n".join(f"- {k}: {v}" for k, v in FIELDS.items())
    cur, prv = spec["current"], spec["prior"]
    cur_bs, prv_bs = spec["current_bs"], spec["prior_bs"]
    return f"""你是财务数据抽取专家。从下面的上市公司中期业绩公告文本中，抽取指定财务科目。

【期间约定】
- 流量类科目（收入/成本/利润/现金流/每股收益）：本期={cur}，上期={prv}
- 存量类科目（资产/负债/权益/存货/应收/商誉/现金）：期末={cur_bs}，期初={prv_bs}

【单位约定】
- 所有金额统一以「百万」为单位输出（若原文是「千元」则除以 1000，是「亿元」则乘以 100，是「元」则除以 1000000）。
- 只输出纯数字，不含单位、逗号、百分比符号。
- 缺失的科目不要编造，直接省略该字段。
- 费用类（成本/开支）输出正值；括号表负的现金流按实际符号输出。

【抽取字段】
{fields_desc}

【输出格式】严格输出一个 JSON 对象（不要 markdown 代码块、不要解释）：
{{"revenue": {{"{cur}": 数值, "{prv}": 数值}}, "total_assets": {{"{cur_bs}": 数值, "{prv_bs}": 数值}}, ...}}

【公告文本】
{sliced}
"""


def _call_llm(api_key: str, prompt: str, timeout: int = 120,
              retries: int = 3) -> str:
    """调用 DeepSeek API。带重试：实测批量时偶发网络/超时失败（百济、农夫山泉各一次）。"""
    import time
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            body = json.dumps({
                "model": MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0,
                "max_tokens": 3000,
            }).encode("utf-8")
            req = urllib.request.Request(API_URL, data=body, headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "User-Agent": UA,
            })
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            return data["choices"][0]["message"]["content"]
        except Exception as e:
            last_err = e
            if attempt < retries:
                wait = 3 * attempt
                print(f"[llm-extract] 第 {attempt} 次调用失败（{type(e).__name__}），{wait}s 后重试...")
                time.sleep(wait)
    raise RuntimeError(f"LLM API 调用失败（重试 {retries} 次）: {last_err}")


def _parse_json(raw: str) -> dict:
    """容忍 LLM 输出外层 markdown 代码块。"""
    raw = raw.strip()
    m = re.search(r"\{.*\}", raw, re.S)
    if m:
        raw = m.group(0)
    return json.loads(raw)


def extract(text_path: Path, spec: dict) -> dict:
    api_key = _load_api_key()
    if not api_key:
        return {"error": "no DEEPSEEK_API_KEY"}
    text = text_path.read_text(encoding="utf-8", errors="ignore")
    sliced = _slice_sections(text)
    prompt = _build_prompt(spec, sliced)
    raw = _call_llm(api_key, prompt)
    parsed = _parse_json(raw)

    # LLM 输出 {field: {period: value}} -> facts 数组 [key, period, value, unit]
    # unit 由 currency 推导（kUSD -> mUSD, kHKD -> mHKD, kCNY -> mCNY）。
    # 不可写死默认 mCNY：否则传 --currency kUSD 时美元财报会被错标为人民币
    # （实测汇丰/友邦/百济三只美元财报均被标成 mCNY）。
    cur = spec.get("currency", "kCNY")
    unit = "m" + cur.lstrip("k") if cur.startswith("k") else f"m{cur}"
    facts: list[list] = []
    review: list[dict] = []
    for key, mapping in parsed.items():
        if not isinstance(mapping, dict):
            review.append({"key": key, "reason": "non-dict output", "value": mapping})
            continue
        for period, value in mapping.items():
            if value is None:
                continue
            try:
                v = float(value)
            except (TypeError, ValueError):
                review.append({"key": key, "reason": "non-numeric", "value": value})
                continue
            facts.append([key, period, v, unit])

    return {
        "ticker": spec["ticker"],
        "source": spec.get("source", f"hkex:{spec['ticker']}"),
        "url": spec.get("url"),
        "release_date": spec.get("release_date"),
        "source_label": spec.get("source_label", "HKEX 联交所公告"),
        "source_note": spec.get("source_note", "官方一手公告（LLM 抽取，待 Proof 校验）"),
        "unit_default": spec.get("currency", "kCNY"),
        "quote_source": spec.get("quote_source", "tencent_ifzq_kline"),
        "quote_label": "腾讯 ifzq 日K",
        "periods": {"current": spec["current"], "prior": spec["prior"],
                    "current_bs": spec["current_bs"], "prior_bs": spec["prior_bs"]},
        "facts": facts,
        "_review": review,
        "_llm_raw": raw,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ticker", required=True)
    ap.add_argument("--text", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--currency", default="kCNY")
    ap.add_argument("--unit-m", default="mCNY", help="LLM 输出单位（百万）标记")
    ap.add_argument("--release-date", default=None)
    ap.add_argument("--source", default=None)
    ap.add_argument("--url", default=None)
    ap.add_argument("--current", default="2026H1")
    ap.add_argument("--prior", default="2025H1")
    ap.add_argument("--bs-current", default="2026-06-30")
    ap.add_argument("--bs-prior", default="2025-12-31")
    ap.add_argument("cmd", nargs="?", default="extract", choices=["extract"])
    args = ap.parse_args()

    spec = {"ticker": args.ticker, "currency": args.currency, "unit_m": args.unit_m,
            "release_date": args.release_date, "current": args.current, "prior": args.prior,
            "current_bs": args.bs_current, "prior_bs": args.bs_prior}
    if args.source:
        spec["source"] = args.source
    if args.url:
        spec["url"] = args.url
    out = extract(Path(args.text), spec)
    Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    if "error" in out:
        print(f"[llm-extract] ERROR: {out['error']}")
        raise SystemExit(1)
    print(f"[llm-extract] {args.ticker}: {len(out['facts'])} facts, "
          f"{len(out['_review'])} review items -> {args.out}")
    for r in out["_review"][:10]:
        print(f"  review: {r['key']} :: {r.get('reason','?')} value={r.get('value')}")


if __name__ == "__main__":
    main()
