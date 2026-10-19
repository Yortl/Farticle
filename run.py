#!/usr/bin/env python3
"""用法:
  python3 run.py "artificial intelligence"               # 全部启用媒体, 近365天
  python3 run.py "climate change" --days 180 --outlets guardian,atlantic
  python3 run.py "remote work" --min-words 250           # 过滤太短的文章 (四级300词以下原文常被删改)
  python3 run.py --check                                  # 5秒自检: 只跑发现层
输出: articles/<slug>/<domain>/NNN-标题slug.md  +  articles/<slug>/index.json
"""
import argparse
import json
import re
import sys
import time
from pathlib import Path

from discover import discover
from fetchers import fetch_article
from outlets import OUTLETS

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))


def slugify(s: str, maxlen: int = 60) -> str:
    s = re.sub(r"[^\w\s-]", "", s.lower())
    return re.sub(r"[\s_]+", "-", s).strip("-")[:maxlen] or "untitled"


def load_decoder():
    """GN 链接解码器; 连续 3 次失败即弃用 (解码挂了只会白等)。"""
    try:
        from googlenewsdecoder import gnewsdecoder
    except ImportError:
        return None
    fails = {"n": 0}

    def dec(link):
        if fails["n"] >= 3:
            raise RuntimeError("gn decoder disabled after repeated failures")
        r = gnewsdecoder(link, interval=1)
        if not (isinstance(r, dict) and r.get("success")):
            fails["n"] += 1
        return r

    return dec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("topic", nargs="?", help='话题, 如 "artificial intelligence"')
    ap.add_argument("--days", type=int, default=365, help="只收录近 N 天 (默认365)")
    ap.add_argument("--min-words", type=int, default=200, help="正文最少词数 (默认200)")
    ap.add_argument("--outlets", help="逗号分隔, 默认全部 enabled 的媒体")
    ap.add_argument("--delay", type=float, default=2.0, help="每篇文章间隔秒数")
    ap.add_argument("--check", action="store_true", help="自检: 只验证发现层连通性")
    args = ap.parse_args()

    if args.check:
        from discover import bing_news
        got = bing_news("climate change", "theguardian.com", 90)
        print(f"[check] bing_news guardian: {len(got)} 条"
              + (f", 示例: {got[0]['url'][:80]}" if got else " <- 发现层异常!"))
        sys.exit(0 if got else 1)
    if not args.topic:
        ap.error("需要话题参数")

    names = (args.outlets.split(",") if args.outlets
             else [k for k, v in OUTLETS.items() if v["enabled"]])
    decode = load_decoder()
    slug = slugify(args.topic)
    outdir = ROOT / "articles" / slug
    outdir.mkdir(parents=True, exist_ok=True)

    # ---- 发现 ----
    found: list[dict] = []
    for name in names:
        cfg = OUTLETS.get(name.strip())
        if not cfg:
            print(f"[skip] 未知媒体: {name}")
            continue
        arts = discover(args.topic, cfg["domain"], since_days=args.days, decode=decode)
        print(f"[发现] {name:12s} {len(arts):3d} 篇")
        for a in arts:
            a["outlet"] = name
        found.extend(arts)
    time.sleep(0.5)

    # ---- 抓取 ----
    index, ok_cnt, fail_cnt = [], 0, 0
    per_domain: dict[str, int] = {}
    for i, art in enumerate(found, 1):
        title = art["title"] or art["url"]
        if not art["url"]:
            print(f"[跳过] GN链接未解出真实URL: {title[:60]}")
            fail_cnt += 1
            index.append({**art, "status": "unresolved_url", "how": "gn_decode_failed", "file": ""})
            continue
        if art["source"] not in {OUTLETS[n]["domain"] for n in names if n in OUTLETS}:
            continue
        key = _dk = art["outlet"] + "/" + str(per_domain.get(art["outlet"], 0) + 1)
        tier = OUTLETS[art["outlet"]]["tier"]
        res = fetch_article(art["url"], tier)
        if res["status"] != "ok" or len(res["text"].split()) < args.min_words:
            print(f"[失败] {art['outlet']:10s} {title[:55]}  ({res['how']})")
            fail_cnt += 1
            index.append({**art, "status": "failed", "how": res["how"], "file": ""})
        else:
            per_domain[art["outlet"]] = per_domain.get(art["outlet"], 0) + 1
            n = per_domain[art["outlet"]]
            fname = f"{n:02d}-{slugify(title, 50)}.md"
            fpath = outdir / art["outlet"]
            fpath.mkdir(exist_ok=True)
            words = len(res["text"].split())
            header = (f"---\ntitle: {title}\nsource: {art['source']}\noutlet: {art['outlet']}\n"
                      f"published: {art['published']}\nurl: {art['url']}\n"
                      f"archived: {res['final_url']}\nvia: {res['how']}\nwords: {words}\n"
                      f"topic: {args.topic}\nfetched: {time.strftime('%Y-%m-%d')}\n---\n\n")
            (fpath / fname).write_text(header + res["text"] + "\n", encoding="utf-8")
            print(f"[OK]   {art['outlet']:10s} {words:5d}词  {title[:55]}  ({res['how']})")
            ok_cnt += 1
            index.append({**art, "status": "ok", "how": res["how"], "words": words,
                          "file": str((fpath / fname).relative_to(ROOT))})
        time.sleep(args.delay)

    (outdir / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n完成: {ok_cnt} 篇成功, {fail_cnt} 篇失败/跳过 -> {outdir}/index.json")


if __name__ == "__main__":
    main()
