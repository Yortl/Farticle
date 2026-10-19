"""抓取层: 直抓 -> (metered 失败时) wayback 降级 -> trafilatura 提取 Markdown。
诚实失败: 拿不到就标 failed, 不塞残缺正文。"""
import gzip
import io
import json
import time
import urllib.parse
import urllib.request

import trafilatura

UA = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip",
}


def _get(url: str, timeout: int = 60, attempts: int = 2):
    last = None
    for i in range(attempts):
        try:
            # ponytail: socket 全局默认超时兜底, 防个别站点把连接吊死 (如 wapo 经代理的 CLOSE-WAIT 挂起)
            import socket
            if socket.getdefaulttimeout() is None:
                socket.setdefaulttimeout(timeout + 30)
            resp = urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout)
            raw = resp.read()
            if resp.headers.get("Content-Encoding") == "gzip":
                raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
            return resp.status, raw
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 + i * 3)
    raise last


def wayback_snapshots(url: str, limit: int = 3) -> list[str]:
    """该 URL 在 Wayback 里的近期快照。只查 availability API (稳定, 3s 内返回);
    CDX 对部分 URL 同样吊死连接, 故弃用。"""
    snaps = []
    try:
        _, raw = _get("https://archive.org/wayback/available?url=" + urllib.parse.quote(url, safe=""),
                      timeout=30)
        closest = json.loads(raw).get("archived_snapshots", {}).get("closest", {})
        if closest.get("available") and closest.get("timestamp"):
            snaps.append(closest["timestamp"])
    except Exception:
        pass
    return snaps




def extract(html: str) -> str | None:
    return trafilatura.extract(html, output_format="markdown", include_comments=False,
                               include_tables=False) or None


def fetch_article(url: str, tier: str = "free") -> dict:
    """返回 {status, text, final_url, how}; status: ok | failed"""
    how = "direct"
    try:
        _, html = _get(url, timeout=50)
        text = extract(html.decode("utf-8", "ignore"))
        if text and len(text) >= 300:
            return {"status": "ok", "text": text, "final_url": url, "how": how}
    except Exception as e:  # noqa: BLE001
        direct_err = f"{type(e).__name__}: {e}"[:120]
    else:
        direct_err = "extract_too_short"

    if tier != "free":
        # ponytail: wapo/nyt 等对直抓和 SPN 均可能吊死连接 (经代理 CLOSE-WAIT), wayback 只查已有缓存。
        # ponytail: archive.today 未接入 (本网络握手超时); 只用 wayback。
        for ts in wayback_snapshots(url):
            snap = f"https://web.archive.org/web/{ts}/{url}"
            try:
                _, page = _get(snap, timeout=50)
                text = extract(page.decode("utf-8", "ignore"))
                if text and len(text) >= 300:
                    return {"status": "ok", "text": text, "final_url": snap, "how": "wayback_cache"}
            except Exception:
                continue
        return {"status": "failed", "text": "", "final_url": url, "how": f"wayback_after_{direct_err}"}
    return {"status": "failed", "text": "", "final_url": url, "how": f"direct_{direct_err}"}
