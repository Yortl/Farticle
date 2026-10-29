"""发现层: Bing News RSS (给真实直链) + Google News RSS (覆盖更全, 链接需解码, 解码失败只记标题)。
只用标准库。Bing 的 apiclick 包装链接用 url= 参数解包即可拿到原文 URL —— 比 Google News 的
batchexecute 解码稳得多, 所以 Bing 为主。"""
import json
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

UA = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}

def _rss(url: str, timeout: int = 30):
    req = urllib.request.Request(url, headers=UA)
    data = urllib.request.urlopen(req, timeout=timeout).read()
    return ET.fromstring(data)

def _unwrap_bing(link: str) -> str | None:
    m = re.search(r"[?&]url=([^&]+)", link)
    return urllib.parse.unquote(m.group(1)) if m else None

def _norm(url: str) -> str:
    u = urllib.parse.urlparse(url)
    return (u.netloc.removeprefix("www.") + u.path.rstrip("/")).lower()

def bing_news(topic: str, domain: str, since_days: int):
    """Bing News RSS: 直接返回真实文章 URL。"""
    q = f'"{topic}" site:{domain}'
    url = "https://www.bing.com/news/search?" + urllib.parse.urlencode({"q": q, "format": "RSS"})
    out = []
    try:
        root = _rss(url)
    except Exception:
        return out
    cutoff = datetime.now(timezone.utc) - timedelta(days=since_days)
    for it in root.findall(".//item"):
        link = _unwrap_bing(it.findtext("link") or "")
        if not link or not link.startswith("http"):
            continue
        try:
            pub = parsedate_to_datetime(it.findtext("pubDate"))
        except Exception:
            pub = None
        if pub and pub < cutoff:
            continue
        out.append({
            "url": link,
            "title": (it.findtext("title") or "").strip(),
            "source": domain,
            "published": pub.isoformat() if pub else "",
            "via": "bing_news",
        })
    return out

