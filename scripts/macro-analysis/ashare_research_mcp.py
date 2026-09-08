#!/usr/bin/env python3
"""A-share equity-research MCP server (stdio transport, mcp 2.0 low-level Server).

WHY THIS EXISTS
---------------
The `equity-research` preset shipped with 89 Python scripts but ZERO
financial-statement *capability* wired into the agent: every script only ever
called `RPT_LICO_FN_CPD` (the income statement). A 2026-09-08 blind comparison
showed the agent produced a 283-line report on 600416 that never mentioned
cash flow, the balance sheet, non-recurring gains or segment margins -- not
because its workflow forgot to ask, but because the capability layer could not
answer. Re-stating the rule in workflow.md cannot fix a missing data source.

This server closes that gap the dsh-native way: capability as a TOOL, not as
prose. Workflow.md then only owns discipline and shape.

Tools (each returns one structured object, wrapped in a single text block):
  ashare_income      -- income statement time series (incl. 扣非 / non-recurring)
  ashare_cashflow    -- cash flow statement + operating-CF vs net-profit divergence
  ashare_balance     -- balance sheet key items + leverage
  ashare_segments    -- revenue & gross margin by product / region (business attribution)
  ashare_valuation   -- PB / PS / market cap with PE trap guards

Design rules baked into the output (so discipline survives context compression):
  * every numeric block carries a `read` field: plain-language "what it means"
  * every tool carries `caveats`: known unit/calibre traps (e.g. 东财 f162 is
    动态市盈率 NOT PE-TTM)
  * every tool carries `must_check_next`: the follow-up questions the number
    raises, so the agent cannot stop at "not provided"

Read-only. No predictions, no buy/sell signals, no position sizing, no timing.
"""
from __future__ import annotations

import asyncio
import json
import os
import ssl
import sys
import urllib.parse
import urllib.request
from typing import Any

from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.types import (
    CallToolRequestParams,
    CallToolResult,
    ListToolsResult,
    PaginatedRequestParams,
    TextContent,
    Tool,
)

ssl._create_default_https_context = ssl._create_unverified_context

EM = "https://datacenter-web.eastmoney.com/api/data/v1/get"
UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://data.eastmoney.com/",
    "Accept": "*/*",
}
YI = 1e8  # yuan -> 亿元


# --------------------------------------------------------------------------
# transport helpers
# --------------------------------------------------------------------------

# Eastmoney is NOT consistent about the report-date column across reports.
# Verified 2026-09-08 against 600416: RPT_LICO_FN_CPD uses `REPORTDATE`
# (no underscore) while the DMSK/BALANCE/MAINOP reports use `REPORT_DATE`.
# Getting this wrong yields a silent "排序列不存在" empty result — the exact
# failure mode that let the cash-flow capability stay missing for months.
_DATE_FIELD = {
    "RPT_LICO_FN_CPD": "REPORTDATE",
    "RPT_DMSK_FN_CASHFLOW": "REPORT_DATE",
    "RPT_DMSK_FN_BALANCE": "REPORT_DATE",
    "RPT_F10_FN_MAINOP": "REPORT_DATE",
}


def _probe_listed(code: str) -> tuple[bool | None, str]:
    """Is this code still live? Returns (live|None, name).

    Used only to make fail-loud errors *actionable*: "已退市/代码无效" and
    "接口故障" demand completely different follow-ups, but both arrive as an
    empty eastmoney result.

    Measured 2026-09-08 on a 200-code random sample of the local pool:
    coverage is 92-93.5%, and **every single miss** (13/13) was a stock that
    is no longer traded — 9 carry an ST/退 name, the other 4 (600068 葛洲坝 /
    600723 首商股份 / 900935 阳晨B股 / 600102 莱钢股份) look normal by name
    but return **volume = 0** because they were absorbed/delisted.

    So the reliable test is `volume == 0`, NOT "does the name contain ST".
    A normal-trading stock (600416, volume 71456) is essentially always
    covered. Say this explicitly instead of letting the agent write "数据缺失".
    """
    try:
        pfx = "sh" if code[0] in "69" else ("sz" if code[0] in "03" else "sh")
        req = urllib.request.Request(
            f"https://qt.gtimg.cn/q={pfx}{code}", headers=UA)
        with urllib.request.urlopen(req, timeout=10) as resp:
            txt = resp.read().decode("gbk", "ignore")
        parts = txt.split("~")
        if len(parts) <= 10 or not parts[1].strip():
            return False, ""
        try:
            vol = float(parts[6]) if len(parts) > 6 else 0.0
        except ValueError:
            vol = 0.0
        # volume 0 == suspended from trading (absorbed / delisted / B-share)
        return (vol > 0), parts[1].strip()
    except Exception:
        return None, ""


