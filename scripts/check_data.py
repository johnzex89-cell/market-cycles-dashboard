"""
上线前的数据合理性自检。任何一条不过就以非 0 退出，CI 中止、不推坏数据上线。

为什么需要它：这个看板的数据全部来自第三方网页表格（multpl）和学术数据库（French），
上游改版、改单位、临时返回半截数据都不会报错，只会让图悄悄变成错的。
守卫的价值在于「静默错误变成显式失败」。

⚠️ 每条断言都必须是「只有真出问题时才会红」的。宁可少一条，不要写会误报的守卫
——误报的守卫等于没有守卫，因为几次之后就没人看它了。
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DATA_JSON = os.path.join(ROOT, "docs", "data.json")

# —— 阈值：都留了宽裕的余量，只拦真正离谱的情况 ——
MIN_CYCLES, MAX_CYCLES = 25, 60        # 1961 至今，10% 阈值实测 37 段
MAX_PRICE_STALE_DAYS = 75              # 价格是月度数据，超过两个半月没动就是上游断了
MAX_BREADTH_STALE_DAYS = 240           # French 季度性更新，滞后半年多才算异常
CAPE_MIN, CAPE_MAX = 4.0, 70.0         # 历史极值 5.6(1932) ~ 44(1999)，留足余量
MIN_CAPE_POINTS = 600
MIN_BREADTH_POINTS = 600
MAX_LATEST_PRICE_JUMP = 0.35           # 单次更新价格跳动超过 35% ⟹ 多半是单位/口径变了
MAX_CAPE_DEVIATION = 0.03              # 自算 CAPE10 与 multpl 官方值的最大容许偏差（实测 0.9%）


def month_to_date(ym: str) -> dt.date:
    y, m = ym.split("-")
    return dt.date(int(y), int(m), 1)


def main() -> int:
    failures: list[str] = []
    warnings: list[str] = []

    def check(cond: bool, msg: str) -> None:
        if not cond:
            failures.append(msg)

    # 可传路径参数，供突变测试对着临时副本跑，不必动真文件
    target = sys.argv[1] if len(sys.argv) > 1 else DATA_JSON
    if not os.path.exists(target):
        print(f"❌ 找不到 {target}")
        return 1

    with open(target, encoding="utf-8") as f:
        d = json.load(f)

    meta, cycles = d["meta"], d["cycles"]
    latest = meta["latest"]
    today = dt.date.today()

    # 1. 周期数量：上游价格序列被截断/阈值参数被改坏，这里会红
    check(MIN_CYCLES <= len(cycles) <= MAX_CYCLES,
          f"周期段数 {len(cycles)} 不在 [{MIN_CYCLES},{MAX_CYCLES}] 内")

    # 2. 牛熊必须交替出现（算法坏掉时最典型的症状是连着两段同向）
    for a, b in zip(cycles, cycles[1:]):
        if a["kind"] == b["kind"]:
            failures.append(f"周期未交替：{a['start']} 与 {b['start']} 同为 {a['kind']}")
            break

    # 3. 每段幅度方向必须与类型一致
    for c in cycles:
        if c["kind"] == "bull" and c["pct"] < 0:
            failures.append(f"牛市段 {c['start']} 幅度为负 {c['pct']}")
            break
        if c["kind"] == "bear" and c["pct"] > 0:
            failures.append(f"熊市段 {c['start']} 幅度为正 {c['pct']}")
            break

    # 4. 数据新鲜度
    price_age = (today - month_to_date(latest["month"])).days
    check(price_age <= MAX_PRICE_STALE_DAYS,
          f"价格数据停在 {latest['month']}，已 {price_age} 天没更新")

    if latest["breadth"] is not None:
        b_age = (today - month_to_date(meta["sources"]["breadth"]["latest"])).days
        if b_age > MAX_BREADTH_STALE_DAYS:
            warnings.append(f"宽度数据停在 {meta['sources']['breadth']['latest']}，已 {b_age} 天")

    # 5. 估值区间
    if latest["cape5"] is not None:
        check(CAPE_MIN <= latest["cape5"] <= CAPE_MAX,
              f"5年CAPE {latest['cape5']} 超出合理区间 [{CAPE_MIN},{CAPE_MAX}]")
    check(len(d["cape5"]) >= MIN_CAPE_POINTS,
          f"CAPE 点数只有 {len(d['cape5'])}（应 ≥{MIN_CAPE_POINTS}）")
    check(len(d["breadth"]) >= MIN_BREADTH_POINTS,
          f"宽度点数只有 {len(d['breadth'])}（应 ≥{MIN_BREADTH_POINTS}）")

    # 6. 价格序列本身：不能有非正值，不能有相邻月暴跳（单位变化的典型症状）
    prices = [v for _, v in d["price"]]
    check(all(p > 0 for p in prices), "价格序列中存在非正值")
    for (m1, p1), (m2, p2) in zip(d["price"], d["price"][1:]):
        if p1 > 0 and abs(p2 / p1 - 1) > MAX_LATEST_PRICE_JUMP:
            failures.append(f"{m1}→{m2} 价格跳动 {(p2/p1-1)*100:.0f}%，疑似口径/单位变化")
            break

    # 7. 估值口径交叉验证：自算 CAPE10 必须贴合 multpl 官方 CAPE10。
    #    这是估值层唯一的外部校准点。260817 就是靠它发现「盈利被双重通胀调整」——
    #    那个 bug 让 1999-12 的 CAPE10 从 44 掉到 20，图上早期年份的冷热全反了，
    #    而所有内部断言（区间、点数、新鲜度）全都照样绿。
    cross = d.get("cape10_cross_check") or []
    check(len(cross) >= 12, f"CAPE 交叉验证样本只有 {len(cross)} 个月（应 ≥12）")
    if cross:
        worst_m, worst_dev = None, 0.0
        for m, own, official in cross:
            if official:
                dev = abs(own / official - 1)
                if dev > worst_dev:
                    worst_m, worst_dev = m, dev
        check(worst_dev <= MAX_CAPE_DEVIATION,
              f"自算 CAPE10 与官方值最大偏差 {worst_dev*100:.1f}%（{worst_m}），"
              f"超过 {MAX_CAPE_DEVIATION*100:.0f}% ⟹ 估值口径可能又漂了")
        print(f"  估值口径交叉验证：最大偏差 {worst_dev*100:.2f}%（{worst_m}）")

    # 8. 最新一段必须是"进行中"，其余必须是已确认（画图逻辑依赖这个前提）
    check(cycles[-1]["confirmed"] is False, "最后一段应标记为进行中")
    check(all(c["confirmed"] for c in cycles[:-1]), "存在非最后一段却未确认的周期")

    # —— 输出 ——
    print(f"自检目标：{os.path.relpath(target, ROOT)}")
    print(f"  周期 {len(cycles)} 段 | 最新 {latest['month']} 价格 {latest['price']} "
          f"（{price_age} 天前）| CAPE5 {latest['cape5']} | 宽度 {latest['breadth']}")
    for w in warnings:
        print(f"  ⚠️  {w}")
    if failures:
        print(f"\n❌ 自检未通过（{len(failures)} 条）：")
        for f_ in failures:
            print(f"   - {f_}")
        return 1
    print(f"\n✅ 自检通过（8 类断言）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
