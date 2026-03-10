"""
ttbrowserkit.sites - 站点模块集合

每个站点模块封装特定网站的自动化操作逻辑。
"""

from .generic import GenericSite
from .xiaohongshu import XiaohongshuSite
from .zhihu import ZhihuSite

__all__ = ["GenericSite", "XiaohongshuSite", "ZhihuSite"]
