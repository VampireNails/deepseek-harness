#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
ashare_evidence.py — A 股真实数据证据采集（新闻 / 公告 / 研报三层）

定位：为「风险判决书 / 描述性体检」提供**可回跳核实的证据区**。

★ 三条硬约束（改代码前先读，勿违反）

1. 【单模块，禁止拆分】东财防封节流状态（`EM_SESSION` + `_em_last_call`）是模块级单例。
   拆成 news/announce/research 三个文件 = 三份独立节流器，并发调用会突破东财
   「每秒 >5 次 / 单 IP 并发 ≥10 封 IP」红线。

2. 【真实源纪律】本模块**不调用任何 LLM**，所有文本来自真实 HTTP API；每条证据强制标注
   `source / url / time`，用户须能点回原文核实。
   教训来源：TradingAgents 直到 v0.2.5 才让 sentiment agent 读真新闻，此前是幻觉编造输入。

3. 【fail-loud】拉取/解析失败一律返回 `status=UNAVAILABLE` + `error` 原因，**绝不静默返回空列表**。
   「没拉到」与「确实没有」是两件事——静默会让判决书把「证据缺失」误读成「无利空」。

来源分级（对齐 FinGPT / FinRobot DataOps）：
    official      交易所 / 监管机构一手披露（巨潮公告）
    third_party   第三方加工（东财新闻、券商研报）

红线：只做描述与风险归因佐证，**不产买卖信号、不给目标价、不做择时**。

用法：
    python ashare_evidence.py --code 600519 --layer all --limit 5
    python ashare_evidence.py --code 600519 --layer announcement --json
    python ashare_evidence.py --code 600519 --layer all --out outputs/2026-09-06/evidence_600519.json
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

import requests

# ──────────────────────────────────────────────────────────────────────
# 东财统一防封入口（全局单例，勿在本模块外另建 session）
# ──────────────────────────────────────────────────────────────────────
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

_EM_SESSION = requests.Session()
_EM_SESSION.headers.update({"User-Agent": UA})
EM_MIN_INTERVAL = 1.0          # 两次东财请求最小间隔（秒）
_em_last_call = [0.0]          # 模块级上次请求时间戳（单例）
CNINFO_MIN_INTERVAL = 0.6      # 巨潮请求最小间隔（分页取数时必须节流）
_cninfo_last_call = [0.0]


def em_get(url: str, params: dict | None = None, headers: dict | None = None,
           timeout: int = 20, **kwargs):
    """东财系接口统一入口：串行节流 + 复用 Keep-Alive 会话 + 默认 UA。

    所有 eastmoney.com 请求（push2 / datacenter / reportapi / search / np-weblist）
    都必须走这里，否则高频会封 IP。
    """
    wait = EM_MIN_INTERVAL - (time.time() - _em_last_call[0])
    if wait > 0:
        time.sleep(wait + random.uniform(0.1, 0.5))
    try:
        return _EM_SESSION.get(url, params=params, headers=headers, timeout=timeout, **kwargs)
    finally:
        _em_last_call[0] = time.time()


def _layer(status: str, trust: str, endpoint: str, items: list,
           error: str | None = None, **extra) -> dict:
    """统一层返回结构。status: OK / EMPTY / UNAVAILABLE"""
    out = {
        "status": status,
        "source_trust": trust,
        "endpoint": endpoint,
        "count": len(items),
        "items": items,
        "error": error,
    }
    out.update(extra)
    return out


def _strip_html(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s or "").strip()


