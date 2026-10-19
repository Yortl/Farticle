# exam-english-scraper — 四六级/考研外刊阅读素材抓取

针对特定话题, 批量抓取考试常考外刊 (Guardian / CSMonitor / Atlantic / Economist / SciAm
/ Psychology Today / BBC / NatGeo / Time / NYT / WaPo / New Scientist ...) 的**纯净 Markdown
正文**, 每篇带 YAML 头 (来源/日期/原链/存档链/词数), 输出到 `articles/<话题>/<媒体>/NNN-标题.md`
+ `index.json`。

## 用法

```bash
python3 run.py --check                                    # 5秒自检发现层
python3 run.py "artificial intelligence"                  # 全部启用媒体, 近365天
python3 run.py "remote work" --days 180 --min-words 300 --outlets guardian,atlantic,csmonitor
```

依赖: `pip3 install trafilatura googlenewsdecoder` (标准库 + 这两个; Python 3.10+)。

## 架构 (为什么稳)

```
Bing News RSS ──► 真实文章直链 (主力, apiclick?url= 参数直接解包)
Google News RSS ─► 标题/日期补充 (链接需 googlenewsdecoder 解码, 失败仅记标题)
      ▼
free 媒体:   requests 直抓
metered 媒体: 直抓失败 → Wayback Machine (availability API → CDX → SPN现场存档)
      ▼
trafilatura 提取正文 (自动剔付费墙弹窗/广告/侧栏), <min-words 判失败, 不写半成品
```

## 媒体分级 (outlets.py)

- `free` 直抓稳定 (13个): guardian, csmonitor, bbc, natgeo, time, newsweek, sciam, psychtoday,
  wired, cnbc, npr, vox, arstechnica — 后 5 个是 2026-10 为替代死源补入的, 实测 100% 直抓成功
- `metered` 靠 wayback 缓存 (有多少算多少): atlantic, newscientist, wapo
- `dead`/hard 默认关闭 — 2026-10-19 实测直抓与 wayback 均 403, 本 IP 无解:
  **economist, nytimes** (2 源禁用留档), wsj, hbr, ft

## 实测基准 (本机, 2026-10)

- Guardian 直抓 100% 成功; CSMonitor 直抓 100% 成功 (含伪装 404 路径也能进正文页)
- Atlantic 直抓全 403, wayback_cache 补回约 1/3 (其余是归档未收录的新文); Economist/NYT 同理
- archive.today 本网络握手超时, 未接入; Google News batchexecute 解码在本环境不可用 (库失效)
- **Economist 整刊已解决**: economist_epub.py 每周从 hehonghui/awesome-english-ebooks (38k★,
  每周六自动更新) 下载整刊 epub → 解析 toc 逐篇提取 → articles/economist-issues/<期号>/NNN-栏目-标题.md。
  首期 2026-10-17 实测 72 篇全成功 (含 Leaders/China/Business/Science 等全部栏目)。
- NYT 无 GitHub 现成整刊渠道; calibre nytimes.recipe 需 camoufox 反指纹浏览器, 未验证。

## 已知边界 (诚实声明)

- 每媒体单话题实际收益: free 媒体 5-15 篇/月, metered 1-5 篇/月 (受归档覆盖率限制)
- Bing RSS 约给 10-15 条/查询, Google RSS 100 条但解码失败时只有标题
- 想再提成功率: 加代理池换出口 IP, 或接入 archive.today (headless), 或上_paid 订阅 cookie
