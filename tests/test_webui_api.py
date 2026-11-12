import asyncio
import importlib.util
import json
import sys
import types
from pathlib import Path
import unittest


def load_plugin_module():
    sdk = types.ModuleType("maibot_sdk")

    class ConfigBase:
        pass

    def identity_decorator(*args, **kwargs):
        return lambda target: target

    sdk.API = sdk.Command = sdk.HookHandler = sdk.Tool = identity_decorator
    sdk.Field = lambda default=None, **kwargs: default
    sdk.MaiBotPlugin = type("MaiBotPlugin", (), {})
    sdk.PluginConfigBase = ConfigBase
    sdk_types = types.ModuleType("maibot_sdk.types")
    for name in ("ErrorPolicy", "HookMode", "HookOrder", "ToolParamType"):
        setattr(sdk_types, name, types.SimpleNamespace(BLOCKING="blocking", EARLY="early", SKIP="skip", STRING="string"))
    sdk_types.ToolParameterInfo = lambda **kwargs: kwargs
    sys.modules["maibot_sdk"] = sdk
    sys.modules["maibot_sdk.types"] = sdk_types
    path = Path(__file__).parents[1] / "plugin.py"
    spec = importlib.util.spec_from_file_location("memo_webui_test_plugin", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


module = load_plugin_module()


def plugin_with(tasks):
    plugin = object.__new__(module.PersonalAssistantPlugin)
    plugin._tasks = tasks
    plugin._tasks_lock = asyncio.Lock()
    plugin.saved = False
    plugin.deleted = []

    async def save():
        plugin.saved = True

    plugin._save_tasks = save
    plugin._delete_task_media = lambda task: plugin.deleted.append(task["id"])
    return plugin


class WebUITests(unittest.TestCase):
    def test_webui_declaration_uses_supported_page_icon(self):
        root = Path(__file__).parents[1]
        manifest = json.loads((root / "_manifest.json").read_text(encoding="utf-8"))
        webui = json.loads((root / "webui.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["version"], "1.2.0")
        self.assertIn(manifest["display"]["icon"]["type"], {"lucide", "emoji", "local"})
        self.assertIn(webui["pages"][0]["icon"], {"puzzle", "chart", "settings", "database", "list"})
        self.assertEqual({"create", "update", "delete"} & set(webui["pages"][0]["actions"]), {"create", "update", "delete"})

    def test_list_filters_and_only_returns_safe_fields(self):
        plugin = plugin_with([
            {"id": "a", "task_text": "Drink water", "user_id": "100", "status": "pending", "remind_at": 20, "secret": "hidden"},
            {"id": "b", "task_text": "Call home", "user_id": "200", "status": "done", "remind_at": 10},
        ])
        result = asyncio.run(plugin.web_list_tasks("water", "pending"))
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["tasks"][0]["id"], "a")
        self.assertNotIn("secret", result["tasks"][0])

    def test_cancel_removes_pending_task_and_persists(self):
        plugin = plugin_with([{"id": "a", "status": "pending"}, {"id": "b", "status": "done"}])
        result = asyncio.run(plugin.web_cancel_task("a"))
        self.assertEqual(result, {"success": True})
        self.assertEqual([task["id"] for task in plugin._tasks], ["b"])
        self.assertEqual(plugin.deleted, ["a"])
        self.assertTrue(plugin.saved)

    def test_cancel_cannot_remove_sending_task(self):
        plugin = plugin_with([{"id": "a", "status": "sending"}])
        with self.assertRaises(ValueError):
            asyncio.run(plugin.web_cancel_task("a"))
        self.assertEqual(len(plugin._tasks), 1)
        self.assertFalse(plugin.saved)

    def test_daily_filter_and_repeat_field(self):
        plugin = plugin_with([
            {"id": "daily", "task_text": "Water", "user_id": "100", "status": "pending", "remind_at": 30, "repeat_daily": True},
            {"id": "once", "task_text": "Walk", "user_id": "100", "status": "pending", "remind_at": 40},
        ])
        result = asyncio.run(plugin.web_list_tasks("", "pending", "daily"))
        self.assertEqual([item["id"] for item in result["tasks"]], ["daily"])
        self.assertTrue(result["tasks"][0]["repeat_daily"])

    def test_daily_completion_moves_task_to_next_day(self):
        plugin = plugin_with([{"id": "daily", "status": "sending", "repeat_daily": True, "remind_at": 1}])
        removed = asyncio.run(plugin._mark_task("daily", "done"))
        self.assertFalse(removed)
        self.assertEqual(plugin._tasks[0]["status"], "pending")
        self.assertGreater(plugin._tasks[0]["remind_at"], 86400)
        self.assertTrue(plugin.saved)


if __name__ == "__main__":
    unittest.main()
