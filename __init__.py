# 生图插件包初始化文件（提供商注册表见 providers.py）

from __future__ import annotations

from typing import Any

__all__ = ["Main"]


def __getattr__(name: str) -> Any:
    if name == "Main":
        from .main import Main

        return Main
    raise AttributeError(name)