# ──────────────────────────────────────────────────────────────────────
# Layer: 新闻（third_party）—— 东财个股新闻 + 全球资讯 7×24
# ──────────────────────────────────────────────────────────────────────
def fetch_news(code: str, page_size: int = 10) -> dict:
    """东财个股新闻（search-api-web JSONP）。source_trust=third_party。"""
    endpoint = "search-api-web.eastmoney.com/search/jsonp"
    try:
        cb = "jQuery_news"
        url = "https://search-api-web.eastmoney.com/search/jsonp"
        inner = json.dumps({
            "uid": "", "keyword": str(code), "type": ["cmsArticleWebOld"],
            "client": "web", "clientType": "web", "clientVersion": "curr",
            "param": {"cmsArticleWebOld": {"searchScope": "default", "sort": "default",
                      "pageIndex": 1, "pageSize": page_size, "preTag": "", "postTag": ""}},
        }, separators=(",", ":"))
        r = em_get(url, params={"cb": cb, "param": inner},
                   headers={"Referer": "https://so.eastmoney.com/"})
        text = r.text
        d = json.loads(text[text.index("(") + 1: text.rindex(")")])
        # 东财实际返回：result.cmsArticleWebOld 直接就是文章列表（非 {list:[...]} 嵌套）
        articles = d.get("result", {}).get("cmsArticleWebOld") or []
        items = [{
            "title": _strip_html(a.get("title", "")),
            "summary": _strip_html(a.get("content", ""))[:200],
            "time": a.get("date", ""),
            "source": a.get("mediaName", "") or "东方财富",
            "url": a.get("url", ""),
            "source_trust": "third_party",
        } for a in articles]
        return _layer("OK" if items else "EMPTY", "third_party", endpoint, items)
    except Exception as e:                      # fail-loud：不静默吞异常
        return _layer("UNAVAILABLE", "third_party", endpoint, [],
                      error=f"{type(e).__name__}: {e}")


def fetch_global_news(page_size: int = 20) -> dict:
    """东财全球资讯 7×24（np-weblist，财联社下线后的替代源）。"""
    endpoint = "np-weblist.eastmoney.com/comm/web/getFastNewsList"
    try:
        url = "https://np-weblist.eastmoney.com/comm/web/getFastNewsList"
        params = {"client": "web", "biz": "web_724", "fastColumn": "102",
                  "sortEnd": "", "pageSize": str(page_size),
                  "req_trace": str(uuid.uuid4())}
        r = em_get(url, params=params, headers={"Referer": "https://kuaixun.eastmoney.com/"})
        d = r.json()
        items = [{
            "title": it.get("title", ""),
            "summary": (it.get("summary", "") or "")[:200],
            "time": it.get("showTime", ""),
            "source": "东方财富·全球资讯",
            "url": "https://kuaixun.eastmoney.com/",
            "source_trust": "third_party",
        } for it in (d.get("data", {}) or {}).get("fastNewsList", []) or []]
        return _layer("OK" if items else "EMPTY", "third_party", endpoint, items)
    except Exception as e:
        return _layer("UNAVAILABLE", "third_party", endpoint, [],
                      error=f"{type(e).__name__}: {e}")


# ──────────────────────────────────────────────────────────────────────
# Layer: 公告（official）—— 巨潮 cninfo
# ──────────────────────────────────────────────────────────────────────
_CNINFO_ORGID_CACHE: dict[str, dict] = {}


def _cninfo_orgid(code: str) -> dict:
    """从巨潮搜索接口解析**真实** orgId 与公司简称（带模块级缓存）。

    ★ 历史 bug（2026-09-06，案例 600927 永安期货）：orgId 是巨潮**内部公司 ID**，
      **不能由股票代码逆推**。原实现按 `6 开头 → gssh0{code}` 拼接：
      - 600519 恰好成立（gssh0600519，返回 1684 条）⇒ bug 长期潜伏；
      - 600927 真实值是 `gfbj0833840`，拼成 gssh0600927 ⇒ 返回 **0 条**。
      巨潮此时回 HTTP 200 + 空列表 ⇒ 被判为 `EMPTY`（"确实没有"），
      **实为参数拼错的静默失败**——正是本模块三纪律第③条要防的那一类。
      故：orgId 一律走搜索接口，禁止逆推；零结果时另有自检兜底（见 fetch_announcements）。
    """
    code = str(code)
    if code in _CNINFO_ORGID_CACHE:
        return _CNINFO_ORGID_CACHE[code]
    out = {"org_id": None, "zwjc": "", "resolved": False}
    try:
        r = requests.post(
            "https://www.cninfo.com.cn/new/information/topSearch/query",
            data={"keyWord": code, "maxNum": "10"},
            headers={"User-Agent": UA, "X-Requested-With": "XMLHttpRequest",
                     "Referer": "https://www.cninfo.com.cn/new/commonUrl?url=disclosure/list/notice",
                     "Content-Type": "application/x-www-form-urlencoded"},
            timeout=20)
        for it in (r.json() or []):
            if str(it.get("code", "")) == code:
                out["org_id"] = it.get("orgId")
                out["zwjc"] = _strip_html(it.get("zwjc", ""))
                out["resolved"] = bool(out["org_id"])
                break
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
    _CNINFO_ORGID_CACHE[code] = out
    return out


