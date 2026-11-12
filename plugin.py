"""个人备忘助手插件（MaiBot v2 SDK 插件）

功能概述：
- 用户艾特 bot 或发送 ``/备忘 <要求>`` 指令，创建一条定时备忘任务。
- 艾特 bot 时先用文字 LLM（回复模型 replyer）识别是否为备忘任务；
  确认是任务后，用规划模型（planner）分析「时间 + 内容」；
  使用 ``/备忘`` 指令则跳过意图识别，直接进入任务创建。
- 用户可能分几条消息描述任务：艾特 bot 后进入「收集窗口」（默认 60 秒，可配置），
  期间同一用户的后续文字/图片会被并入任务描述；支持引用消息作为上下文。
- 时间上限（默认 24 小时，可配置）：超过则返回可配置的拒绝话术。
- 到期后麦麦用 LLM + tools 生成「真人化」提醒，并按用户偏好发送：
  auto（默认）：有私聊会话则私聊，否则群聊 @；private：仅私聊，无好友则提示并转群聊 @；group：群聊 @。
- 提醒可携带图片/文件附件（用户发任务时带上的媒体）；完成后自动清理附件与任务记录。
- 任务持久化到插件数据目录 ``tasks.json``；麦麦重启后自动读取并继续运行。
- 超级管理员（``[admin] admin_ids``，同图片收藏插件的 ``command_whitelist``）可通过
  ``/备忘开关 on|off`` 全局开关插件。
"""

from __future__ import annotations

import asyncio
import base64
import json
import math
import os
import random
import re
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar, Literal

from maibot_sdk import API, Command, Field, HookHandler, MaiBotPlugin, PluginConfigBase, Tool
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder, ToolParameterInfo, ToolParamType

# ==================== 常量 ====================

SUPPORTED_CONFIG_VERSION = "1.0.0"

# 图片扩展名探测
_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")

# 图片魔数嗅探
_IMAGE_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"GIF", ".gif"),
)

# 默认拒绝话术（超过时间上限）
_DEFAULT_REJECT_MESSAGE = "这个时间有点远啦，我最多帮你记住{limit}小时以内的事～你换个近一点的时间？"

# 默认收集窗口（秒）
_DEFAULT_CAPTURE_WINDOW = 60.0

# 默认轮询间隔（秒）
_DEFAULT_POLL_INTERVAL = 1.0

# 任务状态
_STATUS_PENDING = "pending"
_STATUS_DONE = "done"
_STATUS_REJECTED = "rejected"
_STATUS_FAILED = "failed"

# 疑似提醒关键词（艾特 bot 时优先触发 LLM 意图识别的粗筛）
_HINT_WORDS = ("提醒", "备忘", "记得", "叫我", "几点", "小时后", "分钟", "明天", "后天", "晚上", "中午", "下午", "上午", "待会", "一会儿", "等下", "之后")

# 指令权限拒绝提示
_CMD_DENIED_MSG = "你的 QQ 不在 [admin] admin_ids 中，无法使用此指令。请先由管理员在插件配置里添加。"


def _ui(label: str, hint: str = "") -> dict[str, Any]:
    extra: dict[str, Any] = {"label": label}
    if hint:
        extra["hint"] = hint
    return extra


# ==================== 配置模型 ====================


class PluginSectionConfig(PluginConfigBase):
    """插件自身配置（[plugin] 配置节）。"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(
        default=True,
        description="是否启用插件（超级管理员可通过 /备忘开关 修改）",
        json_schema_extra=_ui("启用插件", "插件总开关"),
    )
    config_version: str = Field(
        default=SUPPORTED_CONFIG_VERSION,
        description="配置版本（与插件版本同步）",
        json_schema_extra={**_ui("配置版本", "勿改"), "hidden": True, "disabled": True},
    )


class ReminderSectionConfig(PluginConfigBase):
    """提醒设置（[reminder] 配置节）。"""

    __ui_label__ = "提醒"
    __ui_icon__ = "bell"
    __ui_order__ = 1

    default_channel: Literal["auto", "private", "group"] = Field(
        default="auto",
        description="默认提醒渠道：auto=有私聊则私聊否则群聊@；private=仅私聊（无好友提示并转群聊@）；group=仅群聊@",
        json_schema_extra=_ui("默认渠道", "auto/private/group"),
    )
    max_delay_hours: float = Field(
        default=24.0,
        ge=0.1,
        le=720.0,
        description="任务时间上限（小时），超过则拒绝",
        json_schema_extra=_ui("时间上限(小时)", "最远可提醒多久，默认 24 小时"),
    )
    reject_message: str = Field(
        default=_DEFAULT_REJECT_MESSAGE,
        description="超过时间上限时的拒绝话术（支持 {limit} 占位）",
        json_schema_extra=_ui("超限拒绝话术", "含 {limit} 占位符"),
    )
    capture_window_sec: float = Field(
        default=_DEFAULT_CAPTURE_WINDOW,
        ge=5.0,
        le=600.0,
        description="艾特 bot 后收集用户补充描述的时间窗口（秒）",
        json_schema_extra=_ui("收集窗口(秒)", "用户分几条消息描述任务时的等待时间"),
    )
    poll_interval_sec: float = Field(
        default=_DEFAULT_POLL_INTERVAL,
        ge=0.2,
        le=30.0,
        description="调度循环轮询间隔（秒）",
        json_schema_extra=_ui("轮询间隔(秒)", "勿设过小"),
    )
    cleanup_interval_sec: float = Field(
        default=7200.0,
        ge=0.0,
        le=604800.0,
        description="定时清理过期备忘任务的间隔（秒），0 表示不自动清理",
        json_schema_extra=_ui("清理间隔(秒)", "默认 7200（2小时）；0=关闭自动清理"),
    )
    max_tasks_per_user: int = Field(
        default=20,
        ge=1,
        le=200,
        description="单个用户最多可同时存在的待办任务数",
        json_schema_extra=_ui("每人任务上限", "防止刷屏"),
    )
    ack_template: str = Field(
        default="好，我知道了～到点（{time}）提醒你。",
        description="任务创建成功后的确认文案（支持 {task} {time} 占位；默认不含 {task} 以免复述任务）",
        json_schema_extra=_ui("创建确认文案", "占位符 {task} {time}"),
    )


class PromptSectionConfig(PluginConfigBase):
    """提示词配置（[prompts] 配置节）。"""

    __ui_label__ = "提示词"
    __ui_icon__ = "sparkles"
    __ui_order__ = 2

    intent_parse_prompt: str = Field(
        default=(
            "用户艾特了机器人，可能是要发布一个备忘/提醒任务，也可能只是聊天。\n"
            "请判断这是否是一条备忘任务（是否在请求机器人记下某事并在将来提醒）。\n"
            "同时判断用户是否明确指定了提醒渠道：未指定=0，群聊=1，私聊=2。\n"
            "只返回一个 JSON 对象，不要输出其他文字：\n"
            '{"is_task": true/false, "channel": 0/1/2}'
        ),
        description=(
            "艾特 bot 时的任务意图识别提示词（回复模型）。channel：0=未指定，1=群聊，2=私聊。"
            "channel=2（私聊）时若用户没有已有的私聊流会失败，插件仍会走群聊@提醒。"
        ),
        json_schema_extra=_ui("意图识别提示词", "回复模型判断是否备忘任务及渠道；channel 2 私聊无流则回退群聊"),
    )
    reminder_style: str = Field(
        default=(
            "你是在提醒用户他的备忘任务。请严格依据下方给出的【人格设定】【行为风格】【表达风格】"
            "来组织提醒的语气和口吻，像真人朋友一样自然，不要像系统通知，"
            "不要编造任务内容之外的信息。"
        ),
        description="到期提醒的真人化风格提示词",
        json_schema_extra=_ui("提醒风格提示词", "叠加在人设三件套之上；留空则仅依据人设"),
    )


class StorageSectionConfig(PluginConfigBase):
    """存储设置（[storage] 配置节）。"""

    __ui_label__ = "存储"
    __ui_icon__ = "database"
    __ui_order__ = 3

    max_media_mb: float = Field(
        default=20.0,
        ge=0.0,
        le=1024.0,
        description="单个图片/文件附件大小上限（MB），0 表示不限制",
        json_schema_extra=_ui("附件大小上限(MB)", "超过的图片/文件不会被保存为任务附件"),
    )


class AdminSectionConfig(PluginConfigBase):
    """管理员设置（[admin] 配置节）。"""

    __ui_label__ = "管理员"
    __ui_icon__ = "shield"
    __ui_order__ = 4

    admin_ids: list[str] = Field(
        default_factory=list,
        description="超级管理员 QQ 号列表（可用 /备忘开关 全局开关插件）",
        json_schema_extra=_ui("管理员QQ", "只有这些 QQ 能全局开关插件；空=无人能用"),
    )


class MemoConfig(PluginConfigBase):
    """插件配置根模型。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    reminder: ReminderSectionConfig = Field(default_factory=ReminderSectionConfig)
    prompts: PromptSectionConfig = Field(default_factory=PromptSectionConfig)
    storage: StorageSectionConfig = Field(default_factory=StorageSectionConfig)
    admin: AdminSectionConfig = Field(default_factory=AdminSectionConfig)


# ==================== 工具函数 ====================


def extract_plain_id(value: Any) -> str:
    """归一化 QQ 号（兼容 'qq:10001' 前缀）。"""
    text = str(value or "").strip().lower()
    if ":" in text:
        text = text.split(":", 1)[1]
    return text.strip()


def parse_size_bytes(value: Any) -> int:
    """把文件大小解析成字节数（兼容 int / "1024" / "1.5MB" / "300KB" / "2GB"）。

    无法解析时返回 0（表示未知，调用方跳过上限检查）。
    """
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        return max(0, int(value))
    text = str(value).strip()
    if not text:
        return 0
    m = re.match(r"^([0-9.]+)\s*([KMGTP]?)(?:B|iB)?$", text, re.IGNORECASE)
    if not m:
        try:
            return max(0, int(text))
        except ValueError:
            return 0
    num = float(m.group(1))
    unit = m.group(2).upper()
    factor = {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4, "P": 1024**5}.get(unit, 1)
    return int(num * factor)


