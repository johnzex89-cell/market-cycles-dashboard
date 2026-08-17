"""
抓取各数据源 → data/*.json 原始快照。

设计原则（三审结论落地）：
1. **失败不覆盖**：任何一个源抓失败，保留上一次的快照文件，只在日志里报警。
   宁可看板显示旧数据，也不要被上游的一次抽风把历史清空。
2. **只抓不算**：这里不做任何计算，算在 build.py。这样上游改版时能一眼看出
   是"抓取坏了"还是"计算坏了"。
3. **快照带元信息**：每份快照记 source_url / fetched_at / row_count，
   便于事后追溯某天的数字是从哪来的。

只用标准库，CI 上零依赖安装。
"""

from __future__ import annotations

import datetime as dt
import io
import json
import os
import re
import time
import urllib.error
import urllib.request
import zipfile
from typing import Callable

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DATA_DIR = os.path.join(ROOT, "data")
LOG_DIR = os.path.join(ROOT, "logs")

VERSION = "0.1"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36")
TIMEOUT_SEC = 45
RETRY_COUNT = 3
RETRY_BACKOFF_SEC = 5
POLITE_DELAY_SEC = 2          # 同一站点连续请求之间的间隔，别把 multpl 抓毛了

MULTPL_ROW_RE = re.compile(
    r"<td[^>]*>\s*([A-Z][a-z]{2} \d{1,2}, \d{4})\s*</td>\s*"
    r"<td[^>]*>\s*(?:&#x2002;)?\s*(-?[\d.,]+)")

# 每个 multpl 表最少该有多少行，低于这个数说明页面结构变了或被拦了
MULTPL_MIN_ROWS = 1000
FRENCH_MIN_BYTES = 100_000


def log(msg: str, kind: str = "INFO") -> None:
    """按 CLAUDE.md 的日志格式输出，同时落盘。"""
    line = f"[{dt.datetime.now().isoformat(timespec='seconds')}] | v{VERSION} | {kind} | {msg}"
    print(line, flush=True)
    os.makedirs(LOG_DIR, exist_ok=True)
    with open(os.path.join(LOG_DIR, "fetch.log"), "a", encoding="utf-8") as f:
        f.write(line + "\n")


