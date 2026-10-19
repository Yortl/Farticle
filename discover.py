"""发现层: Bing News RSS (给真实直链) + Google News RSS (覆盖更全, 链接需解码, 解码失败只记标题)。
只用标准库。Bing 的 apiclick 包装链接用 url= 参数解包即可拿到原文 URL —— 比 Google News 的
batchexecute 解码稳得多, 所以 Bing 为主。"""
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

def google_news(topic: str, domain: str, since_days: int, decode=None, gotitles: set | None = None):
    """Google News RSS: 标题/日期更全, 但链接是 news.google.com 包装的,
    decode() 由可选的 gnews-decoder 包提供, 解不出来时只记录标题占位。"""
    q = f'"{topic}" site:{domain}'
    url = "https://news.google.com/rss/search?" + urllib.parse.urlencode(
        {"q": q, "hl": "en-US", "gl": "US", "ceid": "US:en"})
    out = []
    try:
        root = _rss(url)
    except Exception:
        return out
    cutoff = datetime.now(timezone.utc) - timedelta(days=since_days)
    for it in root.findall(".//item"):
        title = (it.findtext("title") or "").strip()
        link = it.findtext("link") or ""
        try:
            pub = parsedate_to_datetime(it.findtext("pubDate"))
        except Exception:
            pub = None
        if pub and pub < cutoff:
            continue
        real = None
        if decode is not None and link.startswith("https://news.google.com/"):
            try:
                r = decode(link)
                real = (r.get("url") if isinstance(r, dict) else r) or None
            except Exception:
                real = None
        if real and gotitles is not None and title in gotitles:
            continue  # 与 Bing 结果重复
        out.append({
            "url": real or "",
            "title": title,
            "source": domain,
            "published": pub.isoformat() if pub else "",
            "via": "google_news" + ("" if real else "_unresolved"),
        })
    return out

def _gn_token(title: str) -> str:
    """GN 标题带 ' - The Guardian' 后缀, 去掉后归一化, 用于和 Bing 结果对齐。"""
    t = re.sub(r"\s+-\s+[^-]{2,40}$", "", title)
    return re.sub(r"[^\w]+", " ", t.lower()).strip()


def discover(topic: str, domain: str, since_days: int = 365, decode=None) -> list[dict]:
    items = []
    bing_tokens: set = set()
    for art in bing_news(topic, domain, since_days):
        bing_tokens.add(_gn_token(art["title"]))
        items.append(art)
        time.sleep(0.2)
    for art in google_news(topic, domain, since_days, decode=decode, gotitles=bing_tokens):
        art["_tok"] = _gn_token(art["title"])
        items.append(art)
        time.sleep(0.2)
    # GN 解码失败的条目 (无真实URL): 对应文章 Bing 已收录的丢弃, 剩下的才保留为标题线索
    items = [a for a in items if a.get("via") != "google_news_unresolved" or a["_tok"] not in bing_tokens]
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
