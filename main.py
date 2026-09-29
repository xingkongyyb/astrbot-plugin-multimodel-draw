# -*- coding: utf-8 -*-
"""Apilio GPT-Image-2 异步生图插件 for AstrBot."""
import asyncio
import base64
import io
import json
import os
import re
import tempfile
import time
import uuid
from typing import Any, Dict, List, Optional

import httpx
from PIL import Image as PILImage

from astrbot.api import logger
from astrbot.api import llm_tool
from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core import AstrBotConfig
from astrbot.core.message.components import Image as MsgImage
from astrbot.core.message.components import Video as MsgVideo
from .providers import ProviderManager, ProviderError, log_usage
from .prompt_presets import PromptEnhancer


_QUALITY_SUFFIX = (
    ", high quality, highly detailed, beautiful composition, "
    "no text, no watermark, no deformed hands, no extra limbs"
)


async def _request_with_retry(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    json: Optional[dict] = None,
    retries: int = 4,
) -> httpx.Response:
    last: Optional[Exception] = None
    for attempt in range(1, retries + 1):
        try:
            return await client.request(method, url, json=json)
        except (httpx.TransportError, httpx.TimeoutException) as e:
            last = e
            if attempt < retries:
                await asyncio.sleep(2 * attempt)
    raise last


async def _image_to_data_url(client: httpx.AsyncClient, file_or_url: str) -> Optional[str]:
    """将本地路径/URL 转为 base64 data URL（压缩到最长边 1024）。"""
    s = str(file_or_url)
    data: Optional[bytes] = None
    if s.startswith("http://") or s.startswith("https://"):
        try:
            r = await client.get(s)
            r.raise_for_status()
            data = r.content
        except Exception as e:
            logger.warning(f"[Apilio画图] 参考图下载失败: {e}")
            return None
    elif s.startswith("base64://"):
        data = base64.b64decode(s[len("base64://"):])
    else:
        p = s
        if s.startswith("file://"):
            p = s[len("file://"):]
        if not os.path.exists(p):
            return None
        with open(p, "rb") as f:
            data = f.read()
    if not data:
        return None
    try:
        img = PILImage.open(io.BytesIO(data))
        img = img.convert("RGB")
        if max(img.size) > 1024:
            ratio = 1024 / max(img.size)
            img = img.resize(
                (max(1, round(img.width * ratio)), max(1, round(img.height * ratio))),
                PILImage.LANCZOS,
            )
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=90)
        return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception as e:
        logger.warning(f"[Apilio画图] 参考图处理失败: {e}")
        return None


async def _fetch_image_bytes(
    client: httpx.AsyncClient, file_or_url: str
) -> Optional[bytes]:
    """将本地路径/URL 读取为原始字节。"""
    s = str(file_or_url)
    try:
        if s.startswith("http://") or s.startswith("https://"):
            r = await client.get(s)
            r.raise_for_status()
            return r.content
        p = s
        if s.startswith("file://"):
            p = s[len("file://"):]
        if os.path.exists(p):
            with open(p, "rb") as f:
                return f.read()
    except Exception as e:
        logger.warning(f"[Apilio画图] 图片读取失败: {e}")
    return None


def _walk_find(data: Any, keys: tuple) -> List[str]:
    hits: List[str] = []
    if isinstance(data, dict):
        for k, v in data.items():
            if k in keys and isinstance(v, str) and v:
                hits.append(v)
            hits.extend(_walk_find(v, keys))
    elif isinstance(data, list):
        for item in data:
            hits.extend(_walk_find(item, keys))
    return hits


