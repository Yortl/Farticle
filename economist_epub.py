#!/usr/bin/env python3
"""经济学人整刊 → Markdown 素材。
每周六 hehonghui/awesome-english-ebooks 更新当期 epub; 本脚本:
  1. GitHub API 找最新一期 epub 并下载
  2. 解包 epub (zip), 按 toc.ncx 定位每篇文章 (栏目+标题+正文xhtml)
  3. trafilatura 提正文 → 存 articles/economist-issues/<期号>/NNN-栏目-标题.md
用法: python3 economist_epub.py [--min-words 150] [--keep-epub]
"""
import argparse
import json
import re
import sys
import time
import urllib.request
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

UA = {"User-Agent": "Mozilla/5.0"}
REPO = "hehonghui/awesome-english-ebooks"
ROOT = Path(__file__).parent
NS = {
    "ncx": "urn:uuid:0aa6d5da-9e1d-4c2e-9290-92d2f682d3",  # placeholder, 实际按 tag 名匹配
}


def gh(url: str, timeout: int = 30):
    req = urllib.request.Request(url, headers={**UA, "Accept": "application/vnd.github+json"})
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read())


def latest_epub_url() -> tuple[str, str]:
    """返回 (期号目录名, epub下载url)。"""
    issue_dirs = [f["name"] for f in gh(f"https://api.github.com/repos/{REPO}/contents/01_economist")
                  if f["type"] == "dir" and re.match(r"te_\d{4}\.\d+\.\d+", f["name"])]
    if not issue_dirs:
        raise SystemExit("awesome-english-ebooks 里没找到 economist 期目录")
    issue = sorted(issue_dirs, key=lambda s: s.split("_")[1])[-1]
    files = gh(f"https://api.github.com/repos/{REPO}/contents/01_economist/{issue}")
    epub = next((f["download_url"] for f in files if f["name"].endswith(".epub")), None)
    if not epub:
        raise SystemExit(f"{issue} 里没有 epub (可能刚更新还没传)")
    return issue, epub


def leaf_navpoints(navmap, parent=""):
    """深度优先输出 (父栏目, 标题, href)。"""
    NCX = "{http://www.daisy.org/z3986/2005/ncx/}"
    out = []
    for np in navmap:
        nav_label = np.find(NCX + "navLabel")
        label = "".join(nav_label.itertext()).strip() if nav_label is not None else ""
        content = np.find(NCX + "content")
        href = content.get("src") if content is not None else ""
        children = np.findall(NCX + "navPoint")
        if children:
            out += leaf_navpoints(children, label or parent)
        elif href and label:
            out.append((parent or "Article", label, href.split("#")[0]))
    return out


def slugify(s: str, maxlen: int = 50) -> str:
    s = re.sub(r"[^\w\s-]", "", s.lower())
    return re.sub(r"[\s_]+", "-", s).strip("-")[:maxlen] or "untitled"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-words", type=int, default=150)
    ap.add_argument("--keep-epub", action="store_true", help="保留下载的 epub 供复用")
    args = ap.parse_args()

    import trafilatura

    issue, epub_url = latest_epub_url()
    date = issue.replace("te_", "").replace(".", "-")
    outdir = ROOT / "articles" / "economist-issues" / date
    done_mark = outdir / ".done"
    if done_mark.exists():
        print(f"{date} 期已转换过, 跳过 (删除 {done_mark} 可强制重来)")
        return
    outdir.mkdir(parents=True, exist_ok=True)

    epub_path = outdir / "issue.epub"
    print(f"[1/3] 下载 {issue} epub ...")
    if not epub_path.exists():
        req = urllib.request.Request(epub_url, headers=UA)
        epub_path.write_bytes(urllib.request.urlopen(req, timeout=120).read())
    print(f"      {epub_path.stat().st_size // 1024}KB")

    print("[2/3] 解包并解析目录 ...")
    with zipfile.ZipFile(epub_path) as z:
        names = z.namelist()
        container = ET.fromstring(z.read("META-INF/container.xml"))
        opf_path = container.find(".//{*}rootfile").get("full-path")
        opf_dir = opf_path.rsplit("/", 1)[0] + "/" if "/" in opf_path else ""
        opf = ET.fromstring(z.read(opf_path))
        # manifest id -> href
        base = opf_dir
        href_of = {m.get("id"): base + m.get("href") for m in opf.findall(".//{*}manifest/{*}item")}
        # ncx
        ncx_href = next(m.get("href") for m in opf.findall(".//{*}manifest/{*}item")
                        if m.get("media-type") == "application/x-dtbncx+xml")
        ncx = ET.fromstring(z.read(base + ncx_href))
        articles = leaf_navpoints(ncx.find(".//{*}navMap"))
        print(f"      toc 文章数: {len(articles)}")

        def xhtml_text(href: str) -> str | None:
            raw = z.read(href.replace("./", base, 1) if href.startswith("./") else base + href)
            return trafilatura.extract(raw.decode("utf-8", "ignore"),
                                       output_format="markdown", include_comments=False)

    print("[3/3] 逐篇提取 ...")
    index, saved = [], 0
    seen_href = set()
    with zipfile.ZipFile(epub_path) as z:  # ponytail: zip 必须与读文件同作用域, 否则 "archive closed"
        def xhtml_text(href: str) -> str | None:
            raw = z.read(href.replace("./", base, 1) if href.startswith("./") else base + href)
            return trafilatura.extract(raw.decode("utf-8", "ignore"),
                                       output_format="markdown", include_comments=False)

        for parent, label, href in articles:
            if href in seen_href or not href.endswith((".xhtml", ".html", ".htm")):
                continue
            seen_href.add(href)
            try:
                text = xhtml_text(href)
            except Exception as e:  # noqa: BLE001
                print(f"  [skip] {href}: {e}")
                continue
            words = len(text.split()) if text else 0
            if not text or words < args.min_words:
                continue
            saved += 1
            fname = f"{saved:03d}-{slugify(parent)}--{slugify(label)}.md"
            header = (f"---\ntitle: {label}\nsection: {parent}\nsource: economist.com (print)\n"
                      f"issue: {date}\nvia: awesome-english-ebooks epub\nwords: {words}\n"
                      f"extracted: {time.strftime('%Y-%m-%d')}\n---\n\n")
            (outdir / fname).write_text(header + text + "\n", encoding="utf-8")
            index.append({"file": fname, "section": parent, "title": label, "words": words})
            print(f"  [{saved:03d}] {parent:18s} {words:5d}w  {label[:55]}")

    (outdir / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8")
    done_mark.write_text("ok\n")
    if not args.keep_epub:
        epub_path.unlink()
    assert saved > 10, "提取篇数异常偏低, epub 结构可能变了"
    print(f"完成: {saved} 篇 -> {outdir}  (index.json 同目录)")


if __name__ == "__main__":
    main()
