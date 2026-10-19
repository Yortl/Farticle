"""一次性全区间自检; 断言式, 失败即非零退出。"""
import sys

sys.path.insert(0, ".")
from discover import _gn_token, bing_news, discover  # noqa: E402
from fetchers import extract  # noqa: E402
from run import slugify  # noqa: E402

assert slugify("Hello, World! AI & Jobs") == "hello-world-ai-jobs"
assert _gn_token("Some Title Here - The Guardian") == "some title here"

t = extract("<html><head><title>t</title></head><body><article><h2>Head</h2><p>"
            + "word " * 300 + "</p></article></body></html>")
assert t and "word" in t

got = bing_news("climate change", "theguardian.com", 90)
assert got and all(a["url"].startswith("http") for a in got), "bing 发现层失效"

arts = discover("climate change", "csmonitor.com", since_days=30)
assert arts, "discover 空结果"
print(f"selftest OK: bing={len(got)} discover={len(arts)} extract={len(t)}ch")
