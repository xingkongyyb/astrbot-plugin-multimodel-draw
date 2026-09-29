# -*- coding: utf-8 -*-
"""生图提供商注册表与适配器（OpenAI 兼容 + 自定义请求模板）。

支持:
- 顺序降级 sequential: 按 priority 逐个尝试，失败自动切换下一家
- 并发容错 concurrent: 同时请求所有启用的提供商，谁先成功用谁，其余取消
- 群内命令管理（见 main.py 的 /画图 提供商 子命令）
- 每次调用写入用量日志（token / 出图数 / 耗时 / 结果）
"""

import asyncio
import base64
import json
import os
import tempfile
import time
import uuid
from typing import Any, Dict, List, Optional

import httpx

from astrbot.api import logger

QUALITY_SUFFIX = (
    ", high quality, highly detailed, beautiful composition, "
    "no text, no watermark, no deformed hands, no extra limbs"
)

# ── 长宽比解析：由用户在提示词中决定，插件不写死尺寸 ──────────────────
import re as _re

_ASPECT_HINTS = [
    (_re.compile(r"(16\s*[:：]\s*9|横版|宽屏|横向|宽幅|landscape|wide\s*screen)", _re.I), "1536x1024"),
    (_re.compile(r"(9\s*[:：]\s*16|竖版|纵向|竖屏|长图|portrait|vertical)", _re.I), "1024x1536"),
    (_re.compile(r"(3\s*[:：]\s*2|4\s*[:：]\s*3)", _re.I), "1536x1024"),
    (_re.compile(r"(2\s*[:：]\s*3|3\s*[:：]\s*4)", _re.I), "1024x1536"),
    (_re.compile(r"(1\s*[:：]\s*1|正方形|方形|square)", _re.I), "1024x1024"),
]
_EXPLICIT_SIZE_RE = _re.compile(r"(\d{3,4})\s*[x×X*]\s*(\d{3,4})")


def resolve_size_from_prompt(raw_prompt: str, fallback: str = "") -> str:
    """从用户提示词解析尺寸/长宽比；解析不到时返回 fallback（配置值，可为空）。

    支持两种写法：
      1. 显式尺寸：'1536x1024' / '2048×1152'
      2. 比例关键词：16:9 / 横版 / 竖版 / 9:16 / 正方形 / 4:3 等
    """
    text = str(raw_prompt or "")
    m = _EXPLICIT_SIZE_RE.search(text)
    if m:
        w, h = int(m.group(1)), int(m.group(2))
        if 256 <= w <= 4096 and 256 <= h <= 4096:
            return f"{w}x{h}"
    for pat, size in _ASPECT_HINTS:
        if pat.search(text):
            return size
    return str(fallback or "").strip()

_USAGE_LOG = os.path.join(
    os.path.expanduser("~"), ".astrbot", "logs", "astrbot_plugin_apilio_draw_usage.log"
)


