"""CDP 在线验证: 多渠道真实抓取走生产 extract_readability, 对比库内旧文本验证清洗效果。"""
import re
import sys
import time

sys.path.insert(0, ".")
from cdp_fetch import extract_readability, fetch  # noqa: E402

CDP_HOST = "127.0.0.1:9222"

# 每渠道一个代表 URL (覆盖: Guardian 导航/订阅钩子, CNBC consent 横幅,
# NatGeo 首字下沉+Rates:, Atlantic 书名拆句/摄影信用, NYT 行内链接拆行, WaPo 页脚)
URLS = [
    ("theguardian", "https://www.theguardian.com/commentisfree/2026/oct/13/strikes-teaching-bans-and-slashed-subjects-how-australian-unis-are-an-education-in-uncertainty-and-cost-cutting"),
    ("cnbc", "https://www.cnbc.com/2026/10/20/data-center-stocks-slide-as-korea-ipo-plunges-30percent-pressuring-tech-sentiment.html"),
    ("natgeo", "https://www.nationalgeographic.com/travel/best-of-the-world-2026/article/best-new-hotels"),
    ("atlantic", "https://www.theatlantic.com/technology/2026/10/blocking-internet-archive-will-not-stop-ai/689065/"),
    ("nytimes", "https://www.nytimes.com/2026/10/20/nyregion/measles-amish-school-vaccine-requirement.html"),
    ("wapo", "https://www.washingtonpost.com/technology/2026/10/19/elon-musk-says-hell-give-spacex-share-every-american-child/"),
]

# 清洗后不该出现的噪音指纹
JUNK_PAT = re.compile(
    r"^(Sign in|Subscribe|Newsletters|Skip to|Cookie|Accept Cookies|We and our partners|"
    r"Click 'Got it'|Rates:|Photograph:|Image Courtesy|Most viewed|NEWSLETTER|"
    r"View image in fullscreen|Explore the .*Issue|Sign up|Already a subscriber)", re.M)


def scan(text: str) -> list[str]:
    hits = []
    for m in JUNK_PAT.finditer(text):
        line = text[max(0, m.start() - 1):text.find("\n", m.start()) or m.end()]
        hits.append(line.strip()[:60])
    for l in text.split("\n"):
        ls = l.strip()
        if ls and len(ls) <= 3 and not ls.isdigit():
            hits.append(f"SHORT:{ls}")
    return hits


def main():
    results = []
    for name, url in URLS:
        t0 = time.time()
        try:
            html, title = fetch(CDP_HOST, url, wait_s=15)
            text = extract_readability(html) or ""
            words = len(text.split())
            junk = scan(text)
            ok = words >= 300 and not junk
            results.append((name, words, time.time() - t0, junk, ok))
            print(f"[{name}] {words}w {time.time()-t0:.0f}s junk={len(junk)} {'OK' if ok else 'FAIL'}")
            for j in junk[:5]:
                print(f"    {j}")
            open(f"run/verify_{name}.txt", "w").write(text)
        except Exception as e:  # noqa: BLE001
            results.append((name, 0, time.time() - t0, [str(e)[:80]], False))
            print(f"[{name}] ERROR: {e}")
        time.sleep(2)
    n_ok = sum(1 for r in results if r[4])
    print(f"\nPASS {n_ok}/{len(results)}")
    sys.exit(0 if n_ok == len(results) else 1)


if __name__ == "__main__":
    main()
