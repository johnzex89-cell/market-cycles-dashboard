"""
上线前的数据合理性自检。任何一条不过就以非 0 退出，CI 中止、不推坏数据上线。

为什么需要它：这个看板的数据全部来自第三方网页表格（multpl）和学术数据库（French），
上游改版、改单位、临时返回半截数据都不会报错，只会让图悄悄变成错的。
守卫的价值在于「静默错误变成显式失败」。

⚠️ 每条断言都必须是「只有真出问题时才会红」的。宁可少一条，不要写会误报的守卫
——误报的守卫等于没有守卫，因为几次之后就没人看它了。

🔴 260817 四审的教训（这份文件被推翻重写过一次）：
原来的 8 类断言对 29 个突变只拦住 4 个，而且拦住的那一个还是靠"相邻月大跳"误打误撞。
最要命的是：把第三层换成**完全正确**的序列（数值从 −7.43 变成 +4.99、符号翻转、
名次从第 3 变第 763），它照样全绿 —— 也就是说第三层那个真 bug 从写下到被四审抓出来，
这套守卫一次都没有机会红。
⟹ 补齐三整类零覆盖的断言：**数据源新旧混用 / 序列顺序与连续性 / 字段与明细自相矛盾**，
并给第二、三层都接上**外部标尺**（CAPE10 对 multpl 官方、市值加权对 FF3 官方）。
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
CAPE_MIN, CAPE_MAX = 4.0, 70.0         # 历史极值 4.4(1920) ~ 44(1999)，留足余量
MIN_CAPE_POINTS = 600
MIN_BREADTH_POINTS = 600
MAX_PRICE_JUMP = 0.35                  # 相邻月跳动超过 35% ⟹ 多半是单位/口径变了
MAX_CAPE_DEVIATION = 0.03              # 自算 CAPE10 与 multpl 官方值的最大容许偏差（实测 0.9%）
MAX_VW_MEAN_DEV_PP = 0.15              # 自算市值加权 vs FF3 官方的平均绝对偏差（实测 0.043pp）
MAX_SOURCE_SPREAD_MONTHS = 8           # 各数据源最新月份之间的最大落差（当前实测 5）
MAX_GENERATED_AGE_DAYS = 4             # data.json 生成时间距今，超了说明更新链路挂了
MIN_TOP_DECILE_CAP_SHARE = 0.40        # 最大一档的市值占比（实测 0.78；简单平均会退化成 0.10）


def month_num(ym: str) -> int:
    return int(ym[:4]) * 12 + int(ym[5:7])


def month_to_date(ym: str) -> dt.date:
    return dt.date(int(ym[:4]), int(ym[5:7]), 1)


def check_series(name: str, rows: list, failures: list[str]) -> None:
    """一条 [[月份, 值], ...] 序列的通用体检：唯一、正序、连续、值为数字。

    这三样以前一条都没有 —— 重复月份、整条打乱、尾部逆序在旧版里全部漏过。
    """
    if not rows:
        failures.append(f"{name}: 序列为空")
        return
    months = [r[0] for r in rows]
    if len(set(months)) != len(months):
        dup = [m for m in set(months) if months.count(m) > 1][:3]
        failures.append(f"{name}: 存在重复月份 {dup}")
    nums = [month_num(m) for m in months]
    if nums != sorted(nums):
        bad = next((months[i] for i in range(1, len(nums)) if nums[i] <= nums[i - 1]), "?")
        failures.append(f"{name}: 月份未按时间正序（首个乱序处 {bad}）")
    gaps = [(months[i - 1], months[i]) for i in range(1, len(nums)) if nums[i] - nums[i - 1] != 1]
    if gaps:
        failures.append(f"{name}: 月份不连续，共 {len(gaps)} 处断裂，例如 {gaps[:2]}")
    if any(not isinstance(r[1], (int, float)) for r in rows):
        failures.append(f"{name}: 存在非数值")


def main() -> int:
    failures: list[str] = []
    warnings: list[str] = []

    def check(cond: bool, msg: str) -> None:
        if not cond:
            failures.append(msg)

    target = sys.argv[1] if len(sys.argv) > 1 else DATA_JSON
    if not os.path.exists(target):
        print(f"❌ 找不到 {target}")
        return 1

    with open(target, encoding="utf-8") as f:
        d = json.load(f)

    meta, cycles = d["meta"], d["cycles"]
    latest, sources = meta["latest"], meta["sources"]
    today = dt.date.today()

    # ========== 1. 周期结构 ==========
    check(MIN_CYCLES <= len(cycles) <= MAX_CYCLES,
          f"周期段数 {len(cycles)} 不在 [{MIN_CYCLES},{MAX_CYCLES}] 内")

    for a, b in zip(cycles, cycles[1:]):
        if a["kind"] == b["kind"]:
            failures.append(f"周期未交替：{a['start']} 与 {b['start']} 同为 {a['kind']}")
            break

    for c in cycles:
        if c["kind"] == "bull" and c["pct"] < 0:
            failures.append(f"牛市段 {c['start']} 幅度为负 {c['pct']}")
            break
        if c["kind"] == "bear" and c["pct"] > 0:
            failures.append(f"熊市段 {c['start']} 幅度为正 {c['pct']}")
            break

    # ========== 2. 周期字段必须能由 points 反推（旧版这一整类零覆盖）==========
    # 旧版把 pct 改成 99.9、把两段的 points 互换、把 start/end 改成无关月份，全都漏过。
    for c in cycles:
        pts = c.get("points") or []
        if len(pts) < 2:
            failures.append(f"周期 {c['start']} 的 points 少于 2 个点")
            break
        if pts[0][0] != c["start"] or pts[-1][0] != c["end"]:
            failures.append(f"周期 {c['start']}→{c['end']} 与 points 首尾"
                            f"（{pts[0][0]}→{pts[-1][0]}）不一致")
            break
        if abs(pts[-1][1] - c["pct"]) > 0.15:
            failures.append(f"周期 {c['start']} 的 pct={c['pct']} 与 points 末值 {pts[-1][1]} 不符")
            break
        if abs(pts[0][1]) > 0.01:
            failures.append(f"周期 {c['start']} 的 points 未从 0 起（{pts[0][1]}）")
            break
        if c["months"] != len(pts) - 1:
            failures.append(f"周期 {c['start']} 的 months={c['months']} 与 points 数 {len(pts)} 不符")
            break
        if (c["kind"] == "bull") != (pts[-1][1] >= 0):
            failures.append(f"周期 {c['start']} 的方向与 points 走势相反")
            break

    check(cycles[-1]["confirmed"] is False, "最后一段应标记为进行中")
    check(all(c["confirmed"] for c in cycles[:-1]), "存在非最后一段却未确认的周期")

    # ========== 3. 各序列的顺序 / 唯一 / 连续 ==========
    check_series("price", d["price"], failures)
    check_series("cape5", d["cape5"], failures)
    check_series("breadth", d["breadth"], failures)

    # ========== 4. meta.latest 必须等于各序列尾值（旧版全漏）==========
    if d["price"]:
        check(latest["month"] == d["price"][-1][0],
              f"meta.latest.month={latest['month']} 与 price 尾月 {d['price'][-1][0]} 不一致")
        check(abs(latest["price"] - d["price"][-1][1]) < 1e-6,
              f"meta.latest.price={latest['price']} 与 price 尾值 {d['price'][-1][1]} 不一致")
    if d["cape5"]:
        check(latest["cape5_month"] == d["cape5"][-1][0],
              f"meta.latest.cape5_month 与 cape5 尾月不一致")
        check(abs((latest["cape5"] or 0) - d["cape5"][-1][1]) < 1e-6,
              f"meta.latest.cape5 与 cape5 尾值不一致")
    if d["breadth"] and latest.get("breadth") is not None:
        check(abs(latest["breadth"] - d["breadth"][-1][1]) < 1e-6,
              f"meta.latest.breadth={latest['breadth']} 与 breadth 尾值 {d['breadth'][-1][1]} 不一致")
    last_cycle = cycles[-1]
    check(latest["cycle_kind"] == last_cycle["kind"]
          and latest["cycle_start"] == last_cycle["start"]
          and abs(latest["cycle_pct"] - last_cycle["pct"]) < 0.15,
          "meta.latest 的周期信息与最后一段 cycles 不一致")

    # ========== 5. 数据新鲜度 + 各源新旧混用（旧版全漏）==========
    price_age = (today - month_to_date(latest["month"])).days
    check(price_age <= MAX_PRICE_STALE_DAYS,
          f"价格数据停在 {latest['month']}，已 {price_age} 天没更新")

    gen = meta.get("generated_at", "")
    try:
        gen_dt = dt.datetime.fromisoformat(gen.replace("Z", "+00:00"))
        gen_age = (dt.datetime.now(dt.timezone.utc) - gen_dt).days
        check(gen_age <= MAX_GENERATED_AGE_DAYS,
              f"data.json 生成于 {gen_age} 天前，更新链路可能已中断")
    except Exception:  # noqa: BLE001
        failures.append(f"generated_at 无法解析：{gen!r}")

    # 各数据源的最新月份不能拉开太远 —— 否则就是"新价格 + 一年前的 CPI"这种混用
    src_latest = {k: v.get("latest") for k, v in sources.items()
                  if isinstance(v, dict) and v.get("latest")}
    if len(src_latest) >= 2:
        nums = {k: month_num(v) for k, v in src_latest.items()}
        newest_k = max(nums, key=nums.get)
        oldest_k = min(nums, key=nums.get)
        spread = nums[newest_k] - nums[oldest_k]
        check(spread <= MAX_SOURCE_SPREAD_MONTHS,
              f"各数据源新旧落差 {spread} 个月（{oldest_k}={src_latest[oldest_k]} vs "
              f"{newest_k}={src_latest[newest_k]}），超过 {MAX_SOURCE_SPREAD_MONTHS} ⟹ 疑似新旧混用")

    b_latest = sources.get("breadth", {}).get("latest")
    if b_latest:
        b_age = (today - month_to_date(b_latest)).days
        if b_age > MAX_BREADTH_STALE_DAYS:
            warnings.append(f"宽度数据停在 {b_latest}，已 {b_age} 天")

    # ========== 6. 数值区间 ==========
    if latest["cape5"] is not None:
        check(CAPE_MIN <= latest["cape5"] <= CAPE_MAX,
              f"5年CAPE {latest['cape5']} 超出合理区间 [{CAPE_MIN},{CAPE_MAX}]")
    check(len(d["cape5"]) >= MIN_CAPE_POINTS,
          f"CAPE 点数只有 {len(d['cape5'])}（应 ≥{MIN_CAPE_POINTS}）")
    check(len(d["breadth"]) >= MIN_BREADTH_POINTS,
          f"宽度点数只有 {len(d['breadth'])}（应 ≥{MIN_BREADTH_POINTS}）")

    prices = [v for _, v in d["price"]]
    check(all(p > 0 for p in prices), "价格序列中存在非正值")
    for (m1, p1), (m2, p2) in zip(d["price"], d["price"][1:]):
        if p1 > 0 and abs(p2 / p1 - 1) > MAX_PRICE_JUMP:
            failures.append(f"{m1}→{m2} 价格跳动 {(p2/p1-1)*100:.0f}%，疑似口径/单位变化")
            break

    # ========== 7. 估值口径的外部标尺：自算 CAPE10 vs multpl 官方 ==========
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
        print(f"  估值口径校准：最大偏差 {worst_dev*100:.2f}%（{worst_m}）")

    # ========== 8. 宽度口径的外部标尺：自算市值加权 vs FF3 官方市场收益 ==========
    # 这条是 260817 四审后补的。第三层此前完全没有外部校准，
    # 于是"十分位简单平均"那个错口径（结论符号都反了）在所有断言下全绿。
    cal = sources.get("breadth_calibration") or {}
    vw_cross = d.get("vw_market_cross_check") or []
    check(bool(cal) and cal.get("mean_abs_dev_pp") is not None,
          "缺少宽度口径校准结果（breadth_calibration）")
    if cal.get("mean_abs_dev_pp") is not None:
        check(cal["mean_abs_dev_pp"] <= MAX_VW_MEAN_DEV_PP,
              f"自算市值加权与 FF3 官方市场收益平均偏差 {cal['mean_abs_dev_pp']:.3f}pp，"
              f"超过 {MAX_VW_MEAN_DEV_PP}pp ⟹ 宽度口径可能算错了")
        print(f"  宽度口径校准：平均偏差 {cal['mean_abs_dev_pp']:.3f}pp（{cal.get('months')} 个月）")
    # 权重必须真的用上了：最大一档占全市场约 78% 市值。
    # 若有人把十分位收益改回简单平均，这里会变成 0.1，立刻报红 —— 直接钉死 260817 那个 bug。
    if cal.get("w_cap_share_top") is not None:
        check(cal["w_cap_share_top"] >= MIN_TOP_DECILE_CAP_SHARE,
              f"最大一档市值占比只有 {cal['w_cap_share_top']:.3f}（应 ≥{MIN_TOP_DECILE_CAP_SHARE}）"
              f" ⟹ 宽度合成疑似退化成十分位简单平均")
    else:
        failures.append("缺少宽度权重快照（w_cap_share_top），无法确认是否真按市值加权")

    if vw_cross:
        bad = [m for m, own, off in vw_cross if abs(own - off) > 1.0]
        if bad:
            warnings.append(f"近月市值加权与 FF3 偏差 >1pp 的月份：{bad[:3]}")

    # ========== 9. 投资档位：触发点位必须与历史高点自洽 ==========
    # 这块是直接给人看"跌到多少动手"的，算错了不会有任何外部信号，只能自己钉。
    pb = d.get("playbook")
    if pb:
        ath = pb.get("ath")
        check(isinstance(ath, (int, float)) and ath > 0, "playbook.ath 缺失或非正数")
        if ath:
            check(abs(ath - max(v for _, v in d["price"])) < 1e-6,
                  f"playbook.ath={ath} 与价格序列的最高点不一致")
            for t in pb.get("tiers", []):
                want = ath * (1 - t["drop_pct"] / 100)
                if abs(t["trigger_price"] - want) > 0.5:
                    failures.append(f"档位 {t['name']} 触发价 {t['trigger_price']} 与 "
                                    f"ath×(1−{t['drop_pct']}%)={want:.1f} 不符")
                    break
                reached = -pb["drawdown_pct"] >= t["drop_pct"]
                if reached != t["reached"]:
                    failures.append(f"档位 {t['name']} 的 reached 标记与当前回撤不符")
                    break
            check(abs(pb["drawdown_pct"] - (latest["price"] / ath - 1) * 100) < 0.05,
                  "playbook.drawdown_pct 与最新价/最高点算出来的不一致")

    # —— 输出 ——
    print(f"自检目标：{os.path.relpath(target, ROOT)}")
    print(f"  周期 {len(cycles)} 段 | 最新 {latest['month']} 价格 {latest['price']} "
          f"（{price_age} 天前）| CAPE5 {latest['cape5']} | 宽度 {latest['breadth']}")
    if src_latest:
        print("  各源最新月：" + "、".join(f"{k}={v}" for k, v in sorted(src_latest.items())))
    for w in warnings:
        print(f"  ⚠️  {w}")
    if failures:
        print(f"\n❌ 自检未通过（{len(failures)} 条）：")
        for f_ in failures:
            print(f"   - {f_}")
        return 1
    print("\n✅ 自检通过（9 组断言）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
