"""
数据源可达性探测 —— 只在 CI 环境跑一次，用来定死每一层用哪个源。
本机（中国网络）Yahoo 429 / Stooq Access denied / FRED 连不上，所以必须在
GitHub Actions 的美国 IP 上实测，不能靠猜。

输出到 stdout，CI 日志里直接看结果。不写任何数据文件。
"""

import json
import sys
import urllib.error
import urllib.request

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36")
TIMEOUT = 45


def fetch(url, label):
    """返回 (ok, 状态描述, 正文bytes)。任何异常都吞掉转成失败描述，探测不中断。"""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            body = r.read()
            return True, f"HTTP {r.status}, {len(body)} 字节", body
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}", b""
    except Exception as e:  # 网络层各种失败
        return False, f"{type(e).__name__}: {e}", b""


def probe_yahoo_daily():
    """Yahoo ^GSPC 全历史日线 —— 首选源，覆盖 1927 至今。"""
    url = ("https://query1.finance.yahoo.com/v8/finance/chart/%5EGSPC"
           "?period1=-2208988800&period2=9999999999&interval=1d")
    ok, desc, body = fetch(url, "yahoo")
    if not ok:
        return ok, desc
    try:
        d = json.loads(body)
        r = d["chart"]["result"][0]
        ts, close = r["timestamp"], r["indicators"]["quote"][0]["close"]
        import datetime as dt
        first = dt.datetime.utcfromtimestamp(ts[0]).date()
        last = dt.datetime.utcfromtimestamp(ts[-1]).date()
        return True, f"{desc} | {len(ts)} 个交易日 | {first} → {last} | 末值 {close[-1]:.2f}"
    except Exception as e:
        return False, f"{desc} 但解析失败: {type(e).__name__}: {e}"


def probe_stooq_daily():
    """Stooq ^spx 日线 CSV —— 备用源。本机被 Access denied，看 CI 上如何。"""
    ok, desc, body = fetch("https://stooq.com/q/d/l/?s=%5Espx&i=d", "stooq")
    if not ok:
        return ok, desc
    text = body.decode("utf-8", "ignore")
    if "Access denied" in text or "<html" in text[:200].lower():
        return False, f"{desc} 但正文是拒绝页/挑战页: {text[:60]!r}"
    lines = text.strip().split("\n")
    return True, f"{desc} | {len(lines)} 行 | 首 {lines[1][:30]!r} | 末 {lines[-1][:30]!r}"


def probe_fred():
    """FRED SP500 日线 —— 注意：因授权限制只有近 10 年，只能当日更补丁不能当历史源。"""
    ok, desc, body = fetch("https://fred.stlouisfed.org/graph/fredgraph.csv?id=SP500", "fred")
    if not ok:
        return ok, desc
    lines = body.decode("utf-8", "ignore").strip().split("\n")
    return True, f"{desc} | {len(lines)} 行 | 首 {lines[1][:30]!r} | 末 {lines[-1][:30]!r}"


def probe_multpl(slug):
    """multpl 的月度表 —— 估值层与盈利数据源。"""
    ok, desc, body = fetch(f"https://www.multpl.com/{slug}/table/by-month", slug)
    if not ok:
        return ok, desc
    import re
    text = body.decode("utf-8", "ignore")
    rows = re.findall(
        r"<td[^>]*>\s*([A-Z][a-z]{2} \d{1,2}, \d{4})\s*</td>\s*<td[^>]*>\s*(?:&#x2002;)?\s*([\d.,-]+)",
        text)
    if not rows:
        return False, f"{desc} 但表格解析出 0 行（页面结构可能改了）"
    return True, f"{desc} | {len(rows)} 行 | 最新 {rows[0][0]} = {rows[0][1]}"


def probe_french():
    """Kenneth French 的等权/市值加权组合收益 —— 广度代理层的源。"""
    url = ("https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/"
           "Portfolios_Formed_on_ME_CSV.zip")
    ok, desc, body = fetch(url, "french")
    if not ok:
        return ok, desc
    import io
    import zipfile
    try:
        z = zipfile.ZipFile(io.BytesIO(body))
        name = z.namelist()[0]
        head = z.read(name)[:200].decode("utf-8", "ignore").replace("\r\n", " ")
        return True, f"{desc} | {name} | 头部: {head[:110]!r}"
    except Exception as e:
        return False, f"{desc} 但解压失败: {type(e).__name__}: {e}"


def probe_datahub():
    """datahub 的 Shiller 月度数据 —— 历史价格与实际盈利的源（注意 2023 后列不全）。"""
    url = "https://raw.githubusercontent.com/datasets/s-and-p-500/main/data/data.csv"
    ok, desc, body = fetch(url, "datahub")
    if not ok:
        return ok, desc
    lines = body.decode("utf-8", "ignore").strip().split("\n")
    return True, f"{desc} | {len(lines)} 行 | 末 {lines[-1][:40]!r}"


PROBES = [
    ("Yahoo ^GSPC 日线（首选历史源）", probe_yahoo_daily),
    ("Stooq ^spx 日线（备用历史源）", probe_stooq_daily),
    ("FRED SP500 日线（只有近10年，当日更补丁）", probe_fred),
    ("multpl Shiller CAPE（估值交叉验证）", lambda: probe_multpl("shiller-pe")),
    ("multpl 标普盈利（补最新盈利）", lambda: probe_multpl("s-p-500-earnings")),
    ("Kenneth French（广度代理源）", probe_french),
    ("datahub Shiller 月度（历史价格/实际盈利）", probe_datahub),
]


def main():
    print("=" * 70)
    print("数据源可达性探测 —— 运行环境:", sys.platform)
    print("=" * 70)
    results = []
    for label, fn in PROBES:
        ok, desc = fn()
        mark = "✅" if ok else "❌"
        print(f"\n{mark} {label}\n   {desc}")
        results.append((label, ok))
    print("\n" + "=" * 70)
    good = sum(1 for _, ok in results if ok)
    print(f"汇总: {good}/{len(results)} 个源可达")
    for label, ok in results:
        print(f"  {'✅' if ok else '❌'} {label}")
    print("=" * 70)


if __name__ == "__main__":
    main()
