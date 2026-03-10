"""
知乎 (Zhihu) 站点模块

功能：
- 登录：check_login / login_qrcode / login_with_qrcode
- 下载：download_article（专栏文章 / 问答回答 / 问题页最高赞）
- URL 解析：resolve_url

登录页结构（2026-03 验证）：
- zhihu.com 未登录时直接显示登录页（与 /signin 相同）
- 登录页左侧默认展示知乎 APP 扫码二维码（canvas 元素），无需点击切换
- 二维码容器: .Qrcode-img（内含 canvas.Qrcode-qrcode）
- 登录页特征: .signQr-container / .SignFlowHomepage
- 已登录首页特征: .AppHeader（含用户头像等）
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
from pathlib import Path

from ttbrowserkit.browser import BrowserSession
from ttbrowserkit.html2md import (
    html_to_markdown_with_images as _html_to_markdown_with_images,
    slugify_title as _slugify_title,
)
from ttbrowserkit.retry import browser_retry

logger = logging.getLogger(__name__)

# ========== 选择器常量 ==========

# 登录页特征元素（未登录时存在）— 用于反向检测
_SIGNIN_PAGE_INDICATOR = ".signQr-container"
_SIGNIN_PAGE_INDICATOR_FALLBACK = ".SignFlowHomepage"

# 已登录特征元素（登录后首页存在）
_LOGGED_IN_INDICATOR = ".AppHeader"

# 二维码容器（内含 canvas，用于截图）
_QRCODE_CONTAINER_SELECTOR = ".Qrcode-img"

# ========== URL ==========

_HOME_URL = "https://www.zhihu.com"
_SIGNIN_URL = "https://www.zhihu.com/signin"


# ========== URL 正则 ==========

# 专栏文章: zhuanlan.zhihu.com/p/{id}
_ZHUANLAN_URL_RE = re.compile(r"zhuanlan\.zhihu\.com/p/(\d+)")
# 指定回答: zhihu.com/question/{qid}/answer/{aid}
_ANSWER_URL_RE = re.compile(r"zhihu\.com/question/(\d+)/answer/(\d+)")
# 问题页: zhihu.com/question/{qid}
_QUESTION_URL_RE = re.compile(r"zhihu\.com/question/(\d+)(?:/|$|\?)")
# 独立回答链接: zhihu.com/answer/{aid}（无 question ID，需浏览器跟随重定向）
_ANSWER_ONLY_URL_RE = re.compile(r"zhihu\.com/answer/(\d+)")
# 作者主页: zhihu.com/people/{username}
_PEOPLE_URL_RE = re.compile(r"zhihu\.com/people/([\w-]+)")

# ========== JS 提取脚本 ==========

# 专栏文章页提取
_JS_EXTRACT_ARTICLE = """() => {
    const titleEl = document.querySelector('.Post-Title');
    const contentEl = document.querySelector('.Post-RichText');
    const authorEl = document.querySelector('.AuthorInfo-name') ||
                     document.querySelector('.UserLink-link');
    const timeEl = document.querySelector('.ContentItem-time');
    const voteEl = document.querySelector('.VoteButton--up');

    return JSON.stringify({
        title: titleEl ? titleEl.textContent.trim() : '',
        author: authorEl ? authorEl.textContent.trim() : '',
        content: contentEl ? contentEl.innerHTML : '',
        publish_time: timeEl ? timeEl.textContent.trim() : '',
        vote_count: voteEl ? voteEl.textContent.trim() : '',
    });
}"""

# 问答页提取（指定回答或最高赞回答）
_JS_EXTRACT_ANSWER = """(targetAnswerId) => {
    const questionTitleEl = document.querySelector('.QuestionHeader-title');
    let answerEl = null;

    if (targetAnswerId) {
        // 指定回答：通过 data-zop 属性或 answer card ID 查找
        answerEl = document.querySelector('[data-zop*="' + targetAnswerId + '"]') ||
                   document.getElementById('answer-' + targetAnswerId);
        // 回退：遍历所有 AnswerItem 找匹配的
        if (!answerEl) {
            const items = document.querySelectorAll('.AnswerItem, .List-item');
            for (const item of items) {
                const zop = item.getAttribute('data-zop') || '';
                if (zop.includes(targetAnswerId)) {
                    answerEl = item;
                    break;
                }
            }
        }
    }

    // 没有指定回答或未找到 → 取第一个回答（页面默认按赞数排序）
    if (!answerEl) {
        answerEl = document.querySelector('.AnswerItem') ||
                   document.querySelector('.List-item');
    }

    if (!answerEl) {
        return JSON.stringify({error: 'no_answer_found'});
    }

    const contentEl = answerEl.querySelector('.RichText') ||
                      answerEl.querySelector('.RichContent-inner');
    const authorEl = answerEl.querySelector('.AuthorInfo-name') ||
                     answerEl.querySelector('.UserLink-link');
    const timeEl = answerEl.querySelector('.ContentItem-time');
    const voteEl = answerEl.querySelector('.VoteButton--up');

    return JSON.stringify({
        question_title: questionTitleEl ? questionTitleEl.textContent.trim() : '',
        author: authorEl ? authorEl.textContent.trim() : '',
        content: contentEl ? contentEl.innerHTML : '',
        publish_time: timeEl ? timeEl.textContent.trim() : '',
        vote_count: voteEl ? voteEl.textContent.trim() : '',
    });
}"""

# 作者动态列表提取
_JS_EXTRACT_ACTIVITIES = """(limit) => {
    const items = document.querySelectorAll('.List-item');
    const results = [];
    for (const item of items) {
        if (results.length >= limit) break;

        // 动态类型（"发表了文章" / "回答了问题"）
        const metaTitle = item.querySelector('.ActivityItem-metaTitle');
        const metaText = metaTitle ? metaTitle.textContent.trim() : '';

        // data-zop JSON: {title, type("article"/"answer"), itemId, authorName}
        const contentItem = item.querySelector('.ContentItem[data-zop]');
        if (!contentItem) continue;

        let zop = {};
        try { zop = JSON.parse(contentItem.getAttribute('data-zop') || '{}'); } catch(e) {}

        const itemType = zop.type || '';
        if (itemType !== 'article' && itemType !== 'answer') continue;

        // 链接
        const titleLink = item.querySelector('.ContentItem-title a');
        let url = titleLink ? titleLink.getAttribute('href') : '';
        if (url && url.startsWith('//')) url = 'https:' + url;
        if (url && url.startsWith('/')) url = 'https://www.zhihu.com' + url;

        // 发布时间：ActivityItem-meta 的 span:nth-child(2)
        const metaEl = item.querySelector('.ActivityItem-meta');
        let published = '';
        if (metaEl) {
            const spans = metaEl.querySelectorAll('span');
            if (spans.length >= 2) published = spans[1].textContent.trim();
        }

        // 摘要
        const excerptEl = item.querySelector('.RichContent-inner');
        const excerpt = excerptEl ? excerptEl.textContent.trim().substring(0, 200) : '';

        results.push({
            title: zop.title || '',
            url: url,
            published: published,
            author: zop.authorName || '',
            activity_type: itemType,
            excerpt: excerpt,
            item_id: String(zop.itemId || ''),
        });
    }
    return JSON.stringify(results);
}"""


# ========== 内部辅助 ==========


async def _is_on_signin_page(page) -> bool:
    """检查当前页面是否是登录页（未登录状态）。"""
    try:
        el = await page.query_selector(_SIGNIN_PAGE_INDICATOR)
        if el is not None:
            return True
        el = await page.query_selector(_SIGNIN_PAGE_INDICATOR_FALLBACK)
        return el is not None
    except Exception:
        # 页面正在导航中，上下文可能被销毁
        return False


async def _is_logged_in(page) -> bool:
    """检查当前页面是否处于已登录状态。

    策略：先检查是否在登录页（反向检测，最可靠），
    如果不在登录页则检查是否有 AppHeader。

    知乎首页可能在加载后重定向到 /signin，query_selector
    可能因导航导致上下文销毁，需要捕获异常。
    """
    try:
        if await _is_on_signin_page(page):
            return False
        el = await page.query_selector(_LOGGED_IN_INDICATOR)
        return el is not None
    except Exception:
        # 页面正在导航中，暂时无法判断，返回 False 继续轮询
        return False



async def _wait_past_unhuman(page, timeout: float = 30) -> bool:
    """等待知乎安全验证页（unhuman）自动通过。

    知乎对自动化浏览器会弹出 /account/unhuman 安全验证页面。
    尝试点击验证按钮让其自动通过。

    Returns:
        True — 已通过或不在 unhuman 页面
        False — 仍在 unhuman 页面（超时）
    """
    if "/account/unhuman" not in page.url:
        return True

    # 尝试点击验证按钮
    try:
        btn = await page.query_selector("button")
        if btn:
            logger.info("点击知乎安全验证按钮...")
            await btn.click()
            await asyncio.sleep(3)
    except Exception:
        pass

    poll_interval = 1.0
    elapsed = 0.0
    while elapsed < timeout:
        current_url = page.url
        if "/account/unhuman" not in current_url:
            return True
        await asyncio.sleep(poll_interval)
        elapsed += poll_interval
    return False



class ZhihuSite:
    """知乎站点操作集合

    所有方法均为 @staticmethod + async，每个操作独立创建 BrowserSession，
    遵循 per-operation lifecycle 模式。
    """

    SITE_NAME = "zhihu"
    BASE_URL = "https://www.zhihu.com"

    # ------------------------------------------------------------------
    # 1. check_login — 检测当前登录状态
    # ------------------------------------------------------------------
    @staticmethod
    async def check_login() -> dict:
        """检测知乎登录状态

        使用 headed 模式打开首页，检查是否处于已登录状态。
        知乎未登录时首页直接显示登录页（含 .signQr-container），
        已登录时显示正常首页（含 .AppHeader）。

        Returns:
            {"logged_in": True/False}
        """
        try:
            async with BrowserSession("zhihu", headless=False) as session:
                page = session.page

                await page.goto(_HOME_URL, wait_until="load")
                await asyncio.sleep(2)

                # 先检查是否在 unhuman 验证页
                if "/account/unhuman" in page.url:
                    passed = await _wait_past_unhuman(page, timeout=15)
                    if not passed:
                        return {"logged_in": False, "error": "知乎安全验证未通过"}
                    await asyncio.sleep(1)

                logged_in = await _is_logged_in(page)

                logger.info("知乎登录状态: %s", "已登录" if logged_in else "未登录")
                return {"logged_in": logged_in}

        except Exception as e:
            logger.error("检测知乎登录状态失败: %s", e)
            return {"logged_in": False, "error": str(e)}

    # ------------------------------------------------------------------
    # 2. login_qrcode — 有头模式等待扫码登录
    # ------------------------------------------------------------------
    @staticmethod
    async def login_qrcode() -> dict:
        """有头模式扫码登录

        以非 headless 模式启动浏览器，导航到知乎首页（自动显示登录页和二维码），
        等待用户扫码（最多 240 秒）。

        Returns:
            {"already_logged_in": True}  — 已经登录无需扫码
            {"success": True}            — 扫码成功
            {"success": False, "error": "timeout"} — 超时未完成
        """
        try:
            async with BrowserSession("zhihu", headless=False) as session:
                page = session.page

                await page.goto(_SIGNIN_URL, wait_until="load")
                await asyncio.sleep(2)

                # 检查是否已经登录（可能有 cookie 自动跳转到首页）
                if await _is_logged_in(page):
                    logger.info("知乎已登录，无需扫码")
                    return {"already_logged_in": True}

                # 轮询等待登录（500ms 间隔，最多 240 秒）
                # 扫码成功后知乎会导航到首页，_is_logged_in 内部已处理导航异常
                logger.info("等待用户扫码登录知乎（最多 240 秒）...")
                timeout_seconds = 240
                poll_interval = 0.5
                elapsed = 0.0

                while elapsed < timeout_seconds:
                    if await _is_logged_in(page):
                        logger.info("知乎扫码登录成功")
                        await asyncio.sleep(1)
                        return {"success": True}
                    await asyncio.sleep(poll_interval)
                    elapsed += poll_interval

                logger.warning("知乎扫码登录超时")
                return {"success": False, "error": "timeout"}

        except Exception as e:
            logger.error("知乎扫码登录失败: %s", e)
            return {"success": False, "error": str(e)}

    # ------------------------------------------------------------------
    # 3. login_with_qrcode — 提取二维码图片并等待扫码
    # ------------------------------------------------------------------
    @staticmethod
    async def login_with_qrcode(
        qrcode_save_path: str,
        on_qrcode_ready=None,
    ) -> dict:
        """提取二维码图片 + 轮询等待扫码登录

        以 headed 模式启动浏览器，提取二维码截图保存到本地文件，
        通过回调通知调用方二维码已就绪，然后在同一浏览器会话内
        轮询等待用户扫码（最多 240 秒）。

        知乎登录页的二维码是 canvas 元素（非 img），
        通过对 .Qrcode-img 容器截图来获取二维码图片。

        Args:
            qrcode_save_path: 二维码图片保存路径（PNG）
            on_qrcode_ready: 回调函数，二维码图片保存后调用，参数为图片路径

        Returns:
            {"already_logged_in": True}  — 已经登录
            {"success": True, "qrcode_path": "..."}  — 扫码成功
            {"success": False, "error": "..."}  — 失败/超时
        """
        try:
            async with BrowserSession("zhihu", headless=False) as session:
                page = session.page

                # 导航到登录页（直接用 /signin，避免首页重定向导致上下文销毁）
                try:
                    await page.goto(_SIGNIN_URL, wait_until="load", timeout=30000)
                except Exception as e:
                    return {
                        "success": False,
                        "error": f"页面导航失败，请检查网络连接: {e}",
                    }

                await asyncio.sleep(2)

                # 检查是否已经登录
                if await _is_logged_in(page):
                    logger.info("知乎已登录，无需扫码")
                    return {"already_logged_in": True}

                # 等待二维码容器出现（canvas 在 .Qrcode-img 内）
                try:
                    qrcode_el = await page.wait_for_selector(
                        _QRCODE_CONTAINER_SELECTOR, timeout=15000
                    )
                except Exception:
                    return {
                        "success": False,
                        "error": "二维码加载超时（15秒），知乎页面可能改版或网络异常，请稍后重试",
                    }

                if qrcode_el is None:
                    return {
                        "success": False,
                        "error": "未找到二维码元素，知乎页面可能改版",
                    }

                # 截图保存二维码（对容器截图，包含 canvas 绘制的 QR 码）
                try:
                    await qrcode_el.screenshot(path=qrcode_save_path)
                    logger.info("知乎二维码已保存: %s", qrcode_save_path)
                except Exception as e:
                    return {
                        "success": False,
                        "error": f"二维码截图保存失败: {e}",
                    }

                # 通知调用方二维码已就绪
                if on_qrcode_ready:
                    on_qrcode_ready(qrcode_save_path)

                # 在同一浏览器会话内轮询等待登录
                logger.info("等待用户扫码登录知乎（最多 240 秒）...")
                timeout_seconds = 240
                poll_interval = 0.5
                elapsed = 0.0

                while elapsed < timeout_seconds:
                    if await _is_logged_in(page):
                        logger.info("知乎扫码登录成功")
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
            logger.error("知乎扫码登录失败: %s", e)
            return {"success": False, "error": f"浏览器启动或操作失败: {e}"}

    # ------------------------------------------------------------------
    # 4. resolve_url — 解析知乎链接类型和 ID
    # ------------------------------------------------------------------
    @staticmethod
    async def resolve_url(url: str) -> dict:
        """解析知乎链接，返回 URL 类型和 ID

        支持四种格式：
        - zhuanlan.zhihu.com/p/{id} → type=article
        - zhihu.com/question/{qid}/answer/{aid} → type=answer
        - zhihu.com/question/{qid} → type=question
        - zhihu.com/answer/{aid} → type=answer_only（需浏览器重定向解析）

        Args:
            url: 知乎链接

        Returns:
            {"type": "article"|"answer"|"question"|"answer_only", "url": "...", ...}
            或 {"error": "..."} 失败时
        """
        url = url.strip()

        m = _ZHUANLAN_URL_RE.search(url)
        if m:
            article_id = m.group(1)
            return {
                "type": "article",
                "article_id": article_id,
                "url": f"https://zhuanlan.zhihu.com/p/{article_id}",
            }

        m = _ANSWER_URL_RE.search(url)
        if m:
            qid, aid = m.group(1), m.group(2)
            return {
                "type": "answer",
                "question_id": qid,
                "answer_id": aid,
                "url": f"https://www.zhihu.com/question/{qid}/answer/{aid}",
            }

        m = _QUESTION_URL_RE.search(url)
        if m:
            qid = m.group(1)
            return {
                "type": "question",
                "question_id": qid,
                "url": f"https://www.zhihu.com/question/{qid}",
            }

        m = _ANSWER_ONLY_URL_RE.search(url)
        if m:
            aid = m.group(1)
            return {
                "type": "answer_only",
                "answer_id": aid,
                "url": f"https://www.zhihu.com/answer/{aid}",
            }

        return {"error": f"不支持的知乎链接格式: {url}"}

    # ------------------------------------------------------------------
    # 5. list_author_activities — 获取作者最新动态
    # ------------------------------------------------------------------
    @staticmethod
    async def list_author_activities(author_url: str, limit: int = 20) -> dict:
        """获取知乎作者最新动态（文章+回答）

        以 headed 模式打开作者主页，提取动态列表中的文章和回答。

        Args:
            author_url: 作者主页链接，如 https://www.zhihu.com/people/zhiwei-53-83
            limit: 最多提取条数，默认 20

        Returns:
            {"success": True, "activities": [...]} 或 {"error": "..."}
        """
        m = _PEOPLE_URL_RE.search(author_url)
        if not m:
            return {"error": f"不是有效的知乎作者链接: {author_url}"}

        username = m.group(1)
        url = f"https://www.zhihu.com/people/{username}"

        try:
            async with BrowserSession("zhihu", headless=False) as session:
                page = session.page

                logger.info("获取知乎作者动态: %s", url)
                await page.goto(url, wait_until="domcontentloaded")
                await asyncio.sleep(3)

                # 检查 unhuman 验证
                if "/account/unhuman" in page.url:
                    passed = await _wait_past_unhuman(page, timeout=30)
                    if not passed:
                        return {"error": "知乎安全验证未通过"}
                    await asyncio.sleep(2)

                # 检查登录
                if await _is_on_signin_page(page):
                    return {"error": "需要登录知乎"}

                # 等待动态列表加载
                try:
                    await page.wait_for_selector(".List-item", timeout=15000)
                except Exception:
                    return {"error": f"动态列表加载超时，当前 URL: {page.url}"}

                await asyncio.sleep(1)

                # 提取动态
                raw = await page.evaluate(_JS_EXTRACT_ACTIVITIES, limit)
                activities = json.loads(raw)

                # 模拟真人浏览
                await asyncio.sleep(random.uniform(2, 4))

            logger.info("获取到 %d 条动态", len(activities))
            return {"success": True, "activities": activities}

        except Exception as e:
            logger.error("获取作者动态失败: %s", e)
            return {"error": str(e)}

    # ------------------------------------------------------------------
    # 6. download_article — 下载知乎文章/回答为 Markdown
    # ------------------------------------------------------------------
    @staticmethod
    @browser_retry
    async def download_article(url: str, output_dir: str) -> dict:
        """下载知乎文章/回答为 Markdown + 图片

        支持四种 URL：
        - 专栏文章: zhuanlan.zhihu.com/p/{id}
        - 指定回答: zhihu.com/question/{qid}/answer/{aid}
        - 问题页: zhihu.com/question/{qid}（取默认排序第一个回答）
        - 独立回答: zhihu.com/answer/{aid}（自动跟随重定向到完整 URL）

        Args:
            url: 知乎链接
            output_dir: 输出目录路径，文件名由内部根据 ID+标题自动生成

        Returns:
            {"success": True, "file": "...", "images_downloaded": N, ...}
            或 {"error": "..."} 失败时
        """
        # 解析 URL
        resolved = await ZhihuSite.resolve_url(url)
        if "error" in resolved:
            return resolved

        url_type = resolved["type"]
        canonical_url = resolved["url"]

        # 知乎对 headless 浏览器有严格反检测，必须用 headed 模式
        async with BrowserSession("zhihu", headless=False) as session:
            page = session.page

            logger.info("下载知乎内容: %s -> %s", canonical_url, output_dir)

            await page.goto(canonical_url, wait_until="domcontentloaded")
            await asyncio.sleep(2)

            # 检查是否被重定向到安全验证页（unhuman）
            if "/account/unhuman" in page.url:
                logger.info("遇到知乎安全验证页，等待自动通过...")
                passed = await _wait_past_unhuman(page, timeout=30)
                if not passed:
                    return {
                        "error": "知乎安全验证未通过，请先登录: login-qrcode zhihu"
                    }
                await asyncio.sleep(2)

            # 检查是否在登录页
            if await _is_on_signin_page(page):
                return {
                    "error": "需要登录知乎，请先执行 check-login zhihu / login-qrcode zhihu"
                }

            # answer_only 类型：浏览器会重定向到 /question/{qid}/answer/{aid}
            # 重定向后按 answer 类型处理
            if url_type == "answer_only":
                # 检查重定向后的 URL，提取 question_id 和 answer_id
                redirected_url = page.url
                m = _ANSWER_URL_RE.search(redirected_url)
                if m:
                    resolved["question_id"] = m.group(1)
                    resolved["answer_id"] = m.group(2)
                    canonical_url = f"https://www.zhihu.com/question/{m.group(1)}/answer/{m.group(2)}"
                    url_type = "answer"
                    logger.info("answer_only 重定向到: %s", canonical_url)
                else:
                    # 可能重定向到了问题页或其他页面，尝试按问答页处理
                    logger.info("answer_only 重定向到: %s，按问答页处理", redirected_url)
                    url_type = "answer"
                    resolved["answer_id"] = resolved.get("answer_id", "")

            if url_type == "article":
                # 专栏文章
                try:
                    await page.wait_for_selector(".Post-Title", timeout=15000)
                except Exception:
                    # Check if we're on unhuman page
                    if "/account/unhuman" in page.url:
                        return {"error": "知乎安全验证页未通过，请先登录: login-qrcode zhihu"}
                    return {"error": f"文章页面加载超时，当前 URL: {page.url}"}

                raw = await page.evaluate(_JS_EXTRACT_ARTICLE)
                data = json.loads(raw)

                title = data.get("title", "")
                author = data.get("author", "")
                content_html = data.get("content", "")
                publish_time = data.get("publish_time", "")
                vote_count = data.get("vote_count", "")

            else:
                # 问答页（answer 或 question）
                target_answer_id = resolved.get("answer_id", "")

                try:
                    await page.wait_for_selector(
                        ".QuestionHeader-title", timeout=15000
                    )
                except Exception:
                    if "/account/unhuman" in page.url:
                        return {"error": "知乎安全验证页未通过，请先登录: login-qrcode zhihu"}
                    return {
                        "error": f"问答页面加载超时，当前 URL: {page.url}"
                    }

                # 等一下让回答内容渲染
                await asyncio.sleep(1)

                raw = await page.evaluate(
                    _JS_EXTRACT_ANSWER, target_answer_id
                )
                data = json.loads(raw)

                if data.get("error") == "no_answer_found":
                    return {"error": "未找到回答内容"}

                title = data.get("question_title", "")
                author = data.get("author", "")
                content_html = data.get("content", "")
                publish_time = data.get("publish_time", "")
                vote_count = data.get("vote_count", "")

            # 模拟真人浏览：关闭前随机停留 3-7 秒
            await asyncio.sleep(random.uniform(3, 7))

        # ---- 浏览器已关闭，处理内容 ----

        if not content_html:
            return {"error": "未能提取到正文内容"}

        # ---- 根据 ID + 标题生成文件名 ----
        if url_type == "article":
            file_id = resolved["article_id"]
        elif resolved.get("answer_id"):
            file_id = resolved["answer_id"]
        else:
            file_id = resolved["question_id"]

        title_slug = _slugify_title(title)
        stem = f"{file_id}_{title_slug}" if title_slug else file_id
        out = Path(output_dir)
        md_path = out / f"{stem}.md"
        img_dir = out / stem

        # HTML → Markdown + 下载图片
        body_md, downloaded = await _html_to_markdown_with_images(
            content_html, img_dir, stem
        )

        # 生成 Markdown 文件
        lines: list[str] = []
        lines.append(f"# {title}\n")
        lines.append(f"**作者：** {author}  ")
        lines.append(f"**链接：** {canonical_url}  ")
        if publish_time:
            lines.append(f"**发布时间：** {publish_time}  ")
        if vote_count:
            lines.append(f"**赞同数：** {vote_count}")
        lines.append("")
        lines.append("---")
        lines.append("")
        lines.append(body_md)
        lines.append("")

        md_content = "\n".join(lines)
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text(md_content, encoding="utf-8")

        logger.info(
            "下载完成: %s (图片 %d)", md_path, len(downloaded)
        )
        return {
            "success": True,
            "file": str(md_path),
            "title": title,
            "author": author,
            "images_downloaded": len(downloaded),
            "url_type": url_type,
        }
