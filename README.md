# AstrBot 多模型生图插件

> ⚠️ **本仓库代码由 AI 生成与维护**（DeepSeek Harness / Codex）。
> 逻辑与参数经人工确认，但请自行复核后再用于生产环境。

多后端生图插件。支持任意 OpenAI 兼容图片接口，内置三类后端，按优先级**顺序降级**：
一家失败自动切下一家。自带 WebUI 管理面板（提供商增删改、用量统计）。

## 功能

- 指令：`/画图` `/生图` `/绘图` `/ai生图`
- **多后端顺序降级**：火山方舟 Seedream、百炼万相、任意 OpenAI 兼容接口
- **参考图生图**
- **角色参考图记忆**：记住角色形象，之后按名字调用
- **抠图 / 识图**
- **全局提示词注入**：一段规则自动拼在用户描述前面
- **WebUI 面板**：`pages/dashboard`，可视化管理后端与用量

## 安装

把插件目录放进 AstrBot 的 `data/plugins/` 下，重启即可：

```
data/plugins/astrbot_plugin_apilio_draw/
```

> 目录名必须与 `metadata.yaml` 里的 `name` 一致（当前为 `astrbot_plugin_apilio_draw`）。
> 仓库名是后来取的通用名，跟插件 id 不同名，克隆后请**保持目录名不变**，否则 AstrBot 认不出来。

## 配置要点

| 字段 | 说明 |
| --- | --- |
| `providers` | 后端列表，每项含 `id / enable / priority / api_base / api_key / model / size` |
| `mode` | `sequential` 顺序降级 / `concurrent` 并发容错 |
| `send_mode` | `file` = 原图文件（仅 OneBot/NapCat 可用）；`image` = 图片消息（会被 QQ 压缩）。**QQ 官方机器人通道只支持 `image`** |
| `prompt_injection` | `{enabled, text}`，画图时把 text 拼在用户描述**前面** |
| `image_provider_id` | 面板上的“当前生图模型”（见下方已知缺陷） |

## ⚠️ 已知缺陷：`image_provider_id` 不生效

`providers.py` 里两段逻辑互相抵消：

```python
# generate()：先把指定后端提到列表最前
prefer = str(prefer or "").strip()
if prefer:
    hit = [p for p in enabled if p.id == prefer]
    if hit:
        enabled = hit + [p for p in enabled if p.id != prefer]
...
return await self._generate_sequential(prompt, image_data, deadline, enabled)

# _generate_sequential()：紧接着又按 priority 重排，prefer 被冲掉
async def _generate_sequential(self, prompt, image_data, deadline, enabled):
    ordered = sorted(enabled, key=lambda p: p.priority)
```

**现象**：在面板里选了某个后端，实际出图却总是优先级数值最小的那个。

**规避**：想让哪个后端优先，**直接改它的 `priority`**（数值越小越先试），不要依赖 `image_provider_id`。

## 关于本仓库

- **来源**：本机自研，**没有上游仓库**。`metadata.yaml` 的 `author` 为 `Codex`，`repo` 为空。
- 代码中**不含任何 API Key / 凭据**：密钥都在 AstrBot 的 `data/config/*.json` 里，由使用者自行填写。
- 代码中**不含本机绝对路径**：路径均基于 `~/.astrbot/...` 或系统临时目录拼接。
- `pages/dashboard/vue.global.prod.js` 是面板依赖的 Vue 运行时（第三方，未修改）。

## 许可

**未附许可证。** 本插件未声明许可，如需公开分发或商用请先确认归属。
