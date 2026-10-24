"""清站 cookie 复现弹窗 → 验证自动关闭动作 → 确认关闭后可抓正文。"""
import json
import sys
import time
import urllib.parse
import urllib.request

sys.path.insert(0, ".")
from cdp_fetch import Tab, http_json  # noqa: E402

CDP = "127.0.0.1:9222"

# 每站: 文章 URL + 一组 (名称, 选择器候选) 关闭动作
CLOSE_JS = """
(() => {
  const vis = el => { if (!el) return false; const r = el.getBoundingClientRect();
    return r.width > 20 && r.height > 20 && getComputedStyle(el).visibility !== 'hidden'; };
  const report = {found: [], clicked: []};
  const click = (el, how) => {
    if (!vis(el)) return false;
    const r = el.getBoundingClientRect();
    const x = r.x + r.width/2, y = r.y + r.height/2;
    report.found.push({how, txt: (el.textContent||'').trim().slice(0,25), id: el.id});
    // 优先 JS click (OneTrust 等接受合成事件), 失败再 CDP 坐标
    try { el.click(); } catch (e) {}
    report.clicked.push({how, x: Math.round(x), y: Math.round(y)});
    return true;
  };
  // 1) CMP 固定 id (OneTrust / Cookiebot / Didomi / Quantcast / Sourcepoint / Truste)
  const fixed = [
    ['onetrust-reject-all-handler', null], ['onetrust-reject-btn-handler', null],
    ['onetrust-accept-btn-handler', null], ['onetrust-banner-sdk #onetrust-accept-btn-handler', null],
    ['CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll', null],
    ['CybotCookiebotDialogBodyButtonDecline', null],
    ['didomi-notice-agree-button', null], ['didomi-notice-disagree-button', null],
    ['qc-cmp2-buttons-desktop button', null], ['truste-consent-reject', null],
    ['truste-consent-accept', null], ['_evidon-accept-button', null],
    ['sp_message_container .sp_choice_type_ACCEPT_ALL', null],
    ['sp_message_container .sp_choice_type_REJECT_ALL', null],
    ['usercentrics-root button[data-testid="uc-deny-all-button"]', null],
    ['usercentrics-root button[data-testid="uc-accept-all-button"]', null],
    ['fc-cta-consent', null],  // Funding Choice
  ];
  for (const [sel, _] of fixed) {
    const el = document.querySelector(sel);
    if (el && click(el, 'id:' + sel)) break;
  }
  // 2) 文本匹配 (reject/decline/no thanks 优先, accept 兜底)
  if (!report.clicked.length) {
    const re1 = /^(reject all|reject|decline|no thanks|not now|maybe later|continue without agreeing|refuse|opt out|deny|稍后|拒绝|暂不|不再提示|关闭|知道了)/i;
    const re2 = /^(accept all( cookies)?|i accept|accept|got it|agree|allow all|ok|接受|同意|好的)/i;
    const btns = [...document.querySelectorAll('button, [role="button"], a')].filter(vis)
      .filter(b => { const t = (b.textContent||'').trim(); return t.length > 0 && t.length < 35; });
    const pick = btns.find(b => re1.test((b.textContent||'').trim()))
              || btns.find(b => re2.test((b.textContent||'').trim()));
    if (pick) click(pick, 'text:' + (pick.textContent||'').trim().slice(0,20));
  }
  // 3) 通用关闭 X / 模态关闭钮 (订阅弹窗/推送弹窗)
  if (!report.clicked.length) {
    const closes = [...document.querySelectorAll(
      '[class*="close" i]:not(svg):not(path), [aria-label="Close" i], [aria-label*="dismiss" i], [data-dismiss], .modal .close'
    )].filter(vis).slice(0, 2);
    if (closes.length) click(closes[0], 'x-btn:' + (closes[0].className||'').toString().slice(0,30));
  }
  return JSON.stringify(report);
})()
"""

