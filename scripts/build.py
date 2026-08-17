"""
读 data/*.json 快照 → 算出三层 → 写 docs/data.json。

三层口径（三审后定死，每一条都跟原图有明确差异，页面上必须如实标注）：

第一层 牛熊周期
    原图那 18 段是 Fidelity 人工策展 + 日内极值口径（熊市里有 -15%/-14%/-10%，
    没有统一阈值），任何公开算法都还原不出来。这里改成**可复算的自有规则**：
    月度价格序列上的 ZigZag，反转幅度 ≥ REVERSAL_PCT 即确认转势，
    段幅度按峰→谷 / 谷→峰全程计。
    ⚠️ 价格是 Shiller/multpl 的**月内日均价**，会削平峰谷：
    例如 2020 年 COVID 在此口径下是 -19%，而日线口径是 -34%。这是数据源的硬约束
    （日线源 Yahoo/Stooq/FRED 在本机与 CI 上全部不可达，实测过）。

第二层 估值色带
    按原图口径用 **5 年** CAPE（不是常见的 10 年）：
    实际价格 ÷ 过去 60 个月实际盈利均值。实际值 = 名义值 / CPI × 最新 CPI。
    盈利数据比价格滞后，最后几个月没有 CAPE5，页面上留白不外推。

第三层 市场宽度（代理指标，不是原图口径）
    原图是「S&P 500 成分股中过去 12 个月跑赢指数的**比例**」，需要历史成分股名单 +
    含退市公司的股价，免费拿不到（付费源 Norgate Diamond $787.5/年起）。
    这里用 Kenneth French 的**全市场等权 − 市值加权** 12 个月滚动收益差：
    低 = 少数大票带涨（窄），高 = 普涨（宽）。
    ⚠️ 与原图差异：①是收益差（百分点）不是股票个数比例，所以**必须用独立纵轴**，
    不能沿用原图 20%–85% 那根轴；②是全市场（含小盘股）不是标普 500 内部，
    小盘股独立行情会造成"假宽度"，某些年份可能与原指标方向相反。
"""

from __future__ import annotations

import datetime as dt
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DATA_DIR = os.path.join(ROOT, "data")
DOCS_DIR = os.path.join(ROOT, "docs")

VERSION = "0.1"

# —— 口径参数（改这里等于改图的含义，页面会把这些数字显示出来）——
REVERSAL_PCT = 0.10        # 转势确认阈值：反向走够这么多才算换段
CAPE_YEARS = 5             # 原图口径是 5 年，不是常见的 10 年
CAPE_MONTHS = CAPE_YEARS * 12
BREADTH_WINDOW_MONTHS = 12  # 宽度指标的滚动窗口
CHART_START = "1961-01"     # 图表起点，对齐原图
MIN_MONTHS_FOR_CYCLES = 120  # 少于十年数据就别谈周期划分


def load(name: str) -> dict:
    path = os.path.join(DATA_DIR, f"{name}.json")
    if not os.path.exists(path):
        raise FileNotFoundError(f"缺少数据快照 {path}，先跑 fetch_data.py")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def to_map(snapshot: dict) -> dict[str, float]:
    return {ym: v for ym, v in snapshot["rows"]}


def find_cycles(months: list[str], prices: list[float], reversal: float) -> list[dict]:
    """ZigZag 分段。返回 [{kind, start, end, start_price, end_price, pct}]，
    最后一段是"进行中"（尚未确认转势）。"""
    if len(months) < MIN_MONTHS_FOR_CYCLES:
        raise ValueError(f"只有 {len(months)} 个月的数据，不足以划分周期")

    segs: list[dict] = []
    rising = True                     # 当前在找顶
    anchor_i = 0                      # 本段起点（上一个确认的极值）
    ext_i = 0                         # 本段迄今的极值

    def emit(kind: str, a: int, b: int, confirmed: bool) -> dict:
        return {
            "kind": kind,
            "start": months[a], "end": months[b],
            "start_price": prices[a], "end_price": prices[b],
            "pct": (prices[b] / prices[a] - 1) * 100,
            "months": b - a,
            "confirmed": confirmed,
        }

    for i in range(1, len(months)):
        p = prices[i]
        if rising:
            if p > prices[ext_i]:
                ext_i = i
            elif p <= prices[ext_i] * (1 - reversal):
                segs.append(emit("bull", anchor_i, ext_i, True))
                anchor_i, ext_i, rising = ext_i, i, False
        else:
            if p < prices[ext_i]:
                ext_i = i
            elif p >= prices[ext_i] * (1 + reversal):
                segs.append(emit("bear", anchor_i, ext_i, True))
                anchor_i, ext_i, rising = ext_i, i, True

    # 进行中的一段：端点取到目前的极值，但标 confirmed=False
    segs.append(emit("bull" if rising else "bear", anchor_i, len(months) - 1, False))
    return segs


