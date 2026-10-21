#!/usr/bin/env python3
"""FastAPI 服务: CDP+BPC 文章抓取 + Markdown 展示前端。
API:
  GET  /api/health          Chrome CDP 连通性
  POST /api/fetch           {url}        -> {id, ...}      单篇抓取 (后台线程)
  POST /api/discover        {topic, outlet, days} -> 任务id, 搜索+逐篇抓
  GET  /api/jobs            任务列表
  GET  /api/jobs/{id}       任务状态+文章列表
  GET  /api/articles        已抓文章库
  GET  /api/articles/{aid}  单篇 markdown 正文
  DELETE /api/articles/{aid}
存储: articles_store/*.md (YAML front matter), jobs_store/*.json
启动: python3 server.py [--port 8900] [--cdp 127.0.0.1:9222]
"""
import argparse
import json
import re
import threading
import time
import uuid
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from cdp_fetch import fetch, extract_readability, http_json
from discover import bing_news, section_discover

ROOT = Path(__file__).parent
STORE = ROOT / "web_store"
ART = STORE / "articles"
JOBS = STORE / "jobs"
for p in (ART, JOBS):
    p.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Exam English Fetcher")
CDP_HOST = "127.0.0.1:9222"
_jobs_lock = threading.Lock()


def fm_escape(s: str) -> str:
    return s.replace("\n", " ").strip()


def save_article(title: str, source: str, url: str, words: int, text: str) -> str:
    # 按 url 幂等: 已抓过直接复用 (多源/重跑防重复)
    for f in ART.glob("*.md"):
        if f"\nurl: {url}\n" in f.read_text(encoding="utf-8"):
            return f.stem
    aid = uuid.uuid4().hex[:10]
    fm = (f"---\nid: {aid}\ntitle: {fm_escape(title)}\nsource: {source}\nurl: {url}\n"
          f"words: {words}\nfetched: {time.strftime('%Y-%m-%d %H:%M')}\n---\n\n")
    (ART / f"{aid}.md").write_text(fm + text + "\n", encoding="utf-8")
    return aid


def load_article(aid: str) -> dict | None:
    f = ART / f"{aid}.md"
    if not f.exists():
        return None
    raw = f.read_text(encoding="utf-8")
    m = re.match(r"---\n(.*?)\n---\n\n(.*)", raw, re.S)
    meta = dict(re.findall(r"^(\w+): (.*)$", m.group(1), re.M)) if m else {}
    return {**meta, "id": aid, "body": m.group(2) if m else raw}


def list_articles() -> list[dict]:
    out = []
    for f in sorted(ART.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True):
        a = load_article(f.stem)
        out.append({k: v for k, v in a.items() if k != "body"})
    return out


