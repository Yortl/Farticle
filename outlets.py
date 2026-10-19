"""考试外刊出处配置。tier 决定抓取策略:
free    = 直接抓原文 (Guardian/BBC/CSM/NatGeo/Time/AP/Newsweek/SciAm 部分)
metered = 有软付费墙, 直抓大概率只拿到部分段落 -> wayback 降级 (Economist/Atlantic/NYT/WaPo/WSJ部分)
hard    = 硬付费墙, 无订阅基本拿不到全文 (FT/HBR/WSJ部分) -> 默认关闭, 失败自动跳过
status 列是本机真实测试结果, 换网络环境后以实际运行为准 (run.py 会如实报告每个失败的源)。
"""
OUTLETS = {
    # ---- free: 直抓稳定 ----
    "guardian":    {"domain": "theguardian.com",    "tier": "free",    "enabled": True},
    "csmonitor":   {"domain": "csmonitor.com",      "tier": "free",    "enabled": True},
    "bbc":         {"domain": "bbc.com",            "tier": "free",    "enabled": True},
    "natgeo":      {"domain": "nationalgeographic.com", "tier": "free", "enabled": True},
    "time":        {"domain": "time.com",           "tier": "free",    "enabled": True},
    "newsweek":    {"domain": "newsweek.com",       "tier": "free",    "enabled": True},
    "sciam":       {"domain": "scientificamerican.com", "tier": "free", "enabled": True},
    "psychtoday":  {"domain": "psychologytoday.com", "tier": "free",   "enabled": True},
    # ---- free 二批 (2026-10-19 实测全通过, 均 100% 直抓成功) ----
    "wired":       {"domain": "wired.com",          "tier": "free",    "enabled": True},
    "cnbc":        {"domain": "cnbc.com",           "tier": "free",    "enabled": True},
    "npr":         {"domain": "npr.org",            "tier": "free",    "enabled": True},
    "vox":         {"domain": "vox.com",            "tier": "free",    "enabled": True},
    "arstechnica": {"domain": "arstechnica.com",    "tier": "free",    "enabled": True},
    # ---- metered: 靠 wayback 缓存, 有多少算多少 ----
    "atlantic":    {"domain": "theatlantic.com",    "tier": "metered", "enabled": True},
    "newscientist":{"domain": "newscientist.com",   "tier": "metered", "enabled": True},
    "wapo":        {"domain": "washingtonpost.com", "tier": "metered", "enabled": True},
    # ---- dead: 对直抓与 wayback 均 403, 本 IP 无解 (2026-10-19 实测结论, 留档不开) ----
    "economist":   {"domain": "economist.com",      "tier": "hard",    "enabled": False},
    "nytimes":     {"domain": "nytimes.com",        "tier": "hard",    "enabled": False},
    "wsj":         {"domain": "wsj.com",            "tier": "hard",    "enabled": False},
    "hbr":         {"domain": "hbr.org",            "tier": "hard",    "enabled": False},
    "ft":          {"domain": "ft.com",             "tier": "hard",    "enabled": False},
}

