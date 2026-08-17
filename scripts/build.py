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
    实际价格 ÷ 过去 60 个月实际盈利均值。**只对价格做通胀调整**（multpl 的盈利表本身
    已是实际值，再调一次会让 1999 年 CAPE10 从 44 变成 20 —— 260817 踩过）。
    盈利定稿比股价晚几个月，那几个月**分母沿用截至最后定稿月的窗口、分子用当月股价**
    （Shiller 与 multpl 的标准做法，实测与官方 CAPE10 偏差 ±1% 内），页面用斜纹标出。

第三层 市场宽度（代理指标，不是原图口径）
    原图是「S&P 500 成分股中过去 12 个月跑赢指数的**比例**」，需要历史成分股名单 +
    含退市公司的股价，免费拿不到（付费源 Norgate Diamond $787.5/年起）。
    这里用 Kenneth French 的**全市场等权 − 市值加权** 12 个月滚动收益差：
    低 = 少数大票带涨（窄），高 = 普涨（宽）。
    🔴 合成必须用家数 N 与平均市值 S 加权，**不能把十分位收益简单平均**
    （最大一档占 78% 市值却只拿 10% 权重）—— 260817 四审抓出这个错时，
    它让结论符号整个反了。市值加权侧已与 FF3 官方市场收益对账（平均差 0.043pp/月）。
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

    # 进行中的一段：**端点取最新月**（不是段内极值）。
    # 口径选择：卡片要回答的是"从上个转折点到现在涨了多少"，所以用最新月。
    # 但同时记下段内极值，页面可以显示"距高点还差多少" —— 四审指出这里原本
    # 代码用最新月、注释却写"取极值"，两者打架；实测 47% 的月末两者会不一致
    # （中位差 3.3pp，最大 26.7pp），所以必须写清楚是哪个口径，并把另一个也给出来。
    last = emit("bull" if rising else "bear", anchor_i, len(months) - 1, False)
    last["peak_month"] = months[ext_i]
    last["peak_pct"] = (prices[ext_i] / prices[anchor_i] - 1) * 100
    # 距极值的回撤（牛市为负数＝已从高点回落；正好在高点则为 0）
    last["from_peak_pct"] = (prices[len(months) - 1] / prices[ext_i] - 1) * 100
    segs.append(last)
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