def job_write(jid: str, data: dict):
    with _jobs_lock:
        (JOBS / f"{jid}.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def job_read(jid: str) -> dict | None:
    f = JOBS / f"{jid}.json"
    return json.loads(f.read_text()) if f.exists() else None


def slugify(s: str, maxlen: int = 60) -> str:
    s = re.sub(r"[^\w\s-]", "", s.lower())
    return re.sub(r"[\s_]+", "-", s).strip("-")[:maxlen] or "untitled"


def run_job(jid: str, kind: str, url: str = "", topic: str = "", outlet: str = "", per: int = 3, days: int = 30,
             min_words: int = 120, max_words: int = 100000):
    data = job_read(jid)
    try:
        if kind == "fetch":
            text, title = "", ""
            # 两次尝试: 第1次可能只抓到 consent 弹窗层 (点了但正文未渲染), 第2次 cookie 已存
            for attempt in range(2):
                html, title = fetch(CDP_HOST, url, wait_s=18)
                text = extract_readability(html) or ""
                if len(text.split()) >= 80:
                    break
                time.sleep(3)
            words = len(text.split())
            if words < 80:
                raise RuntimeError(f"正文仅 {words} 词, 可能被墙/BPC未生效 (看浏览器 tab)")
            if not (min_words <= words <= max_words):
                raise RuntimeError(f"正文 {words} 词, 不在设定范围 [{min_words}, {max_words}]")
            title = title[:140] or url
            aid = save_article(title, url.split("/")[2] if "//" in url else url, url, words, text)
            data.update(status="done", articles=[{"id": aid, "title": title, "words": words}])
        else:  # discover: 多源
            got, seen_titles, seen_urls = [], set(), set()
            outlets = [o.strip() for o in outlet.split(",") if o.strip()]
            total = len(outlets) * per
            done_n = 0
            for outlet_name in outlets:
                cfg = OUTLETS.get(outlet_name) or next(
                    (v for v in OUTLETS.values() if v["domain"] == outlet_name), None)
                if not cfg:
                    data.setdefault("errors", []).append(f"未知媒体 {outlet_name}")
                    continue
                arts = bing_news(topic, cfg["domain"], since_days=days)[: max(per * 3, 8)]
                if len(arts) < per and cfg.get("sections"):
                    # bing 不收录的站 (bjreview 等): 走栏目列表页发现
                    arts += section_discover(topic, cfg, cdp_host=CDP_HOST)
                got_this = 0
                for a in arts:
                    if got_this >= per or done_n >= total:
                        break
                    tok = re.sub(r"[^\w]+", " ", a["title"].lower()).strip()
                    if tok in seen_titles or a["url"] in seen_urls:
                        continue
                    seen_urls.add(a["url"])
                    data["status"] = "running"
                    data["progress"] = f"[{outlet_name}] {got_this+1}/{per}: {a['title'][:55]}"
                    job_write(jid, data)
                    try:
                        # freedium 镜像源 (economist): 原文页只有摘要, 用镜像拿全文
                        target = (cfg.get("mirror", "") + a["url"]) if cfg.get("mirror") else a["url"]
                        text = ""
                        for attempt in range(2):
                            html, ptitle = fetch(CDP_HOST, target, wait_s=15)
                            text = extract_readability(html) or ""
                            if min_words <= len(text.split()) <= max_words:
                                break
                            time.sleep(3)
                        words = len(text.split())
                        if not (min_words <= words <= max_words):
                            why = "过短" if words < min_words else "过长"
                            data.setdefault("skipped", []).append(f"{outlet_name} ({words}词{why}): {a['title'][:45]}")
                            continue
                        aid = save_article(ptitle or a["title"], cfg["domain"], a["url"], words, text)
                        got.append({"id": aid, "outlet": outlet_name, "title": a["title"],
                                    "words": words, "url": a["url"]})
                        seen_titles.add(tok)
                        got_this += 1
                        done_n += 1
                    except Exception as e:  # noqa: BLE001
                        data.setdefault("errors", []).append(f"{outlet_name}: {a['title'][:45]}: {str(e)[:55]}")
                    time.sleep(2)
                if done_n >= total:
                    break
            data.update(status="done", articles=got)
        data["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
    except Exception as e:  # noqa: BLE001
        data.update(status="error", error=str(e)[:300])
    job_write(jid, data)


from outlets import OUTLETS  # noqa: E402

OUTLET_LABELS = {
    "atlantic": "大西洋月刊", "wapo": "华盛顿邮报", "newscientist": "新科学家",
    "guardian": "卫报", "bbc": "BBC",
    "time": "时代周刊", "newsweek": "新闻周刊", "sciam": "科学美国人",
    "psychtoday": "今日心理学", "natgeo": "国家地理",
    "wired": "Wired", "cnbc": "CNBC", "npr": "NPR", "vox": "Vox",
    "arstechnica": "Ars Technica",
    "economist": "经济学人", "nytimes": "纽约时报",
    "chinadaily": "中国日报", "bjreview": "北京周报",
}


@app.get("/api/outlets")
def api_outlets():
    return {k: {"label": OUTLET_LABELS.get(k, k), "tier": v["tier"], "domain": v["domain"],
                "enabled": v["enabled"]}
            for k, v in OUTLETS.items() if v["enabled"]}


class FetchReq(BaseModel):
    url: str


class DiscoverReq(BaseModel):
    topic: str
    outlets: list[str] = ["atlantic"]
    per_outlet: int = 2
    days: int = 30
    min_words: int = 120
    max_words: int = 100000


@app.get("/api/health")
def health():
    try:
        v = http_json(CDP_HOST, "/json/version", timeout=8)
        return {"ok": True, "chrome": v["Browser"]}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:120]}


@app.post("/api/fetch")
def api_fetch(req: FetchReq):
    jid = uuid.uuid4().hex[:8]
    data = {"id": jid, "kind": "fetch", "status": "running", "url": req.url,
            "created": time.strftime("%Y-%m-%d %H:%M:%S"), "articles": []}
    job_write(jid, data)
    threading.Thread(target=run_job, args=(jid, "fetch", req.url), daemon=True).start()
    return data


@app.post("/api/discover")
def api_discover(req: DiscoverReq):
    jid = uuid.uuid4().hex[:8]
    data = {"id": jid, "kind": "discover", "status": "running", "topic": req.topic,
            "outlets": req.outlets, "per_outlet": req.per_outlet,
            "created": time.strftime("%Y-%m-%d %H:%M:%S"),
            "articles": [], "progress": "searching..."}
    job_write(jid, data)
    threading.Thread(target=run_job,
                     args=(jid, "discover", "", req.topic, ",".join(req.outlets), req.per_outlet,
                           req.days, req.min_words, req.max_words),
                     daemon=True).start()
    return data


@app.get("/api/jobs")
def api_jobs():
    out = []
    for f in sorted(JOBS.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:30]:
        out.append(json.loads(f.read_text()))
    return out


@app.get("/api/jobs/{jid}")
def api_job(jid: str):
    j = job_read(jid)
    if not j:
        raise HTTPException(404)
    return j


@app.get("/api/articles")
def api_articles():
    return list_articles()


@app.get("/api/articles/{aid}")
def api_article(aid: str):
    a = load_article(aid)
    if not a:
        raise HTTPException(404)
    return a


@app.delete("/api/articles/{aid}")
def api_del(aid: str):
    f = ART / f"{aid}.md"
    if f.exists():
        f.unlink()
    return {"deleted": aid}


@app.get("/")
def index():
    return FileResponse(ROOT / "web" / "index.html")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8900)
    ap.add_argument("--cdp", default="127.0.0.1:9222")
    args = ap.parse_args()
    CDP_HOST = args.cdp
    uvicorn.run(app, host="0.0.0.0", port=args.port, log_level="warning")
