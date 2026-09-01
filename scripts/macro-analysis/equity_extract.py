#!/usr/bin/env python3
"""Semi-automatic facts extractor: announcement text -> facts.json draft.

规模化瓶颈解法：人工逐股整理字段无法扩展，本脚本用**双语科目映射表**（中文繁简/英文）
从公告文本正则抽取数值，输出 ingest 可直接消费的 facts.json，并给出置信度与人工复核队列。

设计：
- 文本归一化：中文匹配用"去空格版"（pypdf 中文常带字间空格），英文匹配用原行；
- 每个科目取行内前两个数值，按 kind=flow|stock 分别映射到 [current, prior] 或 [current_bs, prior_bs]；
- 置信度：长/精确关键词=high，短/泛关键词=low；low 与多候选项进 review 队列（不入库或标注待核）。

用法:
  equity_extract.py extract --ticker hk00981 --text <file> --out <facts.json>
                            [--currency kUSD] [--release-date 2026-08-27]
                            [--current 2026H1 --prior 2025H1 --bs-current 2026-06-30 --bs-prior 2025-12-31]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# key -> (kind, [中文关键词（去空格匹配）], [英文关键词（原行匹配）], 期望数值个数)
KEYMAP: dict[str, tuple[str, list[str], list[str]]] = {
    "revenue":               ("flow", ["收入", "收益", "營業額", "营业额", "營業收入"], ["Revenue", "Turnover"]),
    "cost_of_services":      ("expense", ["銷售成本", "销售成本", "營業成本", "营业成本"], ["Cost of sales", "Cost of revenue"]),
    "gross_profit":          ("flow", ["毛利"], ["Gross profit"]),
    "net_profit":            ("flow", ["期內溢利", "期内溢利", "本期淨利潤", "净利润"], ["Profit for the period", "Profit for the year"]),
    "net_profit_attributable": ("flow", ["歸屬於本公司擁有人的期內利潤", "歸母淨利潤", "母公司擁有人應佔溢利", "归属于母公司股东的净利润"],
                                ["Profit for the period attributable to owners", "Profit attributable to owners of the Company"]),
    "rd_expense":            ("expense", ["研究及開發", "研究开发", "研發開支", "研发费用"], ["Research and development", "R&D"]),
    "admin_expense":         ("expense", ["一般及行政開支", "管理费用", "行政開支"], ["General and administrative", "Administrative expenses"]),
    "sm_expense":            ("expense", ["銷售及市場推廣開支", "销售及营销", "銷售費用"], ["Selling and marketing", "Selling expenses"]),
    "income_tax_expense":    ("expense", ["所得稅開支", "所得税费用", "稅項"], ["Income tax expense", "Income tax"]),
    "da_expense":            ("flow", ["折舊及攤銷", "折旧及摊销"], ["Depreciation and amortisation", "Depreciation and amortization"]),
    "ebitda":                ("flow", ["息稅折舊及攤銷前利潤"], ["EBITDA"]),
    "ocf":                   ("flow", ["經營活動所得現金淨額", "经营活动产生的现金流量净额", "經營活動現金流量淨額"],
                              ["Net cash generated from operating activities", "Net cash from operating activities"]),
    "investing_cf":          ("flow", ["投資活動所用現金淨額", "投资活动产生的现金流量净额"],
                              ["Net cash used in investing activities", "Net cash generated from investing activities"]),
    "financing_cf":          ("flow", ["融資活動所得現金淨額", "筹资活动产生的现金流量净额"],
                              ["Net cash generated from financing activities", "Net cash from financing activities"]),
    "capex":                 ("flow", ["購建固定資產", "购建固定资产", "資本開支", "资本开支"],
                              ["Purchase of property, plant and equipment", "Capital expenditure"]),
    "eps_basic":             ("flow", ["基本每股盈利", "基本每股收益"], ["Basic earnings per share"]),
    "total_assets":          ("stock", ["總資產", "资产总计", "資產總額"], ["Total assets"]),
    "total_liabilities":     ("stock", ["總負債", "负债合计", "負債總額"], ["Total liabilities"]),
    "total_equity":          ("stock", ["總權益", "权益合计", "股東權益合計"], ["Total equity"]),
    "cash_and_equivalents":  ("stock", ["現金及現金等價物", "现金及现金等价物"], ["Cash and cash equivalents"]),
    "inventories":           ("stock", ["存貨", "存货"], ["Inventories"]),
    "trade_receivables_net": ("stock", ["貿易應收賬款", "应收账款", "貿易應收款項"], ["Trade receivables"]),
    "goodwill":              ("stock", ["商譽", "商誉"], ["Goodwill"]),
    "shares_outstanding":    ("stock", ["已發行股份", "已发行股份", "總股本"], ["Total number of shares", "Number of shares"]),
    "net_debt_official":     ("stock", ["淨債務", "净债务", "淨負債"], ["Net debt"]),
    "borrowings_current":    ("stock", ["短期借款", "即期借款", "一年內到期的借款"], ["Short-term borrowings", "Current borrowings", "Borrowings due within one year"]),
    "borrowings_non_current": ("stock", ["長期借款", "非即期借款", "长期借款"], ["Long-term borrowings", "Non-current borrowings"]),
    "non_controlling_interests": ("stock", ["非控股權益", "少数股东权益", "非控制性權益"], ["Non-controlling interests"]),
    "non_recurring_total":   ("flow", ["非經常性損益合計", "非经常性损益合计"], ["Total non-recurring", "Non-recurring profit or loss"]),
}

NUM = re.compile(r"\(?\d[\d,]*\.?\d*\)?%?")


def parse_numbers(line: str) -> list[float]:
    vals: list[float] = []
    for raw in NUM.findall(line):
        s = raw.replace(",", "")
        neg = s.startswith("(") and s.endswith(")")
        if neg:
            s = s[1:-1]
        if s.endswith("%"):
            continue  # 百分比不当事值（避免把毛利率当毛利）
        try:
            v = float(s)
        except ValueError:
            continue
        vals.append(-v if neg else v)
    return vals


def best_line(lines: list[str], zh_keys: list[str], en_keys: list[str]) -> tuple[int, str]:
    """优先中文去空格精确匹配（越长越优先），其次英文原行匹配。"""
    for keys, mode in ((zh_keys, "zh"), (en_keys, "en")):
        for key in sorted(keys, key=len, reverse=True):
            for i, ln in enumerate(lines):
                tgt = ln.replace(" ", "") if mode == "zh" else ln
                if key in tgt:
                    return i, key
    return -1, ""


def strip_change_columns(vals: list[float]) -> tuple[list[float], bool]:
    """港股/美股财报表格常见 [当期, 上期, 变动%]，剔除末尾的变动率列。"""
    if len(vals) < 3 or len(vals) < 2:
        return vals, False
    a, b = vals[0], vals[1]
    out = list(vals[:2])
    stripped = False
    for extra in vals[2:]:
        if b != 0 and abs(extra - (a / b - 1) * 100) <= 0.6:
            stripped = True
            continue
        out.append(extra)
    return out, stripped


def strip_note_refs(vals: list[float]) -> list[float]:
    """港股报表常在科目名后带附注编号（如 'Inventories 16 4,360,884 3,629,802'），
    孤立小整数须剔除，否则附注号会被当成当期值。"""
    if len(vals) >= 2 and abs(vals[0]) < 100 and abs(vals[1]) > 1000:
        return vals[1:]
    return vals


def pick_rows(lines: list[str], zh_keys: list[str], en_keys: list[str]) -> list[tuple[int, str]]:
    """返回所有匹配行（按关键词长度降序），供调用方挑选最佳数值行。"""
    hits: list[tuple[int, str]] = []
    for keys, mode in ((zh_keys, "zh"), (en_keys, "en")):
        for key in sorted(keys, key=len, reverse=True):
            for i, ln in enumerate(lines):
                tgt = ln.replace(" ", "") if mode == "zh" else ln
                if key in tgt:
                    hits.append((i, key))
    return hits


def extract(text_path: Path, spec: dict) -> dict:
    text = text_path.read_text(encoding="utf-8", errors="ignore")
    lines = [l.strip() for l in text.split("\n")]
    used_lines: set[int] = set()
    facts, review = [], []

    # 先为所有科目求候选（取匹配词最长者），再按"匹配词长度"降序处理：
    # 更精确的关键词优先占用行，避免 net_profit 抢走 net_profit_attributable 的行。
    cands = []
    for key, (kind, zh, en) in KEYMAP.items():
        hits = pick_rows(lines, zh, en)
        if hits:
            best = max(hits, key=lambda t: len(t[1]))
            cands.append((len(best[1]), key, best[0], best[1], kind, hits))
    cands.sort(key=lambda t: -t[0])

    for _len, key, _idx, matched, kind, hits in cands:
        vals: list[float] = []
        used_line = None
        # 遍历所有匹配行（含 net debt 等多处出现的科目），挑第一个"剔除附注号/变动率后 >=2 数值且首值非零"的行
        for i, _m in sorted(hits, key=lambda t: t[0]):
            if i in used_lines:
                continue
            for j in range(i, min(i + 8, len(lines))):
                cand = strip_note_refs(strip_change_columns(parse_numbers(lines[j]))[0])
                cand = strip_change_columns(cand)[0]
                if len(cand) >= 2 and cand[0] != 0:
                    vals, used_line = cand, i
                    break
                if len(cand) == 1 and not vals:
                    vals, used_line = cand, i
            if len(vals) >= 2:
                break
        if not vals:
            review.append({"key": key, "reason": "no numeric row", "line": lines[hits[0][0]][:120]})
            continue
        if kind == "expense":
            vals = [abs(v) for v in vals]  # 费用统一存正值（公告常用括号表负）
        used_lines.add(used_line)
        # 置信度：匹配词越长越可信；剔除变动率后仍多候选则需人工确认
        conf = "high" if len(matched) >= 6 else ("medium" if len(matched) >= 3 else "low")
        if len(vals) > 2:
            conf = "low"
        if key in ("ebitda", "capex") and len(vals) < 2:
            conf = "low"  # 说明性文字行易误配
        if kind in ("flow", "expense"):
            periods = [spec["current"], spec["prior"]]
        else:
            periods = [spec["bs_current"], spec["bs_prior"]]
        vals2 = vals[:2] if len(vals) >= 2 else [vals[0], None]
        for p, v in zip(periods, vals2):
            if v is None:
                continue
            facts.append([key, p, v])
        if conf != "high":
            review.append({"key": key, "confidence": conf, "matched": matched,
                           "values": vals[:4], "line": lines[used_line][:120]})

    return {
        "ticker": spec["ticker"],
        "source": spec.get("source", f"hkex:{spec['ticker']}"),
        "url": spec.get("url"),
        "release_date": spec.get("release_date"),
        "source_label": spec.get("source_label", "HKEX 联交所公告"),
        "source_note": spec.get("source_note", "官方一手公告（抽取器半自动抽取，待 Proof 校验）"),
        "unit_default": spec.get("currency", "kCUR"),
        "quote_source": spec.get("quote_source", "tencent_ifzq_kline"),
        "quote_label": "腾讯 ifzq 日K",
        "periods": {"current": spec["current"], "prior": spec["prior"],
                    "current_bs": spec["bs_current"], "prior_bs": spec["bs_prior"]},
        "facts": facts,
        "_review": review,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ticker", required=True)
    ap.add_argument("--text", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--currency", default="kCUR")
    ap.add_argument("--release-date", default=None)
    ap.add_argument("--source", default=None)
    ap.add_argument("--url", default=None)
    ap.add_argument("--current", default="2026H1")
    ap.add_argument("--prior", default="2025H1")
    ap.add_argument("--bs-current", default="2026-06-30")
    ap.add_argument("--bs-prior", default="2025-12-31")
    ap.add_argument("cmd", nargs="?", default="extract", choices=["extract"])
    args = ap.parse_args()

    spec = {"ticker": args.ticker, "currency": args.currency, "release_date": args.release_date,
            "current": args.current, "prior": args.prior,
            "bs_current": args.bs_current, "bs_prior": args.bs_prior}
    if args.source:
        spec["source"] = args.source
    if args.url:
        spec["url"] = args.url
    out = extract(Path(args.text), spec)
    Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[extract] {args.ticker}: {len(out['facts'])} facts, {len(out['_review'])} review items -> {args.out}")
    for r in out["_review"][:12]:
        print(f"  review: {r['key']} ({r.get('confidence','?')}) :: {r['line'][:80]}")


if __name__ == "__main__":
    main()
