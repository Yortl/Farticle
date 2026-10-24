#!/usr/bin/env python3
"""通过局域网 Chrome CDP (已装 BPC 插件) 抓取付费墙文章。
前提: 局域网电脑 Chrome 以以下参数启动:
  chrome --remote-debugging-port=9222 --remote-allow-origins=*

用法:
  python3 cdp_fetch.py 192.168.1.99:9222 "https://www.economist.com/..." [--out article.md]
  python3 cdp_fetch.py 192.168.1.99:9222 --check          # 连通性自检
"""
import argparse
import base64
import json
import re
import socket
import sys
import time
import urllib.request
from pathlib import Path


def http_json(host: str, path: str, timeout: int = 15):
    req = urllib.request.Request(f"http://{host}{path}", headers={"Host": host})
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read())


class Tab:
    """最小 CDP websocket 客户端 (标准库实现, 避免 websocket-client 依赖)。"""

    def __init__(self, ws_url: str):
        # ws://host:port/devtools/page/XXX
        assert ws_url.startswith("ws://"), ws_url
        rest = ws_url[5:]
        hostport, path = rest.split("/", 1)
        self.host, port = hostport.split(":")
        self.sock = socket.create_connection((self.host, int(port)), timeout=30)
        # 手写 HTTP upgrade
        key = base64.b64encode(b"0123456789abcdef").decode()
        self.sock.sendall(
            (f"GET /{path} HTTP/1.1\r\nHost: {hostport}\r\nUpgrade: websocket\r\n"
             f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
             f"Origin: http://{hostport}\r\n\r\n").encode())
        # 读完响应头
        buf = b""
        while b"\r\n\r\n" not in buf:
            buf += self.sock.recv(4096)
        self.buf = buf.split(b"\r\n\r\n", 1)[1]
        self.mid = 0

    def _recv_exact(self, n: int) -> bytes:
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("ws closed")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def _recv_frame(self) -> bytes:
        # ponytail: CDP 大响应会分片 (FIN=0 首帧 opcode=1, 后续 opcode=0), 必须拼完再返回
        b1, b2 = self._recv_exact(2)
        opcode = b1 & 0x0F
        fin = bool(b1 & 0x80)
        ln = b2 & 0x7F
        if ln == 126:
            ln = int.from_bytes(self._recv_exact(2), "big")
        elif ln == 127:
            ln = int.from_bytes(self._recv_exact(8), "big")
        mask = b2 & 0x80
        mask_key = self._recv_exact(4) if mask else b""
        payload = bytearray(self._recv_exact(ln))
        if mask:
            for i in range(len(payload)):
                payload[i] ^= mask_key[i % 4]
        payload = bytes(payload)
        if opcode == 0x8:
            raise ConnectionError("ws close frame")
        if opcode in (0x9, 0xA):  # ping/pong: 忽略
            return self._recv_frame()
        if fin and opcode != 0 and not getattr(self, "_frag", None):
            return payload  # 常规完整帧
        # 分片聚合
        if opcode != 0:  # 首片
            self._frag = {"buf": payload}
        else:  # 续片
            self._frag["buf"] += payload
        if fin:
            out = self._frag["buf"]
            del self._frag
            return out
        return self._recv_frame()

    def call(self, method: str, **params) -> dict:
        self.mid += 1
        msg = json.dumps({"id": self.mid, "method": method, "params": params}).encode()
        mask = bytes(bytearray(b"\x80\x00\x00\x00"))  # dummy, replace per byte below
        frame = bytearray()
        frame.append(0x81)  # FIN + text
        n = len(msg)
        if n < 126:
            frame.append(0x80 | n)
        elif n < 65536:
            frame.append(0x80 | 126)
            frame += n.to_bytes(2, "big")
        else:
            frame.append(0x80 | 127)
            frame += n.to_bytes(8, "big")
        mk = b"\x01\x02\x03\x04"
        frame += mk
        frame += bytes(b ^ mk[i % 4] for i, b in enumerate(msg))
        self.sock.sendall(bytes(frame))
        # 读帧直到拿到对应 id 的响应 (忽略事件)
        deadline = time.time() + 60
        while time.time() < deadline:
            data = json.loads(self._recv_frame().decode("utf-8", "ignore"))
            if data.get("id") == self.mid:
                return data
        raise TimeoutError(method)

    def eval_js(self, expr: str):
        r = self.call("Runtime.evaluate", expression=expr, returnByValue=True, awaitPromise=True)
        return r.get("result", {}).get("result", {}).get("value")

    def close(self):
        self.sock.close()