def _em(report_name: str, code: str, page_size: int = 8) -> list[dict]:
    """Query an eastmoney datacenter report. Fail-loud: never return [] silently."""
    date_col = _DATE_FIELD.get(report_name, "REPORT_DATE")
    filt = urllib.parse.quote(f'(SECURITY_CODE="{code}")')
    q = (f"columns=ALL&filter={filt}&pageSize={page_size}"
         f"&sortColumns={date_col}&sortTypes=-1&source=WEB&client=WEB"
         f"&reportName={report_name}")
    req = urllib.request.Request(f"{EM}?{q}", headers=UA)
    with urllib.request.urlopen(req, timeout=25) as resp:
        payload = json.loads(resp.read().decode("utf-8", "ignore"))
    data = ((payload or {}).get("result") or {}).get("data")
    if data is None:
        live, name = _probe_listed(code)
        if "ST" in name.upper() or "退" in name or live is False:
            hint = (f"诊断：该股是「{name or code}」，属 ST / 退市 / 已停止交易标的。"
                    "★ 已知能力边界：东财 datacenter 的财报报表对**不再交易的股票**"
                    "普遍无数据（2026-09-08 抽样 200 只：覆盖率 92-93.5%，"
                    "未命中的 13 只**全部**是已退市或被吸收合并、成交量为 0 的标的；"
                    "正常交易股票基本 100% 覆盖）。这不是数据错误，重试无用。"
                    "请改用 ashare_risk_scan 走公告/新闻取证，"
                    "并**明确告知用户该股已停止交易、财务三表能力不可用**，"
                    "不要写「数据缺失」。")
        elif live is False:
            hint = ("诊断：行情源也查不到该代码，大概率是**已退市 / 代码写错 / 新股未上市**。"
                    "这不是接口故障，重试无用 —— 请向用户确认代码，不要写「数据缺失」。")
        elif live is True:
            hint = (f"诊断：行情源能查到该代码（{name}，仍在交易），但东财该报表无数据 —— "
                    "可能是该报表不适用此标的（如金融业无存货）或数据延迟。"
                    "可用其他工具交叉验证，不要直接写「数据缺失」。")
        else:
            hint = "诊断：行情源也无法判定（网络问题？），请重试后再下结论。"
        raise RuntimeError(
            f"eastmoney returned no `result.data` for {report_name}/{code}. "
            f"{hint} raw={json.dumps(payload, ensure_ascii=False)[:160]}")
    for row in data:  # normalise the date so callers never touch raw column names
        row["_date"] = str(row.get(date_col) or "")[:10]
    return data


