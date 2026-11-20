# 验证记录

日期：2026-11-20。插件版本：1.3.0，ID：`github.sora-maru.personal-assistant-webui`。

## 对照版本

- MaiBot 正式版 [v1.4.7](https://github.com/Mai-with-u/MaiBot/releases/tag/v1.4.7)，提交 `b7f36825463413c47e61d44fe8a93427f6fe5a6f`；该标签包含 WebUI 1.10.7。
- Plugin SDK [v2.23.0](https://github.com/Mai-with-u/maibot-plugin-sdk/releases/tag/v2.23.0)，提交 `f9984673d7e6061832e9306cf0e0daaf375f0f91`。
- 本地测试解释器 Python 3.13.5、Pydantic 2.14.3。SDK 从上述官方标签源码导入，未用占位类代替 SDK。

## 已通过

- 原版 `PluginManifest` 模型及 `VersionComparator`：manifest 和 Host/SDK 范围有效。原模型定义完整执行；仅去除会启动宿主集成的导入、日志实例和集成服务类。
- 原版 `load_webui_extension`：schema 2、自包含 HTML、页面 `list` 图标和所有页面参数通过校验。
- 真实 SDK 注册 14 个组件：6 个命令、1 个 Hook、2 个 Tool、5 个私有 API。全部页面 query/action 均对应真实且非公开的 API；参数与方法签名一致。默认配置和中文配置 Schema 可生成，既有 `plugin.config_version = "1.0.0"` 保留。
- Manifest 声明的全部能力在 v1.4.7 Host 注册表中存在。
- **21 项测试通过**：创建、编辑、删除与附件清理、任务上限及非法时间、旧任务重载、每日任务顺延、真实 SDK RPC 返回值解包、发送失败判定、使用记录的平台查询会话、单次任务清理、编辑/删除发送中任务保护、过期清理保留待发送任务、卸载等待取消并恢复状态等。
- 浏览器使用 v1.4.7 原始页面 SDK、CSP 与 `sandbox="allow-scripts allow-forms"`。仅父页面的 Host API 转发由回环测试服务模拟，后端使用真实插件和 SDK，并执行原版 WebUI 参数校验。实际操作新增每日任务、编辑、类型筛选、取消删除、确认删除；HTML 文本转义和错误提示正常，无页面 JavaScript 异常和受屏蔽的原生对话框。
- 在 1440×1000 桌面和 390×480 手机视口验证滚动与弹窗按钮边界；短窗口中的编辑保存、取消按钮可达，空列表不会整页横向溢出。
- Python 编译和 Git diff 格式检查通过。

## 复现

```console
python -m pip install -r requirements-dev.txt
python tools/check_compat.py --host-root /path/to/MaiBot-v1.4.7 --sdk-root /path/to/maibot-plugin-sdk-v2.23.0 --run-tests
```

浏览器测试为 `tests/test_webui_browser.cjs`，依赖和环境变量见 README。`tools/package_plugin.py` 在干净工作树中从已提交 Git blob 构建归档，写入 `CONTENTS.sha256`，再重新打开归档检查 CRC、文件集合与每个文件的 SHA-256；ZIP 总摘要输出为同名 `.sha256` 文件。

## 验证边界

没有启动完整 MaiBot 服务，没有连接真实 QQ、第三方适配器或 LLM。测试中的 RPC 回执、消息目标和模型结果由本地测试环境提供；SDK、Host 的独立清单/WebUI 模型和浏览器页面通信脚本为原版代码。上述检查不代替用户实例的真实消息投递验收，也不表示 GitHub Release 或官方市场版本已经更新。

实例升级后，请检查插件正常加载及页面可打开，创建一条近期单次任务和一条每日任务，确认私聊/群聊实际到达、平台配置正确；重启后确认任务继续保留。旧实例继续使用其对应版本，更新时保留配置、`tasks.json` 与附件数据目录。
