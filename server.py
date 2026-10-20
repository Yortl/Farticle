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

from cdp_fetch import fetch, http_json
from discover import bing_news

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


def run_job(jid: str, kind: str, url: str = "", topic: str = "", outlet: str = "", per: int = 3, days: int = 30):
    data = job_read(jid)
    try:
        if kind == "fetch":
            text, title = fetch(CDP_HOST, url, wait_s=18)
            words = len(text.split())
            if words < 80:
                raise RuntimeError(f"正文仅 {words} 词, 可能被墙/BPC未生效 (看浏览器 tab)")
            title = title[:140] or url
            aid = save_article(title, url.split("/")[2] if "//" in url else url, url, words, text)
            data.update(status="done", articles=[{"id": aid, "title": title, "words": words}])
        else:  # discover
            cfg = OUTLETS.get(outlet) or next(
                (v for v in OUTLETS.values() if v["domain"] == outlet), None)
            if not cfg:
                raise RuntimeError(f"未知媒体 {outlet}")
            arts = bing_news(topic, cfg["domain"], since_days=days)[: max(per * 3, 8)]
            data["total_candidates"] = len(arts)
            got = []
            for a in arts:
                if len(got) >= per:
                    break
                data["status"] = "running"
                data["progress"] = f"{len(got)}/{per}: {a['title'][:60]}"
                job_write(jid, data)
                try:
                    text, ptitle = fetch(CDP_HOST, a["url"], wait_s=15)
                    words = len(text.split())
                    if words < 120:
                        data.setdefault("skipped", []).append(a["title"][:60])
                        continue
                    aid = save_article(ptitle or a["title"], cfg["domain"], a["url"], words, text)
                    got.append({"id": aid, "title": a["title"], "words": words, "url": a["url"]})
                except Exception as e:  # noqa: BLE001
                    data.setdefault("errors", []).append(f"{a['title'][:50]}: {str(e)[:60]}")
                time.sleep(2)
            data.update(status="done", articles=got)
        data["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
    except Exception as e:  # noqa: BLE001
        data.update(status="error", error=str(e)[:300])
    job_write(jid, data)


from outlets import OUTLETS  # noqa: E402

OUTLET_LABELS = {
    "atlantic": "大西洋月刊", "wapo": "华盛顿邮报", "newscientist": "新科学家",
    "guardian": "卫报", "csmonitor": "基督教科学箴言报", "bbc": "BBC",
    "time": "时代周刊", "newsweek": "新闻周刊", "sciam": "科学美国人",
    "psychtoday": "今日心理学", "natgeo": "国家地理",
    "wired": "Wired", "cnbc": "CNBC", "npr": "NPR", "vox": "Vox",
    "arstechnica": "Ars Technica",
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
    outlet: str = "atlantic"
    per: int = 3
    days: int = 30


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
            "outlet": req.outlet, "created": time.strftime("%Y-%m-%d %H:%M:%S"),
            "articles": [], "progress": "searching..."}
    job_write(jid, data)
    threading.Thread(target=run_job,
                     args=(jid, "discover", "", req.topic, req.outlet, req.per, req.days),
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
