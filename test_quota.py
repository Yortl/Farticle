"""配额与词数逻辑自检 (离线, 不连网/不连 CDP)。python3 test_quota.py"""
import sys

sys.path.insert(0, ".")
import server  # noqa: E402

# 词数范围可选
assert server.ok_words(500, None, None)          # 未勾选: 不限范围
assert server.ok_words(3000, None, None)
assert not server.ok_words(50, None, None)       # 天然下限 80
assert server.ok_words(400, 400, 1200) and server.ok_words(1200, 400, 1200)
assert not server.ok_words(399, 400, 1200) and not server.ok_words(1201, 400, 1200)

# per = 该源最终入库篇数: 已在库的算数, 且只算本篇数一次
calls = []
server.fetch = lambda host, url, wait_s=15: calls.append(url) or (
    "<p>" + "word " * 700 + "</p>", f"t{len(calls)}")
server.bing_news = lambda topic, domain, since_days: [
    {"url": f"https://economist.com/{i}", "title": f"Article {i}"} for i in range(6)]
server.extract_readability = lambda html: html.replace("<p>", "").replace("</p>", "")
server.OUTLETS = {"economist": {"domain": "economist.com", "tier": "mirror", "enabled": True,
                                "mirror": "https://mirror/"}}
# 前 2 篇已在库: 仍应计满 per=2, 第 3 篇不再抓
existing_ids = {"https://economist.com/0": "a0", "https://economist.com/1": "a1"}
server.save_article = lambda title, source, url, words, text, pub_date="": (
    (existing_ids[url], False) if url in existing_ids else ("new" + url[-1], True))
jobs = {}
server.job_write = lambda jid, d: jobs.__setitem__(jid, dict(d))
server.job_read = lambda jid: {"status": "running"}
server.time.sleep = lambda s: None
server.run_job("j1", "discover", topic="x", outlet="economist", per=2, days=30)
d = jobs["j1"]
assert d["status"] == "done", d
assert len(d["articles"]) == 2, d["articles"]
assert all(not a["new"] for a in d["articles"]), "已入库的两篇应标注 new=False"
assert d["existing"] == 2, d
assert len(calls) == 2, f"per=2 只应抓 2 篇, 实抓 {len(calls)}"

# 未勾选词数: 200 词短文也入库, 不判过短
calls.clear()
server.extract_readability = lambda html: "word " * 200
server.save_article = lambda *a, **k: ("aa", True)
server.run_job("j2", "discover", topic="x", outlet="economist", per=1, days=30)
assert len(jobs["j2"]["articles"]) == 1, jobs["j2"]
assert not jobs["j2"].get("skipped"), jobs["j2"]  # 不限范围时 200 词不该被跳

# 勾选 400-1200: 短文跳过并继续找, 直到凑够入库数 (第1个 URL 恒 200 词, 第2个 800 词)
calls.clear()


def _fetch(host, url, wait_s=15):
    calls.append(url)
    return (f"<u>{url}</u>", "t")


server.fetch = _fetch
server.extract_readability = lambda html: "word " * (200 if "economist.com/0" in html else 800)
got = {}
server.save_article = lambda title, source, url, words, text, pub_date="": got.__setitem__(words, url) or ("x", True)
server.run_job("j3", "discover", topic="x", outlet="economist", per=1, days=30,
               min_words=400, max_words=1200)
assert jobs["j3"]["skipped"], jobs["j3"]          # 200 词被跳过
assert 800 in got, got                            # 继续抓到合格的 800 词

# --- 测试 extract_date & pub_year 年份过滤 ---
assert server.extract_date("", fallback="2026-10-07T08:00:00Z") == "2026-10-07"
assert server.extract_date("", url="https://theguardian.com/2026/oct/21/test") == "2026-10-21"
assert server.extract_date("", url="https://economist.com/2025/11/02/test") == "2025-11-02"

# 勾选限定年份 2026: 2025 年跳过, 2026 年入库
server.bing_news = lambda topic, domain, since_days: [
    {"url": "https://economist.com/2025/01/15/old", "title": "Old Article"},
    {"url": "https://economist.com/2026/05/20/new", "title": "New Article"},
]
server.extract_readability = lambda html: "word " * 500
got_dates = []
server.save_article = lambda title, source, url, words, text, pub_date="": got_dates.append(pub_date) or ("x", True)
server.run_job("j4", "discover", topic="x", outlet="economist", per=1, days=30, pub_year=2026)
assert len(jobs["j4"]["articles"]) == 1
assert jobs["j4"]["articles"][0]["date"] == "2026-05-20"
assert any("2025" in s for s in jobs["j4"].get("skipped", []))

print("test_quota: all ok")