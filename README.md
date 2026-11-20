# 个人备忘助手 WebUI（Personal Assistant WebUI）

让麦麦帮你记住提醒：艾特 bot 或发 `/备忘` 指令创建定时备忘任务，到期后麦麦用 LLM 生成**真人化提醒**，按你的偏好私聊或群聊 `@` 发送，支持携带图片/文件附件。任务持久化，重启后自动恢复。MaiBot WebUI 可新增、编辑、删除、筛选任务，并设置每日重复，无需另开管理端口。

本插件基于 [LDrumDusk/maibot_personal_assistant](https://github.com/LDrumDusk/maibot_personal_assistant) 修改，保留原插件 MIT 许可证及署名。

## 快速开始

1. 安装插件（插件 ID：`github.sora-maru.personal-assistant-webui`）。
2. （可选）在插件配置 `[admin] admin_ids` 填入你的 QQ，获得全局开关权限。
3. 在群里或私聊艾特 bot：`@麦麦 一小时后提醒我喝水`
   —— 或用指令：`/备忘 一小时后提醒我喝水`

麦麦会回复确认，到点提醒你。

MaiBot WebUI 插件扩展侧栏中的 **备忘任务管理** 页面可按状态和单次/每日类型筛选，搜索任务内容或 QQ 号，并新增、编辑、删除和刷新任务。管理页创建的任务可填写 QQ 号、群号、提醒时间和私聊/群聊渠道；每日任务发送后保留，并顺延到次日的相同时间。页面沿用 MaiBot 登录态，不会开放独立 HTTP 端口；单次列表最多展示 500 条匹配结果。

当前插件版本 **1.3.0**，适配 [MaiBot v1.4.7](https://github.com/Mai-with-u/MaiBot/releases/tag/v1.4.7)、WebUI **1.10.7**、[Plugin SDK v2.23.0](https://github.com/Mai-with-u/maibot-plugin-sdk/releases/tag/v2.23.0)，需要 Python **3.12+**。清单兼容范围为 Host **1.4.7–1.4.99**、SDK **2.23.0–2.23.99**；实际验证版本为 1.4.7 / 2.23.0。仍在使用 MaiBot 1.3.x 的用户可保留插件 v1.2.0。

通过当前发行版更新时保留插件配置和相同插件 ID 的数据目录。`plugin.config_version` 仍为 `1.0.0`，无需修改；原 `tasks.json` 格式、任务 ID 和附件目录继续使用。不要同时启用原个人备忘助手和本插件，以免相同聊天指令重复触发。

### WebUI 任务与每日重复

- 普通 QQ 的平台标识填 `qq`；其他连接器请使用其实际平台标识和对应用户/群组 ID。已有任务缺少平台字段时按 `qq` 读取。
- 表单中的提醒时间按浏览器本地时区填写；每日重复以运行 MaiBot 的服务器时区为准。服务器与浏览器均设为 `Asia/Shanghai` 时，每天保持相同北京时间。
- 每日任务首次提醒仍受“时间上限”和“每人任务上限”约束。每次成功或失败后都保留，并顺延至下次相同时间；不会在失败后立即重发。上次发送失败会在列表中显示。
- 服务器停机期间不会补发多天的每一次提醒：恢复后处理一次已到期任务，再安排下一个未来日期。单次任务在处理结束后清理。
- 任务进入“发送中”后不可编辑或删除。编辑保留原附件；删除需在页面内确认，并清理该任务附件。

## 使用方式

| 触发 | 说明 |
|---|---|
| 艾特 bot + 一句话 | 回复模型判断是否为备忘任务；确认后规划模型提取时间与内容 |
| 私聊 bot + 一句话 | 同上（私聊直接和 bot 对话即触发识别） |
| `/备忘 <要求>` | 直接创建任务（如 `/备忘 明天下午3点提醒我开会`） |
| 分几条消息 | 艾特 bot 后 60 秒内（可配置）继续补充描述/图片，会被并入任务 |
| 引用消息 + 艾特 bot | 被引用消息内容会作为上下文一并考虑 |

> **独占处理**：识别为备忘任务后，插件会**接管该消息**（阻止 bot 主流程/planner 再处理），避免「双管齐下」——用户设置备忘时 bot 自己的 planner 不会同时插手。像「我目前有什么备忘」这类**非创建任务**的提问会正常放行，由 planner 回答（可通过插件的 `reminder_list_mine` 工具看到真实备忘）。

## 提醒渠道

| 偏好 | 行为 |
|---|---|
| `auto`（默认） | **群聊任务默认群聊 @**（任务发在哪个群就在哪个群提醒）；**私聊任务默认私聊**。稳定一致，不依赖私聊会话是否恰好存在 |
| `private` | 尝试私聊；无私聊会话则返回提示，并转群聊 @ 提醒 |
| `group` | 群聊 @ 提醒 |

> 用户可在任务里说「私聊提醒我」「在群里提醒我」，插件会按规则识别渠道偏好（不额外消耗 LLM）。

## 配置（config.toml）

```toml
[plugin]
enabled = true          # 插件总开关（超级管理员可用 /备忘开关 修改）
config_version = "1.0.0"

[reminder]
default_channel = "auto"      # auto/private/group
max_delay_hours = 24.0        # 时间上限（小时），超过拒绝
reject_message = "这个时间有点远啦，我最多帮你记住{limit}小时以内的事～你换个近一点的时间？"
capture_window_sec = 60.0     # 艾特后收集补充描述的时间窗口（秒）
poll_interval_sec = 1.0       # 调度轮询间隔（秒）
cleanup_interval_sec = 7200.0 # 定时清理过期任务的间隔（秒），0=关闭自动清理
max_tasks_per_user = 20       # 单个用户最多待办任务数
ack_template = "好，我知道了～到点（{time}）提醒你。   # 创建成功确认文案；默认不含 {task}，不复述任务（需复述可自行加 {task}）"

[prompts]
intent_parse_prompt = "..."   # 艾特 bot 时判断是否备忘任务的提示词（回复模型）
reminder_style = "..."         # 提醒风格提示词（叠加在人设三件套之上，留空则仅依据人设）

[storage]
max_media_mb = 20.0          # 单个图片/文件附件大小上限（MB），0=不限制

[admin]
admin_ids = []                # 超级管理员 QQ 号（可用 /备忘开关 全局开关插件）
```

- `max_delay_hours`：最远可提醒多久，默认 24 小时；超过返回 `reject_message`。
- `capture_window_sec`：艾特 bot 后等待用户补充描述的时间，默认 60 秒。
- `cleanup_interval_sec`：定时清理已到期任务（含已提醒/失败/拒绝）的间隔，默认 7200 秒（2 小时）；`0` 表示不自动清理。
- `max_media_mb`：单个附件大小上限，默认 20MB；超过的图片/文件不会作为任务附件（0 表示不限制）。
- 提示词均可自定义（`[prompts]`），满足不同人设/风格。

## 指令

| 指令 | 权限 | 说明 |
|---|---|---|
| `/备忘 <要求>` | 所有人 | 创建备忘任务 |
| `/备忘查` | 所有人 | 查看自己的待办任务 |
| `/备忘删 <id>` | 所有人 | 取消自己的任务（id 见 `/备忘查`） |
| `/备忘开关 on\|off` | 超级管理员 | 全局开关插件 |
| `/备忘管理` | 超级管理员 | 查看所有用户的待办任务（仅用户ID、创建时间、截止时间） |
| `/备忘清理 [expired\|all]` | 超级管理员 | 清理已过期（默认）或全部任务；`/备忘清理全部` 等同 `all` |

## 附件

- 发布任务时带上图片 → 图片**落盘**到插件数据目录 `media/<任务id>/`，到期提醒时麦麦把图片一起发给你。
- 发布任务时带上视频/文件 → 记录主程序文件引用（`file_ref`），到期时作为文件发送；不占插件存储。
- 单个附件超过 `[storage] max_media_mb`（默认 20MB）会被跳过，不会作为任务附件。
- 单次任务处理结束后清理附件和记录；每日任务保留附件，直到主动删除。文件引用须仍可在目标会话使用，不能通过修改群组绕过宿主的文件归属限制。

## 真人化提醒

- **识别是否任务**：回复模型（`replyer`）判断艾特消息是否为备忘任务。
- **分析任务**：确认是任务后，规划模型（`planner`）提取提醒时间与任务内容。
- **生成提醒**：到期用回复模型（`replyer`）结合人设三件套（人格设定/行为风格/表达风格）、任务内容、上下文生成口语化提醒，不是机械复制粘贴。
- **输出语句分割**：插件生成的确认/提醒文本会按主程序「输出语句分割」规则断句（`response_splitter` 配置），过长回复拆成多条发送，与麦麦正常回复风格一致（不注入错别字，保证提醒准确）。

## 持久化与恢复

- 任务记录存于插件数据目录 `tasks.json`；图片附件落盘于 `media/<任务id>/`。
- 麦麦重启后自动读取，未到期的继续等待，已到期的补发提醒。
- 视频/文件不占插件存储，仅记录主程序文件引用（`file_ref`）。

## 工具（LLM 可调用）

- `reminder_list_mine`：查看当前用户待办备忘。
- `reminder_cancel`：取消当前用户的一条备忘。

## 开发者

- 基于 [LDrumDusk/maibot_personal_assistant](https://github.com/LDrumDusk/maibot_personal_assistant) 修改，保留 MIT 许可及原作者署名。
- 管理 API 只返回任务所需字段，不暴露附件内容或持久化文件路径。
- 依赖：标准库 + `tomlkit`（写入 config.toml）
- 许可证：MIT
- 本地验证记录见 [VALIDATION.md](VALIDATION.md)。

在开发环境安装 `requirements-dev.txt` 后，可运行 `python -m unittest discover -s tests -v`。测试使用真实 SDK 的配置模型、装饰器和能力代理，仅模拟 Host RPC 结果，不连接 QQ 或 LLM。

使用官方标签源码验证宿主协议：

```console
python tools/check_compat.py --host-root /path/to/MaiBot-v1.4.7 --sdk-root /path/to/maibot-plugin-sdk-v2.23.0 --run-tests
```

浏览器回归见 `tests/test_webui_browser.cjs`。它使用宿主原始页面 SDK/CSP 和实际插件后端；需要 Node.js 的 Playwright 模块、Chromium/Edge，以及 `MAIBOT_HOST_ROOT`、`SDK_ROOT`、`TEST_PYTHON` 环境变量。可通过 `PLAYWRIGHT_MODULE` 和 `BROWSER_EXECUTABLE` 指定已有环境。
