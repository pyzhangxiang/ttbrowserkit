"""
base.py - 站点模块抽象基类

所有站点模块必须继承 BaseSite 并实现 check_login 和 login_qrcode 方法。
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class BaseSite(ABC):
    """
    站点模块抽象基类。

    子类必须定义：
        SITE_NAME: 站点标识名（用于 Cookie 文件命名等）
        BASE_URL:  站点根 URL

    子类必须实现：
        check_login():   检测当前登录状态
        login_qrcode():  执行二维码登录流程
    """

    SITE_NAME: str
    BASE_URL: str

    @staticmethod
    @abstractmethod
    async def check_login() -> dict:
        """
        检测当前是否已登录。

        返回：
            dict: 包含 logged_in (bool) 和 message (str) 等信息
        """
        ...

    @staticmethod
    @abstractmethod
    async def login_qrcode() -> dict:
        """
        执行二维码登录流程。

        返回：
            dict: 包含 success (bool) 和 message (str) 等信息
        """
        ...
