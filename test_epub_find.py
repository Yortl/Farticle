"""epub_find 自检: 从存量整刊提取库搜标题, 验证命中/不命中两路。"""
import server

# 命中: 本期真实标题
hit = server.epub_find("What if the machines take over science?")
assert hit and len(hit[0].split()) > 300, f"应命中整篇, got {hit and len(hit[0].split())}"
print("hit ok:", hit[0][:60].replace("\n", " "), "...", len(hit[0].split()), "words, date:", hit[1])

# 命中: 噪音后缀的网页标题 (discover 给的标题带栏目尾巴)
hit2 = server.epub_find("What if the machines take over science? - The Economist")
assert hit2 and hit2[0] == hit[0], "带后缀标题应命中同一篇"

# 不命中: 无关标题
miss = server.epub_find("Completely unrelated topic about quantum pottery")
assert miss is None, "无关标题不应命中"
print("miss ok: None")
print("PASS")
