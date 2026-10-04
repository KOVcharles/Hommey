# Prompt 维护入口

主规则在 `main.md`。子 Agent 规则由 `specialist-common.md` 和
`specialists/<role>.md` 组成，角色、工具、Skill 的映射在 `../profiles.py`。
文件在进程导入时加载；修改后需重启相应进程，不是热加载。

## 应该改哪里

| 内容 | 唯一维护位置 |
|---|---|
| 主 Agent 的职责、判断原则、证据边界、交付方式 | `main.md` |
| 子 Agent 的共同输入约定与报告原则 | `specialist-common.md` |
| 某角色独有的业务要求 | `specialists/<role>.md` |
| 工具用途和参数 | `../engine.py` 的 MAIN_TOOLS、`../services.py` 的 TOOLS、`../contracts.py` |
| 运行时快照字段 | `../engine.py` 的 initial_snapshot |
| 原生消息渲染和窗口 | `../context_window.py` |
| 当前用户、历史和持久化记录的选择 | `../services.py` 的 context、`../engine.py` 的 execute |

不要把工具完整签名、工具 schema、所有 Skill 正文、结果目录复制到提示词文件。
主规则说明决策原则，工具定义说明调用契约，Skill 按需提供业务方法。
不要在规则文件写入用户行程、来源原文、请求 ID 或本轮执行过程。

## 查看模型真正收到的内容

在启动服务的环境中设置：

```powershell
$env:HOMMEY_CONTEXT_DEBUG_DIR='D:/Hommey/tmp/debug/model-context'
```

然后沿用项目正常启动方式。此功能默认关闭；也可由运行配置的
`context_debug_dir` 指定目录。每次调用模型 SDK 前，按会话请求和角色轮次写入：

- `context.md`：按角色展开的可读消息与工具定义。
- `request.json`：经过现有敏感文本脱敏的 messages、tools、tool_choice。
- `manifest.json`：消息和工具字符数、实际已加载提示词文件的哈希。

这是 SDK 入参快照，不是抓取服务商 HTTP 请求；字符数不是 token 数。
保留脱敏后内容仍可能包含业务资料，按项目的调试文件保留规则管理目录。
提示词文件修改但进程未重启时，manifest 仍记录进程实际加载的旧版本。

详细结构和验证结果见 `../../docs/2026-10-01-native-context-implementation.md`。
