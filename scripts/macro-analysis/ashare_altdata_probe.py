# -*- coding: utf-8 -*-
"""
方向 2 · 工序 0：另类数据可回溯性探针（读-only，不写库）
================================================================
目的：在决定落库哪些另类数据源之前，验证 5 件事（缺一不可）：
  1. 端点在本机网络环境（含代理）下的连通性 —— 记忆约束：push2his / push2
     主域曾被代理拦截，需重测；datacenter-web 是否通畅亦需实测。
  2. 长历史可回溯性 —— 不是"只有最近 N 条"。CSI800 对齐窗口 2014-01-02 起，
     拿不到 2014~2015 数据的源对横截面长历史检验无用（只能转"每日自存"）。
  3. CSI800 覆盖率 —— 抽样沪深创代表股，验证非单市场可用。
  4. 字段语义与单位 —— 元/万元、日期格式、是否含未来值（解禁）。
  5. 分页能力 —— datacenter pageSize 上限，决定全量回填的翻页策略。

候选源（复用 a-stock-data SKILL 内嵌代码 + em_get 限流）：
  S1 两融日度明细      RPTA_WEB_RZRQ_GGMX   filter SCODE    (2010 起，标的内)
  S2 全市场龙虎榜      RPT_DAILYBILLBOARD_DETAILSNEW  filter TRADE_DATE
  S3 大宗交易          RPT_DATA_BLOCKTRADE  filter SECURITY_CODE
  S4 限售解禁          RPT_LIFT_STAGE       filter SECURITY_CODE
  S5 股东户数          RPT_HOLDERNUMLATEST  filter SECURITY_CODE  ★疑点：LATEST 后缀
  S6 个股资金流120日   push2his fflow/daykline  (域名连通性 + 深度只有 120 日)
  S7 push2 个股快照    push2 stock/get       (主域连通性；板块/分钟资金流依赖它)

用法：python ashare_altdata_probe.py [--out outputs/2026-09-04/altdata_probe.json]
产物：stdout 可读矩阵 + JSON（含每源 verdict：backfill / selfstore_only / dead）。
"""
import argparse
import json
import os
import random
import sys
import time
from datetime import datetime

import requests

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
DATACENTER_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
EM_SESSION = requests.Session()
EM_SESSION.headers.update({"User-Agent": UA})
EM_MIN_INTERVAL = 1.2  # 东财防封：串行 + 间隔≥1s + 抖动


def em_get(url, params=None, headers=None, timeout=15, **kw):
    """东财统一请求入口：节流 + 会话复用（from a-stock-data SKILL）。"""
    wait = EM_MIN_INTERVAL - (time.time() - _em_last[0])
    if wait > 0:
        time.sleep(wait + random.uniform(0.1, 0.4))
    try:
        return EM_SESSION.get(url, params=params, headers=headers, timeout=timeout, **kw)
    finally:
        _em_last[0] = time.time()


_em_last = [0.0]


def dc(report_name, filter_str="", page_size=50, page_number=1,
       sort_columns="", sort_types="-1", timeout=20):
    """东财数据中心统一查询。返回 dict：{ok, rows, count, raw}。"""
    params = {
        "reportName": report_name, "columns": "ALL",
        "filter": filter_str, "pageNumber": str(page_number),
        "pageSize": str(page_size), "sortColumns": sort_columns,
        "sortTypes": sort_types, "source": "WEB", "client": "WEB",
    }
    try:
        r = em_get(DATACENTER_URL, params=params, timeout=timeout)
        if r.status_code != 200:
            return {"ok": False, "http": r.status_code}
        d = r.json()
        res = d.get("result") or {}
        return {
            "ok": bool(d.get("success")),
            "rows": res.get("data") or [],
            "count": res.get("count"),
            "msg": d.get("message", ""),
        }
    except Exception as e:  # noqa: BLE001 —— 探针要捕获一切网络异常
        return {"ok": False, "err": f"{type(e).__name__}: {e}"}