def _cninfo_query(payload: dict) -> dict:
    """巨潮公告检索统一入口（带模块级节流，防封）。

    ⚠ 分页取数（fetch_announcements_range）会在一次调用内发多个请求，
      若无限流等于「每秒 >5 次」封禁风险 ⇒ 此处统一节流，与东财同源纪律。
    """
    wait = CNINFO_MIN_INTERVAL - (time.time() - _cninfo_last_call[0])
    if wait > 0:
        time.sleep(wait + random.uniform(0.05, 0.2))
    try:
        return requests.post(
            "https://www.cninfo.com.cn/new/hisAnnouncement/query",
            data=payload,
            headers={"User-Agent": UA,
                     "Content-Type": "application/x-www-form-urlencoded",
                     "Referer": "https://www.cninfo.com.cn/new/disclosure",
                     "Origin": "https://www.cninfo.com.cn"},
            timeout=20).json() or {}
    finally:
        _cninfo_last_call[0] = time.time()


def _cninfo_items(d: dict) -> list:
    def _ts_to_date(ts):
        if isinstance(ts, (int, float)) and ts > 0:
            return datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d")
        return str(ts)[:10] if ts else ""

    items = []
    for it in d.get("announcements") or []:
        anno_id = it.get("announcementId", "")
        items.append({
            "title": _strip_html(it.get("announcementTitle", "")),
            "type": it.get("announcementTypeName") or "",   # 巨潮常返回 null
            "time": _ts_to_date(it.get("announcementTime")),
            "source": "巨潮资讯网（交易所指定披露平台）",
            "url": f"https://www.cninfo.com.cn/new/disclosure/detail?annoId={anno_id}",
            "source_trust": "official",
        })
    return items


def fetch_announcements(code: str, page_size: int = 20) -> dict:
    """巨潮公告全文检索（cninfo.com.cn）。source_trust=official（一手披露）。

    三道闸：①orgId 走搜索接口（禁逆推）②orgId 缺失时降级拼规则 + 留痕
            ③**零结果自检**——若 orgId 路径 0 条而公司名检索有结果，判为 orgId 失效，
              采用公司名结果并标注 `note`，避免把「参数错」静默报成「确实没有」。
    """
    endpoint = "www.cninfo.com.cn/new/hisAnnouncement/query"
    try:
        code = str(code)
        info = _cninfo_orgid(code)
        org_id = info.get("org_id")
        zwjc = info.get("zwjc") or ""
        fallback = False
        if not org_id:
            # 降级：搜索接口不可用时才用旧规则，并明确留痕
            fallback = True
            if code.startswith("6"):
                org_id = f"gssh0{code}"
            elif code.startswith(("8", "4")):
                org_id = f"gsbj0{code}"
            else:
                org_id = f"gssz0{code}"

        base = {"tabName": "fulltext", "pageSize": str(page_size), "pageNum": "1",
                "column": "", "category": "", "plate": "", "seDate": "",
                "searchkey": "", "secid": "", "sortName": "", "sortType": "",
                "isHLtitle": "true"}
        items = _cninfo_items(_cninfo_query({**base, "stock": f"{code},{org_id}"}))

        note = None
        if not items and zwjc:
            # 零结果自检：排除「orgId 拼错/变更」这一静默失败形态
            retry = _cninfo_items(_cninfo_query({**base, "searchkey": zwjc,
                                                 "column": "sse" if code.startswith("6") else "szse"}))
            if retry:
                items = retry
                note = (f"orgId 路径返回 0 条，已用公司名「{zwjc}」检索兜底 "
                        f"（orgId={org_id} 可能已失效）")

        extra = {"org_id": org_id, "zwjc": zwjc}
        if fallback:
            extra["org_id_fallback"] = True
        if note:
            extra["note"] = note
        return _layer("OK" if items else "EMPTY", "official", endpoint, items, **extra)
    except Exception as e:
        return _layer("UNAVAILABLE", "official", endpoint, [],
                      error=f"{type(e).__name__}: {e}")