def _num(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _yi(v: Any) -> float | None:
    n = _num(v)
    return None if n is None else round(n / YI, 4)


def _pct_raw(v: Any) -> float | None:
    """Value is ALREADY a percentage (41.4 means 41.4%).

    Verified 2026-09-08: YSTZ / SJLTZ / XSMLL / WEIGHTAVG_ROE /
    DEBT_ASSET_RATIO arrive this way. Multiplying by 100 here produced a
    4141% debt ratio — a silent 100x error that reads as plausible text.
    """
    n = _num(v)
    return None if n is None else round(n, 2)


def _pct_frac(v: Any) -> float | None:
    """Value is a FRACTION and needs x100 (0.2 means 20%).

    Verified 2026-09-08: only the F10 MAINOP report (MBI_RATIO /
    GROSS_RPOFIT_RATIO) uses this convention.
    """
    n = _num(v)
    return None if n is None else round(n * 100, 2)


def _d(v: Any) -> str:
    return str(v or "")[:10] or "—"


# --------------------------------------------------------------------------
# tool: income statement
# --------------------------------------------------------------------------

INCOME_FIELDS = {
    "TOTAL_OPERATE_INCOME": "营收",
    "YSTZ": "营收同比%",
    "PARENT_NETPROFIT": "归母净利",
    "SJLTZ": "净利同比%",
    "DEDUCT_PARENT_NETPROFIT": "扣非净利",
    "BASIC_EPS": "EPS",
    "DEDUCT_BASIC_EPS": "扣非EPS",
    "WEIGHTAVG_ROE": "ROE%",
    "XSMLL": "毛利率%",
    "BPS": "每股净资产",
}


def _ashare_income(args: dict) -> dict:
    code = (args.get("code") or "").strip()
    if not code:
        raise ValueError("code is required (6-digit A-share code, e.g. 600416)")
    n = int(args.get("periods") or 8)
    rows = _em("RPT_LICO_FN_CPD", code, n)
    series = []
    for r in rows:
        rev = _yi(r.get("TOTAL_OPERATE_INCOME"))
        np_ = _yi(r.get("PARENT_NETPROFIT"))
        eps = _num(r.get("BASIC_EPS"))
        dk_eps = _num(r.get("DEDUCT_BASIC_EPS"))
        # RPT_LICO_FN_CPD has NO absolute 扣非净利 column -- only the per-share
        # figure. Recover the share count from 净利/EPS, then scale back up.
        # (This gap is exactly why earlier rounds reported "扣非 unavailable".)
        shares = (np_ * YI / eps) if (np_ is not None and eps) else None
        dk = round(dk_eps * shares / YI, 4) if (dk_eps is not None and shares) else None
        item = {
            "报告期": r.get("_date"),
            "公告日": _d(r.get("NOTICE_DATE") or r.get("APPOINT_PUBLISH_DATE")),
            "营收_亿": rev,
            "营收同比_%": _pct_raw(r.get("YSTZ")),
            "归母净利_亿": np_,
            "净利同比_%": _pct_raw(r.get("SJLTZ")),
            "扣非净利_亿": dk,
            "扣非推算方式": "扣非EPS × (归母净利/EPS) 推算，非官方绝对额" if dk is not None else None,
            "EPS": eps,
            "扣非EPS": dk_eps,
            "ROE_%": _pct_raw(r.get("WEIGHTAVG_ROE")),
            "毛利率_%": _pct_raw(r.get("XSMLL")),
            "每股净资产": _num(r.get("BPS")),
        }
        if np_ is not None and dk is not None:
            item["非经常性损益_亿"] = round(np_ - dk, 4)
            item["非经常占净利_%"] = (
                None if np_ == 0 else round((np_ - dk) / abs(np_) * 100, 1))
        series.append(item)

    latest = series[0] if series else {}
    dk_l = latest.get("扣非净利_亿")
    np_l = latest.get("归母净利_亿")
    read = []
    if dk_l is not None and np_l is not None:
        if dk_l < 0 <= np_l:
            read.append(
                f"最新期归母净利 {np_l} 亿为正，但扣非净利 {dk_l} 亿已为负 —— "
                f"主业实际在亏，账面利润全靠非经常性损益（外快）撑起来。")
        elif dk_l >= 0 and np_l:
            share = round(dk_l / np_l * 100, 1)
            read.append(f"最新期扣非净利 {dk_l} 亿，占归母净利 {share}%。")
    dk_trend = [s.get("扣非净利_亿") for s in series[:5] if s.get("扣非净利_亿") is not None]
    if len(dk_trend) >= 3:
        mono_down = all(dk_trend[i] >= dk_trend[i + 1] for i in range(len(dk_trend) - 1))
        if mono_down:
            read.append(
                f"扣非净利近 {len(dk_trend)} 期连续下滑（{' → '.join(str(x) for x in dk_trend)} 亿），"
                f"主业盈利能力在持续退化，不是单期波动。")

    return {
        "code": code,
        "source": "东财 datacenter RPT_LICO_FN_CPD（业绩报表）",
        "series": series,
        "read": read or ["（数据已返回，未触发解读规则）"],
        "caveats": [
            "东财 f162 字段是「动态市盈率」（按最新报告期年化），不是 PE(TTM)，两者可差 2 倍，禁用 f162 当 PE-TTM。",
            "净利趋近 0 时 PE 会爆炸失真，此时应改用 PB / PS 为估值锚。",
        ],
        "must_check_next": [
            "非经常性损益的构成：政府补助（可持续）还是卖资产（一次性）？用 ashare_segments 之外还需查公告。",
            "最新期扣非为负时，必须查 ashare_cashflow 验证经营现金流是否同步恶化。",
        ],
    }


# --------------------------------------------------------------------------
# tool: cash flow
# --------------------------------------------------------------------------

def _ashare_cashflow(args: dict) -> dict:
    code = (args.get("code") or "").strip()
    if not code:
        raise ValueError("code is required")
    n = int(args.get("periods") or 8)
    rows = _em("RPT_DMSK_FN_CASHFLOW", code, n)
    series = [{
        "报告期": r.get("_date"),
        "经营净现金流_亿": _yi(r.get("NETCASH_OPERATE")),
        "投资净现金流_亿": _yi(r.get("NETCASH_INVEST")),
        "筹资净现金流_亿": _yi(r.get("NETCASH_FINANCE")),
        "销售商品收到现金_亿": _yi(r.get("SALES_SERVICES")),
        "支付给职工现金_亿": _yi(r.get("PAY_STAFF_CASH")),
    } for r in rows]

    # cross-check against net profit (income statement)
    inc = {}
    try:
        for r in _em("RPT_LICO_FN_CPD", code, n):
            inc[r.get("_date")] = _yi(r.get("PARENT_NETPROFIT"))
    except Exception as e:  # never let a cross-check kill the primary result
        inc = {"_error": str(e)}

    for s in series:
        s["归母净利_亿"] = inc.get(s["报告期"])
        ocf, npf = s.get("经营净现金流_亿"), s.get("归母净利_亿")
        if ocf is not None and npf is not None:
            s["现金流/净利_倍"] = None if npf == 0 else round(ocf / npf, 2)

    latest = series[0] if series else {}
    read = []
    ocf = latest.get("经营净现金流_亿")
    npf = latest.get("归母净利_亿")
    if ocf is not None:
        read.append(
            f"最新期（{latest.get('报告期')}）经营活动净现金流 {ocf} 亿："
            + ("净流出，主营业务的钱在往外掏，不是往里进。"
               if ocf < 0 else "净流入，主业在产生现金。"))
    if ocf is not None and npf is not None:
        if npf > 0 and ocf < 0:
            read.append(
                f"关键背离：账上赚了 {npf} 亿净利润，经营现金流却是 {ocf} 亿 —— "
                f"利润没有变成现金，通常意味着钱压在应收账款或存货里，利润质量存疑。")
        elif npf < 0 and ocf > 0:
            # 首版只覆盖了"盈利但现金流出"，漏掉这个方向。2026-09-08 万科
            # (净利 -149.5 亿 / OCF +4.95 亿) 暴露：两者含义完全相反，
            # 把"减值导致的账面亏损"读成现金危机会得出反向结论。
            read.append(
                f"关键背离（反向）：账上亏了 {abs(npf)} 亿，经营现金流却是正的 "
                f"{ocf} 亿 —— 亏损里很可能主要是折旧、摊销、资产减值这类"
                f"**不需要真金白银付出**的项目。这不等于安全，但直接把亏损"
                f"读成「现金快断了」是错的，必须去查减值的构成。")
        elif ocf < 0 and npf < 0:
            read.append("净利与经营现金流双负，主业既没赚到账面利润也没收到现金。")
    sale = latest.get("销售商品收到现金_亿")
    if sale and ocf is not None:
        read.append(
            f"最新期销售商品实际收到现金 {sale} 亿，而经营净现金流 {ocf} 亿，"
            f"差额 {round(ocf - sale, 3)} 亿 —— 收到的货款被其他经营支出吃掉的部分。")

    return {
        "code": code,
        "source": "东财 datacenter RPT_DMSK_FN_CASHFLOW（现金流量表）",
        "series": series,
        "read": read or ["（数据已返回，未触发解读规则）"],
        "caveats": [
            "现金流是收付实现制，净利是权责发生制，两者背离本身就是最重要的财务质量信号。",
            "单季/半年报现金流季节性极强，必须与去年同期比，不能只看绝对值。",
        ],
        "must_check_next": [
            "经营现金流为负时：查 ashare_balance 的应收账款与存货，确认钱压在哪里。",
            "持续为负时：查筹资现金流是否靠借款续命（借新还旧）。",
        ],
    }


# --------------------------------------------------------------------------
# tool: balance sheet
# --------------------------------------------------------------------------

def _ashare_balance(args: dict) -> dict:
    code = (args.get("code") or "").strip()
    if not code:
        raise ValueError("code is required")
    n = int(args.get("periods") or 8)
    rows = _em("RPT_DMSK_FN_BALANCE", code, n)
    series = [{
        "报告期": r.get("_date"),
        "货币资金_亿": _yi(r.get("MONETARYFUNDS")),
        "应收账款_亿": _yi(r.get("ACCOUNTS_RECE")),
        "存货_亿": _yi(r.get("INVENTORY")),
        "总资产_亿": _yi(r.get("TOTAL_ASSETS")),
        "总负债_亿": _yi(r.get("TOTAL_LIABILITIES")),
        "资产负债率_%": _pct_raw(r.get("DEBT_ASSET_RATIO")),
        # CURRENT_RATIO arrives as a percentage (189.2 == 1.89x)
        "流动比率_倍": (None if _num(r.get("CURRENT_RATIO")) is None
                    else round(_num(r.get("CURRENT_RATIO")) / 100, 2)),
    } for r in rows]

    latest = series[0] if series else {}
    read = []
    cash = latest.get("货币资金_亿")
    if cash is not None and len(series) > 1:
        prev = series[1].get("货币资金_亿")
        if prev:
            chg = round((cash / prev - 1) * 100, 1)
            # 方向词必须由数值决定，不能写死 —— 首版写死"降至"，在工行
            # （33990→35177 亿，实为上升）上输出反向结论。跨票自检抓到。
            direction = "升至" if chg >= 0 else "降至"
            if chg <= -20:
                tail = "手头现金在快速消耗，需确认是否影响正常经营。"
            elif chg >= 50:
                tail = "现金大幅增加，需确认是经营回款还是借款/再融资所得（看筹资现金流）。"
            else:
                tail = "变动幅度尚在正常范围。"
            read.append(
                f"货币资金从上一期 {prev} 亿{direction} {cash} 亿"
                f"（{chg:+}%）：{tail}")
    ar = latest.get("应收账款_亿")
    inv = latest.get("存货_亿")
    ta = latest.get("总资产_亿")
    if ar and ta:
        read.append(f"应收账款 {ar} 亿，占总资产 {round(ar / ta * 100, 1)}%"
                    + ("（占比偏高，回款风险值得单独查证）。" if ar / ta > 0.25 else "。"))
    if ar and inv:
        read.append(f"应收 {ar} 亿 + 存货 {inv} 亿 = {round(ar + inv, 2)} 亿资金被占用，"
                    "这正是经营现金流为负时最该看的地方。")
    dar = latest.get("资产负债率_%")
    if dar is not None:
        # 负债率的"高低"必须带行业上下文：银行 92% 是常态，制造业 92% 是危机。
        # 首版只报数字不报口径，跨票自检在 601398 工行上暴露了误判风险。
        if dar > 85:
            tail = ("（>85% 属极高杠杆。⚠ 但银行业/地产/券商此区间是行业常态，"
                    "必须先确认行业再判断是否异常，不得直接当风险结论。）")
        elif dar > 60:
            tail = "（偏高，需结合有息负债与货币资金判断偿债压力。）"
        else:
            tail = "（处于常见区间。）"
        read.append(f"资产负债率 {dar}%。{tail}")
    if dar is not None and dar > 100:
        read.append("★ 负债率 >100% = 资不抵债（净资产为负），"
                    "此时 PB 为负、无意义，估值只能用 PS 或清算口径。")

    # 科目为 None 必须解释，否则 agent 会把"银行业无此科目"误读为"数据缺失"。
    missing = [n for n, v in (("应收账款", ar), ("存货", inv)) if not v]
    if missing:
        read.append(
            f"「{'、'.join(missing)}」为 None：可能是银行/保险/券商等金融业"
            "本就没有该科目（属正常），也可能是未披露 —— 必须先确认行业再下结论，"
            "不得写成「数据缺失」。")

    return {
        "code": code,
        "source": "东财 datacenter RPT_DMSK_FN_BALANCE（资产负债表）",
        "series": series,
        "read": read or ["（数据已返回，未触发解读规则）"],
        "caveats": [
            "字段名是 MONETARYFUNDS（带 S），写成 MONETARYFUND 会静默返回 0 —— 已修正，勿改回。",
            "资产负债表是时点数，单期意义有限，必须看趋势。",
            "毛利率/存货为 None 时先确认行业（金融业无此科目），别当数据缺失。",
        ],
        "must_check_next": [
            "应收账款金额大时：必须查证前五大债务人是否出现破产重整/大额坏账（用 web 检索公告原文）。",
            "货币资金大幅下降时：查 ashare_cashflow 的筹资现金流，确认是否在还债。",
            "短期借款高 + 货币资金低：流动性风险，需查是否有股权质押。",
        ],
    }


# --------------------------------------------------------------------------
# tool: segments (business attribution)
# --------------------------------------------------------------------------

def _ashare_segments(args: dict) -> dict:
    code = (args.get("code") or "").strip()
    if not code:
        raise ValueError("code is required")
    rows = _em("RPT_F10_FN_MAINOP", code, 40)
    if not rows:
        return {"code": code, "error": "no MAINOP rows", "segments": []}
    latest_date = rows[0].get("_date")
    segs = []
    for r in rows:
        if r.get("_date") != latest_date:
            continue
        segs.append({
            "维度": {1: "按行业", 2: "按产品", 3: "按地区"}.get(
                _num(r.get("MAINOP_TYPE")), str(r.get("MAINOP_TYPE"))),
            "项目": r.get("ITEM_NAME"),
            "收入_亿": _yi(r.get("MAIN_BUSINESS_INCOME")),
            "收入占比_%": _pct_frac(r.get("MBI_RATIO")),
            "毛利率_%": _pct_frac(r.get("GROSS_RPOFIT_RATIO")),
        })

    read = []
    prod = [s for s in segs if s["维度"] == "按产品" and s["收入_亿"]]
    if prod:
        prod.sort(key=lambda s: -(s["收入_亿"] or 0))
        top = prod[0]
        read.append(
            f"最大收入来源是「{top['项目']}」{top['收入_亿']} 亿（占 {top['收入占比_%']}%），"
            f"毛利率 {top['毛利率_%']}%。")
        profitable = [s for s in prod if (s["毛利率_%"] or 0) > 0]
        if profitable:
            best = max(profitable, key=lambda s: s["毛利率_%"])
            if best["项目"] != top["项目"]:
                read.append(
                    f"真正赚钱的不是收入最大的业务：「{best['项目']}」毛利率 {best['毛利率_%']}%，"
                    f"远高于「{top['项目']}」的 {top['毛利率_%']}% —— "
                    f"公司靠高毛利业务贡献利润，低毛利业务撑规模。")

    return {
        "code": code,
        "报告期": latest_date,
        "source": "东财 datacenter RPT_F10_FN_MAINOP（主营构成）",
        "segments": segs,
        "read": read or ["（数据已返回，未触发解读规则）"],
        "caveats": [
            "MBI_RATIO / GROSS_RPOFIT_RATIO 返回的是小数（0.2 = 20%），已统一 ×100，勿再乘。",
            "主营构成只到年报/半年报粒度，季报通常不披露。",
        ],
        "must_check_next": [
            "收入下滑时：区分是「需求萎缩（全行业下滑）」还是「丢单（份额被抢）」，"
            "需对照同行同期收入增速，不能只看自己。",
            "毛利率下滑时：区分降价（竞争恶化）、成本上涨、还是产品结构变化。",
        ],
    }


# --------------------------------------------------------------------------
# tool: valuation
# --------------------------------------------------------------------------

def _ashare_valuation(args: dict) -> dict:
    code = (args.get("code") or "").strip()
    if not code:
        raise ValueError("code is required")
    prefix = "sh" if code[0] in "56" else ("sz" if code[0] in "0123" else "sh")
    url = f"https://qt.gtimg.cn/q={prefix}{code}"
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=20) as r:
        raw = r.read().decode("gbk", "ignore")
    f = raw.split("~")
    if len(f) < 46:
        raise RuntimeError(f"unexpected gtimg payload: {raw[:120]}")

    def g(i):
        try:
            return float(f[i])
        except (IndexError, ValueError):
            return None

    price = g(3)
    total_shares_yi = g(38)   # 总股本（亿股）
    float_shares_yi = g(44)   # 流通股本（亿股）
    mktcap = g(45) if len(f) > 45 else None   # 总市值（亿）
    pb = g(46) if len(f) > 46 else None

    # derive TTM net profit & revenue from statements
    ttm_np = ttm_rev = None
    try:
        inc = _em("RPT_LICO_FN_CPD", code, 8)
        def period_np(r):
            return _num(r.get("PARENT_NETPROFIT"))
        latest, prev_year = inc[0], None
        ld = latest.get("_date") or ""
        for r in inc[1:]:
            rd = r.get("_date") or ""
            if rd[:4] == str(int(ld[:4]) - 1) and rd[5:10] == ld[5:10]:
                prev_year = r
                break
        if prev_year is not None:
            # TTM = latest cumulative + (last full year - same-period last year)
            last_fy = [r for r in inc if (r.get("_date") or "").endswith("12-31")]
            if last_fy and prev_year is not None:
                fy_np = _num(last_fy[0].get("PARENT_NETPROFIT"))
                fy_rev = _num(last_fy[0].get("TOTAL_OPERATE_INCOME"))
                sp_np = period_np(prev_year)
                sp_rev = _num(prev_year.get("TOTAL_OPERATE_INCOME"))
                cur_np = period_np(latest)
                cur_rev = _num(latest.get("TOTAL_OPERATE_INCOME"))
                if None not in (fy_np, sp_np, cur_np):
                    ttm_np = round((fy_np - sp_np + cur_np) / YI, 4)
                if None not in (fy_rev, sp_rev, cur_rev):
                    ttm_rev = round((fy_rev - sp_rev + cur_rev) / YI, 4)
    except Exception:
        pass

    out = {
        "code": code,
        "名称": f[1] if len(f) > 1 else None,
        "最新价": price,
        "总股本_亿股": total_shares_yi,
        "流通股本_亿股": float_shares_yi,
        "总市值_亿": mktcap,
        "PB": pb,
        "TTM归母净利_亿": ttm_np,
        "TTM营收_亿": ttm_rev,
        "source": "腾讯行情 qt.gtimg.cn + 东财 RPT_LICO_FN_CPD（TTM 自行推算）",
    }
    if mktcap and ttm_np:
        out["PE_TTM"] = None if ttm_np == 0 else round(mktcap / ttm_np, 1)
    if mktcap and ttm_rev:
        out["PS_TTM"] = round(mktcap / ttm_rev, 2)

    read = [f"总市值 {mktcap} 亿，市净率 PB {pb}。"
            f"（PB 的意思是：你花的价格是公司账面净资产的几倍。）"]
    # 注意：`if out.get("PE_TTM")` 对负值也成立 —— 首版因此在万科（PE=-0.4）
    # 上输出"约需 -0.4 年回本"这种毫无意义的句子。必须显式判 None，并单独处理负 PE。
    pe = out.get("PE_TTM")
    if pe is not None:
        if pe < 0:
            read.append(
                f"PE(TTM) {pe} 倍 —— ★ 负值意味着公司过去 12 个月是**亏损**的。"
                "亏损时 PE 没有定义（「负几年回本」是无意义的说法），"
                "禁止用 PE 下任何结论，估值改用 PB 和 PS。")
        elif pe > 100:
            read.append(
                f"PE(TTM) {pe} 倍 —— 这个数字极高，通常不是「贵」，"
                "而是分母（净利）趋近于零导致失真；此时 PE 没有参考意义，"
                "请改用 PB 和 PS 判断。")
        else:
            read.append(f"PE(TTM) {pe} 倍，按当前盈利速度约需 {pe} 年回本。")
    if out.get("PS_TTM"):
        read.append(f"PS(TTM) {out['PS_TTM']} 倍（市值 / 年营收），"
                    "净利失真时用它做估值锚更稳。")

    out["read"] = read
    out["caveats"] = [
        "★ 东财 f162 是「动态市盈率」（最新报告期年化），不是 PE(TTM)。600416 实测 f162=561.6 而 PE(TTM)=267.6，差 2 倍。禁用 f162。",
        "净利趋近 0 时 PE 必然爆炸失真 —— 必须切到 PB / PS，不得把失真 PE 写进结论。",
        "腾讯行情是实时快照，与财报口径不同源，市值与 PB 只作粗略锚定。",
    ]
    out["must_check_next"] = [
        "PB 需与同行业对比才有意义（单看绝对值无法判断贵贱）。",
        "总股本与财报 BPS 推算的净资产可以交叉验证 PB 是否可信。",
    ]
    return out


