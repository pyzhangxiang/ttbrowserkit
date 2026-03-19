# ttbrowserkit

[xiaohongshu-mcp](https://github.com/xpzouying/xiaohongshu-mcp) 的 Python 移植版，作为 [OpenClaw](https://github.com/nicepkg/openclaw) 的 Skill 使用。

基于 Playwright 实现浏览器自动化，支持小红书、知乎等站点的内容搜索、抓取、下载。

## 功能

- 搜索内容
- 获取笔记/文章详情
- 下载笔记为 Markdown（含图片、评论）
- 扫码登录（支持 Agent 自动获取二维码图片）
- 获取推荐 Feed
- 解析小红书短链接
- Cookie 自动持久化，无需重复登录
- 自动重试

支持站点：小红书、知乎

## 安装

```bash
# Python 3.10+
pip install -r requirements.txt
playwright install chromium
```

