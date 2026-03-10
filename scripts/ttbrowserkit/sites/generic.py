"""
通用博客站点模块 (Generic Site)

使用 Playwright 渲染 + readability-lxml 提取正文，
适用于任意 JS 渲染的博客站点（如 ShaderBits 等）。
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from urllib.parse import urlparse

from ttbrowserkit.browser import BrowserSession
from ttbrowserkit.html2md import html_to_markdown_with_images, slugify_title

logger = logging.getLogger(__name__)

# JS: 在 DOM 中将 iframe 转为链接（避免被 readability 丢弃）
_JS_IFRAMES_TO_LINKS = """() => {
    document.querySelectorAll('iframe').forEach(iframe => {
        const src = iframe.src || '';
        if (!src) return;
        const a = document.createElement('a');
        a.href = src;
        a.textContent = src;
        const p = document.createElement('p');
        p.appendChild(a);
        iframe.replaceWith(p);
    });
}"""


class GenericSite:
    """通用博客站点：Playwright 渲染 + Readability 正文提取"""

    SITE_NAME = "generic"
    BASE_URL = ""

    @staticmethod
    async def check_login() -> dict:
        return {"logged_in": True, "message": "通用站点无需登录"}

    @staticmethod
    async def login_qrcode() -> dict:
        return {"success": True, "message": "通用站点无需登录"}

    @staticmethod
    async def download_article(url: str, output_dir: str) -> dict:
        """下载任意博客文章为 Markdown + 图片

        流程：
        1. Playwright headless 渲染页面（等待 JS 执行完毕）
        2. readability-lxml 自动提取正文 HTML
        3. HTMLToMarkdown 转 MD + 下载图片
        4. 写入 .md 文件

        Args:
            url: 文章 URL
            output_dir: 输出目录

        Returns:
            {"success": True, "file": "...", ...} 或 {"error": "..."}
        """
        try:
            from readability import Document
        except ImportError:
            return {
                "error": "缺少 readability-lxml 依赖，请执行: pip install readability-lxml lxml"
            }

        url = url.strip()
        if not url.startswith("http"):
            url = "https://" + url

        # 1. Playwright 渲染页面
        async with BrowserSession("generic", headless=True) as session:
            page = session.page

            logger.info("下载通用博客文章: %s -> %s", url, output_dir)

            try:
                await page.goto(url, wait_until="networkidle", timeout=60000)
            except Exception:
                # networkidle 可能超时，回退到 load
                try:
                    await page.goto(url, wait_until="load", timeout=30000)
                except Exception as e:
                    return {"error": f"页面加载失败: {e}"}

            # 额外等待确保 JS 渲染完成
            await asyncio.sleep(2)

            # 将 iframe 转为链接（避免 readability 丢弃视频嵌入）
            await page.evaluate(_JS_IFRAMES_TO_LINKS)

            full_html = await page.content()
            page_title = await page.title()

        # 2. readability-lxml 提取正文（keep_all_images 保留图片在原位）
        doc = Document(full_html)
        content_html = doc.summary(keep_all_images=True)
        title = doc.short_title() or page_title or ""

        if not content_html or len(content_html.strip()) < 50:
            return {"error": "Readability 未能提取到有效正文内容"}

        # 3. 生成文件名
        parsed = urlparse(url)
        # 用 URL path 的最后一段作为 ID 前缀
        path_parts = [p for p in parsed.path.strip("/").split("/") if p]
        url_slug = path_parts[-1] if path_parts else parsed.netloc
        # 清理 URL slug 中的特殊字符
        url_slug = re.sub(r"[^\w\-]", "_", url_slug)

        title_slug = slugify_title(title)
        stem = f"{url_slug}_{title_slug}" if title_slug else url_slug
        # 确保 stem 不会太长
        if len(stem) > 120:
            stem = stem[:120].rstrip("_")

        out = Path(output_dir)
        md_path = out / f"{stem}.md"
        img_dir = out / stem

        # 4. HTML → Markdown + 下载图片
        body_md, downloaded = await html_to_markdown_with_images(
            content_html, img_dir, stem
        )

        # 5. 写入 .md 文件
        lines: list[str] = []
        lines.append(f"# {title}\n")
        lines.append(f"**链接：** {url}  ")
        lines.append("")
        lines.append("---")
        lines.append("")
        lines.append(body_md)
        lines.append("")

        md_content = "\n".join(lines)
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text(md_content, encoding="utf-8")

        logger.info("下载完成: %s (图片 %d)", md_path, len(downloaded))
        return {
            "success": True,
            "file": str(md_path),
            "title": title,
            "images_downloaded": len(downloaded),
        }
