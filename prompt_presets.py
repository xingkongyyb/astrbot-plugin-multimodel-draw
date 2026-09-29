# -*- coding: utf-8 -*-
"""生图注入提示词（极简版）。

用户只需一个输入框：一段注入提示词作为全局规则，
画图时自动预载到用户描述前面，模型按此规则出图。

配置结构（插件配置 prompt_injection 字段）:
    {"enabled": true, "text": "你的注入提示词..."}
"""

from typing import Dict, Optional

DEFAULT_INJECTION = (
    "高质量图片：细节丰富，构图完整，光影自然，色彩和谐，"
    "层次分明，质感真实，避免文字与水印。"
)


class PromptEnhancer:
    """由配置构建的注入提示词增强器。"""

    def __init__(self, cfg: Optional[dict] = None):
        cfg = cfg or {}
        self.enabled: bool = bool(cfg.get("enabled", True))
        self.text: str = str(cfg.get("text") or DEFAULT_INJECTION).strip()

    def enhance(self, raw: str) -> str:
        """注入规则前置到用户描述。禁用或无文本时原样返回。"""
        raw = str(raw or "").strip()
        if not raw:
            return ""
        if not self.enabled or not self.text:
            return raw
        return f"{self.text}\n{raw}"

    def injection_payload(self) -> dict:
        """返回可给 UI 编辑/持久化的结构。"""
        return {"enabled": self.enabled, "text": self.text}