def _try_click_turnstile(tab) -> bool:
    """检测 Cloudflare Turnstile 勾选框并模拟点击。返回是否找到并点了。
    Turnstile 的 checkbox 在跨域 iframe (challenges.cloudflare.com) 里, 页面 JS 摸不到,
    必须用 CDP 的 DOM 域按坐标点击。"""
    try:
        # 找 turnstile iframe 的位置
        box = tab.eval_js("""
            (() => {
              const f = [...document.querySelectorAll('iframe')]
                .find(f => (f.src||'').includes('challenges.cloudflare.com'));
              if (!f) return null;
              const r = f.getBoundingClientRect();
              return {x: r.x + r.width/2, y: r.y + r.height/2, w: r.width, h: r.height};
            })()
        """)
        if not box or box.get("w", 0) < 20:
            return False
        # checkbox 位于 iframe 内偏左位置 (整框中心偏左 ~70px 处)
        cx, cy = box["x"] + min(70, box["w"] / 2), box["y"] + box["h"] / 2
        tab.call("Input.dispatchMouseEvent", type="mouseMoved", x=cx, y=cy)
        time.sleep(0.4)
        tab.call("Input.dispatchMouseEvent", type="mousePressed", x=cx, y=cy,
                 button="left", clickCount=1)
        tab.call("Input.dispatchMouseEvent", type="mouseReleased", x=cx, y=cy,
                 button="left", clickCount=1)
        print(f"[cdp] turnstile clicked at ({cx:.0f},{cy:.0f})", file=sys.stderr)
        return True
    except Exception as e:  # noqa: BLE001
        print(f"[cdp] turnstile click fail: {str(e)[:60]}", file=sys.stderr)
        return False


def _click_sourcepoint(tab) -> bool:
    """Sourcepoint CMP (Guardian/BBC/Atlantic): 弹窗是全屏 OOPIF iframe, 顶文档 JS 摸不到,
    只能坐标点击。Accept 优先 — Guardian 的 "Reject all and subscribe" 会跳转 Ad-Lite 结账页。
    按钮行位于对话框底部 (实测 Guardian: y≈738, Accept 左 Reject 右)。"""
    try:
        info = tab.eval_js("""
            (() => {
              const c = document.querySelector('[id^="sp_message_container"]');
              if (!c) return null;
              const r = c.getBoundingClientRect();
              if (r.height < 50) return null;
              const f = c.querySelector('iframe');
              const fr = f ? f.getBoundingClientRect() : r;
              return JSON.stringify({x: fr.x, y: fr.y, w: fr.width, h: fr.height});
            })()
        """)
        if not info:
            return False
        fr = json.loads(info)
        if fr["w"] < 300 or fr["h"] < 200:  # 非全屏弹窗不在此处理
            return False
        # 截图找深蓝色按钮位置太重; 用实测布局: 中央对话框底部一行两钮, 高度约 74% 处
        bx, by = fr["x"] + fr["w"] * 0.40, fr["y"] + fr["h"] * 0.738  # Accept all (左钮)
        for act in ("mouseMoved", "mousePressed", "mouseReleased"):
            tab.call("Input.dispatchMouseEvent", type=act, x=int(bx), y=int(by),
                     button="left", clickCount=1)
        print(f"[cdp] sourcepoint clicked ({int(bx)},{int(by)})", file=sys.stderr)
        time.sleep(3)
        return True
    except Exception as e:  # noqa: BLE001
        print(f"[cdp] sourcepoint fail: {str(e)[:50]}", file=sys.stderr)
        return False