def fetch_announcements_range(code: str, start_date: str, end_date: str | None = None,
                              max_pages: int = 3, page_size: int = 30,
                              segment: str | None = "year") -> dict:
    """按**日期区间**取巨潮公告 —— 用于回撤窗口的事件归因。

    与 `fetch_announcements` 的区别（勿混用）：
      - `fetch_announcements`       : 取「最近 N 条」，适合常规证据区
      - `fetch_announcements_range` : 取「指定窗口内」的，适合回撤归因

    ⚠ 已实测的两个硬事实（2026-09-06）：
      ① 巨潮**单页上限 30 条**（pageSize 填 100 也只返回 30）⇒ 必须分页；
      ② `seDate` 格式 `YYYY-MM-DD~YYYY-MM-DD`，实测区间过滤准确。
      分页会放大请求数 ⇒ `_cninfo_query` 已统一节流（CNINFO_MIN_INTERVAL）。

    `segment`（覆盖策略，关键）：
      - `None`  : 普通分页，按时间倒序取「最近 max_pages 页」。
                  ⚠ 对长窗口**无效**——5.6 年窗口只取到最近 90 条，等于没覆盖。
      - `"year"`: **按自然年分段**，每年取 1 页 ⇒ 覆盖铺满整个窗口（默认，推荐）。
                  实测：300413 的 5.6 年窗口 = 6 段 × 30 条，**每年都有条目**，
                  而普通分页 90 条全落在最近 1 年。请求数 ≈ 年数，可控（<10）。

    返回额外字段（**精确**覆盖报告，取代模糊的「远非全貌」）：
      `total` / `fetched` / `truncated` / `window` / `segments`
    """
    endpoint = "www.cninfo.com.cn/new/hisAnnouncement/query"
    end_date = end_date or datetime.now().strftime("%Y-%m-%d")
    try:
        code = str(code)
        info = _cninfo_orgid(code)
        org_id = info.get("org_id")
        if not org_id:                      # 与 fetch_announcements 同一降级规则
            org_id = (f"gssh0{code}" if code.startswith("6")
                      else f"gsbj0{code}" if code.startswith(("8", "4"))
                      else f"gssz0{code}")

        def _one(sd: str, ed: str, page: int = 1) -> tuple[list, int]:
            d = _cninfo_query({
                "stock": f"{code},{org_id}", "tabName": "fulltext",
                "pageSize": str(page_size), "pageNum": str(page),
                "column": "", "category": "", "plate": "",
                "seDate": f"{sd}~{ed}",
                "searchkey": "", "secid": "", "sortName": "", "sortType": "",
                "isHLtitle": "true"})
            return _cninfo_items(d), int(d.get("totalAnnouncement") or 0)

        items, total, truncated, segs = [], 0, False, []

        if segment == "year":
            y0 = int(start_date[:4]); y1 = int(end_date[:4])
            for y in range(y0, y1 + 1):
                sd = start_date if y == y0 else f"{y}-01-01"
                ed = end_date if y == y1 else f"{y}-12-31"
                if sd > ed:
                    continue
                batch, t = _one(sd, ed)
                total += t
                segs.append({"year": y, "total": t, "fetched": len(batch)})
                items.extend(batch)
            # 同年不同段可能重复（annoId 去重）
            seen, uniq = set(), []
            for it in items:
                k = it.get("url")
                if k and k not in seen:
                    seen.add(k); uniq.append(it)
            items = uniq
            items.sort(key=lambda x: x.get("time", ""), reverse=True)
            truncated = sum(s["total"] for s in segs) > len(items)
        else:
            for page in range(1, max_pages + 1):
                batch, t = _one(start_date, end_date, page)
                total = t or total
                if not batch:
                    break
                items.extend(batch)
                if len(items) >= total:
                    break
                if page == max_pages and len(items) < total:
                    truncated = True

        return _layer("OK" if items else "EMPTY", "official", endpoint, items,
                      org_id=org_id, zwjc=info.get("zwjc") or "",
                      window=[start_date, end_date], total=total,
                      fetched=len(items), truncated=truncated, segments=segs)
    except Exception as e:
        return _layer("UNAVAILABLE", "official", endpoint, [],
                      window=[start_date, end_date],
                      error=f"{type(e).__name__}: {e}")


