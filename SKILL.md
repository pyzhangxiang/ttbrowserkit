---
name: ttbrowserkit
description: 浏览器自动化工具包。用于小红书等网站的稳定内容抓取、搜索、登录。采用 per-operation Chrome 生命周期，不会卡住。当需要浏览器自动化抓取小红书内容时触发。
---

# ttbrowserkit - 浏览器自动化工具包

基于 Playwright 的浏览器自动化工具，每次操作启动独立 Chrome 实例，操作完成后自动关闭，不会卡住。支持 Cookie 持久化、反检测、自动重试。

## 重要：-of 两步执行规范

### 问题背景
浏览器自动化操作需要网络请求，执行时间较长（5-60秒），terminal 工具无法可靠捕获长时间运行命令的完整输出。

### 解决方案：-of 参数 + read_file 两步法

**三条铁律，绝对禁止违反：**

1. **所有命令必须加 `-of _output.txt` 参数** -- 不加则 terminal 可能截断输出！
2. **必须用 `read_file` 工具读取 `_output.txt`** -- 这是获取完整结果的唯一可靠方式！
3. **禁止使用 subprocess 包装或 python -c 包装** -- 已验证无法解决 terminal 截断问题！

### 统一命令模板

**第 1 步：terminal 执行命令（带 -of 参数）**
```bash
python scripts/cli.py <COMMAND> [ARGS] -of _output.txt
```

**第 2 步：read_file 读取结果**
```
使用 read_file 工具读取: <工作目录>/_output.txt
```

**参数说明**：
- `-of _output.txt` 固定使用此文件名，每次执行会覆盖上次结果
- terminal 会输出 `OUTPUT_READY: _output.txt` 表示脚本已完成
- 脚本同步执行，read_file 时脚本一定已完成

## 命令参考

工作目录：`<your_install_path>`

| 命令 | 说明 | 参数 |
|------|------|------|
| `check-login` | 检查登录状态 | |
| `login` | 扫码登录（弹出浏览器窗口） | |
| `login-qrcode` | 扫码登录（保存二维码图片，适合 agent 调用） | |
| `search <keyword>` | 搜索内容 | keyword 必填 |
| `detail <feed_id> [xsec_token]` | 获取笔记详情 | feed_id 必填 |
| `download <feed_id> <xsec_token> <output_path>` | 下载笔记为 MD（含图片+评论） | feed_id, xsec_token, output_path 必填 |
| `download <url> <output_path>` | 从链接下载笔记 | 支持短链接和完整链接 |
| `resolve-url <url>` | 解析链接 | 返回 feed_id+xsec_token |
| `feeds` | 获取推荐 feed 列表 | |
| `delete-cookies` | 删除 Cookie | |
| `logout` | 登出（delete-cookies 别名） | |

## 各命令调用格式

```bash
# 检查登录状态
python scripts/cli.py check-login -of _output.txt

# 扫码登录（会弹出浏览器窗口，用户扫码后自动关闭）
python scripts/cli.py login -of _output.txt

# 无头扫码登录（保存二维码图片，适合 agent 调用）
# 会弹出浏览器窗口（小红书 headless 被拦截），但用户不需要看窗口
# 分两阶段输出：
# 阶段 1: OUTPUT_READY → _output.txt 包含 qrcode_path（agent 发图给用户）
# 阶段 2: OUTPUT_READY → _output.txt 包含登录结果
python scripts/cli.py login-qrcode -of _output.txt

# 搜索内容
python scripts/cli.py search "AI绘画" -of _output.txt

# 获取笔记详情
python scripts/cli.py detail 6789abcdef0123456789abcd ABCxsecToken123 -of _output.txt

# 下载笔记为 Markdown（含图片和评论）
python scripts/cli.py download 6789abcdef0123456789abcd ABCxsecToken123 /path/to/output.md -of _output.txt

# 从小红书链接直接下载（支持短链接和完整链接）
python scripts/cli.py download "http://xhslink.com/o/xxxxxx" /path/to/output.md -of _output.txt

# 解析小红书链接为 feed_id + xsec_token
python scripts/cli.py resolve-url "http://xhslink.com/o/xxxxxx" -of _output.txt

# 获取推荐 feeds
python scripts/cli.py feeds -of _output.txt

# 删除 Cookie（需要重新登录）
python scripts/cli.py delete-cookies -of _output.txt

# 登出指定站点（等同于 delete-cookies）
python scripts/cli.py logout xiaohongshu -of _output.txt

```