# --------------------------------------------------------------------------
# tool: risk scan (deterministic, replaces the "check this list" prose skill)
# --------------------------------------------------------------------------

# Lesson 42 (2026-09-08): `ashare-risk-events` was written as a PROSE checklist
# ("go look for these six things"). In the v4 retest the tool-backed dimensions
# all went from 0 to non-zero, but the two checklist-only items (兴蓝风电
# receivable, 股权质押) stayed at exactly 0 — a checklist is skipped when the
# agent runs out of steps, a tool is not. So the checklist becomes a tool.
RISK_PATTERNS = {
    "破产重整": ("重整", "破产", "清算", "被执行", "失信", "预重整", "债权人"),
    "坏账计提": ("坏账", "计提", "减值", "应收账款", "信用减值", "收回"),
    "股权质押": ("质押", "冻结", "平仓", "司法冻结", "轮候"),
    "对外担保": ("担保", "连带责任", "反担保"),
    "重大诉讼": ("诉讼", "仲裁", "判决", "索赔", "开庭", "涉案", "裁定"),
    "商誉": ("商誉",),
    "政府补助": ("补助", "补贴", "非经常", "政府补助"),
}


def _ashare_risk_scan(args: dict) -> dict:
    """Deterministically sweep announcements + news for the six risk classes.

    Uses ashare_evidence.py (巨潮 official + 东财 news) so every hit carries a
    real URL. This is evidence RETRIEVAL, not judgement — the agent still has
    to read the hit and state 事实+金额+来源.
    """
    code = (args.get("code") or "").strip()
    if not code:
        raise ValueError("code is required")
    # optional narrow-down: `ashare_risk_scan --keywords 质押,担保`
    extra = [k.strip() for k in (args.get("keywords") or "").split(",") if k.strip()]
    patterns = dict(RISK_PATTERNS)
    if extra:
        patterns = {"自定义": tuple(extra)}

    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    try:
        import ashare_evidence
    except Exception as e:  # fail-loud: report, never pretend "no risk found"
        return {"code": code, "status": "UNAVAILABLE",
                "error": f"ashare_evidence import failed: {type(e).__name__}: {e}",
                "read": ["取证层不可用 —— 不得据此判断『无风险』。"]}

    pool = []
    layer_status = {}
    for fn, label in ((ashare_evidence.fetch_announcements, "公告(巨潮·一手)"),
                      (ashare_evidence.fetch_news, "新闻(东财)")):
        try:
            d = fn(code, page_size=int(args.get("page_size") or 40))
            layer_status[label] = {"status": d.get("status"), "count": d.get("count", 0),
                                   "error": d.get("error")}
            for it in d.get("items") or []:
                pool.append(dict(it, _layer=label))
        except Exception as e:
            layer_status[label] = {"status": "UNAVAILABLE", "count": 0,
                                   "error": f"{type(e).__name__}: {e}"}

    if not pool:
        return {
            "code": code, "status": "EMPTY", "layers": layer_status,
            "read": ["取证层没有返回任何条目 —— 这是**取不到数**，不是『无风险』，"
                     "禁止写成『未发现风险』。"],
            "must_check_next": ["换用 web 检索公司名（而非代码）再试一次。"],
        }

    hits: dict[str, list] = {k: [] for k in patterns}
    for it in pool:
        blob = f"{it.get('title','')} {it.get('summary','')} {it.get('type','')}"
        for cat, kws in patterns.items():
            if any(kw in blob for kw in kws):
                hits[cat].append({
                    "标题": it.get("title"),
                    "时间": it.get("time"),
                    "来源": it.get("source"),
                    "层": it.get("_layer"),
                    "url": it.get("url"),
                    "摘要": (it.get("summary") or "")[:120],
                })

    read = []
    found = {k: v for k, v in hits.items() if v}
    read.append(
        f"扫描 {len(pool)} 条（公告 {layer_status.get('公告(巨潮·一手)',{}).get('count',0)} + "
        f"新闻 {layer_status.get('新闻(东财)',{}).get('count',0)}），"
        f"命中风险类别 {len(found)}/{len(patterns)}：{'、'.join(found) if found else '无'}。")
    if not found:
        read.append("★ 零命中 = **覆盖盲区，不等于安全**。必须换关键词或用 web 补查，再下结论。")
    for cat, items in found.items():
        read.append(f"【{cat}】命中 {len(items)} 条，最新：{items[0]['标题'][:40]}")

    return {
        "code": code,
        "status": "OK",
        "layers": layer_status,
        "scanned": len(pool),
        "hits": found,
        "read": read,
        "caveats": [
            "本工具只做**检索**，不做判断：命中≠已发生损失，未命中≠无风险。",
            "关键词匹配看的是标题与摘要，**正文里的细节（如某债务人欠款 7.33 亿）可能不在其中**"
            "——命中条目必须点开原文核实金额与主体。",
            "巨潮为交易所指定披露平台（official），东财新闻为第三方（third_party），信任级别不同。",
        ],
        "must_check_next": [
            "每类命中的条目，必须点开 URL 读出：主体是谁、金额多少、当前进展。",
            "『坏账计提』命中时，与 ashare_balance 的应收账款金额交叉核对，估算未计提敞口。",
            "零命中的类别，改用公司名（非代码）做 web 检索补查，不得直接写『未发现』。",
        ],
    }


