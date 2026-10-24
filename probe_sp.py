"""探查 Sourcepoint 弹窗结构: iframe? shadow DOM? 按钮真实位置。"""
import json
import sys
import time
import urllib.parse
import urllib.request

sys.path.insert(0, ".")
from cdp_fetch import Tab, http_json  # noqa: E402

CDP = "127.0.0.1:9222"

SRC = """
(() => {
  const out = {iframes: [], shadowHosts: [], topButtons: []};
  const vis = el => { if (!el) return false; const r = el.getBoundingClientRect();
    return r.width > 20 && r.height > 20; };
  // container 本体
  const c = document.querySelector('[id^="sp_message_container"]');
  if (c) {
    const r = c.getBoundingClientRect();
    out.container = {id: c.id, w: r.width, h: r.height, iframes: c.querySelectorAll('iframe').length,
                     html: c.innerHTML.length, shadow: !!c.shadowRoot};
  }
  // 所有 iframe (含 container 内外)
  for (const f of document.querySelectorAll('iframe')) {
    const r = f.getBoundingClientRect();
    if (r.width > 50 && r.height > 50)
      out.iframes.push({src: (f.src||'').slice(0,110), x: Math.round(r.x), y: Math.round(r.y),
                        w: Math.round(r.width), h: Math.round(r.height), name: f.name});
  }
  // shadow roots anywhere
  for (const el of [...document.querySelectorAll('*')]) {
    if (el.shadowRoot) out.shadowHosts.push({tag: el.tagName, id: el.id,
      cls: (el.className||'').toString().slice(0,40)});
  }
  // 顶文档可见按钮 (sp 有时在顶文档)
  for (const b of [...document.querySelectorAll('button, [role="button"], a')]) {
    const t = (b.textContent||'').trim();
    if (vis(b) && t && t.length < 40)
      out.topButtons.push({t, cls: (b.className||'').toString().slice(0,40)});
  }
  out.topButtons = out.topButtons.slice(0, 15);
  return JSON.stringify(out);
})()
"""


def open_tab(url: str):
    try:
        return http_json(CDP, f"/json/new?{urllib.parse.quote(url, safe='')}")
    except Exception:
        req = urllib.request.Request(f"http://{CDP}/json/new?{urllib.parse.quote(url, safe='')}",
                                     method="PUT")
        return json.loads(urllib.request.urlopen(req, timeout=15).read())


def main():
    for name, url in [("guardian", "https://www.theguardian.com/environment/2026/oct/20/countries-pushing-vital-global-climate-report-delayed"),
                      ("bbc", "https://www.bbc.com/zhongwen/articles/cwvgdlgq3110o/simp"),
                      ("atlantic", "https://www.theatlantic.com/technology/2026/10/blocking-internet-archive-will-not-stop-ai/689065/")]:
        ti = open_tab(url)
        tab = Tab(ti["webSocketDebuggerUrl"])
        try:
            tab.call("Network.enable")
            tab.call("Storage.clearDataForOrigin",
                     origin="https://" + urllib.parse.urlparse(url).netloc,
                     storageTypes="cookies,local_storage,session_storage")
            tab.call("Page.reload")
            time.sleep(12)
            raw = tab.eval_js(SRC)
            d = json.loads(raw) if raw else {}
            print(f"== {name}")
            print(json.dumps(d, indent=1, ensure_ascii=False)[:1200])
        finally:
            try:
                tab.close(); http_json(CDP, f"/json/close/{ti['id']}", timeout=8)
            except Exception:
                pass
        time.sleep(2)


if __name__ == "__main__":
    main()