# ──────────────────────────────────────────────────────────────────────
# Layer: 研报（third_party）—— 东财研报列表 + 同花顺一致预期
# ──────────────────────────────────────────────────────────────────────
def fetch_research(code: str, max_pages: int = 2, with_consensus: bool = True) -> dict:
    """东财研报列表（含评级与 EPS 预测）+ 同花顺一致预期。source_trust=third_party。"""
    endpoint = "reportapi.eastmoney.com/report/list"
    try:
        records = []
        for page in range(1, max_pages + 1):
            params = {
                "industryCode": "*", "pageSize": "100", "industry": "*",
                "rating": "*", "ratingChange": "*",
                "beginTime": "2000-01-01", "endTime": "2030-01-01",
                "pageNo": str(page), "fields": "", "qType": "0",
                "orgCode": "", "code": str(code), "rcode": "",
                "p": str(page), "pageNum": str(page), "pageNumber": str(page),
            }
            r = em_get("https://reportapi.eastmoney.com/report/list", params=params,
                       headers={"Referer": "https://data.eastmoney.com/"}, timeout=30)
            rows = r.json().get("data") or []
            if not rows:
                break
            records.extend(rows)
            if page >= (r.json().get("TotalPage", 1) or 1):
                break

        items = []
        for rec in records:
            info_code = rec.get("infoCode", "")
            items.append({
                "title": rec.get("title", ""),
                "time": (rec.get("publishDate") or "")[:10],
                "source": rec.get("orgSName", "") or "券商研报",
                "url": f"https://pdf.dfcfw.com/pdf/H3_{info_code}_1.pdf" if info_code else "",
                "rating": rec.get("emRatingName", ""),
                "eps_this_year": rec.get("predictThisYearEps", ""),
                "eps_next_year": rec.get("predictNextYearEps", ""),
                "industry": rec.get("indvInduName", ""),
                "source_trust": "third_party",
            })
        layer = _layer("OK" if items else "EMPTY", "third_party", endpoint, items)
        if with_consensus:
            layer["consensus"] = fetch_consensus(code)
        return layer
    except Exception as e:
        return _layer("UNAVAILABLE", "third_party", endpoint, [],
                      error=f"{type(e).__name__}: {e}")


def fetch_consensus(code: str) -> dict:
    """同花顺机构一致预期 EPS（basic.10jqka.com.cn）。需 pandas + lxml/bs4。"""
    endpoint = "basic.10jqka.com.cn/new/<code>/worth.html"
    try:
        import pandas as pd                      # noqa: PLC0415
        from io import StringIO
        r = requests.get(f"https://basic.10jqka.com.cn/new/{code}/worth.html",
                         headers={"User-Agent": UA,
                                  "Referer": "https://basic.10jqka.com.cn/"},
                         timeout=20)
        r.encoding = "gbk"
        dfs = pd.read_html(StringIO(r.text))
        target = None
        for df in dfs:
            cols = [str(c) for c in df.columns]
            if any("每股收益" in c or "均值" in c for c in cols):
                target = df
                break
        if target is None:
            return _layer("EMPTY", "third_party", endpoint, [])
        def _clean(v) -> str:
            s = "" if pd.isna(v) else str(v)
            # pandas 把整数列读成 float（年度 "2026.0"），还原显示
            return s[:-2] if re.fullmatch(r"\d+\.0", s) else s

        items = []
        for _, row in target.iterrows():
            items.append({str(k): _clean(v) for k, v in row.items()})
        # 预测机构数 < 3 的年份要谨慎（券商覆盖太少，一致预期不可靠）
        return _layer("OK" if items else "EMPTY", "third_party", endpoint, items,
                      note="「预测机构数」< 3 的年度，一致预期参考价值有限")
    except ImportError as e:
        return _layer("UNAVAILABLE", "third_party", endpoint, [],
                      error=f"缺依赖 {e}；安装：pip install lxml")
    except Exception as e:
        return _layer("UNAVAILABLE", "third_party", endpoint, [],
                      error=f"{type(e).__name__}: {e}")


# ──────────────────────────────────────────────────────────────────────
# 统一入口
# ──────────────────────────────────────────────────────────────────────
LAYERS = ("news", "announcement", "research", "global")


