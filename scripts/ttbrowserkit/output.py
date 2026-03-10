"""
output.py - 输出文件重定向

实现 OC 的 -of（output file）模式：
将 stdout/stderr 重定向到指定文件，
完成后打印 OUTPUT_READY 标记通知调用方。
"""

from __future__ import annotations

import sys
from typing import TextIO


class OutputRedirector:
    """
    输出重定向 context manager。

    用法：
        with OutputRedirector("/tmp/output.txt"):
            print("这些内容会写入文件")
        # 退出后，原始 stdout 会收到 "OUTPUT_READY: /tmp/output.txt"
    """

    def __init__(self, filepath: str | None) -> None:
        self.filepath = filepath
        self._file: TextIO | None = None
        self._original_stdout: TextIO | None = None
        self._original_stderr: TextIO | None = None

    def __enter__(self) -> OutputRedirector:
        if self.filepath is not None:
            # 保存原始 stdout/stderr
            self._original_stdout = sys.stdout
            self._original_stderr = sys.stderr

            # 打开输出文件并重定向
            self._file = open(self.filepath, "w", encoding="utf-8")
            sys.stdout = self._file
            sys.stderr = self._file

        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if self._file is not None:
            # 恢复原始 stdout/stderr
            sys.stdout = self._original_stdout  # type: ignore[assignment]
            sys.stderr = self._original_stderr  # type: ignore[assignment]

            # 关闭文件
            self._file.close()
            self._file = None

            # 通知调用方输出文件已就绪
            print(f"OUTPUT_READY: {self.filepath}")

        # 不吞异常
        return False