def http_get(url: str) -> bytes:
    """带重试的 GET。瞬态失败退避重试，最终失败抛异常交给上层决定要不要保旧快照。"""
    last_err: Exception | None = None
    for attempt in range(1, RETRY_COUNT + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001 - 网络层什么都可能抛
            last_err = e
            if attempt < RETRY_COUNT:
                wait = RETRY_BACKOFF_SEC * attempt
                log(f"{url} 第 {attempt} 次失败（{type(e).__name__}），{wait}s 后重试", "WARN")
                time.sleep(wait)
    raise RuntimeError(f"{url} 重试 {RETRY_COUNT} 次仍失败: {last_err}")


def parse_multpl(html: str) -> list[list]:
    """把 multpl 的月度表解析成 [["YYYY-MM", value], ...]，按时间正序。

    multpl 表格第一行往往是"当前值"（如 Aug 14, 2026），日期不是月初；
    这里按年月归一，同一个年月以**更晚的日期**为准（即当月最新快照覆盖月初值）。
    """
    rows: dict[str, tuple[dt.date, float]] = {}
    for date_str, val_str in MULTPL_ROW_RE.findall(html):
        d = dt.datetime.strptime(date_str, "%b %d, %Y").date()
        key = f"{d.year:04d}-{d.month:02d}"
        value = float(val_str.replace(",", ""))
        if key not in rows or d > rows[key][0]:
            rows[key] = (d, value)
    return [[k, v] for k, (_, v) in sorted(rows.items())]


def snapshot(name: str, source_url: str, rows: list, extra: dict | None = None) -> None:
    """写快照文件。只有拿到合格数据才会走到这里。"""
    payload = {
        "source_url": source_url,
        "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "row_count": len(rows),
        "rows": rows,
    }
    if extra:
        payload.update(extra)
    os.makedirs(DATA_DIR, exist_ok=True)
    path = os.path.join(DATA_DIR, f"{name}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    newest = rows[-1] if rows else "空"
    log(f"{name}: {len(rows)} 行，最新 {newest} → {os.path.relpath(path, ROOT)}")


def fetch_multpl(name: str, slug: str) -> None:
    url = f"https://www.multpl.com/{slug}/table/by-month"
    html = http_get(url).decode("utf-8", "ignore")
    rows = parse_multpl(html)
    if len(rows) < MULTPL_MIN_ROWS:
        raise RuntimeError(f"{slug} 只解析出 {len(rows)} 行（阈值 {MULTPL_MIN_ROWS}），"
                           f"页面结构可能已改版")
    snapshot(name, url, rows)


def fetch_french() -> None:
    """Kenneth French 的 size 组合月度收益，取全市场等权与市值加权。

    文件结构：多个分节，每节以标题行开头（如 'Average Value Weight Returns -- Monthly'），
    后跟一行列名，再跟 YYYYMM 数据行，节与节之间空行分隔。
    我们要的是全市场的等权/市值加权 —— 用 Lo 30 / Med 40 / Hi 30 三档按其定义无法直接
    还原全市场，因此改用**十分位组合**在 build 阶段合成，这里只负责把两节原样存下来。
    """
    url = ("https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/"
           "Portfolios_Formed_on_ME_CSV.zip")
    raw = http_get(url)
    if len(raw) < FRENCH_MIN_BYTES:
        raise RuntimeError(f"French zip 只有 {len(raw)} 字节，疑似残缺")
    z = zipfile.ZipFile(io.BytesIO(raw))
    text = z.read(z.namelist()[0]).decode("utf-8", "ignore")

    sections = _split_french_sections(text)
    # 注意分节标题用词不统一：市值加权是 "Value Weight"，等权是 "Equal Weighted"（多个 ed）。
    # 所以按正则匹配而不是精确字符串，避免上游改一个词就抓瞎。
    #
    # 🔴 firms / size 两节是**正确加权的必需原料**：十分位收益不能简单平均
    # （最大一档占全市场 78% 市值却只会拿到 10% 权重 —— 那样算出来的不是市值加权，
    # 而是把规模维度抹平了，260817 实测导致宽度指标符号都是反的）。
    #   全市场等权   = Σ Nᵢ·r_ewᵢ / Σ Nᵢ
    #   全市场市值加权 = Σ (Nᵢ·Sᵢ)·r_vwᵢ / Σ (Nᵢ·Sᵢ)
    wanted = {
        "vw": re.compile(r"Average Value Weight(?:ed)? Returns\s*--\s*Monthly", re.I),
        "ew": re.compile(r"Average Equal Weight(?:ed)? Returns\s*--\s*Monthly", re.I),
        "firms": re.compile(r"Number of Firms in Portfolios", re.I),
        "size": re.compile(r"Average Firm Size", re.I),
    }
    out: dict[str, dict] = {}
    for key, pattern in wanted.items():
        matched = [t for t in sections if pattern.search(t)]
        if len(matched) != 1:
            raise RuntimeError(f"French 文件里 {key} 分节匹配到 {len(matched)} 个"
                               f"（期望 1 个），现有分节：{list(sections)}")
        header, data_rows = sections[matched[0]]
        out[key] = {"columns": header, "rows": data_rows}
        log(f"French {key}: {len(data_rows)} 个月, {data_rows[0][0]} → {data_rows[-1][0]}")

    crsp = re.search(r"created using the (\d{6}) CRSP database", text)
    snapshot("french_size_portfolios", url,
             rows=[],  # 结构不同于 multpl，实际数据放在 sections 里
             extra={"crsp_version": crsp.group(1) if crsp else None,
                    "sections": out})


def fetch_ff3() -> None:
    """Fama-French 三因子月度表，取 Mkt-RF + RF = 全市场市值加权收益。

    只作**外部校准**用：我们自己用 Number of Firms × Average Firm Size 合成的
    市值加权全市场收益，必须与它高度吻合（实测平均绝对误差 0.017pp/月）。
    这是第三层唯一的外部标尺 —— 260817 那个"十分位简单平均"的错口径，
    正是因为没有这条校准，8 类断言一次都没红过。
    """
    url = ("https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/"
           "F-F_Research_Data_Factors_CSV.zip")
    raw = http_get(url)
    z = zipfile.ZipFile(io.BytesIO(raw))
    text = z.read(z.namelist()[0]).decode("utf-8", "ignore")

    rows: list[list] = []
    for line in text.split("\n"):
        line = line.strip()
        if not re.match(r"^\d{6},", line):
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 5:
            continue
        try:
            mkt_rf, rf = float(parts[1]), float(parts[4])
        except ValueError:
            continue
        if mkt_rf <= -99 or rf <= -99:
            continue
        ym = f"{parts[0][:4]}-{parts[0][4:6]}"
        rows.append([ym, round(mkt_rf + rf, 4)])       # 市值加权全市场总收益（含无风险利率）

    if len(rows) < 900:
        raise RuntimeError(f"FF3 只解析出 {len(rows)} 个月，疑似格式变化")
    snapshot("ff3_market", url, rows)


def _split_french_sections(text: str) -> dict[str, tuple[list[str], list[list]]]:
    """把 French 的 CSV 切成 {分节标题: (列名, 数据行)}。数据行为 [YYYYMM, v1, v2, ...]。"""
    sections: dict[str, tuple[list[str], list[list]]] = {}
    current_title: str | None = None
    header: list[str] = []
    rows: list[list] = []
    for raw_line in text.split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        # 分节标题：以字母开头、整行没有数据列。注意标题**不一定含 "--"** ——
        # "Number of Firms in Portfolios" / "Average Firm Size" 就没有，
        # 早期只按 "--" 判断把这两节整个漏掉了，而它们正是正确加权所必需的原料。
        if re.match(r"^[A-Za-z]", line) and not re.match(r"^[^,]*,\s*-?[\d.]", line):
            if current_title and rows:
                sections[current_title] = (header, rows)
            current_title, header, rows = line, [], []
            continue
        if current_title and line.startswith(","):
            header = [c.strip() for c in line.split(",")]
            continue
        if current_title and re.match(r"^\d{6},", line):
            parts = [p.strip() for p in line.split(",")]
            rows.append([parts[0]] + [float(p) for p in parts[1:]])
    if current_title and rows:
        sections[current_title] = (header, rows)
    return sections


# name → 抓取函数。任何一个失败都不影响其余的继续跑。
SOURCES: list[tuple[str, Callable[[], None]]] = [
    ("标普月度价格", lambda: fetch_multpl("sp500_price", "s-p-500-historical-prices")),
    ("标普月度盈利", lambda: fetch_multpl("sp500_earnings", "s-p-500-earnings")),
    ("CPI", lambda: fetch_multpl("cpi", "cpi")),
    ("Shiller CAPE10（交叉验证用）", lambda: fetch_multpl("cape10_reference", "shiller-pe")),
    ("French 等权/市值加权 + 家数/规模", fetch_french),
    ("FF3 市场收益（宽度口径的外部校准）", fetch_ff3),
]


def main() -> int:
    log("=" * 60)
    log(f"开始抓取 {len(SOURCES)} 个数据源")
    failures: list[str] = []
    for i, (label, fn) in enumerate(SOURCES):
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            failures.append(label)
            log(f"{label} 抓取失败，保留上一次快照：{type(e).__name__}: {e}", "ERROR")
        if i < len(SOURCES) - 1:
            time.sleep(POLITE_DELAY_SEC)

    if failures:
        log(f"完成，但有 {len(failures)} 个源失败：{'、'.join(failures)}", "WARN")
    else:
        log("全部数据源抓取成功")
    # 只要还有旧快照就不算致命失败，让 build 继续跑
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
