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
import difflib
import json
import re
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from cdp_fetch import fetch, extract_readability, http_json
from discover import discover, section_discover
from run import load_decoder
import htmldate
import trafilatura

ROOT = Path(__file__).parent
STORE = ROOT / "web_store"
ART = STORE / "articles"
JOBS = STORE / "jobs"
for p in (ART, JOBS):
    p.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Exam English Fetcher")
CDP_HOST = "127.0.0.1:9222"
_jobs_lock = threading.Lock()
# 取消标记: job_id -> True (run_job 循环里轮询检查)
_cancelled: set[str] = set()


class JobCancelled(Exception):
    pass


def check_cancel(jid: str):
    """长循环里每篇/每轮调用一次; 被取消则抛异常走统一收尾。"""
    if jid in _cancelled:
        raise JobCancelled()


def fm_escape(s: str) -> str:
    return s.replace("\n", " ").strip()


def ok_words(n: int, lo: int | None, hi: int | None) -> bool:
    """词数是否合格: lo/hi 为 None 表示不限定范围; 天然下限 80 词防导航/摘要类垃圾正文。"""
    if n < 80:
        return False
    return True if lo is None or hi is None else lo <= n <= hi


def extract_date(html: str, url: str = "", fallback: str = "") -> str:
    """提取文章发布日期, 统一返回 YYYY-MM-DD 或空字符串。"""
    if html:
        try:
            d = htmldate.find_date(html, url=url, original_date=True)
            if d and re.match(r"^\d{4}-\d{2}-\d{2}$", d):
                return d
        except Exception:
            pass
        try:
            meta = trafilatura.extract_metadata(html)
            if meta and meta.date and re.match(r"^\d{4}-\d{2}-\d{2}$", meta.date):
                return meta.date
        except Exception:
            pass
    if fallback:
        m = re.search(r"(\d{4}-\d{2}-\d{2})", fallback)
        if m:
            return m.group(1)
    if url:
        m = re.search(r"/(20\d{2})[-/](0[1-9]|1[0-2])[-/](0[1-9]|[12]\d|3[01])", url)
        if m:
            return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
        _months = {"jan":"01","feb":"02","mar":"03","apr":"04","may":"05","jun":"06",
                   "jul":"07","aug":"08","sep":"09","oct":"10","nov":"11","dec":"12"}
        m2 = re.search(r"/(20\d{2})/(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)/(\d{1,2})", url.lower())
        if m2:
            return f"{m2.group(1)}-{_months[m2.group(2)]}-{int(m2.group(3)):02d}"
    return ""


def save_article(title: str, source: str, url: str, words: int, text: str, pub_date: str = "") -> tuple[str, bool]:
    """按 url 幂等: 已抓过直接复用 (多源/重跑防重复)。返回 (id, 是否本次新入库)"""
    for f in ART.glob("*.md"):
        if f"\nurl: {url}\n" in f.read_text(encoding="utf-8"):
            return f.stem, False
    aid = uuid.uuid4().hex[:10]
    date_line = f"date: {pub_date}\n" if pub_date else ""
    fm = (f"---\nid: {aid}\ntitle: {fm_escape(title)}\nsource: {source}\nurl: {url}\n"
          f"words: {words}\n{date_line}fetched: {time.strftime('%Y-%m-%d %H:%M')}\n---\n\n")
    (ART / f"{aid}.md").write_text(fm + text + "\n", encoding="utf-8")
    return aid, True


def load_article(aid: str) -> dict | None:
    f = ART / f"{aid}.md"
    if not f.exists():
        return None
    raw = f.read_text(encoding="utf-8")
    m = re.match(r"---\n(.*?)\n---\n\n(.*)", raw, re.S)
    meta = dict(re.findall(r"^(\w+): (.*)$", m.group(1), re.M)) if m else {}
    if "date" not in meta and "url" in meta:
        d = extract_date("", url=meta["url"])
        if d:
            meta["date"] = d
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