# --------------------------------------------------------------------------
# registration
# --------------------------------------------------------------------------

_DISPATCH = {
    "ashare_income": _ashare_income,
    "ashare_cashflow": _ashare_cashflow,
    "ashare_balance": _ashare_balance,
    "ashare_segments": _ashare_segments,
    "ashare_valuation": _ashare_valuation,
    "ashare_risk_scan": _ashare_risk_scan,
}

TOOLS = [
    Tool(
        name="ashare_income",
        description=(
            "A股利润表时序：营收、归母净利、扣非净利（去掉外快后主业真赚的钱）、"
            "EPS、ROE、毛利率，并自动算出非经常性损益占净利比例。"
            "回答「这家公司主业到底赚不赚钱」时用。"),
        inputSchema={
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "6位A股代码，如 600416"},
                "periods": {"type": "integer", "description": "返回期数，默认 8"},
            },
            "required": ["code"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="ashare_cashflow",
        description=(
            "A股现金流量表：经营/投资/筹资三项净现金流，并自动与净利润做背离检验。"
            "回答「赚到的利润有没有变成真金白银」时用 —— 这是识别利润质量最关键的维度。"),
        inputSchema={
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "6位A股代码"},
                "periods": {"type": "integer", "description": "返回期数，默认 8"},
            },
            "required": ["code"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="ashare_balance",
        description=(
            "A股资产负债表关键科目：货币资金、应收账款、存货、总负债、短期借款、资产负债率。"
            "回答「钱压在哪、负债重不重、会不会断链」时用。"),
        inputSchema={
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "6位A股代码"},
                "periods": {"type": "integer", "description": "返回期数，默认 8"},
            },
            "required": ["code"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="ashare_segments",
        description=(
            "A股主营构成：按产品/行业/地区的收入、占比、毛利率。"
            "回答「公司靠什么赚钱、哪块业务真赚钱、收入下滑是哪块拖累」时用。"),
        inputSchema={
            "type": "object",
            "properties": {"code": {"type": "string", "description": "6位A股代码"}},
            "required": ["code"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="ashare_risk_scan",
        description=(
            "A股重大风险事件定向扫描：自动抓取巨潮公告（一手）与东财新闻，"
            "按破产重整/坏账计提/股权质押/对外担保/重大诉讼/商誉/政府补助七类关键词过滤，"
            "每条命中都带真实 URL。回答『这家公司有没有爆雷隐患』时用。"
            "注意：零命中=覆盖盲区，不等于安全。"),
        inputSchema={
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "6位A股代码"},
                "keywords": {"type": "string",
                             "description": "可选，自定义关键词，逗号分隔（留空用默认七类）"},
                "page_size": {"type": "integer", "description": "每层抓取条数，默认 40"},
            },
            "required": ["code"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="ashare_valuation",
        description=(
            "A股估值：总市值、PB、PS、自算 PE(TTM)，内置 PE 失真保护。"
            "需要给估值定锚时用；净利趋近零时会自动提示改用 PB/PS。"),
        inputSchema={
            "type": "object",
            "properties": {"code": {"type": "string", "description": "6位A股代码"}},
            "required": ["code"],
            "additionalProperties": False,
        },
    ),
]


async def _list_tools(_ctx, _params: PaginatedRequestParams) -> ListToolsResult:
    return ListToolsResult(tools=TOOLS)


async def _call_tool(_ctx, params: CallToolRequestParams) -> CallToolResult:
    handler = _DISPATCH.get(params.name)
    if handler is None:
        return CallToolResult(
            content=[TextContent(type="text", text=json.dumps(
                {"error": f"unknown tool: {params.name}"}, ensure_ascii=False))],
            is_error=True,
        )
    try:
        result = handler(params.arguments or {})
    except Exception as e:
        return CallToolResult(
            content=[TextContent(type="text", text=json.dumps(
                {"error": f"{type(e).__name__}: {e}"}, ensure_ascii=False))],
            is_error=True,
        )
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(result, ensure_ascii=False))],
        is_error=False,
        structured_content=result,
    )