def sniff_image_ext(data: bytes) -> str:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:3] == b"GIF":
        return ".gif"
    if data[:4] == b"RIFF" and len(data) >= 12 and data[8:12] == b"WEBP":
        return ".webp"
    if data[:2] == b"BM":
        return ".bmp"
    return ".jpg"


def seg_type(seg: Any) -> str:
    return str((seg or {}).get("type") or "").strip().lower()


def seg_data(seg: Any) -> str:
    return str((seg or {}).get("data") or "").strip()


def seg_field(seg: Any, *names: str, default: str = "") -> Any:
    """从消息段取字段，兼容主程序 at/reply/file 段的 ``data`` 为字典的格式。

    优先从 ``seg["data"]`` 字典内取，再回退到段顶层（兼容旧/测试格式）。
    """
    seg = seg or {}
    data = seg.get("data")
    if isinstance(data, dict):
        for n in names:
            v = data.get(n)
            if v not in (None, ""):
                return v
    for n in names:
        v = seg.get(n)
        if v not in (None, ""):
            return v
    return default


def seg_target_id(seg: Any) -> str:
    seg = seg or {}
    t = seg_type(seg)
    if t == "at":
        return str(seg_field(seg, "target_user_id", "data")).strip()
    if t == "reply":
        return str(seg_field(seg, "target_message_id", "data")).strip()
    return str(seg_field(seg, "target_user_id", "target_message_id", "data")).strip()


def message_text(message: Any) -> str:
    """从消息里提取纯文本（text 段 + reply 引用内容标记）。"""
    parts: list[str] = []
    for seg in (message or {}).get("raw_message") or []:
        t = seg_type(seg)
        if t == "text":
            parts.append(seg_data(seg))
        elif t == "reply":
            content = str(seg_field(seg, "target_message_content") or "").strip()
            if content:
                parts.append(f"[引用] {content}")
    return "".join(parts).strip()


def message_reply_id(message: Any) -> str:
    for seg in (message or {}).get("raw_message") or []:
        if seg_type(seg) == "reply":
            return str(seg_field(seg, "target_message_id", "data")).strip()
    return ""


def message_at_ids(message: Any) -> list[str]:
    ids: list[str] = []
    for seg in (message or {}).get("raw_message") or []:
        if seg_type(seg) == "at":
            target = seg_target_id(seg)
            if target:
                ids.append(target)
    return ids


def message_media_segments(message: Any) -> list[Any]:
    out: list[Any] = []
    for seg in (message or {}).get("raw_message") or []:
        if seg_type(seg) in ("image", "file"):
            out.append(seg)
    return out


def _b64_decode_any(seg: Any) -> str:
    return str(
        (seg or {}).get("binary_data_base64")
        or (seg or {}).get("image_base64")
        or (seg or {}).get("base64")
        or (seg or {}).get("data")
        or ""
    ).strip()


# ==================== 插件主体 ====================