def short_date(v):
    """'YYYY-MM-DD 00:00:00' / 'YYYY-MM-DD' → 'YYYY-MM-DD'；异常原样返回。"""
    if not v:
        return ""
    s = str(v)
    return s[:10] if len(s) >= 10 else s


def money_yi(v):
    """元 → 亿（读数用）。None → ''。"""
    try:
        return round(float(v) / 1e8, 2)
    except (TypeError, ValueError):
        return None


RESULTS = []


def record(src, case, ok, detail):
    RESULTS.append({"src": src, "case": case, "ok": ok, "detail": detail})
    mark = "OK " if ok else "!! "
    print(f"[{mark}] {src} :: {case} :: {detail}")


def probe_margin(code, date_probe):
    """S1 两融：最近一页 + 2014 定点 + 大分页 + 深市/创业板抽查。"""
    r = dc("RPTA_WEB_RZRQ_GGMX", f'(SCODE="{code}")', page_size=5,
           sort_columns="DATE", sort_types="-1")
    if not r["ok"]:
        record("S1两融", f"{code} 最近", False, r.get("err") or f"http{r.get('http')} / {r.get('msg')}")
        return
    rows = r["rows"]
    if not rows:
        record("S1两融", f"{code} 最近", False, "空数据")
        return
    top = rows[0]
    record("S1两融", f"{code} 最近/总量",
           True, f"最新={short_date(top.get('DATE'))} "
                 f"融资余额={money_yi(top.get('RZYE'))}亿 融资买入={money_yi(top.get('RZMRE'))}亿 "
                 f"融券余额={money_yi(top.get('RQYE'))}亿 count={r.get('count')}")
    # 2014 定点：验证两融回溯到对齐窗口起点
    r2 = dc("RPTA_WEB_RZRQ_GGMX",
            f'(SCODE="{code}")(DATE=\'{date_probe}\')', page_size=3,
            sort_columns="DATE", sort_types="-1")
    if r2["ok"] and r2["rows"]:
        record("S1两融", f"{code} 定点{date_probe}", True,
               f"{len(r2['rows'])}条 余额={money_yi(r2['rows'][0].get('RZYE'))}亿")
    else:
        record("S1两融", f"{code} 定点{date_probe}", False,
               r2.get("err") or f"count={r2.get('count')} msg={r2.get('msg')}")


def probe_margin_pagesize(code):
    """S1b：page_size 上限测试（决定全量回填翻页策略）。"""
    r = dc("RPTA_WEB_RZRQ_GGMX", f'(SCODE="{code}")', page_size=500,
           sort_columns="DATE", sort_types="-1")
    if r["ok"]:
        record("S1两融", f"{code} page_size=500", True,
               f"返回 {len(r['rows'])} 条 count={r.get('count')}")
    else:
        record("S1两融", f"{code} page_size=500", False,
               r.get("err") or f"http{r.get('http')}")


def probe_lhb():
    """S2 龙虎榜：2015 极端日 + 2014 普通日（验证全市场历史可回溯）。"""
    for d in ["2015-07-08", "2014-01-02"]:
        r = dc("RPT_DAILYBILLBOARD_DETAILSNEW",
               f"(TRADE_DATE>='{d}')(TRADE_DATE<='{d}')", page_size=500,
               sort_columns="BILLBOARD_NET_AMT", sort_types="-1")
        if r["ok"] and r["rows"]:
            top = r["rows"][0]
            record("S2龙虎榜", d, True,
                   f"上榜 {len(r['rows'])} 只 count={r.get('count')} "
                   f"榜首={top.get('SECURITY_CODE')} {top.get('SECURITY_NAME_ABBR')} "
                   f"净买={money_yi(top.get('BILLBOARD_NET_AMT'))}亿 "
                   f"原因={str(top.get('EXPLANATION'))[:24]}")
        else:
            record("S2龙虎榜", d, False,
                   r.get("err") or f"count={r.get('count')} rows={len(r.get('rows', []))}")