def cycle_paths(months: list[str], prices: list[float], cycles: list[dict]) -> list[dict]:
    """每段周期内逐月的累计涨跌幅 —— 原图上层那种从 0 长出来的锯齿形状。"""
    idx = {m: i for i, m in enumerate(months)}
    out = []
    for c in cycles:
        a, b = idx[c["start"]], idx[c["end"]]
        base = prices[a]
        pts = [[months[i], round((prices[i] / base - 1) * 100, 2)] for i in range(a, b + 1)]
        out.append({**c, "pct": round(c["pct"], 1), "points": pts})
    return out


def real_series(nominal: dict[str, float], cpi: dict[str, float],
                base_cpi: float) -> dict[str, float]:
    """名义 → 实际（用最新 CPI 作基准，跟 Shiller 的做法一致）。"""
    return {m: v / cpi[m] * base_cpi for m, v in nominal.items() if m in cpi}


def compute_cape(months: list[str], real_price: dict[str, float],
                 real_earnings: dict[str, float]) -> list[list]:
    """5 年 CAPE：实际价格 ÷ 过去 60 个月实际盈利均值。盈利不足 60 个月的月份跳过。"""
    e_months = sorted(real_earnings)
    e_idx = {m: i for i, m in enumerate(e_months)}
    out: list[list] = []
    for m in months:
        if m not in real_price or m not in e_idx:
            continue
        i = e_idx[m]
        if i < CAPE_MONTHS - 1:
            continue
        window = [real_earnings[e_months[j]] for j in range(i - CAPE_MONTHS + 1, i + 1)]
        avg = sum(window) / CAPE_MONTHS
        if avg <= 0:
            continue          # 大萧条等盈利为负的时期，CAPE 无意义
        out.append([m, round(real_price[m] / avg, 2)])
    return out


def compute_breadth(french: dict) -> list[list]:
    """全市场等权 − 市值加权的 12 个月滚动累计收益差（百分点）。

    French 的两节都是"按市值分组的组合收益"，每节第一列是 <=0（不用），
    我们取覆盖全市场的三档 Lo 30 / Med 40 / Hi 30 按其名义权重合成过于绕，
    直接用**十分位等权平均**近似全市场：等权节取十分位的简单平均，
    市值加权节同理 —— 两节用同一套组合，差值即"等权 vs 市值加权"的口径差。
    """
    sections = french["sections"]
    vw_cols, vw_rows = sections["vw"]["columns"], sections["vw"]["rows"]
    ew_cols, ew_rows = sections["ew"]["columns"], sections["ew"]["rows"]

    def decile_indices(columns: list[str]) -> list[int]:
        # 列名形如 ['', '<= 0', 'Lo 30', ..., 'Lo 10', '2-Dec', ..., '9-Dec', 'Hi 10']
        names = ["Lo 10"] + [f"{k}-Dec" for k in range(2, 10)] + ["Hi 10"]
        idx = []
        for n in names:
            if n not in columns:
                raise RuntimeError(f"French 列名里找不到 {n!r}，实际列：{columns}")
            idx.append(columns.index(n))
        return idx

    vw_i, ew_i = decile_indices(vw_cols), decile_indices(ew_cols)

    def monthly(rows: list[list], cols: list[int]) -> dict[str, float]:
        out = {}
        for r in rows:
            vals = [r[c] for c in cols]
            if any(v <= -99 for v in vals):      # French 用 -99.99 表示缺失
                continue
            ym = f"{r[0][:4]}-{r[0][4:6]}"
            out[ym] = sum(vals) / len(vals)
        return out

    vw, ew = monthly(vw_rows, vw_i), monthly(ew_rows, ew_i)
    common = sorted(set(vw) & set(ew))

    def rolling(series: dict[str, float], months: list[str], i: int) -> float:
        """过去 12 个月的累计收益（复利），返回百分数。"""
        acc = 1.0
        for j in range(i - BREADTH_WINDOW_MONTHS + 1, i + 1):
            acc *= (1 + series[months[j]] / 100)
        return (acc - 1) * 100

    out: list[list] = []
    for i in range(BREADTH_WINDOW_MONTHS - 1, len(common)):
        diff = rolling(ew, common, i) - rolling(vw, common, i)
        out.append([common[i], round(diff, 2)])
    return out