每条命令执行后，必须用 `read_file` 读取 `_output.txt` 获取完整结果。

## 常用工作流

### 1. 登录流程（agent 主动或用户要求）

当用户要求登录，或操作发现需要登录时（`check-login` 返回 `logged_in: false`），agent 应执行 `login-qrcode` 并将二维码发给用户：

```bash
# 第 1 步：执行 login-qrcode（会弹出浏览器窗口，但用户不需要看）
python scripts/cli.py login-qrcode -of _output.txt
# 第一次 OUTPUT_READY 后 read_file 读取 _output.txt，获取 qrcode_path
```

读取到 `qrcode_path` 后：
- **Discord / 聊天软件**：将 `qrcode_path` 指向的图片作为附件发送给用户，告知"请扫码登录"
- **CLI 终端**：回复用户图片路径，告知"请打开此文件扫码登录"

等待用户扫码后，再次 read_file 读取 `_output.txt` 获取登录结果（第二次 OUTPUT_READY）。

### 2. 自动检测登录 -> 操作内容

当用户给出链接或要求搜索内容时：

```bash
# 第 1 步：检查是否已登录
python scripts/cli.py check-login -of _output.txt
# read_file 读取 _output.txt，检查 logged_in 字段

# 如果未登录 → 走上面的登录流程（login-qrcode + 发图给用户）
# 如果已登录 → 继续执行操作
```

### 3. 搜索 -> 获取详情

```bash
# 搜索
python scripts/cli.py search "关键词" -of _output.txt
# read_file 读取 _output.txt，从结果中提取 feed_id 和 xsec_token

# 获取详情
python scripts/cli.py detail <feed_id> <xsec_token> -of _output.txt
# read_file 读取 _output.txt
```

### 4. 浏览推荐 -> 获取详情

```bash
# 获取推荐列表
python scripts/cli.py feeds -of _output.txt
# read_file 读取 _output.txt，从结果中选择感兴趣的笔记

# 获取详情
python scripts/cli.py detail <feed_id> <xsec_token> -of _output.txt
# read_file 读取 _output.txt
```

### 5. Cookie 问题排查

如果操作持续失败或返回未登录：

```bash
# 删除旧 Cookie
python scripts/cli.py delete-cookies -of _output.txt
# read_file 确认删除成功

# 重新走登录流程
python scripts/cli.py login-qrcode -of _output.txt
# 发图给用户扫码
```

### 6. 下载笔记为 Markdown

支持两种方式：

**方式 A：从小红书链接直接下载（推荐）**

用户给出小红书链接（短链接或完整链接）时，直接用 URL 下载：

```bash
python scripts/cli.py download "http://xhslink.com/o/xxxxxx" <output_dir>/xiaohongshu/<feed_id>_<slug>.md -of _output.txt
```

**方式 B：搜索后用 feed_id 下载**

```bash
# 搜索获取 feed_id 和 xsec_token
python scripts/cli.py search "关键词" -of _output.txt

# 下载笔记为 Markdown + 图片
python scripts/cli.py download <feed_id> <xsec_token> <output_dir>/xiaohongshu/<feed_id>_<slug>.md -of _output.txt
```

> 注：方式 A 中，如果不知道 feed_id，可以先 `resolve-url` 解析链接得到 feed_id 用于文件命名。

## 注意事项

- **OC 内置浏览器已禁用**，所有浏览器操作统一通过 ttbrowserkit 执行
- **登录用 `login-qrcode` 命令**，agent 获取二维码图片后发给用户扫码；`login` 命令仅供用户手动在终端执行
- 发现需要登录时（操作失败或 `check-login` 返回未登录），应主动发起登录流程，不要等用户指示
- 其他命令均为无头模式运行，不弹窗
- 所有输出均为 JSON 格式
- Cookie 自动持久化到 `cookies/` 目录，无需手动管理
- 每次操作启动独立 Chrome 实例，操作完成自动关闭，不存在进程残留