def log_usage(
    *,
    provider: str = "",
    model: str = "",
    ok: bool = False,
    tokens: int = 0,
    images: int = 0,
    elapsed_ms: int = 0,
    prompt: str = "",
    err: str = "",
) -> None:
    """以 JSON Lines 追加写入用量日志（每次生图调用一条）。"""
    try:
        entry = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "plugin": "astrbot_plugin_apilio_draw",
            "provider": str(provider or ""),
            "model": str(model or ""),
            "ok": bool(ok),
            "tokens": int(tokens or 0),
            "images": int(images or 0),
            "elapsed_ms": int(elapsed_ms or 0),
            "prompt": str(prompt or "")[:200],
            "err": str(err or ""),
        }
        with open(_USAGE_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.warning(f"[Apilio画图] 写入用量日志失败: {e}")


class ProviderError(Exception):
    pass


def _deep_replace(obj: Any, mapping: Dict[str, str]) -> Any:
    """递归替换字符串中的 {key} 占位符（用于 template 类型）。"""
    if isinstance(obj, str):
        out = obj
        for k, v in mapping.items():
            out = out.replace("{" + k + "}", v)
        return out
    if isinstance(obj, dict):
        return {k: _deep_replace(v, mapping) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_deep_replace(v, mapping) for v in obj]
    return obj


def _resolve_dotted(data: Any, path: str) -> Any:
    """按 'a.b.0.c' 路径取值（dict 键 + list 下标）。"""
    if not path:
        return None
    cur = data
    for part in path.split("."):
        if cur is None:
            return None
        if isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None
        elif isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


def _save_bytes(data: bytes, prefix: str, ext: str = "png") -> str:
    path = os.path.join(tempfile.gettempdir(), f"{prefix}_{uuid.uuid4().hex}.{ext}")
    with open(path, "wb") as f:
        f.write(data)
    return path


async def _extract_image_bytes(raw: Any, kind: str, prefix: str) -> Optional[str]:
    """把响应里的图片字段转成本地文件路径。kind: b64_json/base64/data_url/url"""
    if raw is None:
        return None
    try:
        if kind == "url":
            try:
                async with httpx.AsyncClient(timeout=120, trust_env=False) as c:
                    r = await c.get(str(raw))
                    r.raise_for_status()
                    ext = (str(raw).split("?")[0].rsplit(".", 1)[-1] or "png").lower()
                    if ext not in ("png", "jpg", "jpeg", "gif", "webp"):
                        ext = "png"
                    return _save_bytes(r.content, prefix, ext)
            except Exception as e:
                logger.warning(f"[Apilio画图] 图片下载失败: {e}")
                return None
        s = str(raw or "")
        if kind == "data_url":
            if "," in s:
                s = s.split(",", 1)[1]
        try:
            data = base64.b64decode(s)
        except Exception:
            return None
        ext = "png"
        return _save_bytes(data, prefix, ext)
    except Exception as e:
        logger.warning(f"[Apilio画图] 图片解码失败: {e}")
        return None


class BaseProvider:
    TYPE = "base"
    CAP = "image"          # image | video —— 决定它归哪个调度器管

    def __init__(self, cfg: Dict[str, Any]):
        self.cfg = dict(cfg or {})
        self.id = str(self.cfg.get("id") or "").strip()
        self.name = str(self.cfg.get("name") or self.id or "未命名")
        self.enable = bool(self.cfg.get("enable", True))
        try:
            self.priority = int(self.cfg.get("priority", 100))
        except (TypeError, ValueError):
            self.priority = 100
        try:
            self.timeout = float(self.cfg.get("timeout", 180) or 180)
        except (TypeError, ValueError):
            self.timeout = 180.0
        self.model = str(self.cfg.get("model") or "")
        self.size = str(self.cfg.get("size") or "").strip()
        self.retries = max(1, int(self.cfg.get("request_retries", 2) or 2))

    def _final_prompt(self, raw: str) -> str:
        qs = self.cfg.get("quality_suffix", QUALITY_SUFFIX)
        if qs is False or qs is None:
            return str(raw or "")
        return str(raw or "") + str(qs)

    def describe(self) -> str:
        return (
            f"{self.id} [{self.TYPE}] {'✅启用' if self.enable else '⛔停用'} "
            f"优先级{self.priority} model={self.model} size={self.size or '默认'}"
        )

    async def generate(self, prompt: str, image_data: Optional[str], deadline: Optional[float]) -> Optional[str]:
        raise NotImplementedError

    async def _request_json(
        self, client: httpx.AsyncClient, method: str, url: str, payload: dict
    ) -> httpx.Response:
        last: Optional[Exception] = None
        for attempt in range(self.retries):
            try:
                r = await client.request(method, url, json=payload)
                if r.status_code != 200 and payload.get(self.cfg.get("ref_field", "image")):
                    # 部分接口不接受参考图时降级为文生图重试一次
                    alt = dict(payload)
                    alt.pop(self.cfg.get("ref_field", "image"), None)
                    r = await client.request(method, url, json=alt)
                if r.status_code == 200:
                    return r
                last = RuntimeError(f"HTTP {r.status_code}: {r.text[:300]}")
                if attempt < self.retries - 1:
                    await asyncio.sleep(2)
            except (httpx.TransportError, httpx.TimeoutException) as e:
                last = e
                if attempt < self.retries - 1:
                    await asyncio.sleep(2 * (attempt + 1))
        raise last or RuntimeError("请求失败")


class OpenAICompatProvider(BaseProvider):
    """OpenAI images/generations 兼容接口（Ark/通义/硅基/OpenRouter 代理等）。"""

    TYPE = "openai_compat"

    def __init__(self, cfg: Dict[str, Any]):
        super().__init__(cfg)
        self.api_base = str(self.cfg.get("api_base") or "").rstrip("/")
        self.api_key = str(self.cfg.get("api_key") or "")
        self.ref_field = str(self.cfg.get("ref_field") or "image")
        self.response_format = str(self.cfg.get("response_format") or "b64_json")
        self.extra_body = dict(self.cfg.get("extra_body") or {})

    async def generate(self, prompt, image_data=None, deadline=None) -> Optional[str]:
        t0 = time.time()
        if not self.api_key:
            log_usage(provider=self.id, model=self.model, ok=False, err="未配置 api_key", prompt=prompt)
            return None
        if not self.api_base:
            log_usage(provider=self.id, model=self.model, ok=False, err="未配置 api_base", prompt=prompt)
            return None
        final_prompt = self._final_prompt(prompt)
        payload: Dict[str, Any] = {"model": self.model, "prompt": final_prompt}
        # 长宽比优先取用户提示词里写明的（插件不限制）；未写明才用配置的 size
        effective_size = resolve_size_from_prompt(prompt, self.size)
        if effective_size:
            payload["size"] = effective_size
        payload["response_format"] = self.response_format
        payload.update(self.extra_body)
        if image_data:
            payload[self.ref_field] = image_data
        headers = {"Authorization": f"Bearer {self.api_key}"}
        try:
            async with httpx.AsyncClient(
                timeout=min(180.0, self.timeout), headers=headers, trust_env=False
            ) as client:
                r = await self._request_json(client, "POST", f"{self.api_base}/images/generations", payload)
                data = r.json()
                items = data.get("data") or []
                if not items:
                    log_usage(provider=self.id, model=self.model, ok=False, err="响应无 data", prompt=prompt)
                    return None
                raw = items[0].get("b64_json") or items[0].get("url") or items[0].get("base64")
                kind = "url" if not items[0].get("b64_json") and (items[0].get("url") or "").startswith("http") else "b64_json"
                path = await _extract_image_bytes(raw, kind, f"{self.id}_{self.model}")
                if not path:
                    log_usage(provider=self.id, model=self.model, ok=False, err="图片字段为空", prompt=prompt)
                    return None
                usage = data.get("usage") or {}
                tokens = int(usage.get("total_tokens") or 0)
                log_usage(
                    provider=self.id, model=self.model, ok=True,
                    tokens=tokens, images=len(items),
                    elapsed_ms=int((time.time() - t0) * 1000), prompt=prompt,
                )
                return path
        except Exception as e:
            log_usage(provider=self.id, model=self.model, ok=False, err=str(e), prompt=prompt)
            return None


class TemplateProvider(BaseProvider):
    """自定义 HTTP 模板适配器：任意 method/url/headers/body/响应解析。

    占位符: {api_key} {model} {size} {prompt} {image}
    响应字段: response_field 支持 'data.0.b64_json' / 'images.0' 等点路径
    image_kind: b64_json | base64 | data_url | url
    """

    TYPE = "template"

    def __init__(self, cfg: Dict[str, Any]):
        super().__init__(cfg)
        self.api_base = str(self.cfg.get("api_base") or "").rstrip("/")
        self.api_key = str(self.cfg.get("api_key") or "")
        self.method = str(self.cfg.get("method") or "POST").upper()
        self.url = str(self.cfg.get("url") or "/images/generations").strip()
        self.headers = dict(self.cfg.get("headers") or {})
        self.body_template = dict(self.cfg.get("body_template") or {})
        self.response_field = str(self.cfg.get("response_field") or "data.0.b64_json")
        self.image_kind = str(self.cfg.get("image_kind") or "b64_json")

    async def generate(self, prompt, image_data=None, deadline=None) -> Optional[str]:
        t0 = time.time()
        final_prompt = self._final_prompt(prompt)
        mapping = {
            "api_key": self.api_key,
            "model": self.model,
            "size": self.size,
            "prompt": final_prompt,
            "image": image_data or "",
        }
        if self.url.startswith("http"):
            url = self.url
        else:
            url = f"{self.api_base}/{self.url.lstrip('/')}"
        headers = _deep_replace(self.headers, mapping)
        headers.setdefault("Content-Type", "application/json")
        body = _deep_replace(self.body_template, mapping)
        try:
            async with httpx.AsyncClient(
                timeout=min(180.0, self.timeout), headers=headers, trust_env=False
            ) as client:
                r = await self._request_json(client, self.method, url, body)
                data = r.json()
                raw = _resolve_dotted(data, self.response_field)
                path = await _extract_image_bytes(raw, self.image_kind, f"{self.id}_{self.model}")
                if not path:
                    log_usage(provider=self.id, model=self.model, ok=False, err=f"解析响应失败: {str(data)[:200]}", prompt=prompt)
                    return None
                usage = _resolve_dotted(data, "usage.total_tokens")
                try:
                    tokens = int(usage or 0)
                except (TypeError, ValueError):
                    tokens = 0
                log_usage(
                    provider=self.id, model=self.model, ok=True,
                    tokens=tokens, images=1,
                    elapsed_ms=int((time.time() - t0) * 1000), prompt=prompt,
                )
                return path
        except Exception as e:
            log_usage(provider=self.id, model=self.model, ok=False, err=str(e), prompt=prompt)
            return None




# ── 阿里云百炼（DashScope / Token Plan）───────────────────────────────────

def _walk_media(data: Any, kind: str) -> List[str]:
    """从百炼响应里挖出图片/视频地址。kind: image | video"""
    out: List[str] = []
    out_node = (data or {}).get("output") or {}

    if kind == "image":
        for ch in (out_node.get("choices") or []):
            for part in ((ch.get("message") or {}).get("content") or []):
                if isinstance(part, dict):
                    u = part.get("image") or part.get("url")
                    if u:
                        out.append(str(u))
    else:
        u = out_node.get("video_url")
        if u:
            out.append(str(u))
        res = out_node.get("results")
        if isinstance(res, dict) and res.get("video_url"):
            out.append(str(res["video_url"]))
        if isinstance(res, list):
            for item in res:
                if isinstance(item, dict) and item.get("url"):
                    out.append(str(item["url"]))

    if not out:
        stack = [data]
        keys = ("image", "video_url", "url")
        while stack:
            cur = stack.pop()
            if isinstance(cur, dict):
                for k, v in cur.items():
                    if k in keys and isinstance(v, str) and v.startswith("http"):
                        out.append(v)
                    else:
                        stack.append(v)
            elif isinstance(cur, list):
                stack.extend(cur)
    return out


async def _download_media(url: str, prefix: str, ext: str = "png") -> Optional[str]:
    try:
        async with httpx.AsyncClient(timeout=300, trust_env=False, follow_redirects=True) as c:
            r = await c.get(url)
            r.raise_for_status()
            if ext == "auto":
                raw_ext = (url.split("?")[0].rsplit(".", 1)[-1] or "bin").lower()
                ext = raw_ext if raw_ext in ("png", "jpg", "jpeg", "webp", "mp4", "mov") else "bin"
            return _save_bytes(r.content, prefix, ext)
    except Exception as e:
        logger.warning(f"[Apilio画图] 媒体下载失败 {str(url)[:80]}: {e}")
        return None


class BailianImageProvider(BaseProvider):
    """百炼图像生成：同步多模态生成接口。

    POST {api_base}/api/v1/services/aigc/multimodal-generation/generation
    """
    TYPE = "bailian_image"
    CAP = "image"

    def __init__(self, cfg: Dict[str, Any]):
        super().__init__(cfg)
        self.api_base = str(self.cfg.get("api_base") or "").rstrip("/")
        self.api_key = str(self.cfg.get("api_key") or "")
        self.negative = str(self.cfg.get("negative_prompt") or "")
        self.watermark = bool(self.cfg.get("watermark", False))
        self.prompt_extend = self.cfg.get("prompt_extend")
        try:
            self.count = max(1, int(self.cfg.get("n") or 1))
        except (TypeError, ValueError):
            self.count = 1

    async def generate(self, prompt, image_data=None, deadline=None) -> Optional[str]:
        t0 = time.time()
        if not self.api_key or not self.api_base:
            log_usage(provider=self.id, model=self.model, ok=False, err="未配置 api_base/api_key", prompt=prompt)
            return None
        final_prompt = self._final_prompt(prompt)
        size = resolve_size_from_prompt(prompt, self.size) or "1024*1024"
        size = size.replace("x", "*").replace("X", "*").replace("×", "*")
        params: Dict[str, Any] = {"size": size, "n": self.count, "watermark": self.watermark}
        if self.negative:
            params["negative_prompt"] = self.negative
        if self.prompt_extend is not None:
            params["prompt_extend"] = bool(self.prompt_extend)
        content: List[Dict[str, Any]] = []
        if image_data and str(image_data).startswith("http"):
            content.append({"image": image_data})
        content.append({"text": final_prompt})
        payload = {"model": self.model,
                   "input": {"messages": [{"role": "user", "content": content}]},
                   "parameters": params}
        url = f"{self.api_base}/api/v1/services/aigc/multimodal-generation/generation"
        try:
            async with httpx.AsyncClient(
                timeout=min(300.0, self.timeout),
                headers={"Authorization": f"Bearer {self.api_key}"}, trust_env=False,
            ) as client:
                r = await self._request_json(client, "POST", url, payload)
                data = r.json()
                links = _walk_media(data, "image")
                if not links:
                    log_usage(provider=self.id, model=self.model, ok=False,
                              err=f"响应无图片: {str(data)[:200]}", prompt=prompt)
                    return None
                path = await _download_media(links[0], f"{self.id}_{self.model}", "auto")
                if not path:
                    log_usage(provider=self.id, model=self.model, ok=False, err="图片下载失败", prompt=prompt)
                    return None
                log_usage(provider=self.id, model=self.model, ok=True, images=len(links),
                          elapsed_ms=int((time.time() - t0) * 1000), prompt=prompt)
                return path
        except Exception as e:
            log_usage(provider=self.id, model=self.model, ok=False, err=str(e), prompt=prompt)
            return None


class BailianVideoProvider(BaseProvider):
    """百炼视频生成：异步任务 + 轮询。

    POST {api_base}/api/v1/services/aigc/video-generation/video-synthesis  (X-DashScope-Async: enable)
    GET  {api_base}/api/v1/tasks/{task_id}
    """
    TYPE = "bailian_video"
    CAP = "video"

    def __init__(self, cfg: Dict[str, Any]):
        super().__init__(cfg)
        self.api_base = str(self.cfg.get("api_base") or "").rstrip("/")
        self.api_key = str(self.cfg.get("api_key") or "")
        self.resolution = str(self.cfg.get("resolution") or "720P")
        self.ratio = str(self.cfg.get("ratio") or "16:9")
        try:
            self.duration = int(self.cfg.get("duration") or 5)
        except (TypeError, ValueError):
            self.duration = 5
        self.watermark = bool(self.cfg.get("watermark", False))
        self.negative = str(self.cfg.get("negative_prompt") or "")
        try:
            self.poll_interval = max(2.0, float(self.cfg.get("poll_interval") or 6))
        except (TypeError, ValueError):
            self.poll_interval = 6.0
        try:
            self.max_wait = max(60.0, float(self.cfg.get("max_wait") or 900))
        except (TypeError, ValueError):
            self.max_wait = 900.0

    async def test_connection(self) -> Optional[str]:
        """只创建任务、不等待渲染 —— 用来验证鉴权与接口可用。"""
        if not self.api_key or not self.api_base:
            return None
        payload = {"model": self.model,
                   "input": {"prompt": "连接测试：一朵云在蓝天飘过"},
                   "parameters": {"watermark": False, "resolution": self.resolution,
                                  "ratio": self.ratio, "duration": 5}}
        url = f"{self.api_base}/api/v1/services/aigc/video-generation/video-synthesis"
        try:
            async with httpx.AsyncClient(timeout=60, trust_env=False) as client:
                r = await client.post(url, json=payload, headers={
                    "Authorization": f"Bearer {self.api_key}", "X-DashScope-Async": "enable"})  # 仅建任务用
                if r.status_code != 200:
                    logger.warning(f"[Apilio画图] 视频连通性测试失败 HTTP {r.status_code}: {r.text[:200]}")
                    return None
                return str((((r.json().get("output")) or {}).get("task_id")) or "") or None
        except Exception as e:
            logger.warning(f"[Apilio画图] 视频连通性测试异常: {e}")
            return None

    async def generate(self, prompt, image_data=None, deadline=None) -> Optional[str]:
        t0 = time.time()
        if not self.api_key or not self.api_base:
            log_usage(provider=self.id, model=self.model, ok=False, err="未配置 api_base/api_key", prompt=prompt)
            return None
        final_prompt = self._final_prompt(prompt)
        input_obj: Dict[str, Any] = {"prompt": final_prompt}
        if image_data and str(image_data).startswith("http"):
            input_obj["img_url"] = image_data          # 图生视频
        if self.negative:
            input_obj["negative_prompt"] = self.negative
        params: Dict[str, Any] = {"watermark": self.watermark}
        if self.resolution:
            params["resolution"] = self.resolution
        if self.ratio:
            params["ratio"] = self.ratio
        if self.duration:
            params["duration"] = self.duration
        payload = {"model": self.model, "input": input_obj, "parameters": params}
        create_url = f"{self.api_base}/api/v1/services/aigc/video-generation/video-synthesis"
        # ★ 关键：X-DashScope-Async 只能出现在「建任务」那一次请求上。
        #   轮询 GET /api/v1/tasks/{id} 若也带这个头，网关会回
        #   403 AccessDenied: current user api does not support asynchronous calls。
        auth = {"Authorization": f"Bearer {self.api_key}"}
        try:
            async with httpx.AsyncClient(timeout=min(120.0, self.timeout), headers=auth,
                                         trust_env=False) as client:
                r = await client.post(create_url, json=payload,
                                      headers={"X-DashScope-Async": "enable"})
                if r.status_code != 200:
                    log_usage(provider=self.id, model=self.model, ok=False,
                              err=f"建任务 HTTP {r.status_code}: {r.text[:200]}", prompt=prompt)
                    return None
                data = r.json()
                task_id = str(((data.get("output") or {}).get("task_id")) or "")
                if not task_id:
                    log_usage(provider=self.id, model=self.model, ok=False,
                              err=f"无 task_id: {str(data)[:200]}", prompt=prompt)
                    return None
                logger.info(f"[Apilio画图] 视频任务已创建 {self.id} task_id={task_id}")
                loop = asyncio.get_event_loop()
                limit = loop.time() + self.max_wait
                if deadline:
                    limit = min(limit, deadline)
                video_url = ""
                last_status = ""
                logger.info(f"[Apilio画图] 开始轮询视频任务 {task_id}（每 {self.poll_interval}s，上限 {int(limit - loop.time())}s）")
                while loop.time() < limit:
                    await asyncio.sleep(self.poll_interval)
                    tr = await client.get(f"{self.api_base}/api/v1/tasks/{task_id}")
                    if tr.status_code != 200:
                        logger.warning(f"[Apilio画图] 轮询任务 {task_id} 返回 HTTP {tr.status_code}: {tr.text[:180]}")
                        continue
                    td = tr.json()
                    out_node = td.get("output") or {}
                    last_status = str(out_node.get("task_status") or "")
                    if last_status == "SUCCEEDED":
                        links = _walk_media(td, "video")
                        video_url = links[0] if links else ""
                        break
                    if last_status in ("FAILED", "CANCELED", "UNKNOWN"):
                        log_usage(provider=self.id, model=self.model, ok=False,
                                  err=f"任务 {last_status}: {str(td)[:200]}", prompt=prompt)
                        return None
                if not video_url:
                    log_usage(provider=self.id, model=self.model, ok=False,
                              err=f"等待超时（最后状态 {last_status or '未知'}）", prompt=prompt)
                    return None
                path = await _download_media(video_url, f"{self.id}_{self.model}", "mp4")
                if not path:
                    log_usage(provider=self.id, model=self.model, ok=False, err="视频下载失败", prompt=prompt)
                    return None
                log_usage(provider=self.id, model=self.model, ok=True, images=1,
                          elapsed_ms=int((time.time() - t0) * 1000), prompt=prompt)
                return path
        except Exception as e:
            log_usage(provider=self.id, model=self.model, ok=False, err=str(e), prompt=prompt)
            return None

def build_provider(cfg: Dict[str, Any]) -> BaseProvider:
    ptype = str(cfg.get("type") or "").strip().lower()
    if ptype == "template":
        return TemplateProvider(cfg)
    if ptype == "bailian_image":
        return BailianImageProvider(cfg)
    if ptype == "bailian_video":
        return BailianVideoProvider(cfg)
    return OpenAICompatProvider(cfg)


class ProviderManager:
    """生图提供商注册表：增删、启停、顺序/并发选路与降级。"""

    def __init__(self, provider_configs: List[dict], mode: str = "sequential", cap: str = "image"):
        self.providers: List[BaseProvider] = []
        self.cap = str(cap or "image")
        for cfg in provider_configs or []:
            try:
                p = build_provider(cfg)
                if getattr(p, "CAP", "image") != self.cap:
                    continue
                self.providers.append(p)
            except Exception as e:
                logger.warning(f"[Apilio画图] 提供商配置无效已跳过: {cfg.get('id', '?')}: {e}")
        self.mode = "concurrent" if str(mode or "").lower() == "concurrent" else "sequential"

    # ---- 查询 ----
    def enabled(self) -> List[BaseProvider]:
        return [p for p in self.providers if p.enable]

    def get(self, pid: str) -> Optional[BaseProvider]:
        for p in self.providers:
            if p.id == pid:
                return p
        return None

    def summary(self) -> str:
        return f"{len(self.providers)} 个提供商（{self.mode} 模式），启用 {len(self.enabled())} 个"

    def describe(self, mode: str = "") -> str:
        lines = [f"🎨 生图提供商（模式：{self.mode}，{self.summary()}）"]
        ordered = sorted(self.providers, key=lambda p: p.priority)
        for i, p in enumerate(ordered, 1):
            lines.append(f"{i}. {p.describe()}")
        if not self.providers:
            lines.append("（暂无提供商，用 /画图 提供商 add 添加）")
        return "\n".join(lines)

    # ---- 主入口 ----
    async def generate(self, prompt: str, image_data: Optional[str] = None,
                       deadline: Optional[float] = None, prefer: str = "") -> Optional[str]:
        enabled = self.enabled()
        if not enabled:
            raise ProviderError("没有启用的提供商，请先添加：/画图 提供商 add")
        prefer = str(prefer or "").strip()
        if prefer:
            hit = [p for p in enabled if p.id == prefer]
            if hit:
                enabled = hit + [p for p in enabled if p.id != prefer]
            else:
                logger.warning(f"[Apilio画图] 指定的模型 {prefer} 不存在或未启用，按优先级选路")
        loop = asyncio.get_event_loop()
        if deadline is None:
            # 视频是异步任务，默认给 30 分钟预算；图片 4 分钟足够
            deadline = loop.time() + (1800.0 if self.cap == "video" else 240.0)
        if self.mode == "concurrent" and len(enabled) > 1:
            return await self._generate_concurrent(prompt, image_data, deadline, enabled)
        return await self._generate_sequential(prompt, image_data, deadline, enabled)

    async def _generate_sequential(self, prompt, image_data, deadline, enabled) -> Optional[str]:
        loop = asyncio.get_event_loop()
        ordered = sorted(enabled, key=lambda p: p.priority)
        last_err = ""
        for p in ordered:
            if deadline - loop.time() < 20.0:
                logger.warning(f"[Apilio画图] 时间预算不足，跳过 {p.id}")
                break
            logger.info(f"[Apilio画图] 顺序降级：尝试提供商 {p.id}（{p.name}）")
            try:
                path = await p.generate(prompt, image_data, deadline)
            except Exception as e:
                path = None
                last_err = str(e)
            if path:
                logger.info(f"[Apilio画图] 提供商 {p.id} 生成成功")
                return path
            logger.warning(f"[Apilio画图] 提供商 {p.id} 失败，切换下一家{('：' + last_err) if last_err else ''}")
        return None

    async def _generate_concurrent(self, prompt, image_data, deadline, enabled) -> Optional[str]:
        tasks = {asyncio.create_task(p.generate(prompt, image_data, deadline)): p for p in enabled}
        pending = set(tasks)
        while pending:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                p = tasks[t]
                try:
                    path = t.result()
                except Exception as e:
                    logger.warning(f"[Apilio画图] 并发任务 {p.id} 异常: {e}")
                    path = None
                if path:
                    logger.info(f"[Apilio画图] 并发容错：{p.id} 先成功，取消其余")
                    for other in pending:
                        other.cancel()
                    return path
        return None
