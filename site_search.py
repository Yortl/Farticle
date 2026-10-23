"""站点原生搜索发现层: 直接命中源站自己的搜索(结果最全最准), 只用标准库。
已验证: discovermagazine.com /search?term=X&page=N (SSR __NEXT_DATA__, totalCount 全量, 12条/页)。
其它站没有公开搜索的继续走 bing/google_news。"""
import json
import re
import urllib.parse
import urllib.request

UA = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}


def site_search_discover(topic: str, domain: str, since_days: int = 365,
                         max_pages: int = 30) -> list[dict]:
    """discovermagazine.com 站内搜索: /search?term=X&page=N, __NEXT_DATA__ JSON。
    12 条/页; totalCount 全量 (yellowstone=302)。空页/无 totalCount 即停。"""
    out, seen = [], set()
    base = f"https://www.{domain}"
    for page in range(1, max_pages + 1):
        url = base + "/search?" + urllib.parse.urlencode({"term": topic, "page": page})
        try:
            req = urllib.request.Request(url, headers=UA)
            html = urllib.request.urlopen(req, timeout=20).read().decode("utf-8", "ignore")
        except Exception:
            break
        m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html, re.S)
        if not m:
            break
        try:
            pp = json.loads(m.group(1))["props"]["pageProps"]
        except Exception:
            break
        items = pp.get("contentList", {}).get("content", [])
        if not items:
            break
        for it in items:
            slug = it.get("slug") or ""
            aid = it.get("id")
            # 真实文章 URL 是 {slug}-{数字id}; 裸 slug 404 (实测 2026-10)
            if not slug or not aid or slug in seen:
                continue
            seen.add(slug)
            out.append({
                "url": f"{base}/{slug}-{aid}",
                "title": (it.get("title") or slug.replace("-", " ")).strip(),
                "source": domain,
                "published": "",
                "via": "site_search",
            })
    return out
