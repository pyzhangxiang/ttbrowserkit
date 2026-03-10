"""
cookies.py - Cookie 持久化管理

使用 Playwright 原生的 storage_state 机制保存和加载浏览器状态，
包括 Cookie、localStorage 等。

Cookie 文件存储在 ttbrowserkit/cookies/ 目录下，
按站点名称命名，如 xiaohongshu.json。
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.async_api import BrowserContext

# Cookie 存储目录：ttbrowserkit/cookies/
COOKIES_DIR = Path(__file__).parent.parent.parent / "cookies"


def get_cookie_path(site_name: str) -> Path:
    """获取指定站点的 Cookie 文件路径。"""
    return COOKIES_DIR / f"{site_name}.json"


def load_storage_state(site_name: str) -> str | None:
    """
    加载站点的 storage_state 文件路径。

    如果 Cookie 文件存在，返回其路径字符串（供 Playwright new_context 使用）；
    如果不存在，返回 None。
    """
    cookie_path = get_cookie_path(site_name)
    if cookie_path.exists():
        return str(cookie_path)
    return None


async def save_storage_state(context: BrowserContext, site_name: str) -> None:
    """
    将浏览器上下文的 storage_state 保存到文件。

    自动创建 cookies 目录（如果不存在）。
    """
    cookie_path = get_cookie_path(site_name)
    cookie_path.parent.mkdir(parents=True, exist_ok=True)
    await context.storage_state(path=str(cookie_path))


def delete_cookies(site_name: str) -> bool:
    """
    删除指定站点的 Cookie 文件。

    返回 True 表示文件已删除，False 表示文件不存在。
    """
    cookie_path = get_cookie_path(site_name)
    if cookie_path.exists():
        cookie_path.unlink()
        return True
    return False
