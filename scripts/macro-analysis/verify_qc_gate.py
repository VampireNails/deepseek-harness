# -*- coding: utf-8 -*-
"""QC 闸门回归验证（2026-09-03 坑⑰ 修复后）"""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
# 层级：macro-analysis -> scripts -> deepseek-harness -> my-deepseek-harness -> 工作区根
# ⚠️ parents[3] 会落在 my-deepseek-harness（SOP 已记载的类坑）
ROOT = Path(__file__).resolve().parents[4]
assert (ROOT / "outputs").is_dir(), f"ROOT 解析错误: {ROOT}"

from ashare_hfq_access import qc_bad_codes, QcMissing, load_hfq_panel

print("=" * 68)
print("T1  语法/导入：三个改过的脚本能否正常 import")
print("=" * 68)
import importlib
for m in ("ashare_hfq_access", "ashare_agri_validate",
          "ashare_agri_backtest", "ashare_fund_backtest"):
    try:
        importlib.import_module(m)
        print(f"  ✓ {m}")
    except Exception as e:
        print(f"  ✗ {m}: {type(e).__name__}: {e}")

print()
print("=" * 68)
print("T2  fail-loud：无质检表的库必须报错，不得静默放行")
print("=" * 68)
import tempfile
tmp = Path(tempfile.mkdtemp()) / "noqc.sqlite"
c = sqlite3.connect(str(tmp))
c.execute("CREATE TABLE daily_quotes_hfq(code TEXT, trade_date TEXT, close REAL)")
c.commit(); c.close()
try:
    qc_bad_codes(tmp)
    print("  ✗ 缺表时未报错 —— 静默放行，坑⑰ 未修复")
except QcMissing as e:
    print(f"  ✓ 抛 QcMissing（fail-loud）")
except Exception as e:
    print(f"  ✗ 抛了非预期异常 {type(e).__name__}: {e}")

print()
print("=" * 68)
print("T3  正常库：能正确读出坏代码集合")
print("=" * 68)
for f in ("ashare_agri_hfq_xq.sqlite", "ashare_semi_hfq_xq.sqlite",
          "ashare_csi800_hfq_xq.sqlite"):
    p = ROOT / "outputs" / f
    if not p.exists():
        print(f"  - {f} 不存在，跳过")
        continue
    try:
        bad = qc_bad_codes(p)
        tot = sqlite3.connect(str(p)).execute(
            "SELECT COUNT(*) FROM hfq_qc").fetchone()[0]
        print(f"  ✓ {f}: QC {tot} 条，坏 {len(bad)} 只 {sorted(bad)[:6]}")
    except Exception as e:
        print(f"  ✗ {f}: {e}")

print()
print("=" * 68)
print("T4  统一访问层 load_hfq_panel：剔除留痕 + 矩阵不含坏股")
print("=" * 68)
p = ROOT / "outputs" / "ashare_agri_hfq_xq.sqlite"
pan = load_hfq_panel(p, since="2023-01-01")
bad = qc_bad_codes(p)
overlap = set(pan.codes) & bad
print(f"  面板 {pan.close.shape[0]} 只 × {pan.close.shape[1]} 日")
print(f"  n_bad={pan.n_bad}  bad_codes={pan.bad_codes}")
print(f"  坏股是否出现在返回面板中: {sorted(overlap) if overlap else '否 ✓'}")
ext = pan.close[:, 1:] / pan.close[:, :-1] - 1.0
import numpy as np
print(f"  面板最大单日收益绝对值: {np.nanmax(np.abs(ext))*100:.2f}%")

print()
print("=" * 68)
print("T5  新股前 N 日剔除（688615 上市第3日 +96% 属市场事实，非数据错误）")
print("=" * 68)
p2 = ROOT / "outputs" / "ashare_csi800_hfq_xq.sqlite"
a = load_hfq_panel(p2, since="2024-01-01", codes=["688615"])
b = load_hfq_panel(p2, since="2024-01-01", codes=["688615"], drop_first_n_days=5)
def mx(pan):
    r = pan.close[:, 1:] / pan.close[:, :-1] - 1.0
    return float(np.nanmax(np.abs(r)) * 100) if pan.close.shape[1] > 1 else 0.0
print(f"  不剔除前 N 日: 最大单日 {mx(a):.1f}%")
print(f"  剔除前 5 日  : 最大单日 {mx(b):.1f}%")

print()
print("=" * 68)
print("T6  废弃源黑名单 fail-loud：5 个腾讯库必须被拦截，5 个雪球库必须放行")
print("=" * 68)
from ashare_hfq_access import DEPRECATED_SOURCES, DeprecatedSource  # noqa: E402
ok_all = True
for name in sorted(DEPRECATED_SOURCES):
    p = ROOT / "outputs" / name
    if not p.exists():
        print(f"  - {name}: 文件已不存在（跳过）"); continue
    try:
        qc_bad_codes(p)
        print(f"  ✗ {name}: 未被拦截（黑名单失效！）"); ok_all = False
    except DeprecatedSource:
        print(f"  ✓ {name}: 已拦截")
    except Exception as e:
        print(f"  ✗ {name}: 抛了非预期异常 {type(e).__name__}: {e}"); ok_all = False
NEW_DBS = ["ashare_csi800_hfq_xq.sqlite", "ashare_agri_hfq_xq.sqlite",
           "ashare_semi_hfq_xq.sqlite", "ashare_single_hfq_xq.sqlite",
           "ashare_wide_hfq_xq.sqlite"]
for name in NEW_DBS:
    p = ROOT / "outputs" / name
    if not p.exists():
        print(f"  ✗ {name}: 缺失"); ok_all = False; continue
    b = qc_bad_codes(p)
    import sqlite3 as _sq
    n = _sq.connect(p).execute("SELECT COUNT(DISTINCT code) FROM hfq_qc").fetchone()[0]
    print(f"  ✓ {name}: 放行，QC {n} 只、判坏 {len(b)} 只 "
          f"({len(b)/n:.2%}){(' ' + str(b)) if b else ''}")
print(f"\n  T6 总体: {'✓ PASS' if ok_all else '✗ FAIL'}")