@register(
    "astrbot_plugin_apilio_draw",
    "Codex",
    "多模型生图",
    "1.1.0",
    "/画图 <描述>（别名 /生图 /绘图 /ai生图）走多后端调度：GPT 中转 4K 优先、火山 Seedream 自动兜底；支持参考图、角色记忆与 WebUI 面板管理。",
)
class ApilioDraw(Star):
    # LLM 工具方法清单：关闭开关后 AI 无法调用（仅保留 /画图 指令）
    _LLM_TOOL_NAMES = (
        "generate_image",
        "remember_character",
        "forget_character",
        "list_character_refs",
        "remove_background",
        "analyze_image",
        "generate_video",
    )
    _LLM_TOOL_ORIG_DOCS: Dict[str, str] = {}

    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config or {}
        self._migrate_providers()
        self._rebuild_manager()
        self._rebuild_enhancer()
        # WebUI 管理页面 API（生图管理）
        try:
            from .web_api import DrawWebAPI

            self.web_api = DrawWebAPI(self)
            self.web_api.register(context)
            logger.info("[Apilio画图] WebUI 生图管理页面 API 已注册")
        except Exception as we:
            logger.warning(f"[Apilio画图] WebUI API 注册失败: {we}")

    def _key(self) -> str:
        return str(self.config.get("api_key") or os.environ.get("APILIO_API_KEY", ""))
    @staticmethod
    def _collect_images(event: AstrMessageEvent) -> List[str]:
        out: List[str] = []

        def _walk(chain) -> None:
            for comp in chain or []:
                if isinstance(comp, MsgImage):
                    src = comp.path or comp.file or comp.url or ""
                    if src:
                        out.append(str(src))
                elif hasattr(comp, "chain"):
                    _walk(getattr(comp, "chain", None))
                elif hasattr(comp, "message"):
                    _walk(getattr(comp, "message", None))

        try:
            chain = getattr(event, "message", None) or event.get_messages()
            _walk(chain)
        except Exception:
            pass
        return out


    # ---------- 角色参考图记忆 ----------
    def _ref_dir(self) -> str:
        d = os.path.join(
            os.path.expanduser("~"),
            ".astrbot",
            "data",
            "plugin_data",
            "astrbot_plugin_apilio_draw",
            "char_refs",
        )
        try:
            os.makedirs(d, exist_ok=True)
        except OSError:
            pass
        return d

    def _ref_index(self) -> Dict[str, Dict[str, str]]:
        p = os.path.join(self._ref_dir(), "index.json")
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except Exception:
            pass
        return {}

    def _save_ref_index(self, idx: Dict[str, Dict[str, str]]) -> None:
        p = os.path.join(self._ref_dir(), "index.json")
        try:
            with open(p, "w", encoding="utf-8") as f:
                json.dump(idx, f, ensure_ascii=False, indent=2)
        except OSError as e:
            logger.warning(f"[Apilio画图] 保存角色参考索引失败: {e}")

    def _find_ref_for_prompt(self, user_key: str, prompt: str) -> Optional[str]:
        """prompt 中提到已保存的角色名时返回其参考图路径。"""
        idx = self._ref_index()
        entries = idx.get(user_key) or {}
        if not entries:
            return None
        low = str(prompt or "").casefold()
        for name, fname in entries.items():
            if str(name).casefold() in low:
                path = os.path.join(self._ref_dir(), str(fname))
                if os.path.exists(path):
                    return path
        return None

    # ---------- 生图提供商（注册表 + 适配器，见 providers.py） ----------

    def _save_config(self) -> None:
        try:
            if hasattr(self.config, "save_config"):
                self.config.save_config()
        except Exception as e:
            logger.warning(f"[Apilio画图] 保存配置失败: {e}")

    def _migrate_providers(self) -> None:
        '旧版配置（doubao_* 字段）迁移为 providers 数组。'
        if self.config.get("providers"):
            return
        providers = []
        dkey = str(self.config.get("doubao_api_key") or "").strip()
        if dkey:
            providers.append({
                "id": "volcark",
                "name": "豆包 Seedream",
                "type": "openai_compat",
                "enable": True,
                "priority": 10,
                "api_base": str(
                    self.config.get("doubao_api_base")
                    or "https://ark.cn-beijing.volces.com/api/plan/v3"
                ).rstrip("/"),
                "api_key": dkey,
                "model": str(self.config.get("doubao_model") or "doubao-seedream-5.0-lite"),
                "size": str(self.config.get("size") or "2K"),
                "timeout": 180.0,
                "extra_body": {"watermark": False},
                "ref_field": "image",
            })
        if providers:
            self.config["providers"] = providers
            self._save_config()
        # 清理旧的提示词模板配置结构（新结构为 prompt_injection）
        if self.config.get("prompt_templates") is not None:
            self.config.pop("prompt_templates", None)
            self._save_config()

    def _rebuild_manager(self) -> None:
        all_cfg = self.config.get("providers") or []
        self._manager = ProviderManager(
            all_cfg,
            mode=self.config.get("mode", "sequential"),
            cap="image",
        )
        self._video_manager = ProviderManager(all_cfg, mode="sequential", cap="video")
        logger.info(
            f"[Apilio画图] 生图提供商已加载：{self._manager.summary()}；"
            f"生视频提供商：{self._video_manager.summary()}"
        )

    def _rebuild_enhancer(self) -> None:
        self._enhancer = PromptEnhancer(self.config.get("prompt_injection") or {})

    def _llm_guard(self) -> Optional[str]:
        """LLM 开关守卫：开启返回 None（放行），关闭返回拒绝提示。"""
        if not self.config.get("llm_enable", True):
            return (
                "error: 画图功能当前已设为「仅指令调用」（AI 调用开关已关闭），"
                "请使用指令 /画图 <描述>（或 /生图、/绘图）触发。"
            )
        return None

    def _apply_llm_enable(self) -> None:
        """根据配置开/关 LLM 工具暴露（AstrBot FunctionTool.active 机制）。

        关闭：StarTools.deactivate_llm_tool() → 工具从 LLM 工具列表消失；
        开启：StarTools.activate_llm_tool() → 恢复。
        状态同时持久化到 AstrBot 的 inactivated_llm_tools（面板工具管理联动）。
        方法内 _llm_guard() 作为硬性兜底。
        """
        enabled = bool(self.config.get("llm_enable", True))
        for name in self._LLM_TOOL_NAMES:
            try:
                if enabled:
                    StarTools.activate_llm_tool(name)
                else:
                    StarTools.deactivate_llm_tool(name)
            except Exception as e:
                logger.warning(f"[Apilio画图] {'启用' if enabled else '停用'}LLM 工具「{name}」失败: {e}")
        logger.info(f"[Apilio画图] AI 调用开关：{'开启' if enabled else '关闭'}")

    @filter.on_astrbot_loaded()
    async def _on_astrbot_loaded(self):
        """AstrBot 全部加载完成后应用 LLM 开关（此时插件已激活，activate 安全）。"""
        self._apply_llm_enable()

    async def _send_image(self, event: AstrMessageEvent, path: str) -> bool:
        """发送生成图。

        发送方式由配置 send_mode 决定：
          file  = 文件形式（群文件/私聊文件，原图不经过 QQ 图片压缩，默认）
          image = 图片消息形式（会被 QQ 二次压缩）
        file 模式失败时自动回退为图片消息。
        """
        mode = str(self.config.get("send_mode", "file") or "file").strip().lower()
        if mode == "file":
            try:
                name = os.path.basename(str(path))
                bot = getattr(event, "bot", None)
                group_id = ""
                try:
                    group_id = event.get_group_id() or ""
                except Exception:
                    group_id = ""
                if group_id:
                    await bot.upload_group_file(
                        group_id=int(group_id), file=str(path), name=name
                    )
                else:
                    await bot.upload_private_file(
                        user_id=int(event.get_sender_id()), file=str(path), name=name
                    )
                logger.info(f"[Apilio画图] 已以文件形式发送原图: {name}")
                return True
            except Exception as e:
                logger.warning(f"[Apilio画图] 文件发送失败，回退图片消息: {e}")
        await event.send(MessageChain().file_image(path))
        return True

    async def _generate(
        self,
        prompt: str,
        image_data: Optional[str] = None,
        deadline: Optional[float] = None,
    ) -> str:
        loop = asyncio.get_event_loop()
        if deadline is None:
            deadline = loop.time() + 240.0
        # 注入提示词：预载到用户描述前（可在 WebUI 编辑）
        try:
            enhanced = self._enhancer.enhance(prompt)
            if enhanced and enhanced.strip():
                prompt = enhanced
                logger.info("[Apilio画图] 已预载注入提示词")
        except Exception as e:
            logger.warning(f"[Apilio画图] 注入提示词失败，使用原样: {e}")
        pid = str(self.config.get("image_provider_id") or "").strip()
        path = await self._manager.generate(prompt, image_data, deadline, prefer=pid)
        if not path:
            raise RuntimeError(
                "所有生图提供商均生成失败（失败详情见用量日志 "
                "astrbot_plugin_apilio_draw_usage.log）"
            )
        return path

    # ---------- /画图 提供商 管理 ----------

    @staticmethod
    def _parse_kv(tokens: List[str]) -> Dict[str, str]:
        out: Dict[str, str] = {}
        for t in tokens:
            if "=" in t:
                k, v = t.split("=", 1)
                out[k.strip()] = v.strip()
        return out

    async def _handle_provider_command(self, args: str) -> str:
        parts = [x for x in args.split() if x.strip()]
        sub = parts[0].lower() if parts else "list"
        try:
            if sub in ("list", "ls"):
                return self._manager.describe(self.config.get("mode", "sequential"))
            if sub == "mode":
                m = parts[1].lower() if len(parts) > 1 else ""
                if m not in ("sequential", "concurrent"):
                    return "用法：/画图 提供商 mode sequential|concurrent"
                self.config["mode"] = m
                self._save_config()
                self._rebuild_manager()
                tip = (
                    "顺序降级（按优先级逐个尝试，失败自动切换下一家）"
                    if m == "sequential"
                    else "并发容错（同时请求，谁先成功用谁；失败时可能产生多家费用）"
                )
                return f"✅ 生图模式已切换为 {m}（{tip}）"
            if sub in ("add", "edit"):
                if len(parts) < 2:
                    return (
                        "用法：/画图 提供商 add type=openai_compat|template id=.. name=.. "
                        "api_base=.. api_key=.. model=.. [size=.. priority=.. timeout=.. "
                        "ref_field=.. extra=<json> url=.. method=.. headers=<json> "
                        "body=<json> response_field=.. image_kind=..]"
                    )
                kv = self._parse_kv(parts[1:])
                ptype = str(kv.get("type") or "").strip().lower()
                if ptype not in ("openai_compat", "template"):
                    return "❌ type 必须是 openai_compat 或 template"
                api_base = str(kv.get("api_base") or "").strip()
                api_key = str(kv.get("api_key") or "").strip()
                model = str(kv.get("model") or "").strip()
                if not api_base or not api_key or not model:
                    return "❌ 缺少必填项：api_base / api_key / model"
                pid = str(kv.get("id") or "").strip() or model
                entry: Dict[str, Any] = {
                    "id": pid,
                    "name": str(kv.get("name") or pid),
                    "type": ptype,
                    "enable": True,
                    "priority": int(kv.get("priority") or 100),
                    "api_base": api_base,
                    "api_key": api_key,
                    "model": model,
                    "timeout": float(kv.get("timeout") or 180),
                }
                if kv.get("size"):
                    entry["size"] = kv["size"]
                if kv.get("ref_field"):
                    entry["ref_field"] = kv["ref_field"]
                if kv.get("extra"):
                    entry["extra_body"] = json.loads(kv["extra"])
                if ptype == "template":
                    for k in ("url", "method", "response_field", "image_kind"):
                        if kv.get(k):
                            entry[k] = kv[k]
                    if kv.get("headers"):
                        entry["headers"] = json.loads(kv["headers"])
                    if kv.get("body"):
                        entry["body_template"] = json.loads(kv["body"])
                providers = list(self.config.get("providers") or [])
                existed = any(p.get("id") == pid for p in providers)
                providers = [p for p in providers if p.get("id") != pid] + [entry]
                self.config["providers"] = providers
                self._save_config()
                self._rebuild_manager()
                return f"✅ 已{'更新' if existed else '添加'}提供商「{pid}」（type={ptype}，model={model}）"
            if sub in ("remove", "rm"):
                pid = parts[1] if len(parts) > 1 else ""
                if not pid:
                    return "用法：/画图 提供商 remove <id>"
                cur = self.config.get("providers") or []
                providers = [p for p in cur if p.get("id") != pid]
                if len(providers) == len(cur):
                    return f"❌ 未找到提供商「{pid}」"
                self.config["providers"] = providers
                self._save_config()
                self._rebuild_manager()
                return f"✅ 已移除提供商「{pid}」"
            if sub in ("enable", "disable"):
                pid = parts[1] if len(parts) > 1 else ""
                providers = self.config.get("providers") or []
                hit = False
                for p in providers:
                    if p.get("id") == pid:
                        p["enable"] = sub == "enable"
                        hit = True
                if not hit:
                    return f"❌ 未找到提供商「{pid}」"
                self._save_config()
                self._rebuild_manager()
                return f"✅ 已{'启用' if sub == 'enable' else '停用'}提供商「{pid}」"
            if sub == "priority":
                if len(parts) < 3:
                    return "用法：/画图 提供商 priority <id> <数值>（越小越优先）"
                pid, val = parts[1], parts[2]
                providers = self.config.get("providers") or []
                hit = False
                for p in providers:
                    if p.get("id") == pid:
                        p["priority"] = int(val)
                        hit = True
                if not hit:
                    return f"❌ 未找到提供商「{pid}」"
                self._save_config()
                self._rebuild_manager()
                return f"✅ 已设置 {pid} 优先级为 {int(val)}"
            if sub == "test":
                pid = parts[1] if len(parts) > 1 else ""
                p = self._manager.get(pid)
                if not p:
                    return f"❌ 未找到提供商「{pid}」"
                if not p.enable:
                    return f"⚠️ 提供商「{pid}」当前已停用"
                loop = asyncio.get_event_loop()
                path = await p.generate("测试图：红色小方块", None, loop.time() + 120.0)
                if path:
                    return f"✅ 提供商「{pid}」测试成功（已产生一次真实调用费用）"
                return f"❌ 提供商「{pid}」测试失败（详见用量日志）"
            return "未知子命令。支持：list / add / remove / enable / disable / mode / priority / test"
        except Exception as e:
            logger.error(f"[Apilio画图] 提供商管理操作失败: {e}", exc_info=True)
            return f"❌ 操作失败：{e}"

    @llm_tool()
    async def generate_image(self, event: AstrMessageEvent, prompt: str, image_url: str = "") -> str:
        """生成或修改图片，并把图片发送给用户。

        Args:
            prompt(string): 详细的图片描述或修改要求
            image_url(string): 参考图片链接（例如历史消息工具返回的图片 URL）
        """
        blocked = self._llm_guard()
        if blocked:
            return blocked
        prompt = str(prompt or "").strip()
        if not prompt:
            return "error: generate_image 需要提供 prompt 参数"
        image_data = None
        refs = []
        if str(image_url or "").strip():
            refs.append(str(image_url).strip())
        refs.extend(self._collect_images(event))
        try:
            for src in refs[:1]:
                async with httpx.AsyncClient(timeout=60) as client:
                    image_data = await _image_to_data_url(client, src)
                    if image_data:
                        break
        except Exception as e:
            logger.warning(f"[Apilio画图] 读取参考图失败: {e}")
        try:
            ref_path = self._find_ref_for_prompt(
                str(event.get_sender_id() or "global"), prompt
            )
        except Exception:
            ref_path = None
        if ref_path and not image_data:
            try:
                async with httpx.AsyncClient(timeout=60) as client:
                    image_data = await _image_to_data_url(client, ref_path)
                if image_data:
                    prompt = prompt + "，与参考图中的角色形象保持一致"
            except Exception as e:
                logger.warning(f"[Apilio画图] 读取角色参考图失败: {e}")
        loop = asyncio.get_event_loop()
        deadline = loop.time() + 240.0
        try:
            path = await self._generate(prompt, image_data, deadline)
            await self._send_image(event, path)
            suffix = "（已使用当前消息/引用消息中的参考图）" if image_data else ""
            return f"图片已生成并发送（文件形式·原图）{suffix}。"
        except (asyncio.TimeoutError, TimeoutError):
            return "error: 生图超时，请稍后重试（一张图通常需要 1~2 分钟）。"
        except Exception as e:
            return f"error: 图片生成失败：{e}"

    @llm_tool()
    async def remember_character(self, event: AstrMessageEvent, name: str, image_url: str = "") -> str:
        """把当前消息/引用消息中的图片保存为指定角色的参考图，之后生成该角色图片时会自动带上参考图保持形象一致。

        Args:
            name(string): 角色名，例如：洛茜、糖糖
            image_url(string): 图片链接；不传时取当前消息/引用消息里的图片
        """
        blocked = self._llm_guard()
        if blocked:
            return blocked
        name = str(name or "").strip()
        if not name:
            return "error: remember_character 需要提供 name 参数（角色名）"
        refs = []
        if str(image_url or "").strip():
            refs.append(str(image_url).strip())
        refs.extend(self._collect_images(event))
        data = None
        try:
            for src in refs[:1]:
                async with httpx.AsyncClient(timeout=60) as client:
                    data = await _fetch_image_bytes(client, src)
                    if data:
                        break
        except Exception as e:
            logger.warning(f"[Apilio画图] 读取角色参考图失败: {e}")
        if not data:
            return "error: 没有找到可保存的图片（当前消息或引用消息里没有图片）"
        try:
            uid = str(event.get_sender_id() or "global")
            safe_name = (
                "".join(c for c in name if c.isalnum() or c == "_")[:40]
                or uuid.uuid4().hex[:8]
            )
            fname = f"{uid}_{safe_name}.jpg"
            path = os.path.join(self._ref_dir(), fname)
            with open(path, "wb") as f:
                f.write(data)
            idx = self._ref_index()
            idx.setdefault(uid, {})[name] = fname
            self._save_ref_index(idx)
            return f"已记住角色「{name}」的参考图，之后画这个角色会保持形象一致。"
        except Exception as e:
            return f"error: 保存角色参考图失败：{e}"

    @llm_tool()
    async def forget_character(self, event: AstrMessageEvent, name: str) -> str:
        """删除之前保存的某个角色参考图。用户要求忘记/删除角色参考时调用。

        Args:
            name(string): 角色名
        """
        blocked = self._llm_guard()
        if blocked:
            return blocked
        name = str(name or "").strip()
        if not name:
            return "error: forget_character 需要提供 name 参数（角色名）"
        try:
            uid = str(event.get_sender_id() or "global")
            idx = self._ref_index()
            fname = (idx.get(uid) or {}).pop(name, None)
            self._save_ref_index(idx)
            if fname:
                path = os.path.join(self._ref_dir(), str(fname))
                if os.path.exists(path):
                    os.remove(path)
                return f"已忘记角色「{name}」的参考图。"
            return f"没有找到角色「{name}」的参考图。"
        except Exception as e:
            return f"error: 删除角色参考图失败：{e}"

    @llm_tool()
    async def list_character_refs(self, event: AstrMessageEvent) -> str:
        """列出当前用户已保存的角色参考图。用户询问记得哪些角色时调用。"""
        blocked = self._llm_guard()
        if blocked:
            return blocked
        try:
            uid = str(event.get_sender_id() or "global")
            names = sorted((self._ref_index().get(uid) or {}).keys())
            if not names:
                return "当前还没有保存任何角色参考图。"
            return "已保存的角色参考图：" + "、".join(names)
        except Exception as e:
            return f"error: 读取角色参考失败：{e}"

    async def _analyze_apilio(self, image_data: str, question: str) -> str | None:
        key = self._key()
        if not key:
            return None
        base = str(self.config.get("api_base") or "https://api.apilio.ai").rstrip("/")
        model = str(self.config.get("vision_model") or "gpt-5.6-luna")
        try:
            async with httpx.AsyncClient(
                timeout=180,
                headers={"Authorization": f"Bearer {key}"},
                trust_env=False,
            ) as client:
                body = {
                    "model": model,
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": question},
                                {"type": "image_url", "image_url": {"url": image_data}},
                            ],
                        }
                    ],
                    "stream": False,
                }
                r = await _request_with_retry(
                    client,
                    "POST",
                    f"{base}/v1/chat/completions",
                    json=body,
                )
                r.raise_for_status()
                data = r.json()
                text = (
                    (data.get("choices") or [{}])[0]
                    .get("message", {})
                    .get("content", "")
                )
                return str(text).strip() or None
        except Exception as e:
            logger.warning(f"[Apilio画图] Apilio 识图失败: {e}")
            return None

    async def _analyze_doubao(self, image_data: str, question: str) -> str | None:
        key = str(self.config.get("doubao_api_key") or "") or os.environ.get(
            "DOUBAO_KEY", ""
        )
        if not key:
            return None
        base = str(
            self.config.get("doubao_api_base")
            or "https://ark.cn-beijing.volces.com/api/v3"
        ).rstrip("/")
        model = str(
            self.config.get("doubao_vision_model") or "doubao-seed-evolving"
        )
        try:
            async with httpx.AsyncClient(
                timeout=180,
                headers={"Authorization": f"Bearer {key}"},
                trust_env=False,
            ) as client:
                body = {
                    "model": model,
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": question},
                                {"type": "image_url", "image_url": {"url": image_data}},
                            ],
                        }
                    ],
                    "stream": False,
                }
                r = await _request_with_retry(
                    client,
                    "POST",
                    f"{base}/chat/completions",
                    json=body,
                )
                r.raise_for_status()
                data = r.json()
                text = (
                    (data.get("choices") or [{}])[0]
                    .get("message", {})
                    .get("content", "")
                )
                return str(text).strip() or None
        except Exception as e:
            logger.warning(f"[Apilio画图] 豆包识图失败: {e}")
            return None

    @staticmethod
    def _green_to_alpha(path: str) -> str:
        img = PILImage.open(path).convert("RGB")
        out = PILImage.new("RGBA", img.size, (0, 0, 0, 0))
        src = img.load()
        dst = out.load()
        for y in range(img.height):
            for x in range(img.width):
                r, g, b = src[x, y]
                if g > 90 and g > r * 1.35 and g > b * 1.35:
                    dst[x, y] = (0, 0, 0, 0)
                else:
                    excess = max(0, g - max(r, b))
                    dst[x, y] = (
                        r,
                        max(0, g - int(excess * 0.7)),
                        b,
                        255,
                    )
        out_path = os.path.join(
            tempfile.gettempdir(), f"cutout_{uuid.uuid4().hex}.png"
        )
        out.save(out_path, format="PNG")
        return out_path

    @llm_tool()
    async def remove_background(self, event: AstrMessageEvent, image_url: str = "") -> str:
        """用 AI 模型抠除图片背景，保留主体并输出透明背景 PNG。

        当用户要求抠图、去背景、把人物/主体扣出来时调用本工具，不要用 shell 或 rembg。

        Args:
            image_url(string): 图片链接（默认使用当前消息/引用消息里的图片）
        """
        blocked = self._llm_guard()
        if blocked:
            return blocked
        refs = []
        if str(image_url or "").strip():
            refs.append(str(image_url).strip())
        refs.extend(self._collect_images(event))
        if not refs:
            return "error: 当前消息或引用消息中没有检测到图片，无法抠图。"
        image_data = None
        try:
            async with httpx.AsyncClient(timeout=120, trust_env=False) as client:
                for src in refs[:1]:
                    image_data = await _image_to_data_url(client, src)
                    if image_data:
                        break
        except Exception as e:
            logger.warning(f"[Apilio画图] 抠图读取图片失败: {e}")
        if not image_data:
            return "error: 图片读取失败，无法抠图。"
        prompt = (
            "抠除背景，只保留主体；背景填充为纯绿色（RGB 0,255,0）；"
            "主体颜色、轮廓、细节完整保留；不要文字水印。"
        )
        try:
            path = await self._generate(prompt, image_data)
            out_path = self._green_to_alpha(path)
            await self._send_image(event, out_path)
            return "已用 AI 模型完成抠图，已发送透明背景 PNG（文件形式·原图）。"
        except Exception as e:
            return f"error: AI 抠图失败：{e}"

    @llm_tool()
    async def analyze_image(self, event: AstrMessageEvent, question: str = "", image_url: str = "") -> str:
        """识别或分析当前消息/引用消息中的图片，返回文字结果。

        Args:
            question(string): 想了解图片的什么问题，例如：图中有什么文字、描述画面、识别物品
            image_url(string): 图片链接（例如历史消息工具返回的图片 URL）
        """
        blocked = self._llm_guard()
        if blocked:
            return blocked
        question = str(question or "").strip() or "请详细描述这张图片的内容。"
        paths = []
        if str(image_url or "").strip():
            paths.append(str(image_url).strip())
        paths.extend(self._collect_images(event))
        if not paths:
            return "error: 当前消息或引用消息中没有检测到图片，无法分析。"
        image_data = None
        try:
            async with httpx.AsyncClient(timeout=120, trust_env=False) as client:
                for src in paths[:1]:
                    image_data = await _image_to_data_url(client, src)
                    if image_data:
                        break
        except Exception as e:
            logger.warning(f"[Apilio画图] 图片预处理失败: {e}")
        if not image_data:
            return "error: 图片读取失败，无法分析。"
        text = await self._analyze_apilio(image_data, question)
        if not text:
            text = await self._analyze_doubao(image_data, question)
        return text or "error: 图片分析失败（Apilio 与豆包均不可用）。"


    # ---------- 生视频 ----------

    async def _generate_video(
        self,
        prompt: str,
        image_url: Optional[str] = None,
        deadline: Optional[float] = None,
    ) -> str:
        loop = asyncio.get_event_loop()
        if deadline is None:
            deadline = loop.time() + 1500.0
        vid = str(self.config.get("video_provider_id") or "").strip()
        if vid:
            logger.info(f"[Apilio画图] 生视频指定模型: {vid}")
        path = await self._video_manager.generate(prompt, image_url, deadline, prefer=vid)
        if not path:
            raise RuntimeError(
                "所有生视频提供商均失败（失败详情见用量日志 "
                "astrbot_plugin_apilio_draw_usage.log）"
            )
        return path

    async def _send_video(self, event: AstrMessageEvent, path: str) -> bool:
        """发送视频：默认发视频消息，失败回退成文件。"""
        mode = str(self.config.get("video_send_mode", "video") or "video").strip().lower()
        if mode == "video":
            try:
                await event.send(MessageChain([MsgVideo.fromFileSystem(path)]))
                logger.info(f"[Apilio画图] 已以视频消息发送: {os.path.basename(path)}")
                return True
            except Exception as e:
                logger.warning(f"[Apilio画图] 视频消息发送失败，回退文件: {e}")
        try:
            name = os.path.basename(str(path))
            bot = getattr(event, "bot", None)
            try:
                group_id = event.get_group_id() or ""
            except Exception:
                group_id = ""
            if group_id:
                await bot.upload_group_file(group_id=int(group_id), file=str(path), name=name)
            else:
                await bot.upload_private_file(user_id=int(event.get_sender_id()), file=str(path), name=name)
            logger.info(f"[Apilio画图] 已以文件形式发送视频: {name}")
            return True
        except Exception as e:
            logger.error(f"[Apilio画图] 视频发送失败: {e}")
            return False

    @llm_tool()
    async def generate_video(self, event: AstrMessageEvent, prompt: str, image_url: str = "") -> str:
        """生成一段视频（文生视频；给一张图则是图生视频），并把视频发送给用户。

        Args:
            prompt(string): 视频内容描述，包含画面、动作、镜头运动
            image_url(string): 图生视频用的图片链接（必须是可公开访问的 http 地址）
        """
        blocked = self._llm_guard()
        if blocked:
            return blocked
        prompt = str(prompt or "").strip()
        if not prompt:
            return "error: generate_video 需要提供 prompt 参数"
        url = str(image_url or "").strip()
        if url and not url.startswith("http"):
            url = ""
        if not self._video_manager.enabled():
            return "error: 没有启用的生视频提供商，请在「生图管理」面板里添加百炼视频模型。"
        loop = asyncio.get_event_loop()
        try:
            path = await self._generate_video(prompt, url or None, loop.time() + 1500.0)
            await self._send_video(event, path)
            return "视频已生成并发送（渲染通常 1~5 分钟）。"
        except Exception as e:
            return f"error: 视频生成失败：{e}"

    @filter.command("生视频", alias={"ai视频", "文生视频", "图生视频"})
    async def make_video(self, event: AstrMessageEvent):
        prompt = event.message_str.strip()
        for prefix in ("ai视频", "文生视频", "图生视频", "生视频"):
            if prompt.startswith(prefix):
                prompt = prompt[len(prefix):].strip()
                break
        if prompt.startswith("/"):
            prompt = prompt[1:].strip()
        if not prompt:
            yield event.plain_result(
                "用法：/生视频 <描述>\n"
                "例如：/生视频 一只戴宇航头盔的橘猫在火星上慢慢走，镜头缓慢推进\n"
                "图生视频：发一张图并附 http 图片链接（本地图暂不支持）"
            )
            return
        if not self._video_manager.enabled():
            yield event.plain_result("❌ 还没有启用的生视频提供商，请到「生图管理」面板添加（百炼视频模型）。")
            return
        image_url = ""
        try:
            for comp in (getattr(event, "message", None) or []):
                src = getattr(comp, "url", "") or ""
                if isinstance(src, str) and src.startswith("http"):
                    image_url = src
                    break
        except Exception:
            image_url = ""
        mode = "图生视频" if image_url else "文生视频"
        yield event.plain_result(f"🎬 正在{mode}（通常 1~5 分钟，出片后自动发给你）…")
        try:
            loop = asyncio.get_event_loop()
            path = await self._generate_video(prompt, image_url or None, loop.time() + 1500.0)
            ok = await self._send_video(event, path)
            if not ok:
                yield event.plain_result("❌ 视频已生成，但发送失败（见日志）。")
        except Exception as e:
            logger.error(f"[Apilio画图] 生视频失败: {e}")
            yield event.plain_result(f"❌ 生视频失败：{e}")

    @filter.command("画图", alias={"生图", "绘图", "ai生图"})
    async def draw(self, event: AstrMessageEvent):
        prompt = event.message_str.strip()
        for prefix in ("ai生图", "画图", "生图", "绘图"):
            if prompt.startswith(prefix):
                prompt = prompt[len(prefix):].strip()
                break
        if prompt.startswith("/"):
            prompt = prompt[1:].strip()
        # 兼容旧 /ai生图 用法里的通道词（豆包/火山/seedream）：通道由优先级自动决定，这里只剥掉词
        for ch in ("豆包", "火山", "seedream", "Seedream", "ark"):
            if prompt.startswith(ch):
                prompt = prompt[len(ch):].strip()
                break
        if prompt.startswith(("提供商", "provider", "服务商")):
            for pre in ("提供商", "provider", "服务商"):
                if prompt.startswith(pre):
                    rest = prompt[len(pre):].strip()
                    break
            yield event.plain_result(await self._handle_provider_command(rest))
            return
        ref_images: List[str] = []
        try:
            chain = getattr(event, "message", None)
            if chain is None:
                chain = event.get_messages()
            for comp in chain:
                if isinstance(comp, MsgImage):
                    src = comp.path or comp.file or comp.url or ""
                    if src:
                        ref_images.append(src)
        except Exception as e:
            logger.warning(f"[Apilio画图] 提取参考图失败: {e}")

        image_data = None
        if ref_images:
            async with httpx.AsyncClient(timeout=60) as client:
                for src in ref_images[:3]:
                    image_data = await _image_to_data_url(client, src)
                    if image_data:
                        break

        try:
            ref_path = self._find_ref_for_prompt(
                str(event.get_sender_id() or "global"), prompt
            )
        except Exception:
            ref_path = None
        if ref_path and not image_data:
            try:
                async with httpx.AsyncClient(timeout=60) as client:
                    image_data = await _image_to_data_url(client, ref_path)
                if image_data:
                    prompt = prompt + "，与参考图中的角色形象保持一致"
            except Exception as e:
                logger.warning(f"[Apilio画图] 读取角色参考图失败: {e}")

        if not prompt:
            yield event.plain_result(
                "用法：/画图 <描述>（可同时发一张参考图做图文生图），"
                "例如 /画图 把猫改成蓝色外套"
            )
            return
        mode = "图文生图" if image_data else "文生图"
        yield event.plain_result(f"🎨 正在{mode}，请稍候…")
        try:
            path = await self._generate(prompt, image_data)
            await self._send_image(event, path)
        except Exception as e:
            logger.error(f"[Apilio画图] 生成失败: {e}")
            yield event.plain_result(f"❌ 生成失败：{e}")