def _click_consent(tab) -> bool:
    """GDPR cookie consent: 找 Reject All / I Accept 按钮点掉 (wapo 欧区出口必弹,
    不点掉 readability 只能提出 consent 文本)。幂等: 没弹窗就不点。"""
    try:
        btn = tab.eval_js("""
            (() => {
              const vis = x => { const r = x.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
              const els = [
                document.getElementById('onetrust-reject-all-handler'),
                document.getElementById('onetrust-reject-btn-handler'),
                document.getElementById('onetrust-accept-btn-handler'),
                ...[...document.querySelectorAll('button')].filter(x =>
                  /^(reject all|accept all( cookies)?|i accept|同意|拒绝全部)$/i.test((x.textContent||'').trim()))
              ].filter(x => x && vis(x));
              // 优先 Reject (隐私友好), 无则 Accept
              const b = els.find(x => /reject|拒绝/i.test(x.textContent || x.id))
                      || els.find(x => /accept|同意/i.test(x.textContent || x.id));
              if (!b) return null;
              const r = b.getBoundingClientRect();
              return {x: r.x + r.width/2, y: r.y + r.height/2};
            })()
        """)
        if not btn:
            return False
        tab.call("Input.dispatchMouseEvent", type="mouseMoved", x=btn["x"], y=btn["y"])
        time.sleep(0.4)
        tab.call("Input.dispatchMouseEvent", type="mousePressed", x=btn["x"], y=btn["y"],
                 button="left", clickCount=1)
        tab.call("Input.dispatchMouseEvent", type="mouseReleased", x=btn["x"], y=btn["y"],
                 button="left", clickCount=1)
        print(f"[cdp] consent clicked ({btn['x']:.0f},{btn['y']:.0f})", file=sys.stderr)
        time.sleep(3)
        return True
    except Exception as e:  # noqa: BLE001
        print(f"[cdp] consent fail: {str(e)[:50]}", file=sys.stderr)
        return False


def fetch(host: str, url: str, wait_s: float = 6.0, stealth: bool = False) -> str:
    """开新 tab 加载 url, 等 BPC 处理完, 返回页面 innerText。
    stealth=False (默认): 真实浏览器零注入 —— 任何 UA 覆盖/JS 补丁/主动 reload 都是 DataDome
    的机器人特征 (UA 与平台指纹矛盾、webdriver getter 被重定义、瞬时滚动), 千万别开。"""
    # /json/new 在新版本 Chrome 需要 PUT
    try:
        tab_info = http_json(host, f"/json/new?{urllib.parse.quote(url, safe='')}")
    except Exception:
        req = urllib.request.Request(f"http://{host}/json/new?{urllib.parse.quote(url, safe='')}",
                                     method="PUT")
        tab_info = json.loads(urllib.request.urlopen(req, timeout=15).read())
    ws = tab_info["webSocketDebuggerUrl"]
    tab_id = tab_info["id"]
    tab = Tab(ws)
    def close_chrome_tab():
        try:
            http_json(host, f"/json/close/{tab_id}", timeout=8)
        except Exception:
            pass
    try:
        time.sleep(wait_s)  # 等 BPC 注入/跳转; 不 reload、不注入任何脚本
        # Cloudflare Turnstile 勾选循环: 挑战页标题 "Just a moment..."
        for attempt in range(4):
            title = (tab.eval_js("document.title") or "").lower()
            has_cf = tab.eval_js(
                "!![...document.querySelectorAll('iframe')]"
                ".find(f => (f.src||'').includes('challenges.cloudflare.com'))")
            if not any(k in title for k in ("just a moment", "attention", "请稍候", "checking", "security")) and not has_cf:
                break
            print(f"[cdp] cloudflare challenge (round {attempt+1}), trying checkbox…",
                  file=sys.stderr)
            time.sleep(4)  # 等勾选框渲染
            _try_click_turnstile(tab)
            time.sleep(8)  # 等验证通过+跳转
        # interstitial 轮询: Atlantic/CF 的 "请稍候" 墙随机出现, reload 即过 (实测 1 次 reload 出正文)
        for attempt in range(5):
            title = (tab.eval_js("document.title") or "")
            blocked = ("请稍候" in title or "moment" in title.lower()
                       or "attention" in title.lower() or title in ("", "economist.com"))
            # GDPR cookie consent: 顶文档弹窗 (wapo OneTrust 等) 点 Reject All;
            # Sourcepoint 全屏 iframe 弹窗 (Guardian/BBC/Atlantic) 走坐标点 Accept
            _click_consent(tab)
            _click_sourcepoint(tab)
            if not blocked:
                break
            print(f"[cdp] interstitial round {attempt+1}, reloading…", file=sys.stderr)
            tab.call("Page.reload")
            time.sleep(8)
        cur = tab.eval_js("location.href")
        title = (tab.eval_js("document.title") or "").split(" - ")[0].split(" | ")[0].strip()
        html = tab.eval_js("document.documentElement.outerHTML") or ""
        print(f"[cdp] final_url={cur}\n[cdp] title={title}\n[cdp] html_chars={len(html)}",
              file=sys.stderr)
        return html, title
    finally:
        tab.close()
        close_chrome_tab()