OVERLAY_CHECK = """
(() => {
  const vis = el => { if (!el) return false; const r = el.getBoundingClientRect();
    return r.width > 30 && r.height > 30 && getComputedStyle(el).visibility !== 'hidden'
      && getComputedStyle(el).display !== 'none'; };
  const sels = ['#onetrust-banner-sdk', '#onetrust-pc-sdk', '#quantcast-tcf', '#qc-cmp2',
    '#CybotCookiebotDialog', '#didomi-host', '[id^="sp_message_container"]', '#usercentrics-root',
    '#truste-consent-track', '[class*="paywall"]', '[class*="paywall-barrier"]',
    '[class*="newsletter-modal"]', '[class*="regwall"]', '[class*="modal"][style*="fixed"]'];
  const open = [];
  for (const s of sels) for (const el of document.querySelectorAll(s)) {
    if (vis(el) || (el.getBoundingClientRect().width > 50 && el.getBoundingClientRect().height > 50))
      open.push(s);
  }
  return JSON.stringify({open: [...new Set(open)], title: document.title.slice(0,60)});
})()
"""


def open_tab(url: str):
    try:
        return http_json(CDP, f"/json/new?{urllib.parse.quote(url, safe='')}")
    except Exception:
        req = urllib.request.Request(f"http://{CDP}/json/new?{urllib.parse.quote(url, safe='')}",
                                     method="PUT")
        return json.loads(urllib.request.urlopen(req, timeout=15).read())


def run_site(name: str, url: str, tab: Tab, results: dict):
    time.sleep(12)  # 等页面+CMP 注入
    r1 = json.loads(tab.eval_js(OVERLAY_CHECK) or "{}")
    act = json.loads(tab.eval_js(CLOSE_JS) or "{}")
    time.sleep(3)  # 等关闭动画/重渲染
    r2 = json.loads(tab.eval_js(OVERLAY_CHECK) or "{}")
    # 二轮 (偏好中心层叠时需要)
    act2 = json.loads(tab.eval_js(CLOSE_JS) or "{}") if r2.get("open") else {"clicked": []}
    if act2.get("clicked"):
        time.sleep(3)
        r2 = json.loads(tab.eval_js(OVERLAY_CHECK) or "{}")
    results[name] = {
        "before": r1.get("open", []), "actions": act.get("found", []),
        "clicked": act.get("clicked", []) + act2.get("clicked", []),
        "after": r2.get("open", []),
        "closed_ok": not r2.get("open") and bool(act.get("clicked") or act2.get("clicked")),
        "no_popup": not r1.get("open"),
        "title": r1.get("title", ""),
    }
    tag = ("NO-POPUP" if results[name]["no_popup"] else
           "CLOSED" if results[name]["closed_ok"] else "STUCK")
    print(f"[{name}] {tag} before={r1.get('open', [])[:3]} "
          f"act={[a.get('how','')[:28] for a in act.get('found', [])][:3]} after={r2.get('open', [])[:3]}")


def main():
    SITES = json.load(open("run/popup_urls.json"))
    results = {}
    for name, url in SITES.items():
        ti = open_tab(url)
        tab = Tab(ti["webSocketDebuggerUrl"])
        try:
            # 精准清本站 cookie (不动其他站), 复现首次访问
            try:
                tab.call("Network.enable")
                tab.call("Storage.clearDataForOrigin",
                         origin="https://" + urllib.parse.urlparse(url).netloc,
                         storageTypes="cookies,local_storage,session_storage,indexeddb")
            except Exception as e:  # noqa: BLE001
                print(f"[{name}] clear-cookie fail: {str(e)[:60]}")
            tab.call("Page.reload")
            run_site(name, url, tab, results)
        except Exception as e:  # noqa: BLE001
            results[name] = {"error": str(e)[:120]}
            print(f"[{name}] ERROR {str(e)[:80]}")
        finally:
            try:
                tab.close()
                http_json(CDP, f"/json/close/{ti['id']}", timeout=8)
            except Exception:
                pass
        time.sleep(2)
    json.dump(results, open("run/popup_close_results.json", "w"), ensure_ascii=False, indent=1)
    n_ok = sum(1 for v in results.values() if v.get("closed_ok") or v.get("no_popup"))
    print(f"\nPASS {n_ok}/{len(results)}")


if __name__ == "__main__":
    main()
