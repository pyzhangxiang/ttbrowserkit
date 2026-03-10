"""
html2md.py - HTML → Markdown 转换器 + 图片下载

从知乎模块抽取的公共模块，供所有站点使用。
"""

from __future__ import annotations

import logging
import re
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)


def guess_image_ext(url: str) -> str:
    """从 URL 推断图片扩展名"""
    path = urlparse(url).path.lower()
    if path.endswith(".png"):
        return ".png"
    elif path.endswith(".webp"):
        return ".webp"
    elif path.endswith(".gif"):
        return ".gif"
    return ".jpg"


def slugify_title(title: str, max_chars: int = 60) -> str:
    """将中文/英文标题转为适合文件名的 slug。

    规则：
    - 保留中文、英文字母、数字
    - 空格和标点替换为下划线
    - 合并连续下划线、去除首尾下划线
    - 截断到 max_chars
    """
    if not title:
        return ""
    # 保留中文(\u4e00-\u9fff)、字母、数字，其余替换为下划线
    slug = re.sub(r"[^\u4e00-\u9fffa-zA-Z0-9]+", "_", title)
    # 合并连续下划线，去首尾
    slug = re.sub(r"_+", "_", slug).strip("_")
    # 截断
    if len(slug) > max_chars:
        slug = slug[:max_chars].rstrip("_")
    return slug



class HTMLToMarkdown(HTMLParser):
    """富文本 HTML → Markdown 转换器

    使用状态变量跟踪 <a> 的 href，处理常用 HTML 标签。
    """

    def __init__(self, img_dir: str, stem: str):
        super().__init__()
        self.img_dir = img_dir
        self.stem = stem
        self.output: list[str] = []
        self.images: list[tuple[str, str]] = []  # (url, local_filename)
        self._img_counter = 0
        self._list_stack: list[str] = []  # "ul" or "ol"
        self._ol_counter: list[int] = []
        self._in_pre = False
        self._suppress_text = False
        self._link_href: str | None = None  # current <a> href
        self._link_text_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]):
        attr_dict = dict(attrs)
        tag = tag.lower()

        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            level = int(tag[1])
            self._ensure_blank_line()
            self.output.append("#" * level + " ")

        elif tag == "p":
            self._ensure_blank_line()

        elif tag == "br":
            self.output.append("\n")

        elif tag == "hr":
            self._ensure_blank_line()
            self.output.append("---\n\n")

        elif tag == "blockquote":
            self._ensure_blank_line()
            self.output.append("> ")

        elif tag == "strong" or tag == "b":
            self.output.append("**")

        elif tag == "em" or tag == "i":
            self.output.append("*")

        elif tag == "a":
            href = attr_dict.get("href", "")
            if href and not href.startswith("javascript:"):
                self._link_href = href
                self._link_text_parts = []

        elif tag == "code":
            if not self._in_pre:
                self.output.append("`")

        elif tag == "pre":
            self._in_pre = True
            self._ensure_blank_line()
            self.output.append("```\n")

        elif tag == "ul":
            self._list_stack.append("ul")
            self._ensure_blank_line()

        elif tag == "ol":
            self._list_stack.append("ol")
            self._ol_counter.append(0)
            self._ensure_blank_line()

        elif tag == "li":
            indent = "  " * max(0, len(self._list_stack) - 1)
            if self._list_stack and self._list_stack[-1] == "ol":
                self._ol_counter[-1] += 1
                self.output.append(f"{indent}{self._ol_counter[-1]}. ")
            else:
                self.output.append(f"{indent}- ")

        elif tag == "img":
            img_url = (
                attr_dict.get("data-original")
                or attr_dict.get("data-actualsrc")
                or attr_dict.get("src")
                or ""
            )
            if img_url and not img_url.startswith("data:"):
                if img_url.startswith("//"):
                    img_url = "https:" + img_url
                self._img_counter += 1
                ext = guess_image_ext(img_url)
                fname = f"{self._img_counter}{ext}"
                self.images.append((img_url, fname))
                self._ensure_blank_line()
                self.output.append(f"![{fname}](./{self.stem}/{fname})\n\n")

        elif tag == "figcaption":
            self._suppress_text = True

        elif tag == "tr":
            self.output.append("| ")

        elif tag == "table":
            self._ensure_blank_line()

    def handle_endtag(self, tag: str):
        tag = tag.lower()

        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self.output.append("\n\n")

        elif tag == "p":
            self.output.append("\n\n")

        elif tag == "blockquote":
            self.output.append("\n\n")

        elif tag == "strong" or tag == "b":
            self.output.append("**")

        elif tag == "em" or tag == "i":
            self.output.append("*")

        elif tag == "a":
            if self._link_href is not None:
                text = "".join(self._link_text_parts)
                self.output.append(f"[{text}]({self._link_href})")
                self._link_href = None
                self._link_text_parts = []

        elif tag == "code":
            if not self._in_pre:
                self.output.append("`")

        elif tag == "pre":
            self._in_pre = False
            self.output.append("\n```\n\n")

        elif tag == "ul":
            if self._list_stack:
                self._list_stack.pop()
            self.output.append("\n")

        elif tag == "ol":
            if self._list_stack:
                self._list_stack.pop()
            if self._ol_counter:
                self._ol_counter.pop()
            self.output.append("\n")

        elif tag == "li":
            self.output.append("\n")

        elif tag == "figcaption":
            self._suppress_text = False

        elif tag in ("td", "th"):
            self.output.append(" | ")

        elif tag == "tr":
            self.output.append("\n")

        elif tag == "table":
            self.output.append("\n")

    def handle_data(self, data: str):
        if self._suppress_text:
            return
        # If inside a link, collect text for the link
        if self._link_href is not None:
            self._link_text_parts.append(data)
            return
        if self._in_pre:
            self.output.append(data)
        else:
            if data.strip():
                self.output.append(data)

    def _ensure_blank_line(self):
        text = "".join(self.output)
        if text and not text.endswith("\n\n"):
            if text.endswith("\n"):
                self.output.append("\n")
            else:
                self.output.append("\n\n")

    def get_markdown(self) -> str:
        raw = "".join(self.output)
        raw = re.sub(r"\n{3,}", "\n\n", raw)
        return raw.strip()


async def html_to_markdown_with_images(
    html: str, img_dir: Path, stem: str
) -> tuple[str, list[str]]:
    """将 HTML 转换为 Markdown，并下载图片。

    Returns:
        (markdown_text, downloaded_filenames)
    """
    parser = HTMLToMarkdown(str(img_dir), stem)
    parser.feed(html)
    md = parser.get_markdown()
    images = parser.images

    downloaded: list[str] = []
    if images:
        img_dir.mkdir(parents=True, exist_ok=True)
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            for url, fname in images:
                try:
                    resp = await client.get(url)
                    resp.raise_for_status()
                    # Use content-type to correct extension if needed
                    ct = resp.headers.get("content-type", "")
                    if "png" in ct and not fname.endswith(".png"):
                        fname = fname.rsplit(".", 1)[0] + ".png"
                    elif "webp" in ct and not fname.endswith(".webp"):
                        fname = fname.rsplit(".", 1)[0] + ".webp"
                    elif "gif" in ct and not fname.endswith(".gif"):
                        fname = fname.rsplit(".", 1)[0] + ".gif"
                    (img_dir / fname).write_bytes(resp.content)
                    downloaded.append(fname)
                    logger.info("图片下载: %s", fname)
                except Exception as e:
                    logger.warning("图片下载失败 %s: %s", fname, e)

    return md, downloaded
