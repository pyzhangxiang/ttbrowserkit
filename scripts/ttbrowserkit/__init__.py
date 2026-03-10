"""
ttbrowserkit - 浏览器自动化工具包核心库

提供基于 Playwright 的浏览器生命周期管理、Cookie 持久化、
反检测注入、重试机制等基础能力。
"""

from .browser import BrowserSession
from .cookies import (
    get_cookie_path,
    load_storage_state,
    save_storage_state,
    delete_cookies,
)

__all__ = [
    "BrowserSession",
    "get_cookie_path",
    "load_storage_state",
    "save_storage_state",
    "delete_cookies",
]
