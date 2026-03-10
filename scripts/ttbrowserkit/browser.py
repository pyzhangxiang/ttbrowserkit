"""
browser.py - 浏览器会话生命周期管理

核心设计：每次操作启动独立的 Chrome 实例，操作完成后自动关闭。
等价于 Go 版本的 newBrowser() + defer Close() 模式。

用法：
    async with BrowserSession("xiaohongshu") as session:
        await session.page.goto("https://www.xiaohongshu.com")
        # 执行操作...
    # Chrome 自动关闭，Cookie 已保存
"""

from __future__ import annotations

from playwright.async_api import async_playwright, Page, Browser, BrowserContext, Playwright
from playwright_stealth import Stealth

from .cookies import load_storage_state, save_storage_state

# 系统 Chrome 路径
DEFAULT_CHROME_PATH = "/usr/bin/google-chrome-stable"

# 反检测启动参数
ANTI_DETECTION_ARGS = [
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-dev-shm-usage",
    "--disable-blink-features=AutomationControlled",
    "--disable-background-networking",
    "--disable-component-update",
    "--no-first-run",
]

# 默认页面操作超时（毫秒）
DEFAULT_TIMEOUT_MS = 30_000


class BrowserSession:
    """
    浏览器会话 async context manager。

    每次进入上下文时启动一个全新的 Chrome 实例，
    退出时保存 Cookie 并彻底关闭浏览器。

    参数：
        site_name: 站点名称，用于 Cookie 文件的读写（如 "xiaohongshu"）
        headless:  是否无头模式，默认 True；登录流程应设为 False
        timeout:   页面操作默认超时时间（毫秒），默认 30000
        chrome_path: Chrome 可执行文件路径
    """

    def __init__(
        self,
        site_name: str,
        *,
        headless: bool = True,
        timeout: int = DEFAULT_TIMEOUT_MS,
        chrome_path: str = DEFAULT_CHROME_PATH,
    ) -> None:
        self.site_name = site_name
        self.headless = headless
        self.timeout = timeout
        self.chrome_path = chrome_path

        # 运行时状态，在 __aenter__ 中初始化
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None

    @property
    def page(self) -> Page:
        """暴露 Playwright Page 对象供外部使用。"""
        if self._page is None:
            raise RuntimeError("BrowserSession 尚未初始化，请在 async with 块内使用")
        return self._page

    async def __aenter__(self) -> BrowserSession:
        """启动 Chrome → 加载 Cookie → 注入反检测 → 返回 self。"""
        # 1. 启动 Playwright
        self._playwright = await async_playwright().start()

        # 2. 启动 Chrome 浏览器实例（launch，不是 connect_over_cdp）
        self._browser = await self._playwright.chromium.launch(
            executable_path=self.chrome_path,
            headless=self.headless,
            args=ANTI_DETECTION_ARGS,
        )

        # 3. 创建浏览器上下文，加载已有的 Cookie（如果存在）
        storage_state = load_storage_state(self.site_name)
        self._context = await self._browser.new_context(
            storage_state=storage_state,
        )

        # 4. 设置默认超时
        self._context.set_default_timeout(self.timeout)

        # 5. 创建页面
        self._page = await self._context.new_page()

        # 6. 注入 playwright-stealth 反检测脚本
        stealth = Stealth()
        await stealth.apply_stealth_async(self._page)

        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """保存 Cookie → 关闭页面 → 关闭上下文 → 关闭浏览器 → 停止 Playwright。"""
        try:
            # 保存 Cookie（即使操作出错也尝试保存）
            if self._context is not None:
                await save_storage_state(self._context, self.site_name)
        except Exception:
            # Cookie 保存失败不应阻塞清理流程
            pass

        # 按顺序关闭资源
        if self._page is not None:
            try:
                await self._page.close()
            except Exception:
                pass

        if self._context is not None:
            try:
                await self._context.close()
            except Exception:
                pass

        if self._browser is not None:
            try:
                await self._browser.close()
            except Exception:
                pass

        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception:
                pass

        # 清理引用
        self._page = None
        self._context = None
        self._browser = None
        self._playwright = None

        # 不吞异常，让调用方处理
        return False