def probe_block(code):
    """S3 大宗：最早可回溯日期 + 总量（升序取首页）。"""
    r = dc("RPT_DATA_BLOCKTRADE", f'(SECURITY_CODE="{code}")', page_size=5,
           sort_columns="TRADE_DATE", sort_types="1")
    if r["ok"] and r["rows"]:
        oldest = r["rows"][0]
        record("S3大宗", f"{code} 最早/总量", True,
               f"最早={short_date(oldest.get('TRADE_DATE'))} count={r.get('count')} "
               f"溢价={oldest.get('PREMIUM_RATIO')}")
    else:
        record("S3大宗", f"{code} 最早/总量", False,
               r.get("err") or f"count={r.get('count')}")


def probe_lift(code):
    """S4 解禁：历史总量 + 时间跨度 + 未来字段。"""
    r = dc("RPT_LIFT_STAGE", f'(SECURITY_CODE="{code}")', page_size=5,
           sort_columns="FREE_DATE", sort_types="1")
    if r["ok"] and r["rows"]:
        oldest = r["rows"][0]
        rec = "count=%s 最早=%s 类型=%s" % (
            r.get("count"), short_date(oldest.get("FREE_DATE")),
            str(oldest.get("LIMITED_STOCK_TYPE"))[:16])
        # 找最新（未来）一条
        r2 = dc("RPT_LIFT_STAGE", f'(SECURITY_CODE="{code}")', page_size=5,
                sort_columns="FREE_DATE", sort_types="-1")
        if r2["ok"] and r2["rows"]:
            rec += f" | 最新(含未来)={short_date(r2['rows'][0].get('FREE_DATE'))}"
        record("S4解禁", f"{code} 历史/未来", True, rec)
    else:
        record("S4解禁", f"{code} 历史/未来", False,
               r.get("err") or f"count={r.get('count')}")


def probe_holder(code):
    """S5 股东户数：★核心疑点 —— LATEST 后缀是否只给最近一期（不可回溯）。"""
    r = dc("RPT_HOLDERNUMLATEST", f'(SECURITY_CODE="{code}")', page_size=20,
           sort_columns="END_DATE", sort_types="-1")
    if r["ok"] and r["rows"]:
        periods = sorted({short_date(x.get("END_DATE")) for x in r["rows"]})
        top = r["rows"][0]
        record("S5股东户数", f"{code} 回溯期数", len(periods) > 1,
               f"独立报告期 {len(periods)} 个: {periods[:3]}... count={r.get('count')} "
               f"最新期户数={top.get('HOLDER_NUM')} 环比={top.get('HOLDER_NUM_RATIO')}")
    else:
        record("S5股东户数", f"{code} 回溯期数", False,
               r.get("err") or f"count={r.get('count')} msg={r.get('msg')}")


def probe_fflow120(code):
    """S6 push2his 资金流：域名连通性 + 深度（预期仅 120 日）。"""
    mkt = 1 if code.startswith("6") else 0
    url = "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"
    params = {"secid": f"{mkt}.{code}", "fields1": "f1,f2,f3,f7",
              "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65",
              "lmt": "120"}
    try:
        r = em_get(url, params=params,
                   headers={"Referer": "https://quote.eastmoney.com/"}, timeout=15)
        if r.status_code != 200:
            record("S6资金流120", code, False, f"http{r.status_code}")
            return
        kl = (r.json().get("data") or {}).get("klines") or []
        if kl:
            record("S6资金流120", code, True,
                   f"{len(kl)}日 最早={kl[0].split(',')[0]} 最新={kl[-1].split(',')[0]} "
                   f"样本={kl[-1][:40]}")
        else:
            record("S6资金流120", code, False, "空 klines")
    except Exception as e:  # noqa: BLE001
        record("S6资金流120", code, False, f"{type(e).__name__}: {e}")


