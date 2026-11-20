"""Loopback-only UI test backend: actual plugin/SDK/Host schema, temporary task data."""
import argparse
import asyncio
import importlib.util
import json
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host-root", type=Path, required=True)
    parser.add_argument("--sdk-root", type=Path, required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(args.sdk_root.resolve()))
    from maibot_sdk.context import PluginContext, PluginPaths

    def load(name, path):
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module

    schema = load("memo_browser_host_schema", args.host_root.resolve() / "src/plugin_runtime/webui_schema.py")
    extension = schema.load_webui_extension(str(ROOT))
    page = extension.pages[0]
    plugin_module = load("memo_browser_plugin", ROOT / "plugin.py")
    loop = asyncio.new_event_loop()
    worker = threading.Thread(target=loop.run_forever, daemon=True)
    worker.start()
    with tempfile.TemporaryDirectory(prefix="memo-ui-test-") as temporary:
        plugin = plugin_module.create_plugin()
        plugin.set_plugin_config({})
        plugin._set_context(PluginContext(
            "github.sora-maru.personal-assistant-webui",
            paths=PluginPaths(data_dir=Path(temporary), runtime_dir=Path(temporary) / "runtime"),
        ))
        components = {item["name"]: item for item in plugin.get_components()}

        async def invoke(body):
            bindings = page.queries if body["kind"] == "queries" else page.actions
            binding = bindings[body["name"]]
            binding.validate_args(body.get("args", {}))
            method = getattr(plugin, components[binding.api]["metadata"]["handler_name"])
            return await method(**body.get("args", {}))

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"<!doctype html><title>MaiBot plugin compatibility test</title>")

            def do_POST(self):
                if self.path != "/rpc":
                    self.send_error(404)
                    return
                try:
                    size = int(self.headers.get("Content-Length", 0))
                    if not 0 < size <= 65536:
                        raise ValueError("Test request too large")
                    body = json.loads(self.rfile.read(size))
                    result = asyncio.run_coroutine_threadsafe(invoke(body), loop).result(timeout=10)
                    response = {"result": result}
                except Exception as exc:
                    response = {"error": str(exc)}
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps(response, ensure_ascii=False).encode("utf-8"))

        with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
            print(f"SERVER_URL=http://127.0.0.1:{server.server_port}", flush=True)
            try:
                server.serve_forever()
            finally:
                loop.call_soon_threadsafe(loop.stop)
                worker.join(timeout=3)


if __name__ == "__main__":
    main()