def epub_find(title: str) -> tuple[str, str] | None:
    """economist 单篇失败兜底: 从整刊 epub 提取库里按标题相似度搜文章, 返回 (正文, 提取日期)。
    搜不到/无存量期 → None。epub 目录由 economist_epub.py 维护, 这里只读。"""
    issues_dir = ROOT / "articles" / "economist-issues"
    if not issues_dir.exists():
        return None
    # 新期优先 (epub 每周六更新, 单篇抓的通常是近几天文章)
    best = (0.0, None)
    for idx in sorted(issues_dir.glob("*/index.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        for it in json.loads(idx.read_text()):
            r = difflib.SequenceMatcher(None, title.lower(), it["title"].lower()).ratio()
            if r > best[0]:
                best = (r, idx.parent / it["file"])
        if best[0] > 0.92:
            break  # ponytail: 找到高置信即停, 不扫全部期; 文章跨期重发极罕见
    if best[0] < 0.6 or not best[1] or not best[1].exists():
        return None
    raw = best[1].read_text(encoding="utf-8")
    m = re.match(r"---\n(.*?)\n---\n\n(.*)", raw, re.S)
    if not m:
        return None
    meta = dict(re.findall(r"^(\w+): (.*)$", m.group(1), re.M))
    if len(m.group(2).split()) < 80:
        return None
    return m.group(2).strip(), meta.get("extracted", meta.get("issue", ""))


def run_job(jid: str, kind: str, url: str = "", topic: str = "", outlet: str = "", per: int = 3, days: int = 30,
             min_words: int | None = None, max_words: int | None = None, pub_year: int | None = None):
    data = job_read(jid)
    try:
        if kind == "fetch":
            text, title, pub_date = "", "", ""
            # 两次尝试: 第1次可能只抓到 consent 弹窗层 (点了但正文未渲染), 第2次 cookie 已存
            for attempt in range(2):
                check_cancel(jid)
                html, title = fetch(CDP_HOST, url, wait_s=18)
                text = extract_readability(html) or ""
                pub_date = extract_date(html, url=url)
                if ok_words(len(text.split()), min_words, max_words):
                    break
                time.sleep(3)
            words = len(text.split())
            is_econ = "economist.com" in url
            if words < 80 or (is_econ and not ok_words(words, min_words, max_words)):
                # economist 单篇失败兜底: freedium 镜像已死, 从整刊 epub 提取库按标题搜
                hit = epub_find(title) if is_econ else None
                if hit:
                    text, pub_date = hit
                    words = len(text.split())
                    data["progress"] = f"镜像失败, 从整刊 epub 命中: {title[:45]}"
                    job_write(jid, data)
            if words < 80:
                raise RuntimeError(f"正文仅 {words} 词, 可能被墙/BPC未生效 (看浏览器 tab)")
            if not ok_words(words, min_words, max_words):
                raise RuntimeError(f"正文 {words} 词, 不在设定范围 [{min_words}, {max_words}]")
            if pub_year is not None:
                art_year = int(pub_date[:4]) if (pub_date and len(pub_date) >= 4 and pub_date[:4].isdigit()) else None
                if art_year != pub_year:
                    raise RuntimeError(f"文章发布年份为 {art_year or '未知'}, 不符合限定年份 {pub_year}")
            title = title[:140] or url
            aid, _is_new = save_article(title, url.split("/")[2] if "//" in url else url, url, words, text, pub_date=pub_date)
            data.update(status="done", articles=[{"id": aid, "title": title, "words": words, "date": pub_date}])
        else:  # discover: 多源; per 指该源最终入库篇数 (已在库的也计数, 标注已存在)
            got, seen_titles, seen_urls, existing = [], set(), set(), 0
            outlets = [o.strip() for o in outlet.split(",") if o.strip()]
            for outlet_name in outlets:
                check_cancel(jid)
                cfg = OUTLETS.get(outlet_name) or next(
                    (v for v in OUTLETS.values() if v["domain"] == outlet_name), None)
                if not cfg:
                    data.setdefault("errors", []).append(f"未知媒体 {outlet_name}")
                    continue
                # 三通道发现 (站内搜索最全 + Bing + GN 月度切片), 不截断候选池
                effective_days = days
                if pub_year is not None:
                    cur_year = time.localtime().tm_year
                    if pub_year < cur_year:
                        effective_days = max(days, (cur_year - pub_year + 1) * 365 + 30)
                arts = discover(topic, cfg["domain"], since_days=effective_days,
                                cdp_host=CDP_HOST)
                data["progress"] = f"[{outlet_name}] 候选 {len(arts)} 篇"
                job_write(jid, data)
                if len(arts) < per and cfg.get("sections"):
                    # 站内搜索/Bing 都不收录的站 (bjreview 等): 走栏目列表页发现
                    arts += section_discover(topic, cfg, cdp_host=CDP_HOST)
                got_this = 0
                dec = load_decoder()
                for a in arts:
                    check_cancel(jid)
                    if got_this >= per:
                        break
                    # GN 包装链接懒解码: 只解真正要抓的, 省掉整池解码的分钟级等待
                    if not a["url"] and a.get("_gn"):
                        a["url"] = (dec(a["_gn"]) or "") if dec else ""
                    if not a["url"]:
                        data.setdefault("skipped", []).append(
                            f"{outlet_name} (GN解码失败): {a['title'][:45]}")
                        continue
                    if a["url"] in seen_urls:  # 本轮已试过 (含失败), 不重复消耗配额
                        continue
                    tok = re.sub(r"[^\w]+", " ", a["title"].lower()).strip()
                    if tok and tok in seen_titles:
                        continue
                    seen_urls.add(a["url"])
                    # 年份预过滤 (抓取前零成本排除): URL 内嵌年份 > discover 的 published 字段 > 无则放行留给抓后判断
                    if pub_year is not None:
                        pub_s = a.get("published") or ""
                        ym = re.search(r"/(20\d{2})[/-]", a["url"])
                        pre_year = int(ym.group(1)) if ym else (
                            int(pub_s[:4]) if pub_s[:4].isdigit() else None)
                        if pre_year is not None and pre_year != pub_year:
                            data.setdefault("skipped", []).append(
                                f"{outlet_name} ({pre_year}年非{pub_year}): {a['title'][:45]}")
                            continue
                    data["status"] = "running"
                    data["progress"] = f"[{outlet_name}] {got_this+1}/{per}: {a['title'][:55]}"
                    job_write(jid, data)
                    try:
                        # freedium 镜像源 (economist): 原文页只有摘要, 用镜像拿全文
                        target = (cfg.get("mirror", "") + a["url"]) if cfg.get("mirror") else a["url"]
                        text, ptitle, pub_date = "", "", ""
                        for attempt in range(2):
                            html, ptitle = fetch(CDP_HOST, target, wait_s=15)
                            text = extract_readability(html) or ""
                            pub_date = extract_date(html, url=a["url"], fallback=a.get("published", ""))
                            if ok_words(len(text.split()), min_words, max_words):
                                break
                            time.sleep(3)
                        words = len(text.split())
                        if not ok_words(words, min_words, max_words) and outlet_name == "economist":
                            # 镜像失败兜底: 从整刊 epub 提取库按标题搜 (economist_epub.py 维护)
                            hit = epub_find(a["title"])
                            if hit:
                                text, pub_date = hit
                                words = len(text.split())
                                data["progress"] = f"[economist] 镜像失败, epub 命中: {a['title'][:45]}"
                                job_write(jid, data)
                        if not ok_words(words, min_words, max_words):
                            why = ("过短" if words < 80 else
                                   "不在词数范围" if (min_words is not None and max_words is not None) else "过短")
                            data.setdefault("skipped", []).append(f"{outlet_name} ({words}词{why}): {a['title'][:45]}")
                            continue
                        if pub_year is not None:
                            art_year = int(pub_date[:4]) if (pub_date and len(pub_date) >= 4 and pub_date[:4].isdigit()) else None
                            if art_year != pub_year:
                                why = f"{art_year}年非{pub_year}" if art_year else "无发布年份"
                                data.setdefault("skipped", []).append(f"{outlet_name} ({why}): {a['title'][:45]}")
                                continue
                        aid, is_new = save_article(ptitle or a["title"], cfg["domain"], a["url"], words, text, pub_date=pub_date)
                        got.append({"id": aid, "outlet": outlet_name, "title": a["title"],
                                    "words": words, "date": pub_date, "url": a["url"], "new": is_new})
                        seen_titles.add(tok)
                        got_this += 1  # 已在库的也计入: per = 该源最终入库篇数
                        existing += 0 if is_new else 1
                    except Exception as e:  # noqa: BLE001
                        data.setdefault("errors", []).append(f"{outlet_name}: {a['title'][:45]}: {str(e)[:55]}")
                    time.sleep(2)
            data.update(status="done", articles=got, existing=existing)
        data["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
    except JobCancelled:
        data["cancelled"] = True
        data.setdefault("skipped", []).append("已被用户取消")
        data.update(status="cancelled",
                    progress="已取消",
                    finished=time.strftime("%Y-%m-%d %H:%M:%S"))
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
    "economist": "经济学人", "nytimes": "纽约时报", "discover": "发现杂志",
    "wsj": "华尔街日报", "times": "泰晤士报", "latimes": "洛杉矶时报",
    "chinadaily": "中国日报", "bjreview": "北京周报",
}


@app.get("/api/outlets")
def api_outlets():
    return {k: {"label": OUTLET_LABELS.get(k, k), "tier": v["tier"], "domain": v["domain"],
                "enabled": v["enabled"]}
            for k, v in OUTLETS.items() if v["enabled"]}


class FetchReq(BaseModel):
    url: str
    pub_year: int | None = None


class DiscoverReq(BaseModel):
    topic: str
    outlets: list[str] = ["atlantic"]
    per_outlet: int = 2
    days: int = 30
    min_words: int | None = None   # None = 不限定词数范围
    max_words: int | None = None
    pub_year: int | None = None    # None = 不限定年份


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
            "pub_year": req.pub_year,
            "created": time.strftime("%Y-%m-%d %H:%M:%S"), "articles": []}
    job_write(jid, data)
    threading.Thread(target=run_job, args=(jid, "fetch", req.url),
                     kwargs={"pub_year": req.pub_year}, daemon=True).start()
    return data


@app.post("/api/discover")
def api_discover(req: DiscoverReq):
    jid = uuid.uuid4().hex[:8]
    data = {"id": jid, "kind": "discover", "status": "running", "topic": req.topic,
            "outlets": req.outlets, "per_outlet": req.per_outlet,
            "min_words": req.min_words, "max_words": req.max_words,
            "pub_year": req.pub_year,
            "created": time.strftime("%Y-%m-%d %H:%M:%S"),
            "articles": [], "skipped": [], "progress": "searching..."}
    job_write(jid, data)
    threading.Thread(target=run_job,
                     args=(jid, "discover", "", req.topic, ",".join(req.outlets), req.per_outlet,
                           req.days, req.min_words, req.max_words, req.pub_year),
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


@app.post("/api/jobs/{jid}/cancel")
def api_cancel(jid: str):
    j = job_read(jid)
    if not j:
        raise HTTPException(404)
    if j["status"] != "running":
        return {"cancelled": False, "status": j["status"]}
    _cancelled.add(jid)
    j["progress"] = "取消中…"
    job_write(jid, j)
    return {"cancelled": True}


@app.get("/api/articles")
def api_articles():
    return list_articles()


@app.get("/api/articles/{aid}")
def api_article(aid: str):
    a = load_article(aid)
    if not a:
        raise HTTPException(404)
    return a


class SaveReq(BaseModel):
    body: str


@app.put("/api/articles/{aid}")
def api_save(aid: str, req: SaveReq):
    """编辑保存: 写回正文并同步 front matter 的词数"""
    f = ART / f"{aid}.md"
    if not f.exists():
        raise HTTPException(404)
    raw = f.read_text(encoding="utf-8")
    m = re.match(r"---\n(.*?)\n---\n\n(.*)", raw, re.S)
    if not m:
        raise HTTPException(400, "格式异常")
    words = len(req.body.split())
    fm = re.sub(r"^words: .*$", f"words: {words}", m.group(1), flags=re.M)
    f.write_text(f"---\n{fm}\n---\n\n{req.body}\n", encoding="utf-8")
    return {"id": aid, "words": words}


@app.delete("/api/articles/{aid}")
def api_del(aid: str):
    f = ART / f"{aid}.md"
    if f.exists():
        f.unlink()
    return {"deleted": aid}


@app.get("/favicon.svg")
def favicon():
    return FileResponse(ROOT / "web" / "favicon.svg", media_type="image/svg+xml")


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