# 精确噪音行 (源自入库坏样本实测): 导航/consent/订阅钩子/图注壳, 整行字面量匹配
_JUNK = {
    "skip to main content", "skip to navigation", "print subscriptions", "search jobs",
    "sign in", "sign out", "newsletters", "share", "show more",
    "sign in: it's quick and easy", "it's quick and easy",
    "view image in fullscreen", "view illustrations", "eur",
    "skip to content", "site navigation", "comment", "video", "games", "society",
    "women", "men", "love & sex", "already a subscriber?", "log in to hide ads.",
    "books newsletter.", "sign up to", "the recap", "as news breaks",
    "tech news alerts", "news alerts", "breaking news alerts", "further reading",
    "back to the top",
    "the guardian - back to home", "the guardian", "news", "opinion", "sport", "culture",
    "lifestyle", "environment", "climate crisis", "wildlife", "energy", "pollution",
    "cookie policy", "cookie settings", "cookie notice", "cookies used", "your privacy",
    "iab and google vendors", "article information", "prefer the guardian on google",
    "subscribe", "subscribe now", "subscribe today", "become a subscriber",
    "subscriber benefits", "newsletters & alerts", "sign up", "sign up now",
    "enter your email", "when needed", "get the most important news as it breaks",
    "sign in or create your guardian account to join the discussion",
    "accept cookies to play video", "subscribe to support independent journalism",
    "view and manage all cookie consent preferences", "manage settingsrejectaccept",
    "skip past newsletter promotion", "after newsletter promotion", "free newsletter |",
    "author,", "role,", "reporting from,", "published", "read more", "continue reading",
}
_CREDITS = {"reuters", "getty images", "afp", "ap", "bloomberg", "xinhua", "epa-efe", "getty"}
_COOKIE_VOCAB = ("cookie", "consent", "vendor")
_NAV_VOCAB = {"news", "opinion", "sport", "culture", "lifestyle", "environment",
              "wildlife", "energy", "pollution", "climate", "politics", "world"}