def main() -> int:
    price_snap = load("sp500_price")
    earn_snap = load("sp500_earnings")
    cpi_snap = load("cpi")
    cape10_snap = load("cape10_reference")
    french_snap = load("french_size_portfolios")

    price = to_map(price_snap)
    earnings = to_map(earn_snap)
    cpi = to_map(cpi_snap)

    months = sorted(price)
    prices = [price[m] for m in months]

    # —— 第一层：牛熊周期 ——
    cycles = find_cycles(months, prices, REVERSAL_PCT)
    paths = cycle_paths(months, prices, cycles)
    shown = [c for c in paths if c["end"] >= CHART_START]

    # —— 第二层：5 年 CAPE ——
    base_cpi = cpi[max(cpi)]
    real_price = real_series(price, cpi, base_cpi)
    real_earn = real_series(earnings, cpi, base_cpi)
    cape5 = compute_cape(months, real_price, real_earn)

    # —— 第三层：宽度代理 ——
    breadth = compute_breadth(french_snap)

    latest_cycle = paths[-1]
    meta = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "version": VERSION,
        "params": {
            "reversal_pct": REVERSAL_PCT * 100,
            "cape_years": CAPE_YEARS,
            "breadth_window_months": BREADTH_WINDOW_MONTHS,
            "chart_start": CHART_START,
        },
        "sources": {
            "price": {"url": price_snap["source_url"], "latest": months[-1],
                      "fetched_at": price_snap["fetched_at"]},
            "earnings": {"url": earn_snap["source_url"], "latest": max(earnings)},
            "cpi": {"url": cpi_snap["source_url"], "latest": max(cpi)},
            "breadth": {"url": french_snap["source_url"],
                        "latest": breadth[-1][0] if breadth else None,
                        "crsp_version": french_snap.get("crsp_version")},
            "cape10_reference": {"url": cape10_snap["source_url"],
                                 "latest_value": cape10_snap["rows"][-1][1]},
        },
        "latest": {
            "month": months[-1],
            "price": prices[-1],
            "cycle_kind": latest_cycle["kind"],
            "cycle_start": latest_cycle["start"],
            "cycle_pct": round(latest_cycle["pct"], 1),
            "cycle_months": latest_cycle["months"],
            "cape5": cape5[-1][1] if cape5 else None,
            "cape5_month": cape5[-1][0] if cape5 else None,
            "cape10_reference": cape10_snap["rows"][-1][1],
            "breadth": breadth[-1][1] if breadth else None,
        },
    }

    out = {
        "meta": meta,
        "cycles": shown,
        "cape5": [r for r in cape5 if r[0] >= CHART_START],
        "breadth": [r for r in breadth if r[0] >= CHART_START],
        "price": [[m, price[m]] for m in months if m >= CHART_START],
    }

    os.makedirs(DOCS_DIR, exist_ok=True)
    path = os.path.join(DOCS_DIR, "data.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))

    print(f"[build v{VERSION}] 周期 {len(shown)} 段（{CHART_START} 起）| "
          f"CAPE5 {len(out['cape5'])} 点，最新 {meta['latest']['cape5_month']}="
          f"{meta['latest']['cape5']} | 宽度 {len(out['breadth'])} 点 | "
          f"当前 {meta['latest']['cycle_kind']} {meta['latest']['cycle_pct']}% "
          f"自 {meta['latest']['cycle_start']}")
    print(f"  → {os.path.relpath(path, ROOT)}  ({os.path.getsize(path)/1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