def google_news_paged(topic: str, domain: str, since_days: int) -> list[dict]:
    """GN RSS: 链接是 news.google.com 包装的, 存 _gn 由调用方懒解码 (CDP cdp_decode);
    解不出只记标题。单次查询封顶 100 条; 按月切片绕开上限
    (yellowstone@discover 2025: 整年86 -> 月度和1088)。切片数 = ceil(since_days/30), 封顶 48 个月。"""
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=since_days)
    months = min(max((since_days + 29) // 30, 1), 48)
    items = []
    for i in range(months):
        lo = start + timedelta(days=30 * i)
        hi = min(lo + timedelta(days=30), end + timedelta(days=1))
        if lo >= end:
            break
        q = (f"{topic} site:{domain} "
             f"after:{lo.strftime('%Y-%m-%d')} before:{hi.strftime('%Y-%m-%d')}")
        url = "https://news.google.com/rss/search?" + urllib.parse.urlencode(
            {"q": q, "hl": "en-US", "gl": "US", "ceid": "US:en"})
        try:
            root = _rss(url)
        except Exception:
            continue
        for it in root.findall(".//item"):
            title = (it.findtext("title") or "").strip()
            link = it.findtext("link") or ""
            try:
                pub = parsedate_to_datetime(it.findtext("pubDate"))
            except Exception:
                pub = None
            if pub and pub < start:
                continue
            items.append({
                "url": "" if link.startswith("https://news.google.com/") else link,
                "_gn": link if link.startswith("https://news.google.com/") else "",
                "title": title,
                "source": domain,
                "published": pub.isoformat() if pub else "",
                "via": "google_news_paged",
            })
        time.sleep(0.3)
    return items


def _gn_token(title: str) -> str:
    """GN 标题带 ' - The Guardian' 后缀, 去掉后归一化, 用于和 Bing 结果对齐。"""
    t = re.sub(r"\s+-\s+[^-]{2,40}$", "", title)
    return re.sub(r"[^\w]+", " ", t.lower()).strip()


def section_discover(topic: str, cfg: dict, cdp_host: str = "127.0.0.1:9222") -> list[dict]:
    """栏目列表页发现 (bjreview 等不被 bing 收录的站点):
    打开 cfg['sections'] 各栏目页, 抓文章链接, 按话题关键词过滤标题。"""
    import time as _t
    import urllib.request as _u
    kw = [w for w in re.split(r"[^a-z]+", topic.lower()) if len(w) > 3]
    out = []
    for sec in cfg.get("sections", []):
        url = f"https://www.{cfg['domain']}/{sec}/"
        try:
            # 复用 CDP: 开 tab → 抓所有文章链接+标题 → 关 tab
            req = _u.Request(f"http://{cdp_host}/json/new?{urllib.parse.quote(url, safe='')}", method="PUT")
            info = json.loads(_u.urlopen(req, timeout=15).read())
            import socket as _s
            from urllib.parse import urlparse as _up
            hostport = _up(info["webSocketDebuggerUrl"]).netloc
            sock = _s.create_connection((hostport.split(":")[0], int(hostport.split(":")[1])), timeout=30)
            # 简化: 用 cdp_fetch.Tab 的手写 ws
            from cdp_fetch import Tab
            tab = Tab(info["webSocketDebuggerUrl"])
            _t.sleep(6)
            links = tab.eval_js("""
                [...document.querySelectorAll('a')].filter(a => {
                    const h = a.href || '';
                    return h.includes('bjreview.com') && /t[0-9]{8}_[0-9]+/.test(h) && a.textContent.trim().length > 15;
                }).map(a => ({url: a.href, title: a.textContent.trim()}))
            """) or []
            tab.close()
            _u.urlopen(f"http://{cdp_host}/json/close/{info['id']}", timeout=8)
            for it in links[:15]:
                title = it["title"]
                tl = title.lower()
                if (not kw or any(w in tl for w in kw)
                        or "ai" in tl.split() or "artificial" in tl):
                    out.append({"url": it["url"], "title": title, "source": cfg["domain"],
                                "published": "", "via": "section"})
            _t.sleep(0.5)
        except Exception as e:
            print(f"[section_discover] {sec} fail: {e}")
            continue
    # 去重
    seen, uniq = set(), []
    for a in out:
        k = a["url"]
        if k in seen:
            continue
        seen.add(k)
        uniq.append(a)
    return uniq


# 站内搜索适配器: 命中源站自己的搜索引擎, 结果最全 (discover yellowstone: 302 条精确)。
# 每站一个函数(topic, domain, since_days, cdp_host) -> [{url,title,source,published,via:'site_search'}]
_SITE_SEARCH = {
    "discovermagazine.com": "site_search_discover",
    "economist.com": "site_search_economist",
}


def site_search(topic: str, domain: str, since_days: int = 365,
                cdp_host: str = "127.0.0.1:9222") -> list[dict]:
    fn_name = _SITE_SEARCH.get(domain)
    if not fn_name:
        return []
    try:
        import site_search as _ss
        fn = getattr(_ss, fn_name)
        if fn.__code__.co_argcount >= 4 or "cdp_host" in fn.__code__.co_varnames:
            return fn(topic, domain, since_days, cdp_host=cdp_host)
        return fn(topic, domain, since_days)
    except Exception:
        return []


def cdp_decode(cdp_host: str = "127.0.0.1:9222"):
    """GN 包装链接 CDP 解码器: 开 tab 等 302 → location.href 即真实 URL。
    googlenewsdecoder 包对新编码 (AU_yqL) 已失效 (实测 2026-10), 此为正解。
    单条失败返回 None, 不弃用。"""
    import json as _json
    import urllib.request as _u
    from cdp_fetch import Tab

    def dec(link: str) -> str | None:
        if not link.startswith("https://news.google.com/"):
            return link
        tab = None
        info = {}
        try:
            q = urllib.parse.quote(link, safe="")
            req = _u.Request(f"http://{cdp_host}/json/new?{q}", method="PUT")
            info = _json.loads(_u.urlopen(req, timeout=15).read())
            tab = Tab(info["webSocketDebuggerUrl"])
            # ponytail: 302 跳转通常 3-8s, 慢时 10s+ — 轮询拿真实落地 URL, 固定 sleep 会把慢跳转误判成失败 (实测 1/3 成功)
            real = ""
            for _ in range(6):
                time.sleep(3)
                real = tab.eval_js("location.href") or ""
                if real and not real.startswith("https://news.google.com/"):
                    break
            return real if real and not real.startswith("https://news.google.com/") else None
        except Exception:
            return None
        finally:
            if tab:
                try:
                    tab.close()
                    _u.urlopen(f"http://{cdp_host}/json/close/{info['id']}", timeout=8)
                except Exception:
                    pass
    return dec


def discover(topic: str, domain: str, since_days: int = 365, decode=None,
             cdp_host: str = "127.0.0.1:9222") -> list[dict]:
    """三通道发现: 站内搜索(最全) + Bing News(真实直链) + GN 按月切片(覆盖兜底)。
    标题归一化去重; GN 包装链接存 _gn 待懒解码 (decode()=cdp_decode 注入时立即批量解)。"""
    items = []
    known_tokens: set = set()
    for art in site_search(topic, domain, since_days, cdp_host=cdp_host):
        known_tokens.add(_gn_token(art["title"]))
        items.append(art)
        time.sleep(0.2)
    for art in bing_news(topic, domain, since_days):
        known_tokens.add(_gn_token(art["title"]))
        items.append(art)
        time.sleep(0.2)
    for art in google_news_paged(topic, domain, since_days):
        art["_tok"] = _gn_token(art["title"])
        items.append(art)
        time.sleep(0.2)
    # GN 解码失败的条目 (无真实URL): 对应文章站内搜索/Bing 已收录的丢弃, 剩下的保留待解
    items = [a for a in items if a.get("via") != "google_news_paged"
             or a["url"] or a["_tok"] not in known_tokens]
    # 按 URL 去重 (同一篇被两家索引)
    seen, uniq = set(), []
    for a in items:
        key = _norm(a["url"]) if a["url"] else "tok:" + a["_tok"]
        if key in seen:
            continue
        seen.add(key)
        a.pop("_tok", None)
        uniq.append(a)
    return uniq