class PersonalAssistantPlugin(MaiBotPlugin):
    """个人备忘助手。"""

    config_model = MemoConfig
    config_reload_subscriptions: ClassVar[tuple[str, ...]] = ("bot",)

    def __init__(self) -> None:
        super().__init__()
        self._plugin_dir = os.path.dirname(os.path.abspath(__file__))
        self._tasks: list[dict[str, Any]] = []
        self._tasks_lock = asyncio.Lock()
        self._scheduler_task: asyncio.Task | None = None
        # 进行中的任务收集：user_id -> capture dict
        self._captures: dict[str, dict[str, Any]] = {}
        # 收集窗口定时器：user_id -> asyncio.Task
        self._capture_timers: dict[str, asyncio.Task] = {}
        self._bot_ids_cache: set[str] | None = None
        self._personality_cache: dict[str, str] | None = None
        self._reject_logged = False

    # ==================== 生命周期 ====================

    async def on_load(self) -> None:
        data_dir = self._data_dir()
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / "media").mkdir(parents=True, exist_ok=True)
        await self._load_tasks()
        self._bot_ids_cache = None
        self._personality_cache = None
        if self.config.plugin.enabled:
            self._start_scheduler()
        self.ctx.logger.info(
            "个人备忘助手已加载：待办 %d 条，时间上限 %.1f 小时，收集窗口 %.0f 秒",
            len([t for t in self._tasks if t.get("status") == _STATUS_PENDING]),
            self.config.reminder.max_delay_hours,
            self.config.reminder.capture_window_sec,
        )

    async def on_unload(self) -> None:
        self._stop_scheduler()
        for t in self._capture_timers.values():
            t.cancel()
        self._capture_timers.clear()
        await self._save_tasks()
        self.ctx.logger.info("个人备忘助手已卸载")

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        del config_data, version
        if scope == "self":
            self.ctx.logger.info("备忘助手配置已更新")
            # 全局开关变化：启用则启动调度，禁用则暂停
            if self.config.plugin.enabled and self._scheduler_task is None:
                self._start_scheduler()
            elif not self.config.plugin.enabled:
                self._stop_scheduler()
        elif scope == "bot":
            self._personality_cache = None
            self._bot_ids_cache = None

    # ==================== 路径与持久化 ====================

    def _data_dir(self) -> Path:
        return Path(self.ctx.paths.data_dir)

    def _tasks_path(self) -> Path:
        return self._data_dir() / "tasks.json"

    def _media_dir(self, task_id: str) -> Path:
        return self._data_dir() / "media" / task_id

    async def _load_tasks(self) -> None:
        path = self._tasks_path()
        if not path.exists():
            self._tasks = []
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                raw = data.get("tasks", [])
            elif isinstance(data, list):
                raw = data
            else:
                raw = []
            self._tasks = [t for t in raw if isinstance(t, dict)]
            for task in self._tasks:
                if task.get("status") == "sending":
                    task["status"] = _STATUS_PENDING
        except Exception as exc:
            self.ctx.logger.warning("读取 tasks.json 失败：%s", exc)
            self._tasks = []

    async def _save_tasks(self) -> None:
        path = self._tasks_path()
        snapshot = list(self._tasks)

        def _write() -> None:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    json.dumps({"version": 1, "tasks": snapshot}, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            except OSError as exc:
                self.ctx.logger.warning("写入 tasks.json 失败：%s", exc)

        await asyncio.to_thread(_write)

    @API("memo_web_list", version="1")
    async def web_list_tasks(self, query: str = "", status: str = "pending", repeat: str = "all") -> dict[str, Any]:
        """Return a bounded, searchable snapshot for the authenticated plugin page."""
        allowed = {"pending", "done", "rejected", "failed"}
        needle = str(query or "").strip().casefold()[:120]
        selected_status = str(status or "pending").strip().lower()
        if selected_status not in allowed and selected_status != "all":
            selected_status = "pending"
        selected_repeat = str(repeat or "all").strip().lower()
        if selected_repeat not in {"all", "daily", "once"}:
            selected_repeat = "all"
        async with self._tasks_lock:
            tasks = [dict(item) for item in self._tasks if isinstance(item, dict)]
        tasks.sort(key=lambda item: float(item.get("remind_at") or 0))
        rows = []
        for item in tasks:
            state = str(item.get("status") or "pending")
            if selected_status != "all" and state != selected_status and not (selected_status == "pending" and state == "sending"):
                continue
            is_daily = bool(item.get("repeat_daily"))
            if selected_repeat == "daily" and not is_daily or selected_repeat == "once" and is_daily:
                continue
            if needle and needle not in (str(item.get("task_text") or "") + " " + str(item.get("user_id") or "")).casefold():
                continue
            rows.append({
                "id": str(item.get("id") or ""),
                "task": str(item.get("task_text") or "")[:500],
                "user_id": str(item.get("user_id") or "")[:80],
                "group_id": str(item.get("group_id") or "")[:80],
                "created_at": float(item.get("created_at") or 0),
                "remind_at": float(item.get("remind_at") or 0),
                "status": state,
                "channel": str(item.get("channel_pref") or "auto"),
                "repeat_daily": is_daily,
                "media_count": len(item.get("media") or []),
            })
        return {"tasks": rows[:500], "total": len(rows), "truncated": len(rows) > 500}

    @staticmethod
    def _web_task_values(payload: str, *, current: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            values = json.loads(payload or "{}")
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("任务内容格式无效") from exc
        if not isinstance(values, dict):
            raise ValueError("任务内容必须是对象")
        text = str(values.get("task_text", current.get("task_text", "") if current else "")).strip()
        user_id = str(values.get("user_id", current.get("user_id", "") if current else "")).strip()
        group_id = str(values.get("group_id", current.get("group_id", "") if current else "")).strip()
        channel = str(values.get("channel_pref", current.get("channel_pref", "auto") if current else "auto")).strip()
        repeat_daily = values.get("repeat_daily", current.get("repeat_daily", False) if current else False)
        if not text or len(text) > 500:
            raise ValueError("提醒内容不能为空，且不能超过 500 个字符")
        if not user_id or len(user_id) > 80:
            raise ValueError("请填写有效的用户 QQ 号")
        if len(group_id) > 80:
            raise ValueError("群号不能超过 80 个字符")
        if channel not in {"auto", "private", "group"}:
            raise ValueError("提醒渠道无效")
        if not isinstance(repeat_daily, bool):
            raise ValueError("每日重复选项无效")
        try:
            remind_at = float(values.get("remind_at", current.get("remind_at") if current else 0))
        except (TypeError, ValueError) as exc:
            raise ValueError("提醒时间无效") from exc
        if not math.isfinite(remind_at) or remind_at <= time.time():
            raise ValueError("提醒时间必须晚于当前时间")
        return {"task_text": text, "user_id": user_id, "group_id": group_id,
                "channel_pref": channel, "repeat_daily": repeat_daily, "remind_at": remind_at}

    @API("memo_web_create", version="1")
    async def web_create_task(self, payload: str) -> dict[str, Any]:
        values = self._web_task_values(payload)
        max_hours = max(0.1, float(self.config.reminder.max_delay_hours))
        if values["remind_at"] > time.time() + max_hours * 3600:
            raise ValueError(f"提醒时间不能晚于 {max_hours:g} 小时")
        async with self._tasks_lock:
            mine = [item for item in self._tasks if item.get("user_id") == values["user_id"] and item.get("status") in {_STATUS_PENDING, "sending"}]
            if len(mine) >= int(self.config.reminder.max_tasks_per_user):
                raise ValueError("该用户的待办任务已达到数量上限")
            task = {"id": uuid.uuid4().hex, **values, "source_stream": "", "source_platform": "qq",
                    "created_at": time.time(), "status": _STATUS_PENDING, "media": [], "reply_to": ""}
            self._tasks.append(task)
            await self._save_tasks()
        return {"success": True, "task_id": task["id"]}

    @API("memo_web_update", version="1")
    async def web_update_task(self, task_id: str, payload: str) -> dict[str, Any]:
        async with self._tasks_lock:
            task = next((item for item in self._tasks if str(item.get("id") or "") == task_id), None)
            if task is None or task.get("status") != _STATUS_PENDING:
                raise ValueError("任务不存在或已进入发送流程")
            values = self._web_task_values(payload, current=task)
            task.update(values)
            await self._save_tasks()
        return {"success": True}

    @API("memo_web_delete", version="1")
    async def web_delete_task(self, task_id: str) -> dict[str, Any]:
        async with self._tasks_lock:
            index = next((i for i, item in enumerate(self._tasks) if str(item.get("id") or "") == task_id), None)
            if index is None:
                raise ValueError("任务不存在")
            task = self._tasks[index]
            if task.get("status") == "sending":
                raise ValueError("任务正在发送，无法删除")
            self._delete_task_media(task)
            self._tasks.pop(index)
            await self._save_tasks()
        return {"success": True}

    @API("memo_web_cancel", version="1")
    async def web_cancel_task(self, task_id: str) -> dict[str, Any]:
        task_id = str(task_id or "").strip()
        if not task_id or len(task_id) > 80:
            raise ValueError("无效的任务 ID")
        removed = False
        async with self._tasks_lock:
            for index, task in enumerate(self._tasks):
                if str(task.get("id") or "") == task_id and task.get("status") == _STATUS_PENDING:
                    self._delete_task_media(task)
                    self._tasks.pop(index)
                    removed = True
                    break
            if removed:
                await self._save_tasks()
        if not removed:
            raise ValueError("任务不存在或已处理")
        return {"success": True}

    # ==================== 全局配置读取 ====================

    async def _get_bot_ids(self) -> set[str]:
        if self._bot_ids_cache is None:
            ids: set[str] = set()
            try:
                qq = await self.ctx.config.get("bot.qq_account", "")
                if qq:
                    ids.add(extract_plain_id(qq))
            except Exception as exc:
                self.ctx.logger.debug("读取 bot.qq_account 失败：%s", exc)
            try:
                platforms = await self.ctx.config.get("bot.platforms", [])
                for item in platforms or []:
                    if isinstance(item, str) and ":" in item:
                        ids.add(extract_plain_id(item.split(":", 1)[1]))
            except Exception as exc:
                self.ctx.logger.debug("读取 bot.platforms 失败：%s", exc)
            self._bot_ids_cache = ids
        return self._bot_ids_cache

    async def _get_persona_styles(self) -> dict[str, str]:
        """读取 bot 人设三件套：人格设定、行为风格、表达风格（bot_config.toml [personality]）。"""
        if self._personality_cache is None:
            styles: dict[str, str] = {}
            for key, field in (
                ("personality.personality", "personality"),
                ("personality.behavior_style", "behavior_style"),
                ("personality.reply_style", "reply_style"),
            ):
                try:
                    value = await self.ctx.config.get(key, "")
                    styles[field] = str(value or "").strip()
                except Exception as exc:
                    self.ctx.logger.warning("读取人设 %s 失败：%s", key, exc)
                    styles[field] = ""
            self._personality_cache = styles
        return self._personality_cache

    def _is_admin(self, user_id: str) -> bool:
        uid = extract_plain_id(user_id)
        admins = [extract_plain_id(q) for q in (self.config.admin.admin_ids or [])]
        return bool(uid and uid in admins)

    # ==================== 调度 ====================

    def _start_scheduler(self) -> None:
        if self._scheduler_task is not None:
            return
        self._scheduler_task = asyncio.create_task(self._scheduler_loop(), name="memo_scheduler")
        self.ctx.logger.info("备忘调度循环已启动")

    def _stop_scheduler(self) -> None:
        if self._scheduler_task is not None:
            self._scheduler_task.cancel()
            self._scheduler_task = None
            self.ctx.logger.info("备忘调度循环已停止")

    async def _scheduler_loop(self) -> None:
        interval = max(0.2, float(self.config.reminder.poll_interval_sec))
        cleanup_interval = max(0.0, float(self.config.reminder.cleanup_interval_sec or 0))
        last_cleanup = 0.0
        while True:
            try:
                await self._tick()
                # 定时清理过期任务（cleanup_interval_sec=0 关闭）
                if cleanup_interval > 0 and (time.time() - last_cleanup) >= cleanup_interval:
                    last_cleanup = time.time()
                    await self._cleanup_expired_tasks()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self.ctx.logger.warning("备忘调度 tick 异常：%s", exc)
            await asyncio.sleep(interval)

    async def _cleanup_expired_tasks(self) -> int:
        """清理所有已到期的任务（含已提醒/失败/拒绝），释放媒体文件。返回清理数。"""
        now = time.time()
        async with self._tasks_lock:
            expired = [t for t in self._tasks if not t.get("repeat_daily") and float(t.get("remind_at") or 0) <= now]
            if not expired:
                return 0
            victim_ids = {id(t) for t in expired}
            for t in expired:
                self._delete_task_media(t)
            self._tasks = [t for t in self._tasks if id(t) not in victim_ids]
        await self._save_tasks()
        self.ctx.logger.info("备忘：定时清理过期任务 %d 条", len(expired))
        return len(expired)

    async def _tick(self) -> None:
        if not self.config.plugin.enabled:
            return
        now = time.time()
        async with self._tasks_lock:
            due = [t for t in self._tasks if t.get("status") == _STATUS_PENDING and float(t.get("remind_at") or 0) <= now]
        for task in due:
            await self._fire_reminder(task)

    # ==================== 消息解析 ====================

    def _extract_user(self, message: Any) -> str:
        info = (message or {}).get("message_info") or {}
        user_info = info.get("user_info") or {}
        uid = str(user_info.get("user_id") or "").strip()
        if uid:
            return uid
        sender = (message or {}).get("sender") or {}
        uid = str(sender.get("user_id") or "").strip()
        if uid:
            return uid
        return str((message or {}).get("user_id") or "").strip()

    def _extract_group(self, message: Any) -> str:
        info = (message or {}).get("message_info") or {}
        group_info = info.get("group_info") or {}
        return str(group_info.get("group_id") or "").strip()

    def _extract_stream(self, message: Any) -> str:
        return str((message or {}).get("stream_id") or (message or {}).get("session_id") or "").strip()

    def _extract_platform(self, message: Any) -> str:
        return str((message or {}).get("platform") or "qq").strip() or "qq"

    async def _is_bot_message(self, message: Any) -> bool:
        sender = extract_plain_id(self._extract_user(message))
        if not sender:
            return False
        bot_ids = await self._get_bot_ids()
        return sender in bot_ids

    # ==================== LLM ====================

    async def _llm_generate(self, prompt: str, task_name: str = "replyer") -> dict[str, Any]:
        try:
            result = await self.ctx.llm.generate(prompt, task_name=task_name)
        except Exception as exc:
            self.ctx.logger.warning("备忘 LLM 调用异常：%s", exc)
            return {"success": False, "response": ""}
        if not result.get("success"):
            self.ctx.logger.warning("备忘 LLM 调用失败：%s", result.get("reasoning") or result.get("response") or "unknown")
        return result

    async def _parse_intent(self, text: str, now: datetime) -> tuple[bool, str, float | None, str]:
        """艾特 bot 时识别是否备忘任务。

        识别「是否为任务」用回复模型（replyer），并同时让模型返回渠道偏好
        （channel：0=未指定，1=群聊，2=私聊）；确认是任务后，
        「分析任务（提取时间/内容）」用规划模型（planner）。
        返回 (is_task, task_text, remind_ts|None, channel_pref)。
        """
        is_task, channel_pref = await self._detect_is_task(text, now)
        if not is_task:
            return False, "", None, ""
        task_text, remind_ts = await self._extract_reminder_from_text(text, now)
        return True, task_text, remind_ts, channel_pref

    async def _detect_is_task(self, text: str, now: datetime) -> tuple[bool, str]:
        """判断用户消息是否为备忘任务（回复模型 replyer）。返回 (is_task, channel_pref)。

        渠道偏好优先取模型返回的 channel（0=auto，1=group，2=private）；
        模型未给出（0/缺失）时用关键词规则 ``_detect_channel_pref`` 兜底。
        """
        prompt = self.config.prompts.intent_parse_prompt or ""
        if not prompt:
            prompt = '请判断以下消息是否为备忘任务，只返回 JSON：{"is_task": true/false, "channel": 0/1/2}'
        full = f"{prompt}\n\n当前时间：{now:%Y-%m-%d %H:%M:%S}\n用户消息：{text}\n"
        result = await self._llm_generate(full, task_name="replyer")
        data = self._parse_llm_json(str(result.get("response") or ""))
        if isinstance(data, dict):
            is_task = bool(data.get("is_task"))
            ch = data.get("channel")
            ch_map = {"0": "auto", "1": "group", "2": "private"}
            ch_pref = ch_map.get(str(ch).strip(), "") if ch is not None else ""
            if ch_pref in ("", "auto"):
                ch_pref = self._detect_channel_pref(text)
            return is_task, ch_pref
        # 兜底：含提醒关键词则视为任务
        return any(w in text for w in _HINT_WORDS), self._detect_channel_pref(text)

    async def _extract_reminder_from_text(self, text: str, now: datetime) -> tuple[str, float | None]:
        """从任务文本提取任务内容与提醒时间（规划模型 planner）。返回 (task_text, remind_ts|None)。"""
        prompt = (
            "从以下备忘要求中提取任务内容与提醒时间。\n"
            "只返回 JSON：{\"remind_at\": \"YYYY-MM-DD HH:MM:SS\" 或 null, \"task_text\": \"任务内容\"}\n"
            f"当前时间：{now:%Y-%m-%d %H:%M:%S}（{now:%A}）\n"
            "注意：remind_at 必须是具体日期时间字符串（如 \"2026-11-06 18:30:00\"）；"
            "相对时间（如\"一分钟后\"\"半小时后\"\"明天下午3点\"\"晚上8点\"）要换算成绝对时间；"
            "如果实在无法确定时间，remind_at 才可为 null。\n"
            f"要求：{text}\n"
        )
        result = await self._llm_generate(prompt, task_name="planner")
        raw = str(result.get("response") or "").strip()
        data = self._parse_llm_json(raw)
        if not isinstance(data, dict):
            return text, None
        task_text = str(data.get("task_text") or "").strip() or text
        remind_ts = self._parse_remind_at(data.get("remind_at"), now)
        remind_str = datetime.fromtimestamp(remind_ts).strftime("%Y-%m-%d %H:%M:%S") if remind_ts else "无"
        self.ctx.logger.info("备忘：规划模型提取 时间=%s 内容=%s", remind_str, task_text[:30])
        return task_text, remind_ts

    @staticmethod
    def _parse_llm_json(raw: str) -> Any:
        text = (raw or "").strip()
        fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
        if fence:
            text = fence.group(1).strip()
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                return None
        return None

    @staticmethod
    def _parse_cn_number(text: str) -> float | None:
        """中文/阿拉伯数字转数值：'一'->1、'两'->2、'半'->0.5、'十'->10、'十五'->15。"""
        t = (text or "").strip()
        if not t:
            return None
        try:
            return float(t)
        except (ValueError, TypeError):
            pass
        digits = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
        if t == "十":
            return 10.0
        if t == "半":
            return 0.5
        if len(t) == 1:
            return float(digits[t]) if t in digits else None
        if len(t) == 2 and t[0] == "十":
            return 10.0 + digits.get(t[1], 0)
        if len(t) == 2 and t[1] == "十":
            return float(digits.get(t[0], 0)) * 10
        if len(t) == 3 and t[1] == "十":
            return float(digits.get(t[0], 0)) * 10 + digits.get(t[2], 0)
        return None

    @staticmethod
    def _parse_remind_at(value: Any, now: datetime) -> float | None:
        """把 LLM 返回的时间解析成时间戳：支持绝对格式、数字时间戳、相对时间（一分钟后等）。"""
        if value is None:
            return None
        raw = str(value or "").strip()
        if not raw:
            return None
        now_ts = now.timestamp()

        # 尝试多种绝对时间格式
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y-%m-%dT%H:%M:%S"):
            try:
                return datetime.strptime(raw, fmt).timestamp()
            except ValueError:
                continue

        # 纯数字：秒级/毫秒级时间戳
        try:
            ts = float(raw)
            if ts > 1e9:
                return ts
            if ts > 1e6:
                return ts
        except (ValueError, TypeError):
            pass

        # 相对时间：N小时[+N分]/N分钟/N天后（小时分支在前，避免"1小时30分后"被分钟分支误判）
        def _num(s: str) -> float | None:
            return PersonalAssistantPlugin._parse_cn_number(s)

        m = re.search(r"([0-9]+|[一二两三四五六七八九十半]+)\s*小时(?:\s*([0-9]+|[一二两三四五六七八九十]+)\s*分钟?)?\s*后", raw)
        if m and (h := _num(m.group(1))) is not None:
            extra = 0.0
            if m.group(2):
                extra = float(_num(m.group(2)) or 0) * 60
            return now_ts + h * 3600 + extra
        m = re.search(r"([0-9]+|[一二两三四五六七八九十半]+)\s*(?:分钟|分)\s*后", raw)
        if m and (n := _num(m.group(1))) is not None:
            return now_ts + n * 60
        m = re.search(r"([0-9]+|[一二两三四五六七八九十]+)\s*天\s*后", raw)
        if m and (d := _num(m.group(1))) is not None:
            return now_ts + d * 86400

        # 明天/今天/今晚 + H点[M分] / H:M
        day_off = 0
        lowered = raw
        if re.search(r"明天|明早|明日", raw):
            day_off = 1
        elif re.search(r"后天", raw):
            day_off = 2
        elif re.search(r"今天|今晚|现在", raw):
            day_off = 0
        elif re.search(r"明晚", raw):
            day_off = 1
        # 时段词：晚上/下午/中午/早上/上午（12小时制调整由用户文本+LLM负责，这里仅定位时刻）
        hm = re.search(r"([0-9]{1,2})\s*[点时:：]\s*([0-9]{1,2})?\s*分?", raw)
        if hm:
            hour = int(hm.group(1))
            minute = int(hm.group(2) or 0)
            # 若带"下午/晚上/今晚/傍晚"且 hour<12，视为 24 小时制
            if re.search(r"下午|晚上|今晚|傍晚|夜里|晚", raw) and hour < 12:
                hour += 12
            base = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            target = base + timedelta(days=day_off)
            return target.timestamp()
        # 仅"明天"无时刻：取明天同一时刻
        if day_off:
            return (now + timedelta(days=day_off)).timestamp()

        return None

    # ==================== 任务创建 ====================

    async def _create_task(
        self,
        *,
        user_id: str,
        group_id: str,
        source_stream: str,
        platform: str,
        task_text: str,
        remind_ts: float | None,
        channel_pref: str,
        media: list[dict[str, Any]],
        reply_to: str,
        now: datetime,
    ) -> tuple[dict[str, Any] | None, str]:
        """创建任务。返回 (task|None, 拒绝原因|空)。"""
        if not task_text:
            return None, "没有识别到任务内容"
        if not remind_ts:
            return None, "没有识别到提醒时间（试试“一小时后”“明天下午3点”这样的说法）"
        now_ts = now.timestamp()
        max_hours = max(0.1, float(self.config.reminder.max_delay_hours))
        max_ts = now_ts + max_hours * 3600
        if remind_ts <= now_ts - 5:
            return None, "这个时间已经过去啦"
        if remind_ts > max_ts:
            reject = self.config.reminder.reject_message or _DEFAULT_REJECT_MESSAGE
            return None, reject.format(limit=f"{max_hours:g}")
        # 每人任务上限
        async with self._tasks_lock:
            mine = [t for t in self._tasks if t.get("user_id") == user_id and t.get("status") == _STATUS_PENDING]
            if len(mine) >= int(self.config.reminder.max_tasks_per_user):
                return None, "你的待办任务太多啦，先清掉一些再来"
            task = {
                "id": uuid.uuid4().hex,
                "user_id": user_id,
                "group_id": group_id,
                "source_stream": source_stream,
                "source_platform": platform,
                "task_text": task_text[:500],
                "remind_at": remind_ts,
                    "channel_pref": channel_pref,
                    "repeat_daily": False,
                "created_at": now_ts,
                "status": _STATUS_PENDING,
                "media": [],
                "reply_to": reply_to,
            }
            # 立即落盘媒体（图片存盘、文件记录引用），失败项会被过滤
            task["media"] = await self._save_media_for_task(task["id"], media)
            self._tasks.append(task)
        await self._save_tasks()
        self.ctx.logger.info(
            "备忘：任务已创建 id=%s user=%s 时间=%s channel=%s 媒体=%d 文本=%s",
            task["id"][:8], user_id,
            datetime.fromtimestamp(remind_ts).strftime("%Y-%m-%d %H:%M:%S"),
            channel_pref, len(task.get("media") or []), task_text[:30],
        )
        return task, ""

    def _validate_channel_pref(self, pref: str) -> str:
        if pref in ("private", "group"):
            return pref
        return "auto"

    @staticmethod
    def _detect_channel_pref(text: str) -> str:
        """从任务文本识别用户偏好的提醒渠道（规则匹配，不耗 LLM）。"""
        t = (text or "").lower()
        if any(k in t for k in ("私聊提醒", "私聊发", "单独发", "私下发", "单独提醒", "私聊我", "单独告诉我", "私聊通知")):
            return "private"
        if any(k in t for k in ("群里提醒", "群里@", "群聊提醒", "在群里", "群里发", "群里at", "群里@我", "群里面提醒", "在群里提醒")):
            return "group"
        return "auto"

    # ==================== 收集窗口 ====================

    async def _begin_capture(
        self,
        *,
        user_id: str,
        group_id: str,
        source_stream: str,
        platform: str,
        initial_text: str,
        media: list[dict[str, Any]],
        reply_to: str,
        channel_pref: str,
        remind_ts: float | None,
        now: datetime,
    ) -> None:
        """艾特 bot 后开启收集窗口：等待用户补充描述，然后创建任务。"""
        capture = {
            "user_id": user_id,
            "group_id": group_id,
            "source_stream": source_stream,
            "platform": platform,
            "text_parts": [initial_text] if initial_text else [],
            "media": media or [],
            "reply_to": reply_to,
            "channel_pref": channel_pref,
            "remind_ts": remind_ts,
            "start_ts": now.timestamp(),
            "started_text": initial_text,
        }
        self._captures[user_id] = capture
        # 取消旧定时器
        old = self._capture_timers.pop(user_id, None)
        if old and not old.done():
            old.cancel()
        timer = asyncio.create_task(self._finalize_capture(user_id), name=f"memo_capture_{user_id[:8]}")
        self._capture_timers[user_id] = timer
        self.ctx.logger.info("备忘收集开始：user=%s，窗口 %.0f 秒", user_id, self.config.reminder.capture_window_sec)

    def _append_capture_text(self, user_id: str, text: str) -> None:
        cap = self._captures.get(user_id)
        if cap and text:
            cap["text_parts"].append(text)

    def _append_capture_media(self, user_id: str, media: list[dict[str, Any]]) -> None:
        cap = self._captures.get(user_id)
        if cap and media:
            cap["media"].extend(media)

    async def _finalize_capture(self, user_id: str) -> None:
        """收集窗口结束后确定任务。"""
        try:
            await asyncio.sleep(self.config.reminder.capture_window_sec)
        except asyncio.CancelledError:
            return
        cap = self._captures.pop(user_id, None)
        self._capture_timers.pop(user_id, None)
        if cap is None:
            return
        if not self.config.plugin.enabled:
            return
        now = datetime.now()
        full_text = "\n".join(p for p in cap["text_parts"] if p).strip() or cap.get("started_text") or ""
        # 优先用进入收集窗口时已识别的时间，避免结束重新提取 LLM 偶发失败
        remind_ts = cap.get("remind_ts")
        if remind_ts is None:
            task_text, remind_ts = await self._extract_reminder_from_text(full_text, now)
        else:
            task_text = full_text
        task, reject = await self._create_task(
            user_id=user_id,
            group_id=cap.get("group_id", ""),
            source_stream=cap.get("source_stream", ""),
            platform=cap.get("platform", "qq"),
            task_text=task_text,
            remind_ts=remind_ts,
            channel_pref=cap.get("channel_pref", "auto"),
            media=cap.get("media", []),
            reply_to=cap.get("reply_to", ""),
            now=now,
        )
        await self._announce_result(cap.get("source_stream", ""), user_id, task, reject)

    # ==================== 输出语句分割（复刻主程序 response_splitter 规则） ====================

    @staticmethod
    def _is_english_letter(char: str) -> bool:
        """检查字符是否为英文字母（忽略大小写）。"""
        return bool(char) and "a" <= char.lower() <= "z"

    @staticmethod
    def _split_into_sentence_segments(text: str) -> list[tuple[str, str]]:
        """把文本断句成 (句子, 尾随分隔符) 元组，并按概率合并。

        复刻主程序 ``src/chat/utils/utils.py`` 的规则断句器：
        分隔符（,，。; 空格 换行）作为分割点，引号内不拆、冒号旁不拆、
        数字/英文间空格不拆；按文本长度决定合并强度。
        """
        # 预处理换行
        text = re.sub(r"\n\s*\n+", "\n", text)
        text = re.sub(r"\n\s*([，,。;\s])", r"\n\1", text)
        text = re.sub(r"([，,。;\s])\s*\n", r"\1\n", text)

        len_text = len(text)
        if len_text < 3:
            if random.random() < 0.01:
                return [(char, "") for char in text]
            return [(text, "")]

        # 标记引号内部（避免在引号内分割）
        quote_chars = {'"', "'", "“", "”", "‘", "’", "「", "」", "『", "』"}
        inside_quote = [False] * len_text
        in_quote = False
        current_quote_char = ""
        for idx, ch in enumerate(text):
            if ch in quote_chars:
                if not in_quote:
                    in_quote = True
                    current_quote_char = ch
                    inside_quote[idx] = False
                else:
                    if ch == current_quote_char or ch in {'"', "'"} and current_quote_char in {'"', "'"}:
                        in_quote = False
                        current_quote_char = ""
                    inside_quote[idx] = False
            else:
                inside_quote[idx] = in_quote

        separators = {"，", ",", " ", "。", ";", "\n"}
        segments: list[tuple[str, str]] = []
        current_segment = ""
        i = 0
        while i < len(text):
            char = text[i]
            if char in separators:
                if inside_quote[i]:
                    can_split = False
                else:
                    if char == "\n":
                        can_split = True
                    else:
                        can_split = True
                        if i > 0 and text[i - 1] in {":", "："}:
                            can_split = False
                        if i < len(text) - 1 and text[i + 1] in {":", "："}:
                            can_split = False
                        if can_split and char == " " and i > 0 and i < len(text) - 1:
                            prev_char = text[i - 1]
                            next_char = text[i + 1]
                            if prev_char in {"-", "—"} or next_char in {"-", "—"}:
                                can_split = False
                            else:
                                prev_is_alnum = prev_char.isdigit() or PersonalAssistantPlugin._is_english_letter(prev_char)
                                next_is_alnum = next_char.isdigit() or PersonalAssistantPlugin._is_english_letter(next_char)
                                if prev_is_alnum and next_is_alnum:
                                    can_split = False
                if can_split:
                    if current_segment:
                        segments.append((current_segment, char))
                    elif char in {" ", "\n"}:
                        segments.append(("", char))
                    current_segment = ""
                else:
                    current_segment += char
            else:
                current_segment += char
            i += 1

        if current_segment:
            segments.append((current_segment, ""))
        segments = [(content, sep) for content, sep in segments if content or sep]
        if not segments:
            return [(text, "")] if text else []

        # 概率合并（按原始长度决定分割强度）
        if len_text < 12:
            split_strength = 0.2
        elif len_text < 32:
            split_strength = 0.6
        else:
            split_strength = 0.7
        merge_probability = 1.0 - split_strength

        merged: list[tuple[str, str]] = []
        idx = 0
        while idx < len(segments):
            current_content, current_sep = segments[idx]
            if idx + 1 < len(segments) and current_content and current_sep != "\n" and random.random() < merge_probability:
                next_content, next_sep = segments[idx + 1]
                if next_content:
                    merged.append((current_content + current_sep + next_content, next_sep))
                else:
                    merged.append((current_content, next_sep))
                idx += 2
            else:
                merged.append((current_content, current_sep))
                idx += 1

        sentence_segments = [(c, s) for c, s in merged if c and c.strip()]
        sentence_segments = [
            (normalized, sep)
            for sentence, sep in sentence_segments
            if (normalized := re.sub(r"[^\S\r\n]*[\r\n]+[^\S\r\n]*", " ", sentence).strip())
        ]
        return sentence_segments

    @staticmethod
    def _merge_segments_to_max_count(segments: list[tuple[str, str]], max_count: int) -> list[str]:
        """把断句结果压缩到最多 max_count 条消息（补回句间分隔符避免硬拼）。"""
        if len(segments) <= max_count:
            return [content for content, _sep in segments]
        if max_count <= 0:
            return [content for content, _sep in segments]
        group_size = (len(segments) + max_count - 1) // max_count
        merged: list[str] = []
        for start in range(0, len(segments), group_size):
            group = segments[start : start + group_size]
            parts: list[str] = []
            for pos, (content, sep) in enumerate(group):
                parts.append(content)
                if pos < len(group) - 1:
                    parts.append(sep)
            merged.append("".join(parts).strip())
        return [m for m in merged if m]

    async def _split_reply_text(self, text: str) -> list[str]:
        """对要发送的文本执行主程序「输出语句分割」风格的断句。

        读取主配置 ``response_post_process`` / ``response_splitter``，复刻规则断句；
        不执行错别字注入（提醒文本需保持准确）。
        返回按顺序发送的文本列表（至少一条）。
        """
        if not text:
            return [""]
        try:
            enable_post = bool(await self.ctx.config.get("response_post_process.enable_response_post_process", True))
            enable_split = bool(await self.ctx.config.get("response_splitter.enable", True))
        except Exception:
            enable_post, enable_split = True, True
        if not enable_post or not enable_split:
            return [text]

        try:
            max_split_num = max(1, int(await self.ctx.config.get("response_splitter.max_split_num", 3)))
        except Exception:
            max_split_num = 3
        try:
            max_length = max(1, int(await self.ctx.config.get("response_splitter.max_length", 512)))
        except Exception:
            max_length = 512

        # 中文占比高且超长：主程序会返回默认回复；插件不适合，这里保持原文不分
        if len(text) > max_length * 2:
            return [text]

        segments = self._split_into_sentence_segments(text)
        if not segments:
            return [text]
        # 短文本（如简短确认）即使断出多段也不拆，保持一条；长提醒仍按规则拆分
        if len(text) <= 40 and len(segments) > 1:
            return [text]
        return self._merge_segments_to_max_count(segments, max_split_num) or [text]

    async def _send_text_split(self, text: str, stream_id: str) -> None:
        """按主程序「输出语句分割」规则拆条后逐条发送。"""
        if not text or not stream_id:
            return
        for part in await self._split_reply_text(text):
            if part.strip():
                await self.ctx.send.text(part, stream_id)

    async def _announce_result(self, stream_id: str, user_id: str, task: dict[str, Any] | None, reject: str) -> None:
        """向用户反馈任务创建结果（成功后连同附件图一起回发）。"""
        if not stream_id:
            return
        try:
            if task:
                ack = self.config.reminder.ack_template or "好，我知道了～到点（{time}）提醒你。"
                remind_str = datetime.fromtimestamp(float(task["remind_at"])).strftime("%Y-%m-%d %H:%M")
                text = ack.format(task=task.get("task_text", "")[:60], time=remind_str)
            else:
                text = reject or "没记下来，再试试？"
            await self._send_text_split(text, stream_id)
            # 创建成功且带附件时，把图片/文件也回发给用户确认收到
            if task and task.get("media"):
                await self._send_task_media(task, stream_id)
        except Exception as exc:
            self.ctx.logger.warning("备忘确认发送失败：%s", exc)

    # ==================== 媒体保存 ====================

    async def _save_media_for_task(self, task_id: str, media: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """把媒体段保存为任务附件（图片落盘、文件记录引用）。返回可持久化的 media 列表。

        单个附件超过 `storage.max_media_mb`（0=不限）会被跳过，不纳入任务。
        """
        limit_bytes = int(float(self.config.storage.max_media_mb or 0) * 1024 * 1024)
        out: list[dict[str, Any]] = []
        media_dir = self._media_dir(task_id)
        try:
            media_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self.ctx.logger.warning("创建媒体目录失败：%s", exc)
            return out
        for idx, item in enumerate(media or []):
            kind = item.get("kind") or "image"
            if kind == "image":
                b64 = item.get("data") or ""
                if not b64:
                    continue
                try:
                    raw = base64.b64decode(b64)
                except Exception:
                    continue
                if limit_bytes and len(raw) > limit_bytes:
                    self.ctx.logger.info("图片超过大小上限(%dMB)，跳过：%d 字节", limit_bytes // 1024 // 1024, len(raw))
                    continue
                ext = sniff_image_ext(raw)
                name = item.get("name") or f"image_{idx}{ext}"
                path = media_dir / f"{idx}{ext}"
                try:
                    await asyncio.to_thread(path.write_bytes, raw)
                except OSError as exc:
                    self.ctx.logger.warning("保存媒体失败：%s", exc)
                    continue
                out.append({"kind": "image", "path": str(path), "name": name, "mime": item.get("mime") or "image/*"})
            elif kind == "file":
                # 文件/视频：记录引用，提醒时经 send.custom("file") 发送
                file_ref = item.get("file_ref") or ""
                if not file_ref and item.get("url"):
                    file_ref = item.get("url")
                if not file_ref:
                    continue
                # 若消息段带 size，检查大小上限
                size = parse_size_bytes(item.get("size"))
                if limit_bytes and size > limit_bytes:
                    self.ctx.logger.info("文件超过大小上限(%dMB)，跳过：%d 字节", limit_bytes // 1024 // 1024, size)
                    continue
                out.append({
                    "kind": "file",
                    "file_ref": file_ref,
                    "name": item.get("name") or "附件",
                    "mime": item.get("mime") or "application/octet-stream",
                })
        return out

    def _delete_task_media(self, task: dict[str, Any]) -> None:
        media = task.get("media") or []
        media_dir = self._media_dir(task.get("id", ""))
        # 删除落盘的图片
        for item in media:
            if item.get("kind") == "image" and item.get("path"):
                try:
                    Path(item["path"]).unlink(missing_ok=True)
                except OSError:
                    pass
        try:
            if media_dir.exists():
                # 仅当为空时删除目录
                if not any(media_dir.iterdir()):
                    media_dir.rmdir()
        except OSError:
            pass

    # ==================== 主 Hook：艾特 bot 检测 ====================

    @HookHandler(
        "chat.receive.after_process",
        name="memo_at_capture",
        mode=HookMode.BLOCKING,
        order=HookOrder.EARLY,
        error_policy=ErrorPolicy.SKIP,
    )
    async def hook_receive(self, **kwargs: Any) -> dict[str, Any] | None:
        """艾特 bot 时识别备忘任务并独占处理（BLOCKING）。

        返回 ``{"action": "abort"}`` 表示已由本插件接管该消息，阻止 bot 主流程
        （planner / 命令链）再处理，避免「双管齐下」；返回 ``None`` 表示放行，
        让 bot 正常处理（例如用户问“我目前有什么备忘”这类非创建任务消息）。
        """
        if not self.config.plugin.enabled:
            return None
        message = kwargs.get("message") or {}
        if not isinstance(message, dict):
            return None
        if message.get("is_notify"):
            return None
        if await self._is_bot_message(message):
            return None

        user_id = extract_plain_id(self._extract_user(message))
        group_id = self._extract_group(message)
        stream_id = self._extract_stream(message)
        platform = self._extract_platform(message)
        if not user_id or not stream_id:
            return None

        now = datetime.now()

        # 收集窗口内的补充消息：并入已有 capture，并拦截（不让 planner 处理）
        cap = self._captures.get(user_id)
        if cap is not None and now.timestamp() - float(cap.get("start_ts") or 0) < self.config.reminder.capture_window_sec:
            text = message_text(message)
            if text:
                self._append_capture_text(user_id, text)
            media = await self._collect_media_with_reply(message)
            if media:
                self._append_capture_media(user_id, media)
            return {"action": "abort"}

        # 是 /备忘 指令？Command 组件会处理，hook 放行（走命令链，不拦截）
        text = message_text(message)
        if re.match(r"^/?备忘(?:\s|$)", text.strip()):
            return None

        # 艾特/提及检测：优先用主程序统一计算的标志；私聊消息直接尝试识别；
        # 群聊未艾特/提及时兜底解析 at 段是否命中 bot（兼容 data 字典格式）
        mentioned = bool(message.get("is_at") or message.get("is_mentioned"))
        is_private = not group_id
        if not mentioned and not is_private:
            at_ids = [extract_plain_id(x) for x in message_at_ids(message)]
            bot_ids = await self._get_bot_ids()
            if not bot_ids or not any(a in bot_ids for a in at_ids):
                return None

        # 粗筛：无提示词则仍尝试 LLM（用户要求艾特即识别）
        is_task, task_text, remind_ts, llm_channel = await self._parse_intent(text or "[无文本]", now)
        if not is_task:
            self.ctx.logger.debug("艾特消息非备忘任务，忽略：user=%s", user_id)
            return None
        # 渠道偏好：LLM 返回优先，关键词规则兜底
        channel_pref = llm_channel or self._detect_channel_pref(task_text or text)
        self.ctx.logger.info(
            "备忘：识别为任务 user=%s remind_ts=%s channel=%s 文本=%s",
            user_id, remind_ts, channel_pref, (task_text or text)[:40],
        )

        # 收集媒体（艾特消息自带的图片/文件 + 被引用消息里的媒体）
        media = await self._collect_media_with_reply(message)
        # 若已识别出具体时间且无媒体，可直接创建（不用等窗口）；否则进收集窗口等补充
        if remind_ts is not None and not media:
            task, reject = await self._create_task(
                user_id=user_id, group_id=group_id, source_stream=stream_id,
                platform=platform, task_text=task_text, remind_ts=remind_ts,
                channel_pref=channel_pref, media=[], reply_to=message_reply_id(message), now=now,
            )
            await self._announce_result(stream_id, user_id, task, reject)
            # 无论成功/拒绝，插件都已接管，阻止 planner 再处理
            return {"action": "abort"}

        # 进收集窗口（等补充描述/媒体）
        await self._begin_capture(
            user_id=user_id, group_id=group_id, source_stream=stream_id,
            platform=platform, initial_text=task_text or text, media=media,
            reply_to=message_reply_id(message), channel_pref=channel_pref, remind_ts=remind_ts, now=now,
        )
        return {"action": "abort"}

    def _collect_media_segments(self, message: Any) -> list[dict[str, Any]]:
        """收集消息段里的图片/文件媒体。图片无 base64 时按 hash 从宿主图片库读取。"""
        media: list[dict[str, Any]] = []
        for seg in message_media_segments(message):
            t = seg_type(seg)
            if t == "image":
                b64 = _b64_decode_any(seg)
                if not b64:
                    # 历史/被引用图片通常只有 hash：从宿主图片库 data/images/<hash>.<ext> 读
                    hashv = str((seg or {}).get("binary_hash") or (seg or {}).get("hash") or "").strip().lower()
                    if hashv:
                        host_images = self._data_dir().parent.parent / "images"
                        for ext in _IMAGE_EXTS:
                            p = host_images / f"{hashv}{ext}"
                            if p.is_file():
                                try:
                                    raw = p.read_bytes()
                                    b64 = base64.b64encode(raw).decode("ascii")
                                except OSError:
                                    b64 = ""
                                break
                if b64:
                    media.append({"kind": "image", "data": b64, "name": str((seg or {}).get("name") or "")})
            elif t == "file":
                file_ref = str(seg_field(seg, "file_ref") or "").strip()
                url = str(seg_field(seg, "url") or "").strip()
                media.append({
                    "kind": "file",
                    "file_ref": file_ref or url,
                    "url": url,
                    "name": str(seg_field(seg, "name", "file", "file_name", "filename") or "附件"),
                    "mime": str(seg_field(seg, "mime_type", "mimeType") or "").strip(),
                    "size": seg_field(seg, "size", "file_size") or "",
                })
        return media

    async def _collect_media_with_reply(self, message: Any) -> list[dict[str, Any]]:
        """收集媒体：当前消息段 + 被引用消息里的图片/文件（支持「引用一条图片消息再交代任务」）。"""
        media = self._collect_media_segments(message)
        reply_id = message_reply_id(message)
        if reply_id:
            try:
                quoted = await self.ctx.message.get_by_id(reply_id)
                if isinstance(quoted, dict):
                    quoted_media = self._collect_media_segments(quoted)
                    # 去重：按已有图片 data 前缀 / file_ref 去重
                    seen_data = {m.get("data")[:64] for m in media if m.get("kind") == "image" and m.get("data")}
                    seen_ref = {m.get("file_ref") for m in media if m.get("kind") == "file"}
                    for m in quoted_media:
                        if m.get("kind") == "image" and m.get("data") and m["data"][:64] in seen_data:
                            continue
                        if m.get("kind") == "file" and m.get("file_ref") in seen_ref:
                            continue
                        media.append(m)
            except Exception as exc:
                self.ctx.logger.debug("读取引用消息媒体失败：%s", exc)
        return media

    # ==================== 提醒 ====================

    async def _fire_reminder(self, task: dict[str, Any]) -> None:
        if not self.config.plugin.enabled:
            return
        task_id = task.get("id", "")
        async with self._tasks_lock:
            current = next((item for item in self._tasks if item.get("id") == task_id), None)
            if current is None or current.get("status") != _STATUS_PENDING:
                return
            current["status"] = "sending"
            task = current
        self.ctx.logger.info("备忘到期触发：task=%s，user=%s", task_id[:8], task.get("user_id"))

        # 确定目标流
        channel_pref = self._validate_channel_pref(task.get("channel_pref") or "auto")
        user_id = task.get("user_id", "")
        group_id = task.get("group_id", "")
        target_stream, notify_private_unavailable = await self._resolve_reminder_stream(channel_pref, user_id, group_id)

        reminder_text = await self._build_reminder_text(task)

        if not target_stream:
            # 无法找到任何目标流：标记失败并保留（可重试）
            await self._mark_task(task_id, _STATUS_FAILED)
            self.ctx.logger.warning("备忘提醒失败：找不到目标流，task=%s", task_id[:8])
            return

        delivered = False
        try:
            if target_stream.get("is_group"):
                # 群聊 @
                await self.ctx.send.custom("at", user_id, target_stream["stream_id"])
                await self._send_text_split(reminder_text, target_stream["stream_id"])
                delivered = True
            else:
                if notify_private_unavailable:
                    await self._send_text_split(reminder_text, target_stream["stream_id"])
                else:
                    await self._send_text_split(reminder_text, target_stream["stream_id"])
                delivered = True

            # 发送附件
            if delivered:
                await self._send_task_media(task, target_stream["stream_id"])
        except Exception as exc:
            self.ctx.logger.warning("备忘提醒发送异常：%s", exc)
            await self._mark_task(task_id, _STATUS_FAILED)
            return

        if delivered:
            await self._mark_task(task_id, _STATUS_DONE)
            self.ctx.logger.info("备忘提醒完成：task=%s", task_id[:8])

    async def _resolve_reminder_stream(self, channel_pref: str, user_id: str, group_id: str) -> tuple[dict[str, Any] | None, bool]:
        """确定提醒目标流。返回 (stream_dict|None, 私聊不可用提示标志)。

        渠道规则（稳定一致，不依赖私聊流是否恰好存在）：
        - channel=private：尝试私聊（活跃私聊流）；无则转群聊@（带提示标志）。
        - channel=group：群聊@。
        - channel=auto：群聊任务默认群聊@；私聊任务默认私聊。
        """

        async def _private() -> dict[str, Any] | None:
            try:
                stream = await self.ctx.chat.get_stream_by_user_id(user_id)
                if stream and isinstance(stream, dict):
                    sid = str(stream.get("stream_id") or stream.get("session_id") or "")
                    return {"stream_id": sid, "is_group": False}
            except Exception as exc:
                self.ctx.logger.warning("查询私聊流失败：%s", exc)
            return None

        async def _group() -> dict[str, Any] | None:
            try:
                gstream = await self.ctx.chat.get_stream_by_group_id(group_id)
                if gstream and isinstance(gstream, dict):
                    gsid = str(gstream.get("stream_id") or gstream.get("session_id") or "")
                    return {"stream_id": gsid, "is_group": True}
            except Exception as exc:
                self.ctx.logger.warning("查询群流失败：%s", exc)
            return None

        # 明确要求私聊：尝试私聊；无好友则转群聊@（带提示标志）
        if channel_pref == "private":
            s = await _private()
            if s:
                self.ctx.logger.info(
                    "备忘提醒目标=私聊：user=%s（明确要求私聊，检测到私聊流 %s）", user_id, s["stream_id"]
                )
                return s, False
            self.ctx.logger.info("备忘：用户指定私聊但无好友，user=%s", user_id)
            if group_id:
                g = await _group()
                if g:
                    self.ctx.logger.info("备忘提醒目标=群聊@：group=%s（指定私聊无好友，转群聊）", group_id)
                    return g, True
            self.ctx.logger.warning("备忘提醒目标=无：私聊不可用且无群聊目标，user=%s", user_id)
            return None, False

        # 明确要求群聊
        if channel_pref == "group" and group_id:
            g = await _group()
            if g:
                self.ctx.logger.info("备忘提醒目标=群聊@：group=%s（明确要求群聊）", group_id)
                return g, False

        # auto：群聊任务默认群聊@；私聊任务默认私聊
        if group_id:
            g = await _group()
            if g:
                reason = "群聊任务默认群聊@（未指定渠道）" if channel_pref == "auto" else "明确要求群聊"
                self.ctx.logger.info("备忘提醒目标=群聊@：group=%s channel=%s（%s）", group_id, channel_pref, reason)
                return g, False
            # 群聊流异常：私聊兜底
            s = await _private()
            if s:
                self.ctx.logger.info("备忘提醒目标=私聊：user=%s（群聊流不可用，私聊兜底）", user_id)
                return s, False
            self.ctx.logger.warning("备忘提醒目标=无：群聊任务但群聊流不可用，user=%s group=%s", user_id, group_id)
            return None, False

        # 私聊任务：默认私聊
        s = await _private()
        if s:
            self.ctx.logger.info("备忘提醒目标=私聊：user=%s（私聊任务默认私聊）", user_id)
            return s, False
        self.ctx.logger.info("备忘提醒：私聊任务但无私聊流，user=%s", user_id)
        return None, False

    async def _build_reminder_text(self, task: dict[str, Any]) -> str:
        """用 LLM 生成真人化提醒文本（结合人设三件套：人格设定、行为风格、表达风格）。"""
        persona = await self._get_persona_styles()
        now = datetime.now()
        style = self.config.prompts.reminder_style or ""
        task_text = task.get("task_text", "")
        # 上下文（可选）
        context = ""
        if task.get("reply_to"):
            try:
                quoted = await self.ctx.message.get_by_id(task["reply_to"])
                if isinstance(quoted, dict):
                    ctx_text = message_text(quoted)
                    if ctx_text:
                        context = f"\n用户原始消息：{ctx_text[:200]}"
            except Exception as exc:
                self.ctx.logger.debug("读取引用消息失败：%s", exc)

        persona_block = ""
        if persona.get("personality"):
            persona_block += f"【人格设定】\n{persona['personality']}\n"
        if persona.get("behavior_style"):
            persona_block += f"【行为风格】\n{persona['behavior_style']}\n"
        if persona.get("reply_style"):
            persona_block += f"【表达风格】\n{persona['reply_style']}\n"

        prompt = (
            f"{style}\n\n"
            f"当前时间：{now:%Y-%m-%d %H:%M:%S}\n"
            f"{persona_block or '【麦麦人设】（无）\n'}"
            f"【用户备忘任务】\n{task_text}\n"
            f"【任务原定时间】\n{datetime.fromtimestamp(float(task.get('remind_at') or now.timestamp())):%Y-%m-%d %H:%M}"
            f"{context}\n\n"
            "请严格依据以上【人格设定】【行为风格】【表达风格】来组织语气和口吻，"
            "用自然、口语化、像真人朋友一样的语气提醒用户这个备忘任务。"
            "直接输出提醒内容，不要加引号、不要加前缀、不要解释你在做什么。"
        )
        result = await self._llm_generate(prompt)
        text = str(result.get("response") or "").strip()
        if not text:
            text = f"到点啦：{task_text}"
        return text[:500]

    async def _send_task_media(self, task: dict[str, Any], stream_id: str) -> None:
        media = task.get("media") or []
        for item in media:
            try:
                if item.get("kind") == "image":
                    path = item.get("path")
                    if path and Path(path).is_file():
                        raw = await asyncio.to_thread(Path(path).read_bytes)
                        await self.ctx.send.image(base64.b64encode(raw).decode("ascii"), stream_id)
                elif item.get("kind") == "file":
                    file_ref = item.get("file_ref") or ""
                    if file_ref:
                        await self.ctx.send.custom(
                            "file",
                            {"file_ref": file_ref, "name": item.get("name") or "附件", "mime_type": item.get("mime") or ""},
                            stream_id,
                        )
            except Exception as exc:
                self.ctx.logger.warning("发送备忘附件失败：%s", exc)

    async def _mark_task(self, task_id: str, status: str) -> bool:
        """到期后终结任务：无论成败都清理附件，并移除任务记录（清理相关数据）。

        返回是否实际移除（done/failed 会移除记录并返回 True；其它状态仅更新，返回 False）。
        """
        removed = False
        changed = False
        async with self._tasks_lock:
            for i, t in enumerate(self._tasks):
                if t.get("id") == task_id:
                    if t.get("repeat_daily") and status in (_STATUS_DONE, _STATUS_FAILED):
                        next_at = datetime.fromtimestamp(float(t.get("remind_at") or time.time())) + timedelta(days=1)
                        while next_at.timestamp() <= time.time():
                            next_at += timedelta(days=1)
                        t["remind_at"] = next_at.timestamp()
                        t["status"] = _STATUS_PENDING
                        changed = True
                        break
                    # 无论成败，先清理附件（落盘图片、媒体目录）
                    self._delete_task_media(t)
                    if status in (_STATUS_DONE, _STATUS_FAILED):
                        self._tasks.pop(i)
                        removed = True
                    else:
                        t["status"] = status
                        changed = True
                    break
        if removed or changed:
            await self._save_tasks()
        return removed

    # ==================== 指令 ====================

    async def _is_command_authorized(self, message: Any) -> bool:
        return self._is_admin(self._extract_user(message))

    @Command(
        "memo_create",
        description="创建一条备忘任务（如：/备忘 一小时后提醒我喝水）",
        pattern=r"^/?备忘(?:\s+(?P<text>[\s\S]+))?$",
    )
    async def cmd_create(self, **kwargs: Any) -> tuple[bool, str, int]:
        stream_id = str(kwargs.get("stream_id") or "")
        message = kwargs.get("message") or {}
        user_id = extract_plain_id(self._extract_user(message))
        group_id = self._extract_group(message)
        platform = self._extract_platform(message)
        matched = kwargs.get("matched_groups") or {}
        raw_text = str(matched.get("text") or "").strip()

        if not raw_text:
            # 无内容：进入收集窗口，等用户补充
            await self.ctx.send.text("请告诉我备忘内容，比如“一小时后提醒我喝水”（或直接补充描述）。", stream_id)
            return False, "缺少任务内容", 1
        if raw_text.lower() in ("查", "列表", "list"):
            return await self.cmd_list(**kwargs)
        if raw_text.lower() in ("管理", "全部", "所有"):
            return await self.cmd_admin_all(**kwargs)
        if raw_text.lower() in ("清理", "清理过期", "清理全部"):
            scope = "all" if "全部" in raw_text else "expired"
            return await self.cmd_admin_clean(_scope=scope, **kwargs)
        if raw_text.lower().startswith("删"):
            return await self.cmd_delete_text(raw_text[1:].strip(), **kwargs)
        if raw_text.lower() in ("开", "on", "启用"):
            return await self.cmd_switch(True, **kwargs)
        if raw_text.lower() in ("关", "off", "停用"):
            return await self.cmd_switch(False, **kwargs)

        now = datetime.now()
        task_text, remind_ts = await self._extract_reminder_from_text(raw_text, now)
        media = await self._collect_media_with_reply(message)
        task, reject = await self._create_task(
            user_id=user_id, group_id=group_id, source_stream=stream_id,
            platform=platform, task_text=task_text, remind_ts=remind_ts,
            channel_pref=self._detect_channel_pref(raw_text), media=media,
            reply_to=message_reply_id(message), now=now,
        )
        await self._announce_result(stream_id, user_id, task, reject)
        return (True, "已创建" if task else f"拒绝：{reject}", 2 if task else 1)

    @Command(
        "memo_switch",
        description="全局开关备忘插件（仅超级管理员）",
        pattern=r"^/?备忘开关\s*(?P<state>on|off|开|关|启用|停用)?\s*$",
    )
    async def cmd_switch(self, state: Any = None, **kwargs: Any) -> tuple[bool, str, int]:
        stream_id = str(kwargs.get("stream_id") or "")
        message = kwargs.get("message") or {}
        if not await self._is_command_authorized(message):
            await self.ctx.send.text(_CMD_DENIED_MSG, stream_id)
            return False, "无权限", 1
        matched = kwargs.get("matched_groups") or {}
        s = str(matched.get("state") or "").strip().lower() if state is None else str(state).strip().lower()
        if not s:
            current = "开启" if self.config.plugin.enabled else "关闭"
            await self.ctx.send.text(f"备忘插件当前：{current}。用法：/备忘开关 on|off", stream_id)
            return True, f"当前：{current}", 1
        new_state = s in ("on", "开", "启用")
        ok = await self._persist_config_field("plugin", "enabled", new_state)
        self._apply_in_memory_config("plugin", "enabled", new_state)
        if new_state:
            self._start_scheduler()
        else:
            self._stop_scheduler()
        text = f"备忘插件已{'开启' if new_state else '关闭'}" + ("" if ok else "（config.toml 写入失败，仅本次生效）")
        await self.ctx.send.text(text, stream_id)
        return True, text, 2

    @Command(
        "memo_list",
        description="查看自己的备忘任务",
        pattern=r"^/?备忘查\s*$",
    )
    async def cmd_list(self, **kwargs: Any) -> tuple[bool, str, int]:
        stream_id = str(kwargs.get("stream_id") or "")
        message = kwargs.get("message") or {}
        user_id = extract_plain_id(self._extract_user(message))
        now_ts = time.time()
        async with self._tasks_lock:
            mine = [t for t in self._tasks if t.get("user_id") == user_id and t.get("status") == _STATUS_PENDING]
            mine.sort(key=lambda t: float(t.get("remind_at") or 0))
        if not mine:
            text = "你还没有待办的备忘任务。"
        else:
            lines = [f"你有 {len(mine)} 条待办备忘："]
            for t in mine[:10]:
                remind_str = datetime.fromtimestamp(float(t.get("remind_at") or now_ts)).strftime("%m-%d %H:%M")
                lines.append(f"· {remind_str} {str(t.get('task_text') or '')[:40]}（{t['id'][:8]}）")
            text = "\n".join(lines)
        await self.ctx.send.text(text, stream_id)
        return True, text, 1

    async def cmd_delete_text(self, task_id: str, **kwargs: Any) -> tuple[bool, str, int]:
        return await self.cmd_delete(task_id=task_id, **kwargs)

    @Command(
        "memo_delete",
        description="取消自己的备忘任务（/备忘删 <id>）",
        pattern=r"^/?备忘删\s*(?P<id>[0-9a-fA-F]{8,})?\s*$",
    )
    async def cmd_delete(self, **kwargs: Any) -> tuple[bool, str, int]:
        stream_id = str(kwargs.get("stream_id") or "")
        message = kwargs.get("message") or {}
        user_id = extract_plain_id(self._extract_user(message))
        matched = kwargs.get("matched_groups") or {}
        task_id = str(matched.get("id") or "").strip().lower()
        if not task_id:
            await self.ctx.send.text("用法：/备忘删 <任务id>（id 见 /备忘查）", stream_id)
            return False, "缺少任务 id", 1
        removed = False
        async with self._tasks_lock:
            for t in self._tasks:
                if t.get("user_id") == user_id and str(t.get("id") or "").startswith(task_id) and t.get("status") == _STATUS_PENDING:
                    self._delete_task_media(t)
                    t["status"] = _STATUS_REJECTED
                    removed = True
                    break
        if removed:
            await self._save_tasks()
            text = "已取消这条备忘任务。"
        else:
            text = "没找到这条任务（或它已处理/不属于你）。"
        await self.ctx.send.text(text, stream_id)
        return True, text, 1 if removed else 2

    @Command(
        "memo_admin_all",
        description="查看所有用户的备忘任务（仅超级管理员）",
        pattern=r"^/?备忘管理\s*$",
    )
    async def cmd_admin_all(self, **kwargs: Any) -> tuple[bool, str, int]:
        """管理员查看所有待办备忘：仅返回用户ID、创建时间、截止时间。"""
        stream_id = str(kwargs.get("stream_id") or "")
        message = kwargs.get("message") or {}
        if not await self._is_command_authorized(message):
            await self.ctx.send.text(_CMD_DENIED_MSG, stream_id)
            return False, "无权限", 1
        now_ts = time.time()
        async with self._tasks_lock:
            tasks = [t for t in self._tasks if t.get("status") == _STATUS_PENDING]
            tasks.sort(key=lambda t: float(t.get("remind_at") or 0))
        if not tasks:
            text = "当前没有待办的备忘任务。"
        else:
            lines = [f"当前共有 {len(tasks)} 条待办备忘："]
            for t in tasks:
                created = datetime.fromtimestamp(float(t.get("created_at") or now_ts)).strftime("%m-%d %H:%M")
                remind = datetime.fromtimestamp(float(t.get("remind_at") or now_ts)).strftime("%m-%d %H:%M")
                lines.append(f"· user={t.get('user_id')} 创建={created} 截止={remind}（{t['id'][:8]}）")
            text = "\n".join(lines)
        await self.ctx.send.text(text, stream_id)
        return True, text, 1

    @Command(
        "memo_admin_clean",
        description="清理过期或全部备忘任务（仅超级管理员）",
        pattern=r"^/?备忘清理\s*(?P<scope>expired|all|过期|全部)?\s*$",
    )
    async def cmd_admin_clean(self, _scope: str = "", **kwargs: Any) -> tuple[bool, str, int]:
        """管理员清理过期任务（remind_at 已到）或全部任务。_scope: expired/all。"""
        stream_id = str(kwargs.get("stream_id") or "")
        message = kwargs.get("message") or {}
        if not await self._is_command_authorized(message):
            await self.ctx.send.text(_CMD_DENIED_MSG, stream_id)
            return False, "无权限", 1
        matched = kwargs.get("matched_groups") or {}
        scope = str(_scope or matched.get("scope") or "expired").strip().lower()
        clean_all = scope in ("all", "全部")
        now = time.time()
        async with self._tasks_lock:
            if clean_all:
                victims = list(self._tasks)
            else:
                victims = [t for t in self._tasks if float(t.get("remind_at") or 0) <= now]
            if not victims:
                await self.ctx.send.text("没有需要清理的备忘任务。", stream_id)
                return True, "没有需要清理", 2
            victim_ids = {id(t) for t in victims}
            for t in victims:
                self._delete_task_media(t)
            self._tasks = [t for t in self._tasks if id(t) not in victim_ids]
        await self._save_tasks()
        label = "全部" if clean_all else "过期"
        text = f"已清理 {len(victims)} 条{label}备忘任务。"
        self.ctx.logger.info("备忘：管理员清理%s任务 %d 条", label, len(victims))
        await self.ctx.send.text(text, stream_id)
        return True, text, 1

    # ==================== 配置写入 ====================

    async def _persist_config_field(self, section: str, field: str, value: Any) -> bool:
        config_path = os.path.join(self._plugin_dir, "config.toml")
        try:
            import tomlkit
            if os.path.exists(config_path):
                with open(config_path, "r", encoding="utf-8") as f:
                    doc = tomlkit.parse(f.read())
            else:
                doc = tomlkit.document()
            section_table = doc.setdefault(section, tomlkit.table())
            section_table[field] = value
            with open(config_path, "w", encoding="utf-8") as f:
                f.write(tomlkit.dumps(doc))
            return True
        except Exception as exc:
            self.ctx.logger.warning("写入 config.toml 失败：%s", exc)
            return False

    def _apply_in_memory_config(self, section: str, field: str, value: Any) -> None:
        try:
            raw = self.get_plugin_config_data()
            raw.setdefault(section, {})[field] = value
            self.set_plugin_config(raw)
        except Exception as exc:
            self.ctx.logger.debug("刷新内存配置失败：%s", exc)

    # ==================== Tool：暴露给 LLM 的辅助能力 ====================

    @Tool(
        "reminder_list_mine",
        description="查看当前用户自己设置的待办备忘任务列表（返回任务 id、内容、提醒时间）",
        parameters=[
            ToolParameterInfo(name="stream_id", param_type=ToolParamType.STRING, description="当前聊天流 ID", required=True),
        ],
    )
    async def tool_list_mine(self, stream_id: str = "", **kwargs: Any) -> dict[str, Any]:
        message = kwargs.get("message") or {}
        user_id = extract_plain_id(self._extract_user(message))
        now_ts = time.time()
        async with self._tasks_lock:
            mine = [t for t in self._tasks if t.get("user_id") == user_id and t.get("status") == _STATUS_PENDING]
            mine.sort(key=lambda t: float(t.get("remind_at") or 0))
        items = [
            {
                "id": t["id"][:8],
                "task": str(t.get("task_text") or "")[:80],
                "remind_at": datetime.fromtimestamp(float(t.get("remind_at") or now_ts)).strftime("%Y-%m-%d %H:%M"),
            }
            for t in mine[:10]
        ]
        return {"success": True, "content": f"共 {len(items)} 条待办备忘", "tasks": items}

    @Tool(
        "reminder_cancel",
        description="取消当前用户的一条待办备忘任务（按任务 id 前 8 位）",
        parameters=[
            ToolParameterInfo(name="task_id", param_type=ToolParamType.STRING, description="任务 id（前 8 位）", required=True),
            ToolParameterInfo(name="stream_id", param_type=ToolParamType.STRING, description="当前聊天流 ID", required=True),
        ],
    )
    async def tool_cancel(self, task_id: str = "", stream_id: str = "", **kwargs: Any) -> dict[str, Any]:
        message = kwargs.get("message") or {}
        user_id = extract_plain_id(self._extract_user(message))
        tid = str(task_id or "").strip().lower()
        removed = False
        async with self._tasks_lock:
            for t in self._tasks:
                if t.get("user_id") == user_id and str(t.get("id") or "").startswith(tid) and t.get("status") == _STATUS_PENDING:
                    self._delete_task_media(t)
                    t["status"] = _STATUS_REJECTED
                    removed = True
                    break
        if removed:
            await self._save_tasks()
            return {"success": True, "content": "已取消该备忘任务。"}
        return {"success": False, "content": "未找到该任务或不属于当前用户。"}


# ==================== 插件入口 ====================


def create_plugin() -> PersonalAssistantPlugin:
    return PersonalAssistantPlugin()
