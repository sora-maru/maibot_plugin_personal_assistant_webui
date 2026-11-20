"""Validate with a tagged MaiBot checkout and the real SDK, without starting a bot.

python tools/check_compat.py --host-root /path/to/MaiBot --sdk-root /path/to/SDK --run-tests
Only standalone upstream models are executed; no Host services or QQ connection start.
"""
import argparse
import ast
import importlib.util
import inspect
import json
import sys
import tomllib
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host-root", type=Path, required=True)
    parser.add_argument("--sdk-root", type=Path)
    parser.add_argument("--run-tests", action="store_true")
    args = parser.parse_args()
    host_root = args.host_root.resolve()
    host_version = tomllib.loads((host_root / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    if args.sdk_root:
        sdk_root = args.sdk_root.resolve()
        sdk_version = tomllib.loads((sdk_root / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
        sys.path.insert(0, str(sdk_root))
    else:
        from importlib.metadata import version
        sdk_version = version("maibot-plugin-sdk")

    # Keep upstream model definitions intact, excluding only imports that boot Host
    # integration, the Host logger instance, and the integration service class.
    manifest_path = host_root / "src/plugin_runtime/runner/manifest_validator.py"
    tree = ast.parse(manifest_path.read_text(encoding="utf-8"))
    tree.body = [node for node in tree.body if not (
        isinstance(node, ast.ImportFrom) and (node.level or (node.module or "").startswith("src."))
        or isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "logger" for target in node.targets)
        or isinstance(node, ast.ClassDef) and node.name == "ManifestValidator"
    )]
    manifest_models = types.ModuleType("memo_host_manifest_models")
    sys.modules[manifest_models.__name__] = manifest_models
    exec(compile(tree, str(manifest_path), "exec"), manifest_models.__dict__)
    manifest = manifest_models.PluginManifest.model_validate(json.loads((ROOT / "_manifest.json").read_text(encoding="utf-8")))
    for version, bounds in ((host_version, manifest.host_application), (sdk_version, manifest.sdk)):
        ok, reason = manifest_models.VersionComparator.is_in_range(version, bounds.min_version, bounds.max_version)
        if not ok:
            raise AssertionError(reason)
    print(f"PASS upstream manifest model and version ranges: Host {host_version}, SDK {sdk_version}")

    schema = load_module("memo_host_webui_schema", host_root / "src/plugin_runtime/webui_schema.py")
    extension = schema.load_webui_extension(str(ROOT))
    plugin_module = load_module("memo_compat_plugin", ROOT / "plugin.py")
    plugin = plugin_module.create_plugin()
    plugin.set_plugin_config({})
    components = {item["name"]: item for item in plugin.get_components()}
    examples = {"list": {"query": "", "status": "pending", "repeat": "daily"},
                "create": {"payload": "{}"}, "update": {"task_id": "example", "payload": "{}"},
                "delete": {"task_id": "example"}, "cancel": {"task_id": "example"}}
    for page in extension.pages:
        for name, binding in {**page.queries, **page.actions}.items():
            component = components[binding.api]
            assert component["type"] == "API"
            assert component["metadata"]["public"] is False
            assert component["metadata"]["version"] == binding.version
            method = getattr(plugin, component["metadata"]["handler_name"])
            inspect.signature(method).bind(**examples[name])
            binding.validate_args(examples[name])
            assert set(binding.parameters) <= set(inspect.signature(method).parameters)
    assert plugin.get_webui_config_schema(plugin_id=manifest.id, plugin_version=manifest.version)
    print(f"PASS upstream WebUI loader and SDK registration: {len(components)} components, all page bindings private and callable")

    registry_source = (host_root / "src/plugin_runtime/capabilities/registry.py").read_text(encoding="utf-8")
    for capability in manifest.capabilities:
        assert f'"{capability}"' in registry_source, f"Capability missing in Host registry: {capability}"
    print("PASS every declared capability exists in Host registry")
    if args.run_tests:
        tests = unittest.defaultTestLoader.discover(str(ROOT / "tests"))
        if not unittest.TextTestRunner(verbosity=2).run(tests).wasSuccessful():
            raise SystemExit(1)


if __name__ == "__main__":
    main()
