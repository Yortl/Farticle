"""全源弹窗探测: 每源开真实页面, 扫描 cookie/consent/订阅/推送弹窗的宿主与可点按钮。"""
import glob
import json
import re
import sys
import time
import urllib.parse
import urllib.request

sys.path.insert(0, ".")
from cdp_fetch import Tab, http_json  # noqa: E402

CDP = "127.0.0.1:9222"

PROBE = """
(() => {
  const vis = el => { if (!el) return false; const r = el.getBoundingClientRect();
    return r.width > 20 && r.height > 20 && getComputedStyle(el).visibility !== 'hidden'; };
  const out = {hosts: [], buttons: []};
  // 已知 CMP/弹窗宿主 (id 前缀或 class 特征)
  const hostSels = [
    '#onetrust-consent-sdk', '#onetrust-banner-sdk', '#onetrust-pc-sdk',
    '#quantcast-tcf', '#qc-cmp2', '[id^="sp_message_container"]', '[id^="didomi"]',
    '#cookiebanner', '.cookie-banner', '#CybotCookiebotDialog', '#usercentrics-root',
    '#truste-consent-track', '#gdpr-consent-notice', '[data-testid="cookie-banner"]',
    '[class*="cookie-banner"]', '[class*="consent-banner"]', '[id*="consent"]',
    '#paywall', '[class*="paywall"]', '[class*="paywall-barrier"]',
    '[class*="newsletter-modal"]', '[class*="signup-modal"]', '[class*="regwall"]',
    '[class*="subscription-banner"]', '[aria-label*="newsletter" i]',
    '#tp-container', '[class*="tp-modal"]', '[class*="piano-modal"]',
    '#_evidon-banner', '#onetrust-pc-dark-filter', '[class*="donation"]',
  ];
  for (const sel of hostSels) {
    for (const el of document.querySelectorAll(sel)) {
      const vis = vis(el);
      if (el.getBoundingClientRect().width > 0 || vis)
        out.hosts.push({sel, vis, id: el.id, cls: (el.className||'').toString().slice(0,60)});
    }
  }
  // 可见按钮: 文案含弹窗动词
  const btnRe = /^(reject all|accept all( cookies)?|i accept|accept|got it|ok,? got it|manage|continue reading|i'll wait|no thanks|not now|maybe later|dismiss|close|skip|agree|allow all|allow|x|sure|later|never ask again|opt out|refuse|deny|do not sell|hide|后续再说|关闭|知道了|接受|拒绝)/i;
  for (const b of [...document.querySelectorAll('button, a.btn, [role="button"], .o-button, a[data-testid]')]) {
    if (!vis(b)) continue;
    const t = (b.textContent || '').trim();
    if (t && t.length < 40 && (btnRe.test(t) || /close|dismiss/i.test(b.className || b.getAttribute('aria-label') || '')))
      out.buttons.push({txt: t, id: b.id, cls: (b.className||'').toString().slice(0,50),
                        tag: b.tagName.toLowerCase()});
  }
  // 推送通知权限请求 (native prompt 探不到, 但页面内自绘的可查)
  if (document.querySelector('[class*="push"], [id*="webpush"], [class*="notifications-prompt"]'))
    out.hosts.push({sel: 'push-prompt?', vis: null, id: '', cls: ''});
  out.title = document.title;
  return JSON.stringify(out);
})()
"""


