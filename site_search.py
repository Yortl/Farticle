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


def site_search_economist(topic: str, domain: str = "economist.com",
                          since_days: int = 365, cdp_host: str = "127.0.0.1:9222",
                          max_pages: int = 12) -> list[dict]:
    """economist.com 站内搜索: /search?q=X&page=N (SSR, 10条/页, page>=1 生效)。
    需要 CDP 渲染 (服务器直抓 403); URL 自带 /栏目/YYYY/MM/DD/ 日期可过滤。
    停止条件: 空页 / 无日期推进 (排序非纯时间, 连续2页全过期即停)。"""
    from datetime import datetime, timedelta, timezone
    from cdp_fetch import Tab
    import time as _t
    cutoff = datetime.now(timezone.utc) - timedelta(days=since_days)
    pat = re.compile(r"economist\.com/([a-z0-9-]+)/(\d{4})/(\d{2})/(\d{2})/")
    out, seen = [], set()
    stale = 0
    for page in range(1, max_pages + 1):
        url = (f"https://www.economist.com/search?"
               + urllib.parse.urlencode({"q": topic, "page": page}))
        tab = None
        info = {}
        try:
            q = urllib.parse.quote(url, safe="")
            req = urllib.request.Request(f"http://{cdp_host}/json/new?{q}", method="PUT")
            info = json.loads(urllib.request.urlopen(req, timeout=15).read())
            tab = Tab(info["webSocketDebuggerUrl"])
            _t.sleep(7)
            # 正则经 json.dumps 传给 JS, 避免 Python/JS 双层转义出错 (实测踩坑)
            rx = r"economist\.com/[a-z0-9-]+/\d{4}/\d{2}/\d{2}/"
            js = ("(() => JSON.stringify([...new Set([...document.querySelectorAll('a[href]')]"
                  ".map(a => a.href).filter(h => new RegExp(" + json.dumps(rx) + ").test(h)))]))()")
            links = tab.eval_js(js)
            links = json.loads(links) if links else []
        except Exception:
            break
        finally:
            if tab:
                try:
                    tab.close()
                    urllib.request.urlopen(
                        f"http://{cdp_host}/json/close/{info['id']}", timeout=8)
                except Exception:
                    pass
        if not links:
            break
        fresh = 0
        for h in links:
            m = pat.search(h)
            if not m:
                continue
            slug = h.split("economist.com/", 1)[1].rstrip("/")
            if slug in seen:
                continue
            seen.add(slug)
            try:
                _sec, y, mo, dy = m.groups()  # (栏目, 年, 月, 日)
                d = datetime(int(y), int(mo), int(dy), tzinfo=timezone.utc)
            except ValueError:
                continue
            if d < cutoff:
                continue
            out.append({
                "url": h.split("?")[0],
                "title": slug.rsplit("/", 1)[1].replace("-", " "),
                "source": domain,
                "published": d.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
                "via": "site_search",
            })
            fresh += 1
        if fresh == 0:
            stale += 1
            if stale >= 2:
                break
        else:
            stale = 0
    return out
