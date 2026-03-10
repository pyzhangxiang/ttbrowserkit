"""
retry.py - 浏览器操作重试机制

基于 tenacity 提供指数退避重试装饰器，
用于处理 Playwright 超时和其他临时性错误。
"""

from __future__ import annotations

from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
)
from playwright.async_api import TimeoutError as PlaywrightTimeout


# 浏览器操作重试装饰器：
# - 最多重试 3 次
# - 指数退避：最小 0.5 秒，最大 3 秒
# - 对 Playwright 超时和一般异常进行重试
# - 最终失败时重新抛出异常
browser_retry = retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=0.5, min=0.5, max=3),
    retry=retry_if_exception_type((PlaywrightTimeout, Exception)),
    reraise=True,
)