def _clean_lines(paras: list[str]) -> list[str]:
    """段级清洗: 噪音行丢弃 + 相邻去重 + 断句/首字下沉合并。两条提取路径共用。"""
    paras = [p.strip() for p in paras if p.strip()]
    # 旧库存量形态: 斜体书名把一句拆成 "stories," | "A Wooded Shore" | ". The more..." 三段 — 预合并
    pre: list[str] = []
    for j, l in enumerate(paras):
        if pre and pre[-1][-1:] not in ".!?:;”’\"'":
            nxt = paras[j + 1] if j + 1 < len(paras) else ""
            if l[:2] == ". " or l[:1] in {",", "—"}:
                pre[-1] += l  # ". xxx"/"—xxx"/", xxx" 是前句延续 (书名/斜体后正文)
                continue
            if l[:1].isupper() and l[-1:] not in ".!?:;," and (nxt.startswith(". ") or nxt[:1] in {",", "—"}):
                pre[-1] = pre[-1].rstrip() + " " + l  # 无标点书名行, 下段以 . / — 续句
                continue
        pre.append(l)
    paras = pre
    out: list[str] = []
    for raw in paras:
        l = raw.strip()
        if not l:
            continue
        low = l.lower()
        if (low in _JUNK or low in _CREDITS
                or set(l) <= set("_-–—|•·… ")
                or low.startswith(("图像来源，", "图像加注文字，"))):
            continue
        if sum(c in low for c in _COOKIE_VOCAB) >= 3:  # consent 横幅整段 (cookie/consent/vendor 反复出现)
            continue
        if sum(t in _NAV_VOCAB for t in low.replace(",", " ").split()) >= 4:  # 导航容器串 (News Opinion Sport ...)
            continue
        if out and l == out[-1]:  # 相邻重复 (分隔线/重复图注)
            continue
        if l == "." and out and out[-1][-1:].isalnum():  # 句末句点被拆成孤行 → 归还前段
            out[-1] += "."
            continue
        if low == "newsletter" and out:  # WaPo/Newsweek 页脚起点: NEWSLETTER → 页脚全截
            break
        if low in {"most viewed", "reuse this content", "reusing this content",
                   "explore more on these topics"}:
            break  # 页面尾部标记: 其后全是相关推荐/页脚, 整体截断
        if low in _JUNK or low in _CREDITS:  # 大写变体 (SKIP TO CONTENT 等) 在此兜底复查
            continue
        if out and out[-1][-1] not in ".,!?:;)]”’\"'" and l[:1].islower():
            out[-1] = out[-1].rstrip() + " " + l  # 残余拆行 (<br> 等): 词边界, 空格接回
            continue
        if not re.search(r"[A-Za-z\u4e00-\u9fff]{3,}", l):
            continue  # 纯符号/数字/≤2字母碎片 (时间戳 0:00、倍速 1.0x、−、图片信用行)
        if re.match(r"^(photos?|images?|photographs?):\s", low) or re.match(r"^\S+://\S+$", l):
            continue  # 图注信用 (Photograph: ...) / 裸链接行
        if re.match(r"^\|?\s*view illustrations", low) \
                or re.match(r"^\((image (courtesy|credit)|photo|image)\b.*\)$", low) \
                or re.match(r"^[a-z. ’']+ for$", low):
            continue  # 图注壳行 / 括号图源 (Image Courtesy of ...) / 半截摄影信用 (Jason Andrew for)
        if re.match(r"^(we (will also use other |and our partners )|click 'got it' |"
                    r"you can (control how your data|adjust your cookie|accept, reject or manage|also opt out)|"
                    r"your consent choices |these cookies and sdks |cookies, device or similar online |"
                    r"data collected under this category |store and/or access information on a device|"
                    r"the choices you make regarding |your data can be used to |"
                    r"certain information \(like an ip |privacy policy to learn more\.?$)", low):
            continue  # consent 法务模板句 (CNBC 等横幅正文被 readability 误收; 丢弃后词数骤减触发服务端重试)
        if re.match(r"^rates?[:\s]", low) or re.match(r"^[a-z. ’']+ for (the )?(atlantic|new york times)$", low):
            continue  # NatGeo 酒店价格行 / "摄影: X for <刊名>" 信用行
        if re.match(r"^(sign up (for|to) |from the \w+ \d{4} issue:|explore the .* issue$)", low) \
                or low in {"get in touch", "view more", "the atlantic", "the new york times",
                           "the washington post", "the economist", "subscribe here.",
                           "first thing is delivered to thousands of inboxes every weekday. if you’re not already signed up,"}:
            continue  # 订阅推销 / 期刊互推盒 / 摄影+刊名残行
        words = l.split()
        if len(words) == 1 and sum(c.isupper() for c in l[1:]) >= 2 and l[0].isupper():
            continue  # 泄漏的 CSS/JS 类名 (AccordionItemContainerButton), 正常英文单词不会内部多驼峰
        out.append(l)
    return out