def probe_push2_stock(code):
    """S7 push2 主域连通性：个股快照（板块资金流/分钟资金流的前置依赖）。"""
    mkt = 1 if code.startswith("6") else 0
    url = "https://push2.eastmoney.com/api/qt/stock/get"
    params = {"fltt": "2", "invt": "2",
              "fields": "f57,f58,f84,f85,f127,f116,f117,f43", "secid": f"{mkt}.{code}"}
    try:
        r = em_get(url, params=params, timeout=15)
        if r.status_code != 200:
            record("S7 push2", code, False, f"http{r.status_code}")
            return
        d = (r.json().get("data") or {})
        if d:
            record("S7 push2", code, True,
                   f"{d.get('f58')} 行业={d.get('f127')} 总市值={money_yi(d.get('f116'))}亿")
        else:
            record("S7 push2", code, False, "空 data")
    except Exception as e:  # noqa: BLE001
        record("S7 push2", code, False, f"{type(e).__name__}: {e}")


def probe_push2delay(code):
    """S7b：push2delay（记忆里唯一可用的东财域）对照。"""
    mkt = 1 if code.startswith("6") else 0
    url = "https://push2delay.eastmoney.com/api/qt/stock/get"
    params = {"fltt": "2", "invt": "2",
              "fields": "f57,f58,f116,f117", "secid": f"{mkt}.{code}"}
    try:
        r = em_get(url, params=params, timeout=15)
        if r.status_code != 200:
            record("S7b push2delay", code, False, f"http{r.status_code}")
            return
        d = (r.json().get("data") or {})
        record("S7b push2delay", code, bool(d),
               f"{d.get('f58')} 总市值={money_yi(d.get('f116'))}亿" if d else "空 data")
    except Exception as e:  # noqa: BLE001
        record("S7b push2delay", code, False, f"{type(e).__name__}: {e}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    t0 = time.time()
    print(f"=== 另类数据可回溯性探针 {datetime.now():%Y-%m-%d %H:%M:%S} ===")
    print("（东财串行限流 1.2s/请求，约 15 请求 ≈ 40s）\n")

    # —— S1 两融（沪 600519 / 深 000858 / 创 300750 抽查 + 2014 定点 + 大分页）
    probe_margin("600519", "2014-12-01")
    probe_margin("000858", "2014-12-01")
    probe_margin("300750", "2018-12-03")  # 300750 2018-06 上市，用上市后定点
    probe_margin_pagesize("600519")

    # —— S2 龙虎榜（历史极端日 + 对齐窗口普通日）
    probe_lhb()

    # —— S3 大宗 / S4 解禁 / S5 股东户数（600519 + 深市 000858 抽查）
    probe_block("600519")
    probe_block("000858")
    probe_lift("600519")
    probe_holder("600519")
    probe_holder("000858")

    # —— S6 资金流120 / S7 push2 / S7b push2delay（域名连通性）
    probe_fflow120("600519")
    probe_push2_stock("600519")
    probe_push2delay("600519")

    # —— 汇总矩阵
    elapsed = time.time() - t0
    print(f"\n=== 探针完成 {elapsed:.0f}s ===")
    fail = [r for r in RESULTS if not r["ok"]]
    print(f"通过 {len(RESULTS) - len(fail)}/{len(RESULTS)}，失败 {len(fail)}")
    for r in fail:
        print(f"  FAIL {r['src']} :: {r['case']} :: {r['detail']}")

    out_path = args.out or os.path.join(
        "D:/tmp/deepseek-harness/outputs",
        datetime.now().strftime("%Y-%m-%d"),
        "altdata_probe.json")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"run_at": datetime.now().isoformat(),
                   "elapsed_s": round(elapsed, 1),
                   "results": RESULTS}, f, ensure_ascii=False, indent=1)
    print(f"JSON → {out_path}")


if __name__ == "__main__":
    main()
