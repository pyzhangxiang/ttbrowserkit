"""
小红书 (Xiaohongshu) 站点模块

移植自 Go 项目 xiaohongshu-mcp 的浏览器自动化逻辑，
包括登录检测、搜索、Feed 详情、首页 Feed 列表等操作。

所有 JS 提取逻辑与 Go 原版保持一致:
- login.go    -> check_login / login_qrcode
- search.go   -> search
- feed_detail.go -> get_feed_detail
- feeds.go    -> get_feeds
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path
from urllib.parse import quote, urlparse, parse_qs, unquote

import httpx

from ttbrowserkit.browser import BrowserSession
from ttbrowserkit.retry import browser_retry

logger = logging.getLogger(__name__)

# ========== 文件名工具 ==========


def _slugify_title(title: str, max_chars: int = 60) -> str:
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


# ========== 选择器常量（对应 Go 源码中的 CSS 选择器） ==========

# 登录状态指示元素 (login.go: `.main-container .user .link-wrapper .channel`)
_LOGIN_INDICATOR = ".main-container .user .link-wrapper .channel"

# 二维码图片 (login.go: `.login-container .qrcode-img`)
_QRCODE_IMG_SELECTOR = ".login-container .qrcode-img"

# 页面错误容器 (feed_detail.go: checkPageAccessible)
_ERROR_WRAPPER_SELECTOR = (
    ".access-wrapper, .error-wrapper, .not-found-wrapper, .blocked-wrapper"
)

# 不可访问关键词 (feed_detail.go: checkPageAccessible)
_INACCESSIBLE_KEYWORDS = [
    "当前笔记暂时无法浏览",
    "该内容因违规已被删除",
    "该笔记已被删除",
    "内容不存在",
    "笔记不存在",
    "已失效",
    "私密笔记",
    "仅作者可见",
    "因用户设置，你无法查看",
    "因违规无法查看",
]

# ========== URL 构造 ==========

_EXPLORE_URL = "https://www.xiaohongshu.com/explore"
_SEARCH_URL_TEMPLATE = (
    "https://www.xiaohongshu.com/search_result?keyword={keyword}&source=web_explore_feed"
)
# feed_detail.go: makeFeedDetailURL
_FEED_DETAIL_URL_TEMPLATE = (
    "https://www.xiaohongshu.com/explore/{feed_id}"
    "?xsec_token={xsec_token}&xsec_source=pc_feed"
)
_HOME_URL = "https://www.xiaohongshu.com"

# 用户主页
_USER_PROFILE_RE = re.compile(r"/user/profile/([a-f0-9]+)")
_USER_PROFILE_URL_TEMPLATE = "https://www.xiaohongshu.com/user/profile/{user_id}"

# ========== JS 提取脚本 (与 Go 源码完全一致) ==========

# search.go 第 217-228 行
_JS_EXTRACT_SEARCH_FEEDS = """() => {
    if (window.__INITIAL_STATE__ &&
        window.__INITIAL_STATE__.search &&
        window.__INITIAL_STATE__.search.feeds) {
        const feeds = window.__INITIAL_STATE__.search.feeds;
        const feedsData = feeds.value !== undefined ? feeds.value : feeds._value;
        if (feedsData) {
            return JSON.stringify(feedsData);
        }
    }
    return "";
}"""

# feed_detail.go 第 812-819 行
_JS_EXTRACT_NOTE_DETAIL_MAP = """() => {
    if (window.__INITIAL_STATE__ &&
        window.__INITIAL_STATE__.note &&
        window.__INITIAL_STATE__.note.noteDetailMap) {
        const noteDetailMap = window.__INITIAL_STATE__.note.noteDetailMap;
        return JSON.stringify(noteDetailMap);
    }
    return "";
}"""

# feeds.go 第 32-43 行
_JS_EXTRACT_FEED_FEEDS = """() => {
    if (window.__INITIAL_STATE__ &&
        window.__INITIAL_STATE__.feed &&
        window.__INITIAL_STATE__.feed.feeds) {
        const feeds = window.__INITIAL_STATE__.feed.feeds;
        const feedsData = feeds.value !== undefined ? feeds.value : feeds._value;
        if (feedsData) {
            return JSON.stringify(feedsData);
        }
    }
    return "";
}"""

# 用户主页笔记提取
_JS_EXTRACT_USER_NOTES = """(limit) => {
    const state = window.__INITIAL_STATE__;
    if (!state || !state.user || !state.user.notes) return "";
    const notes = state.user.notes;
    const data = notes._value !== undefined ? notes._value : (notes.value !== undefined ? notes.value : notes);
    if (!Array.isArray(data)) return "";
    // data 是按 tab 分组的数组，取第一个非空 tab（通常 tab 0 = 笔记）
    for (let i = 0; i < data.length; i++) {
        if (Array.isArray(data[i]) && data[i].length > 0) {
            const items = data[i].slice(0, limit).map(item => {
                const card = item.noteCard || {};
                const user = card.user || {};
                return {
                    noteId: card.noteId || item.id || "",
                    title: card.displayTitle || "",
                    type: card.type || "",
                    author: user.nickname || user.nickName || "",
                    xsecToken: item.xsecToken || card.xsecToken || "",
                    likedCount: (card.interactInfo || {}).likedCount || "0",
                };
            });
            return JSON.stringify(items);
        }
    }
    return "";
}"""

# 等待 __INITIAL_STATE__ 可用
_JS_WAIT_INITIAL_STATE = "() => window.__INITIAL_STATE__ !== undefined"


class XiaohongshuSite:
    """小红书站点操作集合

    所有方法均为 @staticmethod + async，每个操作独立创建 BrowserSession，
    遵循 per-operation lifecycle 模式。
    """

    # ------------------------------------------------------------------
    # 1. check_login — 检测当前登录状态
    #    对应 login.go: CheckLoginStatus
    # ------------------------------------------------------------------
    @staticmethod
    async def check_login() -> dict:
        """检测小红书登录状态

        使用 headless 模式打开首页，检查是否存在已登录用户元素。
        对应 Go 源码 login.go 中的 `.main-container .user .link-wrapper .channel`。

        Returns:
            {"logged_in": True/False}
        """
        try:
            async with BrowserSession("xiaohongshu") as session:
                page = session.page

                # login.go: pp.MustNavigate(...).MustWaitLoad()
                await page.goto(_EXPLORE_URL, wait_until="load")

                # login.go: time.Sleep(1 * time.Second)
                await asyncio.sleep(1)

                # login.go: pp.Has(`.main-container .user .link-wrapper .channel`)
                indicator = await page.query_selector(_LOGIN_INDICATOR)
                logged_in = indicator is not None

                logger.info("登录状态检测: %s", "已登录" if logged_in else "未登录")
                return {"logged_in": logged_in}

        except Exception as e:
            logger.error("检测登录状态失败: %s", e)
            return {"logged_in": False, "error": str(e)}

    # ------------------------------------------------------------------
    # 2. login_qrcode — 有头模式等待扫码登录
    #    对应 login.go: Login / WaitForLogin / FetchQrcodeImage
    # ------------------------------------------------------------------
    @staticmethod
    async def login_qrcode() -> dict:
        """有头模式扫码登录

        以非 headless 模式启动浏览器，导航到首页触发二维码弹窗，
        等待用户扫码（最多 240 秒）。

        对应 Go 源码:
        - login.go: Login() — 导航并等待 channel 元素
        - login.go: WaitForLogin() — 每 500ms 轮询登录指示元素

        Returns:
            {"already_logged_in": True}  — 已经登录无需扫码
            {"success": True}            — 扫码成功
            {"success": False, "error": "timeout"} — 超时未完成
        """
        try:
            # 有头模式，用户需要看到二维码
            async with BrowserSession("xiaohongshu", headless=False) as session:
                page = session.page

                # login.go: pp.MustNavigate(...).MustWaitLoad()
                await page.goto(_EXPLORE_URL, wait_until="load")

                # login.go: time.Sleep(2 * time.Second)
                await asyncio.sleep(2)

                # login.go: 检查是否已经登录
                indicator = await page.query_selector(_LOGIN_INDICATOR)
                if indicator is not None:
                    logger.info("已经登录，无需扫码")
                    return {"already_logged_in": True}

                # login.go: WaitForLogin — 每 500ms 轮询，最多等 240 秒
                logger.info("等待用户扫码登录（最多 240 秒）...")
                timeout_seconds = 240
                poll_interval = 0.5  # 500ms, 与 Go 源码一致
                elapsed = 0.0

                while elapsed < timeout_seconds:
                    indicator = await page.query_selector(_LOGIN_INDICATOR)
                    if indicator is not None:
                        logger.info("扫码登录成功")
                        # 稍等一下让 cookie 写入
                        await asyncio.sleep(1)
                        return {"success": True}
                    await asyncio.sleep(poll_interval)
                    elapsed += poll_interval

                logger.warning("扫码登录超时")
                return {"success": False, "error": "timeout"}

        except Exception as e:
            logger.error("扫码登录失败: %s", e)
            return {"success": False, "error": str(e)}

    # ------------------------------------------------------------------
    # 2b. login_with_qrcode — 无头模式提取二维码图片并等待扫码
    #     对应 login.go: FetchQrcodeImage + WaitForLogin
    # ------------------------------------------------------------------
    @staticmethod
    async def login_with_qrcode(
        qrcode_save_path: str,
        on_qrcode_ready=None,
    ) -> dict:
        """无头模式二维码登录

        以 headless 模式启动浏览器，提取二维码图片保存到本地文件，
        通过回调通知调用方二维码已就绪，然后在同一浏览器会话内
        轮询等待用户扫码（最多 240 秒）。

        对应 Go 源码:
        - login.go: FetchQrcodeImage() — 提取 .login-container .qrcode-img 的 src
        - login.go: WaitForLogin() — 每 500ms 轮询

        Args:
            qrcode_save_path: 二维码图片保存路径（PNG）
            on_qrcode_ready: 回调函数，二维码图片保存后调用，参数为图片路径

        Returns:
            {"already_logged_in": True}  — 已经登录
            {"success": True, "qrcode_path": "..."}  — 扫码成功
            {"success": False, "error": "..."}  — 失败/超时
        """
        try:
            async with BrowserSession("xiaohongshu", headless=False) as session:
                page = session.page

                # 导航到首页触发登录弹窗
                try:
                    await page.goto(_EXPLORE_URL, wait_until="load", timeout=30000)
                except Exception as e:
                    return {
                        "success": False,
                        "error": f"页面导航失败，请检查网络连接: {e}",
                    }

                await asyncio.sleep(2)

                # 检查是否已经登录
                indicator = await page.query_selector(_LOGIN_INDICATOR)
                if indicator is not None:
                    logger.info("已经登录，无需扫码")
                    return {"already_logged_in": True}

                # 等待二维码元素出现
                try:
                    qrcode_el = await page.wait_for_selector(
                        _QRCODE_IMG_SELECTOR, timeout=15000
                    )
                except Exception:
                    return {
                        "success": False,
                        "error": "二维码加载超时（15秒），小红书页面可能改版或网络异常，请稍后重试",
                    }

                if qrcode_el is None:
                    return {
                        "success": False,
                        "error": "未找到二维码元素，小红书页面可能改版",
                    }

                # 截图保存二维码（Playwright 原生元素级截图）
                try:
                    await qrcode_el.screenshot(path=qrcode_save_path)
                    logger.info("二维码已保存: %s", qrcode_save_path)
                except Exception as e:
                    return {
                        "success": False,
                        "error": f"二维码截图保存失败: {e}",
                    }

                # 通知调用方二维码已就绪
                if on_qrcode_ready:
                    on_qrcode_ready(qrcode_save_path)

                # 在同一浏览器会话内轮询等待登录
                logger.info("等待用户扫码登录（最多 240 秒）...")
                timeout_seconds = 240
                poll_interval = 0.5
                elapsed = 0.0

                while elapsed < timeout_seconds:
                    indicator = await page.query_selector(_LOGIN_INDICATOR)
                    if indicator is not None:
                        logger.info("扫码登录成功")
                        await asyncio.sleep(1)
                        return {
                            "success": True,
                            "qrcode_path": qrcode_save_path,
                        }
                    await asyncio.sleep(poll_interval)
                    elapsed += poll_interval

                return {
                    "success": False,
                    "error": "扫码登录超时（240秒），请重新执行 login-qrcode 命令获取新的二维码",
                }

        except Exception as e:
            logger.error("无头扫码登录失败: %s", e)
            return {"success": False, "error": f"浏览器启动或操作失败: {e}"}

    # ------------------------------------------------------------------
    # 3. search — 搜索笔记
    #    对应 search.go: Search
    # ------------------------------------------------------------------
    @staticmethod
    @browser_retry
    async def search(keyword: str) -> dict:
        """搜索小红书笔记

        构造搜索 URL，等待 __INITIAL_STATE__ 加载完成后提取 feeds 数据。

        对应 Go 源码 search.go:
        - makeSearchURL: keyword + source=web_explore_feed
        - MustWaitStable + MustWait(__INITIAL_STATE__)
        - JS 提取 search.feeds (第 217-228 行)

        Args:
            keyword: 搜索关键词

        Returns:
            {"feeds": [...], "count": N}
        """
        async with BrowserSession("xiaohongshu") as session:
            page = session.page

            # search.go: makeSearchURL
            search_url = _SEARCH_URL_TEMPLATE.format(keyword=quote(keyword))
            logger.info("搜索: %s -> %s", keyword, search_url)

            # search.go: page.MustNavigate(searchURL); page.MustWaitStable()
            await page.goto(search_url, wait_until="networkidle")

            # search.go: page.MustWait(`() => window.__INITIAL_STATE__ !== undefined`)
            await page.wait_for_function(_JS_WAIT_INITIAL_STATE, timeout=30000)

            # search.go 第 217-228 行: 提取 feeds
            result = await page.evaluate(_JS_EXTRACT_SEARCH_FEEDS)

            if not result:
                logger.warning("搜索结果为空: keyword=%s", keyword)
                return {"feeds": [], "count": 0}

            feeds = json.loads(result)
            logger.info("搜索完成: keyword=%s, count=%d", keyword, len(feeds))
            return {"feeds": feeds, "count": len(feeds)}

    # ------------------------------------------------------------------
    # 4. get_feed_detail — 获取笔记详情
    #    对应 feed_detail.go: GetFeedDetail / extractFeedDetail
    # ------------------------------------------------------------------
    @staticmethod
    @browser_retry
    async def get_feed_detail(feed_id: str, xsec_token: str) -> dict:
        """获取小红书笔记详情

        对应 Go 源码 feed_detail.go:
        - makeFeedDetailURL: /explore/{feedID}?xsec_token=...&xsec_source=pc_feed
        - checkPageAccessible: 检查页面是否可访问
        - extractFeedDetail: 提取 noteDetailMap (第 812-819 行 JS)

        Args:
            feed_id: 笔记 ID
            xsec_token: 安全 token

        Returns:
            笔记详情 dict (包含 note 和 comments)
        """
        async with BrowserSession("xiaohongshu") as session:
            page = session.page

            # feed_detail.go: makeFeedDetailURL
            detail_url = _FEED_DETAIL_URL_TEMPLATE.format(
                feed_id=feed_id, xsec_token=xsec_token
            )
            logger.info("打开 feed 详情页: %s", detail_url)

            # feed_detail.go: page.MustNavigate(url); page.MustWaitDOMStable()
            await page.goto(detail_url, wait_until="domcontentloaded")
            await page.wait_for_load_state("networkidle")

            # feed_detail.go: sleepRandom(1000, 1000)
            await asyncio.sleep(1)

            # feed_detail.go: checkPageAccessible — 检查页面是否可访问
            error_msg = await _check_page_accessible(page)
            if error_msg:
                logger.warning("笔记不可访问: %s", error_msg)
                return {"error": error_msg, "feed_id": feed_id}

            # feed_detail.go: page.MustWait(__INITIAL_STATE__)
            try:
                await page.wait_for_function(_JS_WAIT_INITIAL_STATE, timeout=30000)
            except Exception:
                logger.warning("等待 __INITIAL_STATE__ 超时: feed_id=%s", feed_id)
                return {"error": "等待页面数据超时", "feed_id": feed_id}

            # feed_detail.go 第 812-819 行: 提取 noteDetailMap
            result = await page.evaluate(_JS_EXTRACT_NOTE_DETAIL_MAP)

            if not result:
                logger.warning("无法获取初始状态数据: feed_id=%s", feed_id)
                return {"error": "无法获取笔记详情数据", "feed_id": feed_id}

            note_detail_map = json.loads(result)

            # feed_detail.go: noteDetailMap[feedID]
            if feed_id in note_detail_map:
                detail = note_detail_map[feed_id]
                logger.info("获取笔记详情成功: feed_id=%s", feed_id)
                return detail
            else:
                # 有时候 key 可能不完全匹配，尝试找第一个
                logger.warning(
                    "feed_id=%s 不在 noteDetailMap 中, keys=%s",
                    feed_id,
                    list(note_detail_map.keys()),
                )
                if note_detail_map:
                    first_key = next(iter(note_detail_map))
                    logger.info("使用第一个 key: %s", first_key)
                    return note_detail_map[first_key]
                return {"error": f"feed {feed_id} not found in noteDetailMap"}

    # ------------------------------------------------------------------
    # 5. get_feeds — 获取首页推荐 Feed 列表
    #    对应 feeds.go: GetFeedsList
    # ------------------------------------------------------------------
    @staticmethod
    @browser_retry
    async def get_feeds() -> dict:
        """获取小红书首页推荐 Feed 列表

        对应 Go 源码 feeds.go:
        - 导航到 https://www.xiaohongshu.com
        - MustWaitDOMStable
        - time.Sleep(1 * time.Second)
        - 提取 feed.feeds (第 32-43 行 JS)

        Returns:
            {"feeds": [...], "count": N}
        """
        async with BrowserSession("xiaohongshu") as session:
            page = session.page

            # feeds.go: pp.MustNavigate("https://www.xiaohongshu.com")
            #           pp.MustWaitDOMStable()
            await page.goto(_HOME_URL, wait_until="domcontentloaded")
            await page.wait_for_load_state("networkidle")

            # feeds.go: time.Sleep(1 * time.Second)
            await asyncio.sleep(1)

            # feeds.go: page.MustWait(__INITIAL_STATE__)
            await page.wait_for_function(_JS_WAIT_INITIAL_STATE, timeout=30000)

            # feeds.go 第 32-43 行: 提取 feed.feeds
            result = await page.evaluate(_JS_EXTRACT_FEED_FEEDS)

            if not result:
                logger.warning("首页 feed 列表为空")
                return {"feeds": [], "count": 0}

            feeds = json.loads(result)
            logger.info("获取首页 feeds 完成: count=%d", len(feeds))
            return {"feeds": feeds, "count": len(feeds)}

    # ------------------------------------------------------------------
    # 6. download_post — 下载笔记为 Markdown + 图片
    # ------------------------------------------------------------------
    @staticmethod
    @browser_retry
    async def download_post(
        feed_id: str,
        xsec_token: str,
        output_dir: str,
        fetch_all_comments: bool = True,
    ) -> dict:
        """下载小红书笔记为 Markdown 文件，同时下载图片到同名目录

        一步完成：导航页面 → 提取数据 → 下载图片 → 生成 Markdown。

        Args:
            feed_id: 笔记 ID
            xsec_token: 安全 token
            output_dir: 输出目录路径，文件名由内部根据 feed_id+标题自动生成
            fetch_all_comments: 是否抓取全部评论（滚动翻页），默认 False

        Returns:
            {"success": True, "file": "...", "images_downloaded": N, ...}
            或 {"error": "..."} 失败时
        """

        # ---- 导航并提取数据（与 get_feed_detail 相同逻辑） ----
        async with BrowserSession("xiaohongshu") as session:
            page = session.page

            detail_url = _FEED_DETAIL_URL_TEMPLATE.format(
                feed_id=feed_id, xsec_token=xsec_token
            )
            logger.info("下载笔记: %s -> %s", feed_id, detail_url)

            await page.goto(detail_url, wait_until="domcontentloaded")
            await page.wait_for_load_state("networkidle")
            await asyncio.sleep(1)

            error_msg = await _check_page_accessible(page)
            if error_msg:
                return {"error": error_msg, "feed_id": feed_id}

            try:
                await page.wait_for_function(_JS_WAIT_INITIAL_STATE, timeout=30000)
            except Exception:
                return {"error": "等待页面数据超时", "feed_id": feed_id}

            result = await page.evaluate(_JS_EXTRACT_NOTE_DETAIL_MAP)
            if not result:
                return {"error": "无法获取笔记详情数据", "feed_id": feed_id}

            note_detail_map = json.loads(result)
            if feed_id in note_detail_map:
                detail = note_detail_map[feed_id]
            elif note_detail_map:
                detail = note_detail_map[next(iter(note_detail_map))]
            else:
                return {"error": f"feed {feed_id} not found in noteDetailMap"}

        # ---- 解析数据 ----
        note = detail.get("note", {})
        comments_data = detail.get("comments", {})

        title = note.get("title", "")
        desc = note.get("desc", "")
        ip_location = note.get("ipLocation", "")
        user = note.get("user", {})
        author = user.get("nickname", "") or user.get("nickName", "")
        image_list = note.get("imageList", [])
        interact = note.get("interactInfo", {})
        comments_list = comments_data.get("list", [])

        # 如果需要全部评论，调用 get_all_comments（会启动新的浏览器会话）
        if fetch_all_comments:
            all_comments_result = await XiaohongshuSite.get_all_comments(
                feed_id, xsec_token
            )
            if "comments" in all_comments_result:
                comments_list = all_comments_result["comments"]
                logger.info(
                    "全量评论: %d 条 (原始 %d 条)",
                    len(comments_list),
                    len(comments_data.get("list", [])),
                )

        # ---- 根据 feed_id + 标题生成文件名 ----
        title_slug = _slugify_title(title)
        stem = f"{feed_id}_{title_slug}" if title_slug else feed_id
        out = Path(output_dir)
        md_path = out / f"{stem}.md"
        img_dir = out / stem

        # ---- 下载图片 ----
        img_dir.mkdir(parents=True, exist_ok=True)
        downloaded = []

        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            for i, img in enumerate(image_list, 1):
                url = img.get("urlDefault", "")
                if not url:
                    continue
                if url.startswith("//"):
                    url = "https:" + url
                try:
                    resp = await client.get(url)
                    resp.raise_for_status()
                    ct = resp.headers.get("content-type", "")
                    if "png" in ct:
                        ext = ".png"
                    elif "webp" in ct:
                        ext = ".webp"
                    else:
                        ext = ".jpg"
                    fname = f"{i}{ext}"
                    (img_dir / fname).write_bytes(resp.content)
                    downloaded.append(fname)
                    logger.info("图片 %d/%d: %s", i, len(image_list), fname)
                except Exception as e:
                    logger.warning("图片 %d 下载失败: %s", i, e)

        # ---- 生成 Markdown ----
        lines: list[str] = []

        # 标题 + 元信息
        lines.append(f"# {title}\n")
        lines.append(f"**作者：** {author}  ")
        lines.append(
            f"**链接：** https://www.xiaohongshu.com/explore/{feed_id}  "
        )
        if ip_location:
            lines.append(f"**IP属地：** {ip_location}  ")
        stats = []
        if interact.get("likedCount"):
            stats.append(f"点赞 {interact['likedCount']}")
        if interact.get("collectedCount"):
            stats.append(f"收藏 {interact['collectedCount']}")
        if interact.get("commentCount"):
            stats.append(f"评论 {interact['commentCount']}")
        if stats:
            lines.append(f"**{' · '.join(stats)}**")
        lines.append("")
        lines.append("---")
        lines.append("")

        # 图片
        for fname in downloaded:
            lines.append(f"![{fname}](./{stem}/{fname})")
            lines.append("")

        # 正文
        if desc:
            lines.append(desc)
            lines.append("")
            lines.append("---")
            lines.append("")

        # 评论
        if comments_list:
            lines.append(f"## 评论 ({len(comments_list)})")
            lines.append("")
            for c in comments_list:
                lines.append(_format_comment(c, indent=0))
            lines.append("")

        md_content = "\n".join(lines)
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text(md_content, encoding="utf-8")

        logger.info(
            "下载完成: %s (图片 %d/%d, 评论 %d)",
            md_path, len(downloaded), len(image_list), len(comments_list),
        )
        return {
            "success": True,
            "file": str(md_path),
            "images_downloaded": len(downloaded),
            "images_total": len(image_list),
            "comments_count": len(comments_list),
            "title": title,
        }

    # ------------------------------------------------------------------
    # 7. get_all_comments — 抓取笔记全部评论（滚动翻页 + XHR 拦截）
    # ------------------------------------------------------------------
    @staticmethod
    @browser_retry
    async def get_all_comments(feed_id: str, xsec_token: str) -> dict:
        """抓取小红书笔记的全部评论

        通过浏览器内滚动触发评论翻页加载，拦截 XHR 响应收集全部评论。
        不需要逆向 API 签名，利用页面自身的 session 和反爬机制。

        Args:
            feed_id: 笔记 ID
            xsec_token: 安全 token

        Returns:
            {"comments": [...], "total": N, "fetched": N, "feed_id": "..."}
            或 {"error": "..."} 失败时
        """
        async with BrowserSession("xiaohongshu") as session:
            page = session.page

            detail_url = _FEED_DETAIL_URL_TEMPLATE.format(
                feed_id=feed_id, xsec_token=xsec_token
            )
            logger.info("获取全部评论: %s", detail_url)

            # 收集 XHR 拦截到的评论
            xhr_comments: list[dict] = []
            xhr_sub_comments: list[dict] = []
            _has_more_comments = True

            async def _on_response(response):
                nonlocal _has_more_comments
                url = response.url
                if "/api/sns/web/v2/comment/page" in url and "/sub/" not in url:
                    try:
                        body = await response.json()
                        data = body.get("data", {})
                        comments = data.get("comments", [])
                        xhr_comments.extend(comments)
                        has_more = data.get("has_more", False)
                        if not has_more:
                            _has_more_comments = False
                        logger.info(
                            "拦截评论 API: +%d 条, has_more=%s",
                            len(comments), has_more,
                        )
                    except Exception as e:
                        logger.debug("解析评论 API 响应失败: %s", e)
                elif "/api/sns/web/v2/comment/sub/page" in url:
                    try:
                        body = await response.json()
                        data = body.get("data", {})
                        comments = data.get("comments", [])
                        xhr_sub_comments.extend(comments)
                        logger.info("拦截子评论 API: +%d 条", len(comments))
                    except Exception as e:
                        logger.debug("解析子评论 API 响应失败: %s", e)

            page.on("response", _on_response)

            # 导航到详情页
            await page.goto(detail_url, wait_until="domcontentloaded")
            await page.wait_for_load_state("networkidle")
            await asyncio.sleep(1)

            # 检查页面是否可访问
            error_msg = await _check_page_accessible(page)
            if error_msg:
                return {"error": error_msg, "feed_id": feed_id}

            # 等待 __INITIAL_STATE__
            try:
                await page.wait_for_function(_JS_WAIT_INITIAL_STATE, timeout=30000)
            except Exception:
                return {"error": "等待页面数据超时", "feed_id": feed_id}

            # 提取初始评论和总评论数
            result = await page.evaluate(_JS_EXTRACT_NOTE_DETAIL_MAP)
            if not result:
                return {"error": "无法获取笔记详情数据", "feed_id": feed_id}

            note_detail_map = json.loads(result)
            detail = note_detail_map.get(feed_id)
            if not detail and note_detail_map:
                detail = note_detail_map[next(iter(note_detail_map))]
            if not detail:
                return {"error": f"feed {feed_id} not found in noteDetailMap"}

            note = detail.get("note", {})
            interact = note.get("interactInfo", {})
            total_comment_count = int(interact.get("commentCount", "0") or "0")
            initial_comments = detail.get("comments", {}).get("list", [])

            logger.info(
                "初始评论: %d 条, 总评论数: %d",
                len(initial_comments), total_comment_count,
            )

            # 如果初始评论已经包含全部，直接返回
            if total_comment_count <= len(initial_comments):
                return {
                    "comments": initial_comments,
                    "total": total_comment_count,
                    "fetched": len(initial_comments),
                    "feed_id": feed_id,
                }

            # ---- 滚动加载更多评论 ----
            max_scroll_time = 120  # 最大滚动时间（秒）
            no_new_count = 0  # 连续无新评论的滚动次数
            max_no_new = 4  # 连续 N 次无新评论则停止
            prev_xhr_count = 0
            start_time = asyncio.get_event_loop().time()

            while True:
                elapsed = asyncio.get_event_loop().time() - start_time
                if elapsed > max_scroll_time:
                    logger.info("滚动超时 (%ds), 停止", max_scroll_time)
                    break

                if not _has_more_comments:
                    logger.info("服务端返回 has_more=false, 停止滚动")
                    break

                # 滚动到页面底部
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await asyncio.sleep(1.5)

                # 检查是否有新评论
                current_xhr_count = len(xhr_comments)
                if current_xhr_count == prev_xhr_count:
                    no_new_count += 1
                    if no_new_count >= max_no_new:
                        logger.info("连续 %d 次滚动无新评论, 停止", max_no_new)
                        break
                    # 多滚一点试试
                    await page.evaluate(
                        "window.scrollBy(0, window.innerHeight * 2)"
                    )
                    await asyncio.sleep(1)
                else:
                    no_new_count = 0
                    prev_xhr_count = current_xhr_count

                logger.info(
                    "已收集 XHR 评论: %d 条 (%.0fs)",
                    current_xhr_count, elapsed,
                )

            # ---- 展开子评论 ----
            # 尝试点击所有 "展开更多回复" 按钮
            expand_clicks = 0
            for _ in range(50):  # 最多点击 50 次
                # 查找 "展开" / "查看更多" 类的按钮
                expand_btn = await page.query_selector(
                    "[class*='show-more'], [class*='expand']"
                    ":not([class*='loading'])"
                )
                if not expand_btn:
                    # 尝试文本匹配
                    expand_btn = await page.evaluate("""() => {
                        const spans = document.querySelectorAll('span, div, a');
                        for (const el of spans) {
                            const text = el.textContent || '';
                            if (/展开\\s*\\d+\\s*条/.test(text) || /查看更多回复/.test(text)) {
                                el.click();
                                return true;
                            }
                        }
                        return false;
                    }""")
                    if expand_btn:
                        expand_clicks += 1
                        await asyncio.sleep(1)
                        continue
                    break
                try:
                    await expand_btn.click()
                    expand_clicks += 1
                    await asyncio.sleep(1)
                except Exception:
                    break

            if expand_clicks > 0:
                logger.info("展开子评论: 点击 %d 次", expand_clicks)

            # ---- 合并评论 ----
            # 用 comment ID 去重
            seen_ids: set[str] = set()
            merged: list[dict] = []

            def _add_comment(c: dict):
                cid = c.get("id", "")
                if not cid:
                    # 没有 id 的用 content hash 代替
                    content = c.get("content", "")
                    user_id = c.get("userInfo", {}).get("userId", "")
                    cid = f"{user_id}:{content}"
                if cid not in seen_ids:
                    seen_ids.add(cid)
                    merged.append(c)

            # 先加初始评论
            for c in initial_comments:
                _add_comment(c)

            # 再加 XHR 拦截到的评论
            for c in xhr_comments:
                _add_comment(c)

            # 将 XHR 子评论合并到对应父评论
            if xhr_sub_comments:
                # 建立 parent comment id -> comment 的映射
                parent_map: dict[str, dict] = {}
                for c in merged:
                    cid = c.get("id", "")
                    if cid:
                        parent_map[cid] = c
                for sc in xhr_sub_comments:
                    target_id = sc.get("targetCommentId", "")
                    if target_id and target_id in parent_map:
                        parent = parent_map[target_id]
                        subs = parent.get("subComments", []) or []
                        # 去重
                        sub_ids = {s.get("id", "") for s in subs}
                        if sc.get("id", "") not in sub_ids:
                            subs.append(sc)
                            parent["subComments"] = subs
                    else:
                        # 子评论找不到父评论，作为顶级评论加入
                        _add_comment(sc)

            fetched = len(merged)
            logger.info(
                "评论收集完成: fetched=%d, total=%d, xhr=%d, sub_xhr=%d",
                fetched, total_comment_count, len(xhr_comments), len(xhr_sub_comments),
            )

            return {
                "comments": merged,
                "total": total_comment_count,
                "fetched": fetched,
                "feed_id": feed_id,
            }

    # ------------------------------------------------------------------
    # 9. resolve_url — 解析小红书链接为 feed_id + xsec_token
    # ------------------------------------------------------------------
    @staticmethod
    async def resolve_url(url: str) -> dict:
        """解析小红书链接，返回 feed_id 和 xsec_token

        支持的链接格式：
        - 短链接: http://xhslink.com/... (302 重定向解析)
        - Explore: https://www.xiaohongshu.com/explore/{feed_id}?xsec_token=...
        - Discovery: https://www.xiaohongshu.com/discovery/item/{feed_id}?xsec_token=...

        Args:
            url: 任意格式的小红书链接

        Returns:
            {"feed_id": "...", "xsec_token": "...", "resolved_url": "..."}
            或 {"error": "..."} 失败时
        """
        return await _resolve_xhs_url(url)

    # ------------------------------------------------------------------
    # 10. list_author_activities — 获取博主笔记列表
    # ------------------------------------------------------------------
    @staticmethod
    async def list_author_activities(author_url: str, limit: int = 20) -> dict:
        """获取小红书博主最新笔记列表

        用浏览器打开博主主页（支持短链接，浏览器自动跟随重定向），
        等待笔记列表加载后从 __INITIAL_STATE__ 提取。

        Args:
            author_url: 博主主页链接，支持：
                - 完整链接: https://www.xiaohongshu.com/user/profile/{user_id}?xsec_token=...
                - 短链接: https://xhslink.com/m/xxx（浏览器跟随重定向）
            limit: 最多提取条数，默认 20

        Returns:
            {"success": True, "activities": [...]} 或 {"error": "..."}
        """
        url = author_url.strip()

        try:
            async with BrowserSession("xiaohongshu", headless=False) as session:
                page = session.page

                logger.info("获取小红书博主笔记: %s", url)
                await page.goto(url, wait_until="domcontentloaded")
                await page.wait_for_load_state("networkidle")
                await asyncio.sleep(3)

                final_url = page.url
                logger.info("最终 URL: %s", final_url)

                # 确认落地页是用户主页
                if "/user/profile/" not in final_url:
                    return {"error": f"未跳转到用户主页，当前 URL: {final_url}"}

                # 等待 __INITIAL_STATE__
                try:
                    await page.wait_for_function(
                        _JS_WAIT_INITIAL_STATE, timeout=30000
                    )
                except Exception:
                    return {"error": f"页面数据加载超时，当前 URL: {final_url}"}

                # 等待笔记数据填充（user_posted API 响应后 Vue 会更新 state）
                await asyncio.sleep(2)

                # 提取笔记列表
                raw = await page.evaluate(_JS_EXTRACT_USER_NOTES, limit)

                if not raw:
                    return {
                        "success": True,
                        "activities": [],
                        "message": "该博主暂无公开笔记",
                    }

                notes = json.loads(raw)

                # 转为统一的 activities 格式
                activities = []
                for note in notes:
                    note_id = note.get("noteId", "")
                    xsec_token = note.get("xsecToken", "")
                    note_url = (
                        f"https://www.xiaohongshu.com/explore/{note_id}"
                        f"?xsec_token={quote(xsec_token)}&xsec_source=pc_feed"
                        if xsec_token else
                        f"https://www.xiaohongshu.com/explore/{note_id}"
                    )
                    activities.append({
                        "title": note.get("title", ""),
                        "url": note_url,
                        "published": "",  # 用户主页不显示发布时间
                        "author": note.get("author", ""),
                        "item_id": note_id,
                        "type": note.get("type", ""),
                    })

                # 模拟真人浏览
                await asyncio.sleep(1)

            logger.info("获取到 %d 条笔记", len(activities))
            return {"success": True, "activities": activities}

        except Exception as e:
            logger.error("获取博主笔记失败: %s", e)
            return {"error": str(e)}


# ========== 内部辅助函数 ==========


async def _check_page_accessible(page) -> str | None:
    """检查页面是否可访问

    对应 feed_detail.go: checkPageAccessible
    查找错误容器元素，如果包含不可访问关键词则返回错误消息。

    Returns:
        None — 页面可访问
        str  — 不可访问的原因
    """
    # feed_detail.go: time.Sleep(500 * time.Millisecond)
    await asyncio.sleep(0.5)

    # feed_detail.go: page.Timeout(2*time.Second).Element(selector)
    try:
        wrapper_el = await page.wait_for_selector(
            _ERROR_WRAPPER_SELECTOR, timeout=2000
        )
    except Exception:
        # 未找到错误容器，说明页面可访问
        return None

    if wrapper_el is None:
        return None

    try:
        text = (await wrapper_el.text_content()) or ""
    except Exception:
        # 无法获取文本，假设页面可访问
        return None

    text = text.strip()

    # feed_detail.go: 检查关键词
    for kw in _INACCESSIBLE_KEYWORDS:
        if kw in text:
            return f"笔记不可访问: {kw}"

    # 如果有文本但不匹配关键词，返回未知错误
    if text:
        return f"笔记不可访问: {text}"

    return None


def _format_comment(comment: dict, indent: int = 0) -> str:
    """格式化单条评论为 Markdown（含子评论递归缩进）"""
    prefix = "  " * indent
    user_info = comment.get("userInfo", {})
    nickname = user_info.get("nickname", "") or user_info.get("nickName", "匿名")
    content = comment.get("content", "")
    ip_loc = comment.get("ipLocation", "")
    like_count = comment.get("likeCount", "")

    header = f"{prefix}- **{nickname}**"
    if ip_loc:
        header += f" ({ip_loc})"
    parts = [header]
    if content:
        # 多行内容缩进对齐
        for line in content.split("\n"):
            parts.append(f"{prefix}  {line}")
    if like_count and str(like_count) != "0":
        parts.append(f"{prefix}  *{like_count} 赞*")

    for sub in comment.get("subComments", []) or []:
        parts.append(_format_comment(sub, indent + 1))

    return "\n".join(parts)


# 小红书链接正则：匹配 /explore/{id} 或 /discovery/item/{id}
_XHS_PATH_RE = re.compile(r"/(?:explore|discovery/item)/([a-f0-9]{24})")

# 短链接域名
_SHORT_LINK_HOSTS = {"xhslink.com", "xhs.link"}


def _parse_xhs_full_url(url: str) -> dict | None:
    """从完整小红书 URL 中提取 feed_id 和 xsec_token"""
    parsed = urlparse(url)
    m = _XHS_PATH_RE.search(parsed.path)
    if not m:
        return None
    feed_id = m.group(1)
    qs = parse_qs(parsed.query)
    xsec_token = qs.get("xsec_token", [""])[0]
    return {"feed_id": feed_id, "xsec_token": xsec_token, "resolved_url": url}


async def _resolve_xhs_url(url: str) -> dict:
    """解析任意格式的小红书链接为 feed_id + xsec_token

    短链接通过 HTTP HEAD 获取 302 Location，不跟随重定向。
    完整链接直接解析 URL path 和 query string。
    """
    url = url.strip()

    # 先尝试直接解析（explore / discovery 链接）
    result = _parse_xhs_full_url(url)
    if result:
        return result

    # 检查是否是短链接
    parsed = urlparse(url if "://" in url else "http://" + url)
    if parsed.hostname not in _SHORT_LINK_HOSTS:
        return {"error": f"不支持的链接格式: {url}"}

    # 短链接：HEAD 请求获取 302 Location
    try:
        async with httpx.AsyncClient(
            timeout=10.0, follow_redirects=False
        ) as client:
            resp = await client.head(url)
            if resp.status_code in (301, 302, 303, 307, 308):
                location = resp.headers.get("location", "")
                if location:
                    result = _parse_xhs_full_url(location)
                    if result:
                        return result
                    return {"error": f"重定向地址无法解析: {location}"}

            # HEAD 可能被拒，回退到 GET
            resp = await client.get(url)
            if resp.status_code in (301, 302, 303, 307, 308):
                location = resp.headers.get("location", "")
                if location:
                    result = _parse_xhs_full_url(location)
                    if result:
                        return result
            return {"error": f"短链接解析失败 (HTTP {resp.status_code})"}
    except Exception as e:
        return {"error": f"短链接请求失败: {e}"}