def extract_readability(html: str) -> str | None:
    """readability 提正文。unwrap 行内标签+smooth 后 get_text("\\n"): 块级边界成行,
    行内 <a>/<em>/<span> (含首字下沉 T|heze) 永不被拆行 — 拆行只可能来自 <br>。"""
    try:
        from readability import Document
        from bs4 import BeautifulSoup
        # OneTrust 偏好中心是 73 万字符的大文本块, readability 评分会赢过真正文 — 进评分前整树摘除
        if "onetrust-consent-sdk" in html:
            pre = BeautifulSoup(html, "lxml")
            for el in pre.find_all(id=lambda v: v and v.startswith("onetrust")):
                el.decompose()
            html = str(pre)
        doc = Document(html)
        soup = BeautifulSoup(doc.summary(), "lxml")
        # summary 内残留的 OneTrust 碎片按类名剔除 (ot-*)
        def _ot(cls) -> bool:
            return any(c.startswith("ot-") or "onetrust" in c for c in (cls or []))
        for el in soup.find_all(class_=_ot):
            el.decompose()
        for tag in soup(["a", "em", "strong", "span", "i", "b", "u", "small", "sup", "sub"]):
            tag.unwrap()  # 行内标签的子孙文本并入父行, 保留词边界
        soup.smooth()  # 合并相邻 NavigableString, 防 get_text 在节点边界插分隔
        text = soup.get_text("\n")
        paras = [p.replace("\xa0", " ").strip() for p in text.split("\n")]
        paras = _clean_lines(paras)
        return "\n\n".join(paras) or None
    except Exception as e:  # noqa: BLE001
        print(f"[cdp] readability fail: {e}", file=sys.stderr)
        return None


def to_markdown(text: str, url: str) -> str:
    lines = [l.strip() for l in text.split("\n")]
    # 去连续空行
    out, prev_blank = [], False
    for l in lines:
        if not l:
            if prev_blank:
                continue
            prev_blank = True
        else:
            prev_blank = False
        out.append(l)
    return f"source: {url}\nfetched_via: LAN Chrome CDP + BPC\ndate: {time.strftime('%Y-%m-%d')}\n\n" + "\n".join(out)


def main():
    import urllib.parse
    ap = argparse.ArgumentParser()
    ap.add_argument("host", help="如 192.168.1.99:9222")
    ap.add_argument("url", nargs="?", help="文章 URL")
    ap.add_argument("--out", help="输出 md 路径")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--wait", type=float, default=6.0)
    args = ap.parse_args()

    if args.check:
        tabs = http_json(args.host, "/json/version")
        print("[check] Chrome:", tabs.get("Browser"))
        ext = [t for t in http_json(args.host, "/json") if t["type"] == "page"]
        print(f"[check] tabs: {len(ext)}")
        return

    if not args.url:
        ap.error("需要 url 或 --check")
    html, title = fetch(args.host, args.url, args.wait)
    text = extract_readability(html) or ""
    if len(text.split()) < 100:
        print("[warn] 正文少于100词, BPC 可能未生效或页面被墙", file=sys.stderr)
    md = to_markdown(text, args.url)
    if args.out:
        Path(args.out).write_text(md, encoding="utf-8")
        print(f"[done] {args.out}", file=sys.stderr)
    else:
        print(md)


if __name__ == "__main__":
    main()
