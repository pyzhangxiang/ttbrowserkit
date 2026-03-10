#!/usr/bin/env python3
"""
cli.py - ttbrowserkit CLI entry point for OC agent

Usage: python scripts/cli.py <command> [args] [-of output_file]

Commands:
    check-login <site>                    Check login status
    login <site>                          Login via QR code (headed mode)
    search <site> <keyword>               Search content
    detail <site> <feed_id> [xsec_token]  Get content detail
    download <site> <feed_id> <xsec_token> <output_path> [--all-comments]  Download post as markdown
    download <site> <url> <output_path> [--all-comments]  Download post from XHS URL
    comments <site> <feed_id> <xsec_token>  Get all comments (scroll + XHR intercept)
    comments <site> <url>                 Get all comments from XHS URL
    resolve-url <site> <url>              Resolve XHS URL to feed_id + xsec_token
    feeds <site>                          Get recommended feeds
    list-author <site> <author_url>       List author activities (zhihu)
    login-qrcode <site>                   Login via QR code (headed, saves QR image)
    login-qrcode-ascii <site>             Same as login-qrcode + print ASCII QR to terminal
    delete-cookies <site>                 Delete cookies for site
    logout <site>                         Alias for delete-cookies

The -of (--output-file) flag redirects all stdout/stderr to a file.
When used, the script prints "OUTPUT_READY: <filepath>" to the original
stdout upon completion, allowing the OC agent to reliably read the full
output via read_file.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

# ---------------------------------------------------------------------------
# Make the ttbrowserkit package importable (it lives alongside this script)
# ---------------------------------------------------------------------------
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))

from ttbrowserkit.output import OutputRedirector
from ttbrowserkit.sites.generic import GenericSite
from ttbrowserkit.sites.xiaohongshu import XiaohongshuSite
from ttbrowserkit.sites.zhihu import ZhihuSite


# ---------------------------------------------------------------------------
# Site registry
# ---------------------------------------------------------------------------
SITES = {
    "generic": GenericSite,
    "xiaohongshu": XiaohongshuSite,
    "zhihu": ZhihuSite,
}

DEFAULT_SITE = "xiaohongshu"


# ---------------------------------------------------------------------------
# Command dispatch
# ---------------------------------------------------------------------------

async def cmd_check_login(site_cls) -> dict:
    return await site_cls.check_login()


async def cmd_login(site_cls) -> dict:
    return await site_cls.login_qrcode()


async def cmd_search(site_cls, keyword: str) -> dict:
    return await site_cls.search(keyword)


async def cmd_detail(site_cls, feed_id: str, xsec_token: str) -> dict:
    return await site_cls.get_feed_detail(feed_id, xsec_token)


async def cmd_feeds(site_cls) -> dict:
    return await site_cls.get_feeds()


async def cmd_download(site_cls, feed_id: str, xsec_token: str, output_dir: str, fetch_all_comments: bool = False) -> dict:
    return await site_cls.download_post(feed_id, xsec_token, output_dir, fetch_all_comments=fetch_all_comments)


async def cmd_comments(site_cls, feed_id: str, xsec_token: str) -> dict:
    return await site_cls.get_all_comments(feed_id, xsec_token)


async def cmd_resolve_url(site_cls, url: str) -> dict:
    return await site_cls.resolve_url(url)


async def cmd_list_author(site_cls, author_url: str, limit: int = 20) -> dict:
    return await site_cls.list_author_activities(author_url, limit)


async def cmd_delete_cookies(site_name: str) -> dict:
    from ttbrowserkit.cookies import delete_cookies
    delete_cookies(site_name)
    return {"ok": True, "message": f"Cookies deleted for {site_name}"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _looks_like_url(s: str) -> bool:
    """判断字符串是否像 URL（而非 feed_id）"""
    return s.startswith("http://") or s.startswith("https://")


def print_usage() -> None:
    print(
        "Usage: python scripts/cli.py <command> [args] [-of output_file]\n"
        "\n"
        "Commands:\n"
        "    check-login [site]                    Check login status\n"
        "    login [site]                          Login via QR code (headed mode)\n"
        "    login-qrcode [site]                   Login via QR code (headed, saves QR image)\n"
        "    login-qrcode-ascii [site]              Same + print ASCII QR to terminal\n"
        "    search [site] <keyword>               Search content\n"
        "    detail [site] <feed_id> [xsec_token]  Get content detail\n"
        "    download [site] <feed_id> <xsec_token> <output_path> [--all-comments]\n"
        "    download [site] <url> <output_path> [--all-comments]  Download from XHS URL\n"
        "    comments [site] <feed_id> <xsec_token>  Get all comments\n"
        "    comments [site] <url>                 Get all comments from XHS URL\n"
        "    resolve-url [site] <url>              Resolve XHS URL to feed_id + xsec_token\n"
        "    feeds [site]                          Get recommended feeds\n"
        "    list-author [site] <author_url>       List author activities (zhihu)\n"
        "    delete-cookies [site]                 Delete cookies for site\n"
        "    logout [site]                         Alias for delete-cookies\n"
        "\n"
        "Default site: xiaohongshu\n"
        "\n"
        "Options:\n"
        "    -of, --output-file <path>   Redirect output to file\n"
        "    --all-comments              Fetch all comments (for download command)\n"
    )


def output_json(data: dict) -> None:
    """Print a dict as formatted JSON to stdout."""
    print(json.dumps(data, ensure_ascii=False, indent=2))


def _write_output(filepath: str | None, data: dict) -> None:
    """Write JSON to output file and print OUTPUT_READY marker."""
    if filepath:
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        sys.stdout.write(f"OUTPUT_READY: {filepath}\n")
        sys.stdout.flush()
    else:
        output_json(data)


def _png_to_terminal_blocks(image_path: str, new_width: int = 60, out=None) -> None:
    """将 QR 码 PNG 图片转为终端字符输出。

    参考 ASCII art 方案：缩放图片 → 灰度 → 二值化 → 黑白字符映射。
    QR 码是纯黑白，用 '██' 表示黑色，'  ' 表示白色。
    0.5 系数校正终端字符高宽比，使输出接近正方形。
    外围加白色静默区帮助手机扫描识别。
    """
    from PIL import Image

    if out is None:
        out = sys.stdout

    img = Image.open(image_path)

    # 缩放，保持纵横比，0.5 校正字符高度
    width, height = img.size
    ratio = height / width
    new_height = int(new_width * ratio * 0.5)
    img = img.resize((new_width, new_height))

    # 转灰度 → 二值化
    img = img.convert("L")

    # 加白色静默区（QR 码规范要求四周白色区域帮助识别）
    quiet = 2  # 2 字符宽的白色边距
    total_w = new_width + quiet * 2
    blank_line = "  " * total_w

    threshold = 128
    out.write(blank_line + "\n")
    out.write(blank_line + "\n")
    for y in range(new_height):
        line = "  " * quiet  # 左静默区
        for x in range(new_width):
            line += "██" if img.getpixel((x, y)) < threshold else "  "
        line += "  " * quiet  # 右静默区
        out.write(line + "\n")
    out.write(blank_line + "\n")
    out.write(blank_line + "\n")
    out.flush()


def _run_login_qrcode(site_cls, output_file_path: str | None, ascii_mode: bool = False, site_name: str = "xiaohongshu") -> int:
    """login-qrcode 的特殊处理：分两阶段输出。

    阶段 1: 二维码就绪 → 写入 qrcode_path → OUTPUT_READY
    阶段 2: 登录结果 → 覆盖写入结果 → OUTPUT_READY

    Args:
        ascii_mode: True 时在终端打印 ASCII QR 码（login-qrcode-ascii 命令）
        site_name: 站点名称，用于提示信息
    """
    import os
    skill_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    qrcode_path = os.path.join(skill_dir, "qrcode.png")

    # 保存原始 stdout（-of 模式下 stdout 可能被重定向）
    original_stdout = sys.stdout

    def on_qrcode_ready(path: str):
        """回调：二维码已保存"""
        # ASCII 模式：在终端打印 QR 码字符画
        if ascii_mode:
            try:
                original_stdout.write(f"\n请用 APP 扫描下方二维码登录 {site_name}：\n\n")
                _png_to_terminal_blocks(path, out=original_stdout)
                original_stdout.write("\n等待扫码中（最多 240 秒）...\n\n")
                original_stdout.flush()
            except Exception as e:
                original_stdout.write(
                    f"[终端 QR 码显示失败: {e}，请打开 {path} 扫码]\n"
                )
                original_stdout.flush()

        # 写入阶段 1 输出文件
        _write_output(output_file_path, {
            "status": "qrcode_ready",
            "qrcode_path": path,
            "message": f"请扫描二维码登录{site_name}，等待最多 240 秒",
        })

    try:
        result = asyncio.run(site_cls.login_with_qrcode(
            qrcode_save_path=qrcode_path,
            on_qrcode_ready=on_qrcode_ready,
        ))
        # 阶段 2: 写入最终结果
        _write_output(output_file_path, result)
        return 0 if result.get("success") or result.get("already_logged_in") else 1
    except Exception as exc:
        _write_output(output_file_path, {"success": False, "error": str(exc)})
        return 1


def _infer_site_from_url(url: str) -> str | None:
    """从 URL 推断站点名称。未识别的 URL 返回 generic。"""
    if "zhihu.com" in url or "zhuanlan.zhihu.com" in url:
        return "zhihu"
    if "xiaohongshu.com" in url or "xhslink.com" in url:
        return "xiaohongshu"
    # 任意 http(s) URL → 通用站点
    if url.startswith("http://") or url.startswith("https://"):
        return "generic"
    return None


def resolve_site(args: list[str]) -> tuple[str, type, list[str]]:
    """
    Determine the site name and class from positional args.

    If the first arg matches a known site name, consume it.
    Otherwise try to infer site from URL in remaining args.
    Falls back to DEFAULT_SITE.

    Returns (site_name, site_class, remaining_args).
    """
    if args and args[0] in SITES:
        site_name = args[0]
        rest = args[1:]
    else:
        rest = args
        # 尝试从参数中的 URL 推断站点
        site_name = None
        for arg in rest:
            site_name = _infer_site_from_url(arg)
            if site_name:
                break
        if not site_name:
            site_name = DEFAULT_SITE
    return site_name, SITES[site_name], rest


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    # ------------------------------------------------------------------
    # 1. Extract -of / --output-file BEFORE any other arg parsing
    #    (must appear anywhere in argv and be removed before dispatch)
    # ------------------------------------------------------------------
    output_file_path: str | None = None
    argv = list(sys.argv[1:])

    for flag in ("--output-file", "-of"):
        if flag in argv:
            idx = argv.index(flag)
            if idx + 1 < len(argv):
                output_file_path = argv[idx + 1]
                argv = argv[:idx] + argv[idx + 2:]
                break

    # Extract --all-comments flag
    fetch_all_comments = False
    if "--all-comments" in argv:
        fetch_all_comments = True
        argv.remove("--all-comments")

    # ------------------------------------------------------------------
    # 2. Determine command
    # ------------------------------------------------------------------
    if not argv:
        print_usage()
        return 0

    command = argv[0]
    rest = argv[1:]

    # ------------------------------------------------------------------
    # 3. Special handling for login-qrcode / login-qrcode-ascii (two-phase output)
    # ------------------------------------------------------------------
    if command == "login-qrcode":
        site_name, site_cls, _ = resolve_site(rest)
        return _run_login_qrcode(site_cls, output_file_path, ascii_mode=False, site_name=site_name)

    if command == "login-qrcode-ascii":
        site_name, site_cls, _ = resolve_site(rest)
        return _run_login_qrcode(site_cls, output_file_path, ascii_mode=True, site_name=site_name)

    # ------------------------------------------------------------------
    # 4. Set up OutputRedirector (handles file redirection + OUTPUT_READY)
    # ------------------------------------------------------------------
    redirector = OutputRedirector(output_file_path)

    with redirector:
        try:
            result = _dispatch(command, rest, fetch_all_comments=fetch_all_comments)
            output_json(result)
            return 0
        except Exception as exc:
            output_json({"error": str(exc)})
            return 1


def _dispatch(command: str, args: list[str], fetch_all_comments: bool = False) -> dict:
    """Route *command* to the right async handler and return the result dict."""

    if command == "check-login":
        site_name, site_cls, _ = resolve_site(args)
        return asyncio.run(cmd_check_login(site_cls))

    elif command == "login":
        site_name, site_cls, _ = resolve_site(args)
        return asyncio.run(cmd_login(site_cls))

    elif command == "search":
        site_name, site_cls, rest = resolve_site(args)
        if not rest:
            raise ValueError("search requires a <keyword> argument")
        keyword = rest[0]
        return asyncio.run(cmd_search(site_cls, keyword))

    elif command == "detail":
        site_name, site_cls, rest = resolve_site(args)
        if not rest:
            raise ValueError("detail requires <feed_id> [xsec_token]")
        feed_id = rest[0]
        xsec_token = rest[1] if len(rest) > 1 else ""
        return asyncio.run(cmd_detail(site_cls, feed_id, xsec_token))

    elif command == "feeds":
        site_name, site_cls, _ = resolve_site(args)
        return asyncio.run(cmd_feeds(site_cls))

    elif command == "download":
        site_name, site_cls, rest = resolve_site(args)
        if len(rest) < 2:
            raise ValueError(
                "download requires <url> <output_dir> "
                "or <feed_id> <xsec_token> <output_dir>"
            )
        # 知乎 / 通用站点下载：URL + output_dir
        if site_name in ("zhihu", "generic"):
            url = rest[0]
            output_dir = rest[1]
            return asyncio.run(site_cls.download_article(url, output_dir))
        # 小红书下载：判断第一个参数是 URL 还是 feed_id
        if _looks_like_url(rest[0]):
            url = rest[0]
            output_dir = rest[1]
            resolved = asyncio.run(cmd_resolve_url(site_cls, url))
            if "error" in resolved:
                return resolved
            feed_id = resolved["feed_id"]
            xsec_token = resolved["xsec_token"]
        else:
            if len(rest) < 3:
                raise ValueError("download requires <feed_id> <xsec_token> <output_dir>")
            feed_id = rest[0]
            xsec_token = rest[1]
            output_dir = rest[2]
        return asyncio.run(cmd_download(site_cls, feed_id, xsec_token, output_dir, fetch_all_comments=fetch_all_comments))

    elif command == "comments":
        site_name, site_cls, rest = resolve_site(args)
        if not rest:
            raise ValueError("comments requires <feed_id> <xsec_token> or <url>")
        if _looks_like_url(rest[0]):
            url = rest[0]
            resolved = asyncio.run(cmd_resolve_url(site_cls, url))
            if "error" in resolved:
                return resolved
            feed_id = resolved["feed_id"]
            xsec_token = resolved["xsec_token"]
        else:
            feed_id = rest[0]
            xsec_token = rest[1] if len(rest) > 1 else ""
        return asyncio.run(cmd_comments(site_cls, feed_id, xsec_token))

    elif command == "resolve-url":
        site_name, site_cls, rest = resolve_site(args)
        if not rest:
            raise ValueError("resolve-url requires <url>")
        return asyncio.run(cmd_resolve_url(site_cls, rest[0]))

    elif command == "list-author":
        site_name, site_cls, rest = resolve_site(args)
        if not rest:
            raise ValueError("list-author requires <author_url>")
        author_url = rest[0]
        limit = int(rest[1]) if len(rest) > 1 else 20
        return asyncio.run(cmd_list_author(site_cls, author_url, limit))

    elif command in ("delete-cookies", "logout"):
        site_name, _, _ = resolve_site(args)
        return asyncio.run(cmd_delete_cookies(site_name))

    else:
        raise ValueError(f"Unknown command: {command}")


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    sys.exit(main())
