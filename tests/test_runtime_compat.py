"""Exercise actual SDK proxies/config/decorators with controlled Host RPC responses."""
import asyncio
import importlib.util
import json
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

from maibot_sdk.context import PluginContext, PluginPaths

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("memo_runtime_test_plugin", ROOT / "plugin.py")
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="memo-sdk-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.calls = []
        self.send_success = True
        self.stream_missing = False
        self.llm_error = False

        async def rpc(method, plugin_id, payload):
            self.assertEqual(method, "cap.call")
            self.assertEqual(plugin_id, "github.sora-maru.personal-assistant-webui")
            capability, args = payload["capability"], payload["args"]
            self.calls.append((capability, args))
            if capability == "config.get":
                return {"success": True, "value": args.get("default", "")}
            if capability.startswith("chat.get_stream_by_"):
                return {"success": True, "stream": None if self.stream_missing else {"stream_id": "target-session"}}
            if capability.startswith("send."):
                return {"success": self.send_success, "sent": self.send_success, "message_id": "delivered-id"}
            if capability == "llm.generate":
                if self.llm_error:
                    raise RuntimeError("controlled RPC error")
                return {"success": True, "response": "到点提醒。", "model": "test-model"}
            raise AssertionError(f"Unexpected capability: {capability}")

        self.plugin = module.create_plugin()
        self.plugin.set_plugin_config({})
        self.plugin._set_context(PluginContext(
            "github.sora-maru.personal-assistant-webui", rpc_call=rpc,
            paths=PluginPaths(data_dir=self.root, runtime_dir=self.root / "runtime"),
        ))

    def payload(self, **changes):
        values = {"task_text": "喝水", "user_id": "10001", "group_id": "20001", "source_platform": "qq",
                  "remind_at": time.time() + 3600, "channel_pref": "group", "repeat_daily": True}
        values.update(changes)
        return json.dumps(values, ensure_ascii=False)

    async def due_task(self, **changes):
        await self.plugin.web_create_task(self.payload())
        task = self.plugin._tasks[0]
        task.update(remind_at=time.time() - 60, **changes)
        return task

    async def test_real_sdk_components_and_defaults(self):
        self.assertEqual(self.plugin.config.reminder.max_tasks_per_user, 20)
        components = {item["name"]: item for item in self.plugin.get_components()}
        declaration = json.loads((ROOT / "webui.json").read_text(encoding="utf-8"))
        page = declaration["pages"][0]
        for binding in [*page["queries"].values(), *page["actions"].values()]:
            component = components[binding["api"]]
            self.assertEqual(component["type"], "API")
            self.assertFalse(component["metadata"]["public"])
        for name in ("reminder_cancel", "reminder_list_mine"):
            self.assertEqual(components[name]["chat_scope"], "all")
            self.assertEqual(components[name]["allowed_session"], [])

    async def test_create_edit_reload_and_delete_preserve_fields(self):
        response = await self.plugin.web_create_task(self.payload())
        task = self.plugin._tasks[0]
        created = task["created_at"]
        media_dir = self.plugin._media_dir(task["id"])
        media_dir.mkdir(parents=True)
        attachment = media_dir / "0.png"
        attachment.write_bytes(b"local image fixture")
        task["media"] = [{"kind": "image", "path": str(attachment)}]
        await self.plugin.web_update_task(response["task_id"], self.payload(task_text="开会", source_platform="qq_official"))
        await self.plugin._load_tasks()
        loaded = self.plugin._tasks[0]
        self.assertEqual(loaded["created_at"], created)
        self.assertEqual(loaded["task_text"], "开会")
        self.assertEqual(loaded["source_platform"], "qq_official")
        self.assertEqual(loaded["media"][0]["path"], str(attachment))
        self.assertTrue(loaded["repeat_daily"])
        await self.plugin.web_delete_task(loaded["id"])
        self.assertFalse(attachment.exists())
        self.assertEqual(json.loads(self.plugin._tasks_path().read_text(encoding="utf-8"))["tasks"], [])

    async def test_old_tasks_reload_without_data_migration(self):
        self.plugin._tasks_path().write_text(json.dumps({"version": 1, "tasks": [
            {"id": "legacy", "status": "sending", "user_id": "10001", "task_text": "旧任务", "remind_at": time.time() + 500},
        ]}), encoding="utf-8")
        await self.plugin._load_tasks()
        rows = (await self.plugin.web_list_tasks())["tasks"]
        self.assertEqual(rows[0]["status"], "pending")
        self.assertEqual(rows[0]["source_platform"], "qq")
        self.assertFalse(rows[0]["repeat_daily"])

    async def test_validation_rejects_bad_time_and_missing_group(self):
        for value in (float("nan"), float("inf"), time.time() - 60):
            with self.assertRaises(ValueError):
                await self.plugin.web_create_task(self.payload(remind_at=value))
        with self.assertRaises(ValueError):
            await self.plugin.web_create_task(self.payload(group_id=""))
        self.assertFalse(self.plugin._tasks)

    async def test_create_respects_task_limit(self):
        config = self.plugin.get_default_config()
        config["reminder"]["max_tasks_per_user"] = 1
        self.plugin.set_plugin_config(config)
        await self.plugin.web_create_task(self.payload())
        with self.assertRaises(ValueError):
            await self.plugin.web_create_task(self.payload())
        self.assertEqual(len(self.plugin._tasks), 1)

    async def test_daily_reminder_uses_recorded_platform_and_sdk_receipt(self):
        task = await self.due_task(source_platform="qq_official")
        scheduled = datetime.fromtimestamp(task["remind_at"])
        await self.plugin._fire_reminder(task)
        lookup = next(args for name, args in self.calls if name == "chat.get_stream_by_group_id")
        self.assertEqual(lookup["platform"], "qq_official")
        send = [(name, args) for name, args in self.calls if name.startswith("send.")]
        self.assertEqual([name for name, args in send], ["send.custom", "send.text"])
        self.assertTrue(all(args["return_details"] is True for name, args in send))
        self.assertEqual(task["last_status"], "done")
        self.assertEqual(task["status"], "pending")
        self.assertEqual(datetime.fromtimestamp(task["remind_at"]), scheduled + timedelta(days=1))
        self.assertEqual(len(self.plugin._tasks), 1)

    async def test_failed_receipt_is_not_recorded_as_success(self):
        self.send_success = False
        task = await self.due_task()
        with self.assertLogs(self.plugin.ctx.logger, level="WARNING"):
            await self.plugin._fire_reminder(task)
        self.assertEqual(task["last_status"], "failed")
        self.assertEqual(task["status"], "pending")
        self.assertGreater(task["remind_at"], time.time())

    async def test_missing_session_reschedules_daily_task(self):
        self.stream_missing = True
        task = await self.due_task()
        with self.assertLogs(self.plugin.ctx.logger, level="WARNING"):
            await self.plugin._fire_reminder(task)
        self.assertEqual(task["last_status"], "failed")
        self.assertEqual(task["status"], "pending")
        self.assertFalse(any(name.startswith("send.") for name, args in self.calls))

    async def test_prepare_exception_does_not_leave_sending_task(self):
        task = await self.due_task()
        self.plugin._build_reminder_text = AsyncMock(side_effect=RuntimeError("controlled generation failure"))
        with self.assertLogs(self.plugin.ctx.logger, level="WARNING"):
            await self.plugin._fire_reminder(task)
        self.assertEqual(task["last_status"], "failed")
        self.assertEqual(task["status"], "pending")

    async def test_one_time_reminder_still_cleans_up(self):
        task = await self.due_task(repeat_daily=False)
        await self.plugin._fire_reminder(task)
        self.assertEqual(self.plugin._tasks, [])

    async def test_cleanup_keeps_overdue_pending_and_sending_tasks(self):
        task = await self.due_task(repeat_daily=False)
        self.plugin._tasks.extend([
            {"id": "sending", "status": "sending", "remind_at": 1},
            {"id": "cancelled", "status": "rejected", "remind_at": 1},
        ])
        self.assertEqual(await self.plugin._cleanup_expired_tasks(), 1)
        self.assertEqual([item["id"] for item in self.plugin._tasks], [task["id"], "sending"])

    async def test_edit_and_delete_refuse_task_already_sending(self):
        task = await self.due_task()
        task["status"] = "sending"
        with self.assertRaises(ValueError):
            await self.plugin.web_update_task(task["id"], self.payload())
        with self.assertRaises(ValueError):
            await self.plugin.web_delete_task(task["id"])

    async def test_postponed_task_is_not_sent_from_stale_due_snapshot(self):
        task = await self.due_task()
        snapshot = dict(task)
        task["remind_at"] = time.time() + 3600
        await self.plugin._fire_reminder(snapshot)
        self.assertEqual(task["status"], "pending")
        self.assertEqual(self.calls, [])

    async def test_unload_awaits_cancellation_and_restores_pending(self):
        task = await self.due_task()
        entered = asyncio.Event()

        async def slow_generation(_task):
            entered.set()
            await asyncio.Event().wait()

        self.plugin._build_reminder_text = slow_generation
        self.plugin._scheduler_task = asyncio.create_task(self.plugin._fire_reminder(task))
        await asyncio.wait_for(entered.wait(), timeout=2)
        await self.plugin.on_unload()
        self.assertIsNone(self.plugin._scheduler_task)
        self.assertEqual(task["status"], "pending")
        await self.plugin._load_tasks()
        self.assertEqual(self.plugin._tasks[0]["status"], "pending")

    async def test_load_and_unload_real_sdk_lifecycle(self):
        config = self.plugin.get_default_config()
        config["plugin"]["enabled"] = False
        self.plugin.set_plugin_config(config)
        await self.plugin.on_load()
        self.assertTrue((self.root / "media").is_dir())
        await self.plugin.on_unload()
        self.assertTrue(self.plugin._tasks_path().is_file())


if __name__ == "__main__":
    unittest.main()
