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
    wanted = {
        "vw": re.compile(r"Average Value Weight(?:ed)? Returns\s*--\s*Monthly", re.I),
        "ew": re.compile(r"Average Equal Weight(?:ed)? Returns\s*--\s*Monthly", re.I),
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
        if re.match(r"^[A-Za-z]", line) and "," not in line.split(",")[0][:40] and "--" in line:
            # 形如 "Average Value Weight Returns -- Monthly"
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
    ("French 等权/市值加权", fetch_french),
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
