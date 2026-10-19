#!/usr/bin/env python3
"""全源测试抓取: 每个源抓 N 篇, 生成单一 Markdown 汇编报告 (供人工检查)。
用法: python3 collect.py "AI" --per-outlet 3 --days 365
"""
import argparse
import json
import time
from pathlib import Path

from discover import _norm, discover
from fetchers import fetch_article
from outlets import OUTLETS
from run import load_decoder, slugify


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("topic")
    ap.add_argument("--variant", help="同义检索词, 与主词合并扩大候选 (如 artificial intelligence)")
    ap.add_argument("--per-outlet", type=int, default=3)
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--min-words", type=int, default=200)
    ap.add_argument("--outlets", help="逗号分隔; 默认全部 enabled 的 free+metered")
    ap.add_argument("--budget", type=int, default=480, help="每源最多耗时秒数, 超出止损 (默认480)")
    args = ap.parse_args()

    names = (args.outlets.split(",") if args.outlets
             else [k for k, v in OUTLETS.items()
                   if v["enabled"] and v["tier"] in ("free", "metered")])
    decode = load_decoder()
    outdir = Path(__file__).parent / "articles" / (slugify(args.topic) + "-test")
    outdir.mkdir(parents=True, exist_ok=True)

    topics = [t for t in (args.topic, args.variant) if t]
    report = [f"# 主题 「{args.topic}」 全源抓取测试报告",
              f"- 时间: {time.strftime('%Y-%m-%d %H:%M')} | 窗口: 近{args.days}天 | "
              f"目标: 每源{args.per_outlet}篇 | 最少词数: {args.min_words} | "
              f"检索词: {topics}", ""]
    summary, total_rows = [], 0
    for name in names:
        cfg = OUTLETS[name]
        # 多检索词发现 + 合并去重; 过滤明显非正文的路径 (话题聚合页/播客/视频)
        found, seen = [], set()
        import re as _re
        bad_pat = _re.compile(r"/(spotlight|video|podcast|audio|insider|live|newsletters?)/")
        for t in topics:
            for a in discover(t, cfg["domain"], since_days=args.days, decode=decode):
                k = _norm(a["url"]) if a["url"] else ""
                if a["url"] and k not in seen and not bad_pat.search(a["url"]):
                    seen.add(k)
                    found.append(a)
            found = found[:20]
        got, misses, reasons = [], 0, []
        t_src = time.time()
        for a in found:
            if len(got) >= args.per_outlet:
                break
            if time.time() - t_src > args.budget:
                reasons.append(f"budget_exceeded_{int(time.time()-t_src)}s")
                break
            res = fetch_article(a["url"], cfg["tier"])
            words = len(res["text"].split()) if res["text"] else 0
            if res["status"] == "ok" and words >= args.min_words:
                got.append((a, res, words))
            else:
                misses += 1
                reasons.append(res["how"][:60])
            time.sleep(1)
        # 存单篇文件
        dest = outdir / name
        dest.mkdir(exist_ok=True)
        report.append(f"\n---\n\n## {name} ({cfg['domain']}, tier={cfg['tier']}) "
                      f"— 成功 {len(got)}/{args.per_outlet}, 失败 {misses}")
        if reasons:
            report.append(f"- 失败方式: {reasons}")
        summary.append((name, cfg["tier"], len(got), misses))
        total_rows += len(got)
        for i, (a, res, words) in enumerate(got, 1):
            fname = f"{i:02d}-{slugify(a['title'], 50)}.md"
            header = (f"---\ntitle: {a['title']}\nsource: {a['source']}\noutlet: {name}\n"
                      f"published: {a['published']}\nurl: {a['url']}\narchived: {res['final_url']}\n"
                      f"via: {res['how']}\nwords: {words}\nfetched: {time.strftime('%Y-%m-%d')}\n---\n\n")
            (dest / fname).write_text(header + res["text"] + "\n", encoding="utf-8")
            report.append(f"\n### {name} #{i} · {a['title']} ({words}词, {res['how']}, {a['published'][:10]})")
            report.append(f"\n> 原链: {a['url']}")
            if res["final_url"] != a["url"]:
                report.append(f"> 存档: {res['final_url']}")
            report.append("\n" + res["text"])
            print(f"[{name}] #{i} OK {words}w: {a['title'][:50]}")
        print(f"[{name}] 完成 {len(got)}/{args.per_outlet} (候选{len(found)})")

    (outdir / "index.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    head = "\n".join(report[:3]) + "\n\n| 源 | tier | 成功 | 失败 |\n|---|---|---|---|\n" + \
        "\n".join(f"| {n} | {t} | {g} | {m} |" for n, t, g, m in summary) + \
        f"\n\n**合计成功: {total_rows} 篇**"
    full = head + "\n" + "\n".join(report[3:])
    rp = outdir / (slugify(args.topic) + "-report.md")
    rp.write_text(full, encoding="utf-8")
    print(f"DONE 成功{total_rows}篇 -> {rp}")


if __name__ == "__main__":
    main()
