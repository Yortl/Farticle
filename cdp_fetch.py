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
    tab = Tab(ws)
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


def extract_readability(html: str) -> str | None:
    """readability 算法提正文 (HTML→纯文本, 段落分行)。"""
    try:
        from readability import Document
        from bs4 import BeautifulSoup
        doc = Document(html)
        text = BeautifulSoup(doc.summary(), "lxml").get_text("\n")
        lines = [l.strip() for l in text.split("\n")]
        # 修首字下沉拆行: "T" + "hese days..." → "These days..."
        merged = []
        i = 0
        while i < len(lines):
            l = lines[i]
            if (len(l) <= 2 and l and i + 1 < len(lines)
                    and lines[i+1] and lines[i+1][:1].islower()):
                merged.append(l + lines[i+1])
                i += 2
                continue
            merged.append(l)
            i += 1
        out = []
        for l in merged:
            if not l and out and not out[-1]:
                continue
            out.append(l)
        return "\n".join(out).strip() or None
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
