"""清洗器单测: _clean_lines 规则的最小回归集, 失败即非零退出。"""
import sys

sys.path.insert(0, ".")
from cdp_fetch import _clean_lines  # noqa: E402

cases = [
    # 行内链接拆行合并 (根因: 旧 get_text("\n") 把 <a> 拆行)
    (["The Amish have been hard hit, although they are far from", "the only group", "affected."],
     ["The Amish have been hard hit, although they are far from the only group affected."]),
    # 孤立句点归还前段
    (["has sown doubts about the safety of vaccines", "."],
     ["has sown doubts about the safety of vaccines."]),
    # 尾部标记截断 (其后全是推荐/页脚)
    (["Real body text.", "Most viewed", "Alberta voters reject plan"], ["Real body text."]),
    (["Real body text.", "NEWSLETTER", "About The Post"], ["Real body text."]),
    # 噪音行丢弃 (含大写变体)
    (["SKIP TO CONTENT", "Sign In", "Newsletters", "Real text here."], ["Real text here."]),
    # 导航容器串
    (["News Opinion Sport Culture Lifestyle Show more Environment"], []),
    # 图片信用行
    (["Photograph: Patrick Greenfield/The Guardian", "(Image Courtesy of NPS)",
      "Reuters", "Getty Images", "Jason Andrew for", "Real text here."],
     ["Real text here."]),
    # NatGeo 酒店价格行 / 泄漏 CSS 类名
    (["Rates: From $1,200, room only.", "AccordionItemContainerButton", "Real text here."],
     ["Real text here."]),
    # 相邻去重
    (["A heading", "A heading", "Real body."], ["A heading", "Real body."]),
    # 残余拆行 (<br>): 空格接回
    (["He said the plan was", "dangerous."], ["He said the plan was dangerous."]),
    # 纯符号/数字碎片丢弃, 但保留正文合法短行
    (["0:00", "1.0x", "−", "Us", "We act now."], ["We act now."]),
]
for i, (inp, want) in enumerate(cases):
    got = _clean_lines(list(inp))
    assert got == want, f"case {i}: {got!r} != {want!r}"
print(f"test_clean OK: {len(cases)} cases")
