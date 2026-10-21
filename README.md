# Farticle 🗞️

外刊文章抓取与阅读系统 — 面向四六级/考研英语阅读素材建设。

按话题从 19 个国际媒体源批量抓取纯净 Markdown 正文, 内置 Web 界面阅读管理。

## 三条抓取通道

| 通道 | 适用 | 说明 |
|---|---|---|
| **免费直抓** | Guardian / BBC / NPR / Vox / Wired / CNBC / SciAm / Time / Newsweek / NatGeo / Ars Technica / Psychology Today / 中国日报 / 北京周报 | Bing News 发现 → 直接抓 |
| **真实浏览器 CDP** | Atlantic / WaPo / NYT | 经局域网 Chrome (装 BPC 插件) 解付费墙; 自动处理 Cloudflare Turnstile / GDPR consent / interstitial |
| **镜像通道** | Economist | freedium-mirror 全文镜像 (BPC 原文页只解摘要) |

另有 `economist_epub.py`: 每周从 awesome-english-ebooks 拉整刊 epub → 逐篇转 Markdown。

## 快速开始

```bash
# Web 服务 (需局域网 Chrome 开启 CDP: chrome --remote-debugging-port=9222 --remote-allow-origins=*)
./farticle.sh install   # 安装依赖 (自动建 venv) + 启动, 首次
./farticle.sh start     # 启动 (后台常驻, pid/日志在 run/)
./farticle.sh stop      # 停止
./farticle.sh restart   # 重启
./farticle.sh update    # git pull + 依赖同步 + 重启
./farticle.sh status    # 运行状态 + CDP 连通性 + 文章数
# 换端口/CDP: FARTICLE_PORT=8901 FARTICLE_CDP=192.168.1.99:9222 ./farticle.sh start
# 直接跑: python3 server.py --port 8900 --cdp 127.0.0.1:9222
# CLI: 单话题全源抓取
python3 run.py "artificial intelligence"
python3 run.py "remote work" --days 180 --min-words 300 --outlets guardian,bbc

# CLI 自检
python3 run.py --check
```

Web 界面: 粘贴 URL 单篇抓取 / 多源 chip 点选 + 每源入库篇数 (已在库的也计入, 会标注) /
可选词数限制 (勾选 400–1200 词才生效, 不勾选不做限制; 短于 80 词的正文一律不收) /
阅读视图 (首字下沉精排版, 可编辑正文) / 文章库管理。

## 架构

```
Bing News RSS (发现, apiclick 解包真实 URL)      discover.py
        ▼
通道分流 (outlets.py 分级: free / metered / mirror)
  ├─ free:  urllib 直抓                        fetchers.py
  ├─ metered/mirror: CDP 开 tab                cdp_fetch.py
  │    ├─ Cloudflare Turnstile 坐标点击
  │    ├─ GDPR consent 自动 Reject All
  │    ├─ interstitial 自动 reload 重试
  │    └─ 取 outerHTML → readability 提正文
        ▼
readability-lxml 清洗 → Markdown + YAML front matter
        ▼
server.py (FastAPI) + web/index.html (单文件前端)
```

## 已知边界

- Economist/NYT 等对服务器 IP 直抓 403, 必须走真实浏览器 CDP
- Wayback 大批量调用会触发 archive.org 429 限流
- Google News RSS 链接解码 (AU_yqL 新编码) 已失效, 故以 Bing 为主发现层
- bjreview 等不被 Bing 收录的站点走栏目列表发现 (`section_discover`)
- 真实浏览器 CDP 通道**必须零注入** — UA 覆盖/webdriver 补丁/瞬移滚动都会被 DataDome 识别为机器人

## License

MIT
