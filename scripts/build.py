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


def real_price_series(nominal: dict[str, float], cpi: dict[str, float],
                      base_cpi: float) -> dict[str, float]:
    """名义价格 → 实际价格（用最新 CPI 作基准，跟 Shiller 的做法一致）。

    🔴 只对**价格**做这个换算。multpl 的盈利表本身已经是通胀调整后的实际盈利
    （实测判据：与 Shiller 的 Real Earnings 列比值 1.09 —— 正好是两者基准年之差的通胀；
    与名义 Earnings 列比值 15.25，完全对不上）。对盈利再调一次＝双重调整，
    会让越早的年份分母被放大得越狠：1999-12 的 CAPE10 会从 44 掉到 20。
    CPI 未公布的最新月份沿用最后已知值（一两个月影响 <1%）。
    """
    last = cpi[max(cpi)]
    return {m: v / cpi.get(m, last) * base_cpi for m, v in nominal.items()}


def compute_cape(months: list[str], real_price: dict[str, float],
                 real_earnings: dict[str, float], years: int) -> list[list]:
    """CAPE：实际价格 ÷ 过去 N 年实际盈利均值。

    盈利定稿比股价滞后几个月。对盈利尚未公布的最新月份，**分母沿用截至最后已知盈利月的
    窗口**（分子仍用当月价格）—— 这正是 Shiller 与 multpl 的标准做法，实测与 multpl
    官方 CAPE10 偏差在 ±1% 以内。这样估值色带能跟股价一样更新到最新月份。
    """
    n = years * 12
    e_months = sorted(real_earnings)
    out: list[list] = []
    for m in months:
        if m not in real_price:
            continue
        upto = [em for em in e_months if em <= m]
        if len(upto) < n:
            continue
        window = [real_earnings[em] for em in upto[-n:]]
        avg = sum(window) / n
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
    real_price = real_price_series(price, cpi, base_cpi)
    cape5 = compute_cape(months, real_price, earnings, CAPE_YEARS)
    # 同法自算 10 年版，仅用于跟 multpl 官方 CAPE10 交叉验证口径有没有漂（见 check_data.py）
    cape10_own = compute_cape(months, real_price, earnings, 10)

    # —— 第三层：宽度代理 ——
    breadth = compute_breadth(french_snap)

    latest_cycle = paths[-1]

    # —— 历史对照：光给一个数字看不出好坏，得说清它在历史上算什么水平 ——
    def median(xs: list[float]) -> float:
        s = sorted(xs)
        n = len(s)
        return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2

    def rank_pct(value: float, pool: list[float]) -> int:
        """value 在 pool 中的百分位（0-100），用于"比历史上百分之多少的时候更…"。"""
        if not pool:
            return 0
        return round(sum(1 for v in pool if v <= value) / len(pool) * 100)

    done_bulls = [c for c in paths if c["kind"] == "bull" and c["confirmed"]]
    done_bears = [c for c in paths if c["kind"] == "bear" and c["confirmed"]]
    cape_vals = [v for _, v in cape5]
    breadth_vals = [v for _, v in breadth]

    def rank_of(value: float, pool: list[float], biggest_first: bool) -> int:
        """名次（第几）。比百分位直观：宽度当前是"第 3 窄"，而百分位会四舍五入成 0%。"""
        s = sorted(pool, reverse=biggest_first)
        return s.index(value) + 1

    # 极值旁证：把"此前的纪录"一并带上，读者能自己判断这个第一名含金量
    cape_sorted = sorted(cape5, key=lambda r: -r[1])
    breadth_sorted = sorted(breadth, key=lambda r: r[1])

    context = {
        "cape_rank_n": rank_of(cape5[-1][1], cape_vals, True) if cape5 else None,
        "cape_total": len(cape_vals),
        "cape_since": cape5[0][0] if cape5 else None,
        "cape_prev_record": cape_sorted[1] if len(cape_sorted) > 1 else None,
        "breadth_rank_n": rank_of(breadth[-1][1], breadth_vals, False) if breadth else None,
        "breadth_total": len(breadth_vals),
        "breadth_since": breadth[0][0] if breadth else None,
        "breadth_record": breadth_sorted[0] if breadth_sorted else None,
        "cycles_since": paths[0]["start"] if paths else None,
        "bull_median_pct": round(median([c["pct"] for c in done_bulls]), 1) if done_bulls else None,
        "bull_median_months": round(median([c["months"] for c in done_bulls])) if done_bulls else None,
        "bear_median_pct": round(median([c["pct"] for c in done_bears]), 1) if done_bears else None,
        "bear_median_months": round(median([c["months"] for c in done_bears])) if done_bears else None,
        "cycle_pct_rank": rank_pct(latest_cycle["pct"],
                                   [c["pct"] for c in (done_bulls if latest_cycle["kind"] == "bull"
                                                       else done_bears)]),
        "cycle_months_rank": rank_pct(latest_cycle["months"],
                                      [c["months"] for c in (done_bulls if latest_cycle["kind"] == "bull"
                                                             else done_bears)]),
        "cape_rank": rank_pct(cape5[-1][1], cape_vals) if cape5 else None,
        "breadth_rank": rank_pct(breadth[-1][1], breadth_vals) if breadth else None,
        "sample_bulls": len(done_bulls),
        "sample_bears": len(done_bears),
    }

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
        "context": context,
    }

    # 口径交叉验证：自算 CAPE10 vs multpl 官方 CAPE10，最近两年逐月比对。
    # 这是整个估值层唯一的外部校准点 —— 双重通胀调整那类 bug 只有它能抓出来。
    official10 = dict(cape10_snap["rows"])
    own10 = dict(cape10_own)
    cross = [[m, own10[m], official10[m]]
             for m in sorted(set(own10) & set(official10))[-24:]]

    out = {
        "meta": meta,
        "cycles": shown,
        "cape10_cross_check": cross,
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