def probe(name: str, url: str) -> dict:
    try:
        ti = http_json(CDP, f"/json/new?{urllib.parse.quote(url, safe='')}")
    except Exception:
        req = urllib.request.Request(f"http://{CDP}/json/new?{urllib.parse.quote(url, safe='')}",
                                     method="PUT")
        ti = json.loads(urllib.request.urlopen(req, timeout=15).read())
    tab = Tab(ti["webSocketDebuggerUrl"])
    try:
        time.sleep(12)
        raw = tab.eval_js(PROBE)
        data = json.loads(raw) if raw else {}
        # 关弹窗动作: 尝试点可见 Reject/No thanks/关闭类按钮
        clicked = []
        if data.get("buttons"):
            for b in tab.eval_js("""
                (() => {
                  const vis = el => { const r = el.getBoundingClientRect();
                    return r.width > 20 && r.height > 20; };
                  const re = /^(reject all|no thanks|not now|maybe later|got it|dismiss|later|refuse|拒绝|关闭)/i;
                  const hits = [...document.querySelectorAll('button, [role="button"]')]
                    .filter(b => vis(b) && re.test((b.textContent||'').trim()) && (b.textContent||'').trim().length < 40)
                    .slice(0, 3)
                    .map(b => { const r = b.getBoundingClientRect();
                        return {x: r.x + r.width/2, y: r.y + r.height/2, t: (b.textContent||'').trim().slice(0,20)}; });
                  return JSON.stringify(hits);
                })()
            """) or []:
                for act in (("mouseMoved",), ("mousePressed",), ("mouseReleased",)):
                    tab.call("Input.dispatchMouseEvent", type=act[0], x=b["x"], y=b["y"],
                             button="left", clickCount=1)
                clicked.append(b["t"])
        time.sleep(2)
        after = tab.eval_js(PROBE)
        after = json.loads(after) if after else {}
        return {"name": name, "before_hosts": data.get("hosts", []),
                "before_btns": data.get("buttons", [])[:10],
                "clicked": clicked, "after_hosts": after.get("hosts", [])}
    except Exception as e:  # noqa: BLE001
        return {"name": name, "error": str(e)[:100]}
    finally:
        try:
            tab.close()
            http_json(CDP, f"/json/close/{ti['id']}", timeout=8)
        except Exception:
            pass


def main():
    # 每源取一篇库内真实文章 URL, 没有的用首页
    urls = {}
    for p in glob.glob("web_store/articles/*.md"):
        t = open(p).read()
        m = re.search(r"source: (\S+)", t)
        u = re.search(r"url: (\S+)", t)
        if m and u and m.group(1) not in urls:
            urls[m.group(1)] = u.group(1)
    SITE_URLS = {
        "time": "https://time.com/section/ideas/", "npr": "https://www.npr.org/sections/health/",
        "vox": "https://www.vox.com/technology", "arstechnica": "https://arstechnica.com/gadgets/",
        "chinadaily": "https://www.chinadaily.com.cn/world", "bjreview": "https://www.bjreview.com/World/",
        "newscientist": "https://www.newscientist.com/article-topic/artificial-intelligence/",
        "discover": "https://www.discovermagazine.com/the-sciences",
        "economist": "https://freedium-mirror.cfd/https://www.economist.com/science-and-technology/",
        "sciam": "https://www.scientificamerican.com/section/mind-matters/",
        "psychtoday": "https://www.psychologytoday.com/us/basics/memory",
        "wired": "https://www.wired.com/story/latest-tech/",
    }
    targets = []
    for dom in ("theguardian.com", "bbc.com", "nationalgeographic.com", "time.com", "newsweek.com",
                "scientificamerican.com", "psychologytoday.com", "wired.com", "cnbc.com", "npr.org",
                "vox.com", "arstechnica.com", "chinadaily.com.cn", "bjreview.com",
                "theatlantic.com", "newscientist.com", "washingtonpost.com", "discovermagazine.com",
                "nytimes.com"):
        targets.append((dom.split(".")[0], urls.get(dom, SITE_URLS.get(dom, f"https://{dom}/"))))
    print(json.dumps({n: u for n, u in targets}, indent=0)[:400])
    results = []
    for name, url in targets:
        r = probe(name, url)
        results.append(r)
        hosts = [h["sel"] for h in r.get("before_hosts", []) if h.get("vis")]
        print(f"[{name}] hosts={len(hosts)} vis={hosts[:4]} btns={len(r.get('before_btns', []))} "
              f"clicked={r.get('clicked')} after={len([h for h in r.get('after_hosts', []) if h.get('vis')])}")
        time.sleep(2)
    json.dump(results, open("run/popup_probe.json", "w"), ensure_ascii=False, indent=1)
    print("saved run/popup_probe.json")


if __name__ == "__main__":
    main()
