# -*- coding: utf-8 -*-
"""生图插件 WebUI 后端 API（AstrBot 插件页面桥接）。

端点（经 dashboard 鉴权，路径 /api/v1/plugins/extensions/astrbot_plugin_apilio_draw/...）：
  GET  /providers          提供商列表 + 模式 + 用量摘要
  POST /provider           新增/更新提供商
  POST /provider/remove    删除提供商
  POST /provider/toggle    启停提供商
  POST /provider/priority  设置优先级
  POST /provider/test      真实调用测试（会产生一次费用）
  POST /mode               切换 sequential / concurrent
  GET  /usage              最近用量日志
"""

import json
import os
from typing import Any, Dict, List, Optional

from astrbot.api import logger
from astrbot.api.star import Context
from quart import jsonify, request

PLUGIN_NAME = "astrbot_plugin_apilio_draw"

USAGE_LOG = os.path.join(
    os.path.expanduser("~"), ".astrbot", "logs", "astrbot_plugin_apilio_draw_usage.log"
)


def _read_usage_log(limit: int = 300) -> List[dict]:
    rows: List[dict] = []
    try:
        with open(USAGE_LOG, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception:
                    continue
    except FileNotFoundError:
        pass
    return rows[-limit:]


def _usage_summary(rows: List[dict]) -> dict:
    ok = sum(1 for r in rows if r.get("ok"))
    fail = len(rows) - ok
    tokens = sum(int(r.get("tokens") or 0) for r in rows)
    images = sum(int(r.get("images") or 0) for r in rows)
    by_provider: Dict[str, dict] = {}
    for r in rows:
        pid = str(r.get("provider") or "?")
        item = by_provider.setdefault(pid, {"calls": 0, "ok": 0, "fail": 0, "tokens": 0, "images": 0})
        item["calls"] += 1
        item["ok"] += 1 if r.get("ok") else 0
        item["fail"] += 0 if r.get("ok") else 1
        item["tokens"] += int(r.get("tokens") or 0)
        item["images"] += int(r.get("images") or 0)
    return {
        "total": len(rows),
        "ok": ok,
        "fail": fail,
        "success_rate": round(ok / len(rows) * 100, 1) if rows else 0.0,
        "tokens": tokens,
        "images": images,
        "by_provider": by_provider,
    }


class DrawWebAPI:
    """生图管理页面后端 API。"""

    ALLOWED_TYPES = ("openai_compat", "template", "bailian_image", "bailian_video")

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin

    # ---------- 注册 ----------

    def register(self, context: Context) -> None:
        routes: List[tuple] = [
            ("/providers", "handle_providers", ["GET"]),
            ("/provider", "handle_provider_upsert", ["POST"]),
            ("/provider/remove", "handle_provider_remove", ["POST"]),
            ("/provider/toggle", "handle_provider_toggle", ["POST"]),
            ("/provider/priority", "handle_provider_priority", ["POST"]),
            ("/provider/test", "handle_provider_test", ["POST"]),
            ("/mode", "handle_mode", ["POST"]),
            ("/active", "handle_active", ["POST"]),
            ("/llm", "handle_llm", ["GET"]),
            ("/llm", "handle_llm_save", ["POST"]),
            ("/usage", "handle_usage", ["GET"]),
            ("/templates", "handle_templates", ["GET"]),
            ("/templates", "handle_templates_save", ["POST"]),
        ]
        for route, handler_name, methods in routes:
            handler = getattr(self, handler_name)
            context.register_web_api(
                f"/{PLUGIN_NAME}{route}",
                handler,
                methods,
                f"Draw Page: {handler_name}",
            )

    # ---------- 内部辅助 ----------

    def _cfg(self) -> dict:
        return self.plugin.config

    def _save(self) -> None:
        self.plugin._save_config()

    def _rebuild(self) -> None:
        self.plugin._rebuild_manager()

    MASK_PREFIX = "已配置"

    @classmethod
    def _fingerprint(cls, key: str) -> str:
        import hashlib
        return hashlib.sha256(key.encode("utf-8")).hexdigest()[:8]

    def _mask_key(self, key: str) -> str:
        """只回显「已配置 + 指纹」，不泄露 key 的任何字符。"""
        if not key:
            return ""
        return f"{self.MASK_PREFIX} · #{self._fingerprint(key)} · {len(key)}位"

    @staticmethod
    def _capability_of(item: dict) -> str:
        t = str(item.get("type") or "").strip().lower()
        if t == "bailian_video":
            return "video"
        cap = str(item.get("capability") or "").strip().lower()
        return "video" if cap == "video" else "image"

    def _providers_public(self) -> List[dict]:
        out = []
        for p in (self._cfg().get("providers") or []):
            item = dict(p)
            if item.get("api_key"):
                item["api_key"] = self._mask_key(str(item["api_key"]))
            item["capability"] = self._capability_of(item)
            out.append(item)
        return out

    def _build_entry(self, kv: dict) -> tuple:
        """校验并构造提供商配置项。返回 (entry, error)。"""
        ptype = str(kv.get("type") or "").strip().lower()
        if ptype not in self.ALLOWED_TYPES:
            return None, "type 必须是 openai_compat 或 template"
        api_base = str(kv.get("api_base") or "").strip()
        api_key = str(kv.get("api_key") or "").strip()
        model = str(kv.get("model") or "").strip()
        if not api_base or not api_key or not model:
            return None, "缺少必填项：api_base / api_key / model"
        pid = str(kv.get("id") or "").strip() or model
        entry: Dict[str, Any] = {
            "id": pid,
            "name": str(kv.get("name") or pid),
            "type": ptype,
            "enable": bool(kv.get("enable", True)),
            "priority": int(kv.get("priority") or 100),
            "api_base": api_base,
            "api_key": api_key,
            "model": model,
            "timeout": float(kv.get("timeout") or 180),
        }
        if kv.get("size"):
            entry["size"] = str(kv["size"]).strip()
        if kv.get("ref_field"):
            entry["ref_field"] = str(kv["ref_field"]).strip()
        if kv.get("extra_body") is not None:
            entry["extra_body"] = kv["extra_body"]
        entry["capability"] = self._capability_of({"type": ptype, "capability": kv.get("capability")})
        if ptype in ("bailian_image", "bailian_video"):
            for k in ("negative_prompt", "resolution", "ratio", "duration", "watermark",
                      "prompt_extend", "poll_interval", "max_wait", "n"):
                v = kv.get(k)
                if v not in (None, ""):
                    entry[k] = v
        if ptype == "template":
            for k in ("url", "method", "response_field", "image_kind"):
                if kv.get(k):
                    entry[k] = str(kv[k]).strip()
            if kv.get("headers"):
                entry["headers"] = kv["headers"]
            if kv.get("body_template"):
                entry["body_template"] = kv["body_template"]
        return entry, None

    # ---------- 端点 ----------

    async def handle_providers(self):
        try:
            rows = _read_usage_log(200)
            return jsonify({
                "success": True,
                "mode": str(self._cfg().get("mode", "sequential")),
                "llm_enable": bool(self._cfg().get("llm_enable", True)),
                "providers": self._providers_public(),
                "image_provider_id": str(self._cfg().get("image_provider_id") or ""),
                "video_provider_id": str(self._cfg().get("video_provider_id") or ""),
                "summary": _usage_summary(rows),
            })
        except Exception as e:
            logger.error(f"[Apilio画图] WebUI providers 失败: {e}", exc_info=True)
            return jsonify({"success": False, "error": str(e)})

    async def handle_provider_upsert(self):
        try:
            data = await request.get_json() or {}
            cfg = dict(data.get("config") or data)
            # 编辑时 UI 提交回来的是掩码串（旧版 ***、新版「已配置 ·#」），保留原 key
            _sub = str(cfg.get("api_key") or "")
            if "***" in _sub or _sub.startswith(self.MASK_PREFIX):
                pid = str(cfg.get("id") or "").strip()
                for p0 in (self._cfg().get("providers") or []):
                    if p0.get("id") == pid:
                        cfg["api_key"] = str(p0.get("api_key") or "")
                        break
            entry, err = self._build_entry(cfg)
            if err:
                return jsonify({"success": False, "error": err})
            providers = list(self._cfg().get("providers") or [])
            existed = any(p.get("id") == entry["id"] for p in providers)
            providers = [p for p in providers if p.get("id") != entry["id"]] + [entry]
            self._cfg()["providers"] = providers
            self._save()
            self._rebuild()
            return jsonify({"success": True, "existed": existed, "id": entry["id"]})
        except Exception as e:
            logger.error(f"[Apilio画图] WebUI provider upsert 失败: {e}", exc_info=True)
            return jsonify({"success": False, "error": str(e)})

    async def handle_provider_remove(self):
        try:
            data = await request.get_json() or {}
            pid = str(data.get("id") or "").strip()
            if not pid:
                return jsonify({"success": False, "error": "缺少 id"})
            providers = [p for p in (self._cfg().get("providers") or []) if p.get("id") != pid]
            self._cfg()["providers"] = providers
            self._save()
            self._rebuild()
            return jsonify({"success": True})
        except Exception as e:
            return jsonify({"success": False, "error": str(e)})

    async def handle_provider_toggle(self):
        try:
            data = await request.get_json() or {}
            pid = str(data.get("id") or "").strip()
            providers = self._cfg().get("providers") or []
            hit = False
            for p in providers:
                if p.get("id") == pid:
                    p["enable"] = bool(data.get("enable", not p.get("enable", False)))
                    hit = True
            if not hit:
                return jsonify({"success": False, "error": f"未找到提供商「{pid}」"})
            self._save()
            self._rebuild()
            return jsonify({"success": True})
        except Exception as e:
            return jsonify({"success": False, "error": str(e)})

    async def handle_provider_priority(self):
        try:
            data = await request.get_json() or {}
            pid = str(data.get("id") or "").strip()
            try:
                prio = int(data.get("priority") or 100)
            except (TypeError, ValueError):
                return jsonify({"success": False, "error": "priority 必须是整数"})
            providers = self._cfg().get("providers") or []
            hit = False
            for p in providers:
                if p.get("id") == pid:
                    p["priority"] = prio
                    hit = True
            if not hit:
                return jsonify({"success": False, "error": f"未找到提供商「{pid}」"})
            self._save()
            self._rebuild()
            return jsonify({"success": True})
        except Exception as e:
            return jsonify({"success": False, "error": str(e)})

    async def handle_provider_test(self):
        try:
            data = await request.get_json() or {}
            pid = str(data.get("id") or "").strip()
            p = self.plugin._manager.get(pid) or self.plugin._video_manager.get(pid)
            if not p:
                return jsonify({"success": False, "error": f"未找到提供商「{pid}」"})
            if not p.enable:
                return jsonify({"success": False, "error": f"提供商「{pid}」当前已停用"})
            import asyncio
            loop = asyncio.get_event_loop()
            # 视频模型：只建任务验证鉴权与参数，不做整段渲染（省钱省时间）
            if getattr(p, "CAP", "image") == "video" and hasattr(p, "test_connection"):
                task_id = await p.test_connection()
                if task_id:
                    return jsonify({"success": True,
                                    "message": f"鉴权与接口通过，已创建测试任务 {task_id}（不等待渲染）"})
                return jsonify({"success": False, "error": "建任务失败（详见用量日志）"})
            path = await p.generate("测试图：红色小方块", None, loop.time() + 120.0)
            if path:
                return jsonify({"success": True, "message": "测试成功（已产生一次真实调用费用）"})
            return jsonify({"success": False, "error": "测试失败（详见用量日志）"})
        except Exception as e:
            return jsonify({"success": False, "error": str(e)})

    async def handle_active(self):
        """切换「当前生图模型」/「当前生视频模型」。id 传空字符串表示恢复按优先级自动选路。"""
        try:
            data = await request.get_json() or {}
            kind = str(data.get("kind") or "").strip().lower()
            pid = str(data.get("id") or "").strip()
            if kind not in ("image", "video"):
                return jsonify({"success": False, "error": "kind 必须是 image 或 video"})
            key = "image_provider_id" if kind == "image" else "video_provider_id"
            if pid:
                hit = [p for p in (self._cfg().get("providers") or []) if str(p.get("id")) == pid]
                if not hit:
                    return jsonify({"success": False, "error": f"未找到提供商「{pid}」"})
                if self._capability_of(hit[0]) != kind:
                    return jsonify({"success": False,
                                    "error": f"「{pid}」不是{'生视频' if kind == 'video' else '生图'}提供商"})
            self._cfg()[key] = pid
            self._save()
            self._rebuild()
            logger.info(f"[Apilio画图] 当前{'生视频' if kind == 'video' else '生图'}模型 -> {pid or '（自动选路）'}")
            return jsonify({"success": True, "kind": kind, "id": pid})
        except Exception as e:
            logger.error(f"[Apilio画图] WebUI active 失败: {e}", exc_info=True)
            return jsonify({"success": False, "error": str(e)})

    async def handle_mode(self):
        try:
            data = await request.get_json() or {}
            m = str(data.get("mode") or "").strip().lower()
            if m not in ("sequential", "concurrent"):
                return jsonify({"success": False, "error": "mode 必须是 sequential 或 concurrent"})
            self._cfg()["mode"] = m
            self._save()
            self._rebuild()
            return jsonify({"success": True, "mode": m})
        except Exception as e:
            return jsonify({"success": False, "error": str(e)})

    async def handle_usage(self):
        try:
            limit = max(10, min(1000, int(request.args.get("limit", 300))))
            rows = _read_usage_log(limit)
            return jsonify({"success": True, "rows": rows, "summary": _usage_summary(rows)})
        except Exception as e:
            return jsonify({"success": False, "error": str(e)})

    async def handle_templates(self):
        """读取注入提示词（未配置时返回内置默认）。"""
        try:
            enh = self.plugin._enhancer
            return jsonify({
                "success": True,
                "injection": enh.injection_payload(),
            })
        except Exception as e:
            logger.error(f"[Apilio画图] WebUI injection 读取失败: {e}", exc_info=True)
            return jsonify({"success": False, "error": str(e)})

    async def handle_templates_save(self):
        """保存用户编辑的注入提示词（开关 + 一段文本）。"""
        try:
            data = await request.get_json() or {}
            raw = data.get("injection") or {}
            if not isinstance(raw, dict):
                return jsonify({"success": False, "error": "injection 必须是对象"})
            enabled = bool(raw.get("enabled", True))
            text = str(raw.get("text") or "").strip()
            self._cfg()["prompt_injection"] = {"enabled": enabled, "text": text}
            self._save()
            self.plugin._rebuild_enhancer()
            return jsonify({"success": True})
        except Exception as e:
            logger.error(f"[Apilio画图] WebUI injection 保存失败: {e}", exc_info=True)
            return jsonify({"success": False, "error": str(e)})

    async def handle_llm(self):
        """读取 AI 调用开关状态。"""
        try:
            return jsonify({
                "success": True,
                "llm_enable": bool(self._cfg().get("llm_enable", True)),
            })
        except Exception as e:
            return jsonify({"success": False, "error": str(e)})

    async def handle_llm_save(self):
        """保存 AI 调用开关（开=允许 LLM 调用，关=仅指令调用）。"""
        try:
            data = await request.get_json() or {}
            enabled = bool(data.get("enabled", True))
            self._cfg()["llm_enable"] = enabled
            self._save()
            self.plugin._apply_llm_enable()
            return jsonify({"success": True, "llm_enable": enabled})
        except Exception as e:
            logger.error(f"[Apilio画图] WebUI AI 调用开关保存失败: {e}", exc_info=True)
            return jsonify({"success": False, "error": str(e)})