def collect_evidence(code: str, layers=("news", "announcement", "research"),
                     limit: int = 5, news_size: int = 10,
                     ann_size: int = 20, research_pages: int = 2,
                     window_from: str | None = None) -> dict:
    """采集指定股票的真实证据，返回结构化 dict。

    layers: news / announcement / research / global
    limit : 每层最多保留多少条（None = 全部）
    window_from: 回撤高点日期（YYYY-MM-DD）。给定时**额外**按该窗口取公告
        （按年分段，覆盖整个窗口），结果放 `window_announcements`。
        仅在深度回撤时才应传入——普通情形无需多花 6~8 次请求。
    """
    out = {
        "code": str(code),
        "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "disclaimer": "真实数据证据，仅用于描述与风险归因佐证；不构成投资建议。",
        "layers": {},
    }
    for name in layers:
        if name == "news":
            d = fetch_news(code, page_size=news_size)
        elif name == "announcement":
            d = fetch_announcements(code, page_size=ann_size)
        elif name == "research":
            d = fetch_research(code, max_pages=research_pages)
        elif name == "global":
            d = fetch_global_news(page_size=news_size)
        else:
            raise ValueError(f"unknown layer: {name} (可选: {LAYERS})")
        if limit and d.get("items"):
            d["items"] = d["items"][:limit]
            d["count"] = len(d["items"])
            d["truncated_to"] = limit
        out["layers"][name] = d

    if window_from:
        # 回撤窗口真取数（按年分段）。失败也要落进产物，不得静默省略
        # —— 否则「没拉到」会被读成「窗口内没有事件」。
        try:
            out["window_announcements"] = fetch_announcements_range(
                code, window_from, segment="year")
        except Exception as e:                      # noqa: BLE001
            out["window_announcements"] = {
                "status": "UNAVAILABLE", "items": [], "window": [window_from, ""],
                "error": f"{type(e).__name__}: {e}"}
    return out


# ──────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────
_TRUST_LABEL = {"official": "一手(official)", "third_party": "三方(third_party)"}


def _print_layer(name: str, d: dict) -> None:
    st = d.get("status")
    flag = {"OK": "✓", "EMPTY": "○", "UNAVAILABLE": "✗"}.get(st, "?")
    print(f"\n{flag} [{name}] status={st}  trust={_TRUST_LABEL.get(d.get('source_trust'), '-')}"
          f"  count={d.get('count', 0)}")
    if d.get("error"):
        print(f"   ⚠ error: {d['error']}")
    if d.get("note"):
        print(f"   ℹ {d['note']}")
    for it in d.get("items", []):
        if name == "announcement":
            print(f"   {it.get('time','')} | {it.get('type','')} | {it.get('title','')[:52]}")
        elif name == "research":
            print(f"   {it.get('time','')} | {it.get('source','')} | {it.get('rating','')}"
                  f" | {it.get('title','')[:40]}")
        else:
            print(f"   {it.get('time','')} | {it.get('source','')} | {it.get('title','')[:52]}")
        if it.get("url"):
            print(f"      └ {it['url']}")
    if name == "research" and isinstance(d.get("consensus"), dict):
        c = d["consensus"]
        print(f"   ── 一致预期 status={c.get('status')} rows={c.get('count',0)}"
              + (f"  ⚠ {c['error']}" if c.get("error") else ""))
        for row in (c.get("items") or [])[:4]:
            print(f"      {row}")


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")   # Windows 中文输出
    except Exception:
        pass

    ap = argparse.ArgumentParser(description="A 股真实数据证据采集（新闻/公告/研报）")
    ap.add_argument("--code", required=True, help="6 位股票代码，如 600519")
    ap.add_argument("--layer", default="news,announcement,research",
                    help=f"逗号分隔，可选 {LAYERS} 或 all")
    ap.add_argument("--limit", type=int, default=5, help="每层最多保留条数（0=全部）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--out", default="", help="另存 JSON 到指定路径")
    args = ap.parse_args()

    if args.layer.strip().lower() == "all":
        layers = list(LAYERS)
    else:
        layers = [x.strip() for x in args.layer.split(",") if x.strip()]
    bad = [x for x in layers if x not in LAYERS]
    if bad:
        print(f"[FAIL] 未知 layer: {bad}；可选 {LAYERS}", file=sys.stderr)
        return 2

    data = collect_evidence(args.code, layers=layers,
                            limit=(args.limit or None))

    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print(f"=== 证据采集 · {args.code} ===")
        print(f"采集时间 {data['fetched_at']}　{data['disclaimer']}")
        for name in layers:
            _print_layer(name, data["layers"][name])

    if args.out:
        p = Path(args.out)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n[saved] {p}")

    # fail-loud：任一层不可用 → 非零退出，防止流水线把「没拉到」当「没问题」
    if any(data["layers"][n].get("status") == "UNAVAILABLE" for n in layers):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