def compute_breadth(french: dict) -> tuple[list[list], dict[str, float]]:
    """全市场等权 − 市值加权的 12 个月滚动累计收益差（百分点）。

    返回 (宽度序列, 市值加权月收益序列)，后者供 check_data.py 与 FF3 官方市场收益校准。

    🔴 十分位收益**绝不能简单平均**（260817 四审抓出的错，且导致结论方向反了）：
    最大一档占全市场约 78% 的市值却只会拿到 10% 权重，最小一档只占 0.35% 市值也拿 10%
    —— 那样算出来的不是"市值加权 vs 等权"，而是把规模维度抹平后的组内加权差。
    实测后果：2026-06 由 −7.43（"史上第 3 窄"）变成 +4.99（第 763 窄），符号相反；
    全序列 24% 的月份符号不一致；且错口径完全没反应出 2024 年初 Mag-7 独涨那段。

    正确合成（用 French 自带的家数 N 与平均市值 S）：
        全市场等权    = Σ Nᵢ·r_ewᵢ / Σ Nᵢ
        全市场市值加权 = Σ (Nᵢ·Sᵢ)·r_vwᵢ / Σ (Nᵢ·Sᵢ)
    后者实测与 FF3 官方市场收益平均绝对误差仅 0.017pp/月。
    """
    sections = french["sections"]
    for key in ("vw", "ew", "firms", "size"):
        if key not in sections:
            raise RuntimeError(f"French 快照缺少 {key} 分节，重跑 fetch_data.py")

    def decile_indices(columns: list[str]) -> list[int]:
        # 列名形如 ['', '<= 0', 'Lo 30', ..., 'Lo 10', '2-Dec', ..., '9-Dec', 'Hi 10']
        names = ["Lo 10"] + [f"{k}-Dec" for k in range(2, 10)] + ["Hi 10"]
        idx = []
        for n in names:
            if n not in columns:
                raise RuntimeError(f"French 列名里找不到 {n!r}，实际列：{columns}")
            idx.append(columns.index(n))
        return idx

    def to_month_map(part: dict) -> dict[str, list[float]]:
        cols = decile_indices(part["columns"])
        out: dict[str, list[float]] = {}
        for r in part["rows"]:
            vals = [r[c] for c in cols]
            if any(v <= -99 for v in vals):          # French 用 -99.99 表示缺失
                continue
            out[f"{r[0][:4]}-{r[0][4:6]}"] = vals
        return out

    vw_m = to_month_map(sections["vw"])
    ew_m = to_month_map(sections["ew"])
    n_m = to_month_map(sections["firms"])
    s_m = to_month_map(sections["size"])

    common = sorted(set(vw_m) & set(ew_m) & set(n_m) & set(s_m))
    if len(common) < 600:
        raise RuntimeError(f"French 四节共同月份只有 {len(common)} 个，数据疑似残缺")

    ew_mkt: dict[str, float] = {}
    vw_mkt: dict[str, float] = {}
    weights_snapshot: dict[str, dict] = {}
    for ym in common:
        n, s = n_m[ym], s_m[ym]
        cap = [ni * si for ni, si in zip(n, s)]      # 每档总市值 = 家数 × 平均市值
        tot_n, tot_cap = sum(n), sum(cap)
        if tot_n <= 0 or tot_cap <= 0:
            continue
        weights_snapshot[ym] = {
            "cap_share_top": max(cap) / tot_cap,      # 最大一档的市值占比（实测约 0.78）
            "n_share_top": max(n) / tot_n,            # 家数最多一档的家数占比（实测约 0.39）
        }
        ew_mkt[ym] = sum(ni * r for ni, r in zip(n, ew_m[ym])) / tot_n
        vw_mkt[ym] = sum(ci * r for ci, r in zip(cap, vw_m[ym])) / tot_cap

    months = sorted(set(ew_mkt) & set(vw_mkt))

    def rolling(series: dict[str, float], ms: list[str], i: int) -> float:
        """过去 12 个连续月的累计收益（复利），返回百分数。"""
        acc = 1.0
        for j in range(i - BREADTH_WINDOW_MONTHS + 1, i + 1):
            acc *= (1 + series[ms[j]] / 100)
        return (acc - 1) * 100

    def month_num(ym: str) -> int:
        return int(ym[:4]) * 12 + int(ym[5:])

    out: list[list] = []
    for i in range(BREADTH_WINDOW_MONTHS - 1, len(months)):
        # 窗口必须是 12 个**连续**日历月，否则宁可不出这个点：
        # 上游缺月时按列表位置取窗口会静默变成跨 13+ 个月，算出来照样"看着正常"。
        if month_num(months[i]) - month_num(months[i - BREADTH_WINDOW_MONTHS + 1]) \
                != BREADTH_WINDOW_MONTHS - 1:
            continue
        diff = rolling(ew_mkt, months, i) - rolling(vw_mkt, months, i)
        out.append([months[i], round(diff, 2)])
    return out, vw_mkt, weights_snapshot.get(months[-1], {})


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
    breadth, vw_market, breadth_weights = compute_breadth(french_snap)

    # 口径校准：自算的市值加权全市场收益 vs FF3 官方市场收益。
    # 第三层唯一的外部标尺 —— 没有它，260817 那个错口径 8 类断言一次都没红过。
    ff3 = to_map(load("ff3_market"))
    both = sorted(set(vw_market) & set(ff3))
    vw_cross = [[m, round(vw_market[m], 4), ff3[m]] for m in both[-36:]]
    devs = [abs(vw_market[m] - ff3[m]) for m in both]
    vw_mean_dev = sum(devs) / len(devs) if devs else None
    vw_max_dev = max(devs, default=None)

    latest_cycle = paths[-1]

    # —— 行动档位：把"跌多少该加多少"变成看得见的触发点位 ——
    # 档位阈值来自 260817 的回测：按跌幅分档远优于按月份平摊
    #（递增五档平均成本仅比最低点高 2.7%，最坏 43%；"跌10%就全买"最坏高 82%）。
    # 用**历史最高点**做基准，而不是当前周期起点 —— 加码看的是"离最高点跌了多少"。
    ath = max(prices)
    ath_month = months[prices.index(ath)]
    drawdown = (prices[-1] / ath - 1) * 100
    ACTION_TIERS = [
        (0, "常规", "当年到账的钱正常投，不留等待金"),
        (10, "第一档", "把当年剩余额度提前投出"),
        (20, "第二档", "动用次年额度的一半"),
        (30, "第三档", "动用次年全部额度"),
        (40, "第四档", "历史级机会（1974/2008 级），能调动的都上"),
    ]
    tiers = [{
        "drop_pct": t,
        "name": name,
        "action": act,
        "trigger_price": round(ath * (1 - t / 100), 1),
        "reached": -drawdown >= t,
    } for t, name, act in ACTION_TIERS]
    current_tier = max((t for t in tiers if t["reached"]), key=lambda t: t["drop_pct"])
    next_tier = next((t for t in tiers if not t["reached"]), None)

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
        "cape_cheapest": cape_sorted[-1] if cape_sorted else None,
        # 页面拿这几个标志性时点做对照，避免在前端硬编码数字
        "cape_examples": [[m, v] for m, v in cape5 if m in ("2009-03", "1982-07")],
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
            "breadth_calibration": {
                "mean_abs_dev_pp": round(vw_mean_dev, 4) if vw_mean_dev is not None else None,
                "max_abs_dev_pp": round(vw_max_dev, 4) if vw_max_dev is not None else None,
                "months": len(both),
                "note": "自算市值加权全市场收益 vs FF3 官方；最大偏差集中在 2000 年市值剧变期",
                # 权重分布快照：用来钉死"确实按市值/家数加权了"。
                # 若有人改回十分位简单平均，cap_share_top 会掉到 0.1，守卫立刻红。
                **{f"w_{k}": round(v, 4) for k, v in breadth_weights.items()},
            },
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
            "cycle_peak_month": latest_cycle.get("peak_month"),
            "cycle_from_peak_pct": (round(latest_cycle["from_peak_pct"], 2)
                                    if latest_cycle.get("from_peak_pct") is not None else None),
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
        "playbook": {
            "ath": round(ath, 1), "ath_month": ath_month,
            "drawdown_pct": round(drawdown, 2),
            "tiers": tiers,
            "current_tier": current_tier["name"],
            "next_tier": next_tier,
            # 回测依据（1960 年以来 18 次下跌 / 10 个 20 年起点），页面直接引用，不硬编码
            "evidence": {
                "ladder_median_premium_pct": 2.7,     # 递增五档：成本比最低点高（中位）
                "ladder_worst_premium_pct": 43.0,     # 最坏一次
                "allin_worst_premium_pct": 82.1,      # 跌10%就全买的最坏一次
                "lump_sum_20y": 4.06,                 # 存量一次性投入 20 年（中位倍数）
                "lump_36m_20y": 3.30,                 # 存量分 36 个月投入
                "flow_buy_now_20y": 2.44,             # 现金流到账就买
                "flow_keep50_20y": 2.12,              # 留 50% 等跌 30%（含 4% 现金利息）
                "bear_median_lag_months": 2,          # 从跌10%到见底的中位月数
                "bear_max_lag_months": 18,            # 最长（1973-74）
            },
        },
        "cycles": shown,
        "cape10_cross_check": cross,
        "vw_market_cross_check": vw_cross,
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