server = Server("ashare-research")
server.add_request_handler("tools/list", PaginatedRequestParams, _list_tools)
server.add_request_handler("tools/call", CallToolRequestParams, _call_tool)


async def _main() -> None:
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


# --------------------------------------------------------------------------
# Synchronous CLI mode
#
# The MCP stdio transport needs @deepseek-ai/dsh-mcp-client installed in the
# profile; when it is absent this same capability must still be reachable.
# `--cli` exposes every tool as a plain one-shot invocation so a dsh Skill
# (or any shell tool) can drive it without the MCP package.
#   python ashare_research_mcp.py --cli --code 600416 --tool ashare_cashflow
#   python ashare_research_mcp.py --cli --code 600416 --tool all
# --------------------------------------------------------------------------

def _cli() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="A-share research tools (sync CLI)")
    ap.add_argument("--code", required=True, help="6-digit A-share code")
    ap.add_argument("--tool", default="all",
                    choices=sorted(_DISPATCH) + ["all"])
    ap.add_argument("--periods", type=int, default=8)
    a = ap.parse_args()

    names = sorted(_DISPATCH) if a.tool == "all" else [a.tool]
    out: dict[str, Any] = {"code": a.code}
    failed = []
    for name in names:
        try:
            out[name] = _DISPATCH[name]({"code": a.code, "periods": a.periods})
        except Exception as e:
            failed.append(f"{name}: {type(e).__name__}: {e}")
    if failed:
        out["errors"] = failed
    print(json.dumps(out, ensure_ascii=False))
    return 1 if failed else 0


if __name__ == "__main__":
    import sys
    if "--cli" in sys.argv:
        sys.argv.remove("--cli")
        raise SystemExit(_cli())
    asyncio.run(_main())
