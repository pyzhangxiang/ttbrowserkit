#!/usr/bin/env python3
"""
mcp_server.py - ttbrowserkit MCP stdio server for Claude Code

Exposes browser automation capabilities as MCP tools.

Run directly:
    python3 scripts/mcp_server.py

CC configuration (add to settings):
    {
        "mcpServers": {
            "ttbrowserkit": {
                "command": "python3",
                "args": ["<your_install_path>/scripts/mcp_server.py"]
            }
        }
    }
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

# ---------------------------------------------------------------------------
# Make the ttbrowserkit package importable
# ---------------------------------------------------------------------------
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

from ttbrowserkit.sites.xiaohongshu import XiaohongshuSite
from ttbrowserkit.sites.zhihu import ZhihuSite


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _json_text(data: dict) -> list[TextContent]:
    """Wrap a dict as a single TextContent JSON blob."""
    return [TextContent(type="text", text=json.dumps(data, ensure_ascii=False, indent=2))]


async def _safe_call(coro) -> list[TextContent]:
    """Run *coro*, catch any exception and return it as JSON error."""
    try:
        result = await coro
        return _json_text(result)
    except Exception as exc:
        return _json_text({"error": str(exc)})


# ---------------------------------------------------------------------------
# Server setup
# ---------------------------------------------------------------------------

app = Server("ttbrowserkit")


@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="xhs_check_login",
            description="检查小红书登录状态。返回当前 Cookie 是否有效。",
            inputSchema={
                "type": "object",
                "properties": {},
                "required": [],
            },
        ),
        Tool(
            name="xhs_login",
            description="小红书扫码登录。启动有头浏览器窗口，显示二维码等待用户扫码。",
            inputSchema={
                "type": "object",
                "properties": {},
                "required": [],
            },
        ),
        Tool(
            name="xhs_login_qrcode",
            description="小红书无头扫码登录。提取二维码图片保存到本地，返回图片路径，等待用户扫码。",
            inputSchema={
                "type": "object",
                "properties": {},
                "required": [],
            },
        ),
        Tool(
            name="xhs_search",
            description="在小红书搜索内容。返回搜索结果列表。",
            inputSchema={
                "type": "object",
                "properties": {
                    "keyword": {
                        "type": "string",
                        "description": "搜索关键词",
                    },
                },
                "required": ["keyword"],
            },
        ),
        Tool(
            name="xhs_feed_detail",
            description="获取小红书笔记详情。需要 feed_id 和 xsec_token。",
            inputSchema={
                "type": "object",
                "properties": {
                    "feed_id": {
                        "type": "string",
                        "description": "笔记 ID",
                    },
                    "xsec_token": {
                        "type": "string",
                        "description": "笔记的 xsec_token（从搜索结果或 feeds 获取）",
                    },
                },
                "required": ["feed_id", "xsec_token"],
            },
        ),
        Tool(
            name="xhs_feeds",
            description="获取小红书推荐 feed 列表。",
            inputSchema={
                "type": "object",
                "properties": {},
                "required": [],
            },
        ),
        Tool(
            name="xhs_download",
            description="下载小红书笔记为 Markdown 文件（含图片和评论）。支持直接传入小红书链接（短链接或完整链接），也支持传 feed_id + xsec_token。可选抓取全部评论。",
            inputSchema={
                "type": "object",
                "properties": {
                    "feed_id": {
                        "type": "string",
                        "description": "笔记 ID（与 url 二选一）",
                    },
                    "xsec_token": {
                        "type": "string",
                        "description": "笔记的 xsec_token（与 feed_id 配合使用）",
                    },
                    "url": {
                        "type": "string",
                        "description": "小红书链接（短链接或完整链接，与 feed_id 二选一）",
                    },
                    "output_path": {
                        "type": "string",
                        "description": "输出 .md 文件的完整路径",
                    },
                    "all_comments": {
                        "type": "boolean",
                        "description": "是否抓取全部评论（滚动翻页），默认 false",
                    },
                },
                "required": ["output_path"],
            },
        ),
        Tool(
            name="xhs_comments",
            description="获取小红书笔记全部评论。通过滚动翻页+XHR拦截抓取所有评论（含子评论）。支持直接传入小红书链接或 feed_id + xsec_token。",
            inputSchema={
                "type": "object",
                "properties": {
                    "feed_id": {
                        "type": "string",
                        "description": "笔记 ID（与 url 二选一）",
                    },
                    "xsec_token": {
                        "type": "string",
                        "description": "笔记的 xsec_token（与 feed_id 配合使用）",
                    },
                    "url": {
                        "type": "string",
                        "description": "小红书链接（短链接或完整链接，与 feed_id 二选一）",
                    },
                },
                "required": [],
            },
        ),
        Tool(
            name="xhs_resolve_url",
            description="解析小红书链接为 feed_id + xsec_token。支持短链接（xhslink.com）和完整链接。",
            inputSchema={
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "小红书链接（短链接或完整链接）",
                    },
                },
                "required": ["url"],
            },
        ),
        Tool(
            name="xhs_delete_cookies",
            description="删除小红书的 Cookie 文件。下次操作需要重新登录。",
            inputSchema={
                "type": "object",
                "properties": {},
                "required": [],
            },
        ),
        Tool(
            name="xhs_logout",
            description="登出小红书（删除 Cookie，等同于 xhs_delete_cookies）。",
            inputSchema={
                "type": "object",
                "properties": {},
                "required": [],
            },
        ),
        # ---- Zhihu tools ----
        Tool(
            name="zhihu_check_login",
            description="检查知乎登录状态。返回当前 Cookie 是否有效。",
            inputSchema={
                "type": "object",
                "properties": {},
                "required": [],
            },
        ),
        Tool(
            name="zhihu_login",
            description="知乎扫码登录。启动有头浏览器窗口，显示二维码等待用户扫码。",
            inputSchema={
                "type": "object",
                "properties": {},
                "required": [],
            },
        ),
        Tool(
            name="zhihu_login_qrcode",
            description="知乎无头扫码登录。提取二维码图片保存到本地，返回图片路径，等待用户扫码。",
            inputSchema={
                "type": "object",
                "properties": {},
                "required": [],
            },
        ),
        Tool(
            name="zhihu_delete_cookies",
            description="删除知乎的 Cookie 文件。下次操作需要重新登录。",
            inputSchema={
                "type": "object",
                "properties": {},
                "required": [],
            },
        ),
        Tool(
            name="zhihu_logout",
            description="登出知乎（删除 Cookie，等同于 zhihu_delete_cookies）。",
            inputSchema={
                "type": "object",
                "properties": {},
                "required": [],
            },
        ),
        Tool(
            name="zhihu_download",
            description="下载知乎文章/回答为 Markdown 文件（含图片）。支持专栏文章、指定回答、问题页（取默认排序第一个回答）。",
            inputSchema={
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "知乎链接（专栏文章/问答回答/问题页）",
                    },
                    "output_path": {
                        "type": "string",
                        "description": "输出 .md 文件的完整路径",
                    },
                },
                "required": ["url", "output_path"],
            },
        ),
        Tool(
            name="zhihu_resolve_url",
            description="解析知乎链接类型和 ID。支持专栏文章、指定回答、问题页。",
            inputSchema={
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "知乎链接",
                    },
                },
                "required": ["url"],
            },
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:

    if name == "xhs_check_login":
        return await _safe_call(XiaohongshuSite.check_login())

    elif name == "xhs_login":
        return await _safe_call(XiaohongshuSite.login_qrcode())

    elif name == "xhs_login_qrcode":
        import os
        skill_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        qrcode_path = os.path.join(skill_dir, "qrcode.png")
        return await _safe_call(XiaohongshuSite.login_with_qrcode(qrcode_path))

    elif name == "xhs_search":
        keyword = arguments.get("keyword", "")
        if not keyword:
            return _json_text({"error": "keyword is required"})
        return await _safe_call(XiaohongshuSite.search(keyword))

    elif name == "xhs_feed_detail":
        feed_id = arguments.get("feed_id", "")
        xsec_token = arguments.get("xsec_token", "")
        if not feed_id or not xsec_token:
            return _json_text({"error": "feed_id and xsec_token are required"})
        return await _safe_call(XiaohongshuSite.get_feed_detail(feed_id, xsec_token))

    elif name == "xhs_feeds":
        return await _safe_call(XiaohongshuSite.get_feeds())

    elif name == "xhs_download":
        feed_id = arguments.get("feed_id", "")
        xsec_token = arguments.get("xsec_token", "")
        url = arguments.get("url", "")
        output_path = arguments.get("output_path", "")
        all_comments = arguments.get("all_comments", False)
        if not output_path:
            return _json_text({"error": "output_path is required"})
        # 支持 URL 模式：自动解析 feed_id + xsec_token
        if url and not feed_id:
            resolved = await XiaohongshuSite.resolve_url(url)
            if "error" in resolved:
                return _json_text(resolved)
            feed_id = resolved["feed_id"]
            xsec_token = resolved["xsec_token"]
        if not feed_id:
            return _json_text({"error": "feed_id or url is required"})
        return await _safe_call(XiaohongshuSite.download_post(feed_id, xsec_token, output_path, fetch_all_comments=all_comments))

    elif name == "xhs_comments":
        feed_id = arguments.get("feed_id", "")
        xsec_token = arguments.get("xsec_token", "")
        url = arguments.get("url", "")
        # 支持 URL 模式
        if url and not feed_id:
            resolved = await XiaohongshuSite.resolve_url(url)
            if "error" in resolved:
                return _json_text(resolved)
            feed_id = resolved["feed_id"]
            xsec_token = resolved["xsec_token"]
        if not feed_id:
            return _json_text({"error": "feed_id or url is required"})
        return await _safe_call(XiaohongshuSite.get_all_comments(feed_id, xsec_token))

    elif name == "xhs_resolve_url":
        url = arguments.get("url", "")
        if not url:
            return _json_text({"error": "url is required"})
        return await _safe_call(XiaohongshuSite.resolve_url(url))

    elif name in ("xhs_delete_cookies", "xhs_logout"):
        try:
            from ttbrowserkit.cookies import delete_cookies
            delete_cookies("xiaohongshu")
            return _json_text({"ok": True, "message": "Cookies deleted for xiaohongshu"})
        except Exception as exc:
            return _json_text({"error": str(exc)})

    # ---- Zhihu handlers ----
    elif name == "zhihu_check_login":
        return await _safe_call(ZhihuSite.check_login())

    elif name == "zhihu_login":
        return await _safe_call(ZhihuSite.login_qrcode())

    elif name == "zhihu_login_qrcode":
        import os
        skill_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        qrcode_path = os.path.join(skill_dir, "qrcode.png")
        return await _safe_call(ZhihuSite.login_with_qrcode(qrcode_path))

    elif name in ("zhihu_delete_cookies", "zhihu_logout"):
        try:
            from ttbrowserkit.cookies import delete_cookies
            delete_cookies("zhihu")
            return _json_text({"ok": True, "message": "Cookies deleted for zhihu"})
        except Exception as exc:
            return _json_text({"error": str(exc)})

    elif name == "zhihu_download":
        url = arguments.get("url", "")
        output_path = arguments.get("output_path", "")
        if not url:
            return _json_text({"error": "url is required"})
        if not output_path:
            return _json_text({"error": "output_path is required"})
        return await _safe_call(ZhihuSite.download_article(url, output_path))

    elif name == "zhihu_resolve_url":
        url = arguments.get("url", "")
        if not url:
            return _json_text({"error": "url is required"})
        return await _safe_call(ZhihuSite.resolve_url(url))

    else:
        return _json_text({"error": f"Unknown tool: {name}"})


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------

async def main() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
