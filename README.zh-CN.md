# Cinema4D MCP - 模型上下文协议（MCP）服务器

[English](README.md)

Cinema4D MCP Server 将 Cinema 4D 连接到 Claude，让你可以通过自然语言提示辅助完成 3D 场景操作。

## 目录

- [组成部分](#组成部分)
- [前置条件](#前置条件)
- [安装](#安装)
- [设置](#设置)
- [使用](#使用)
- [Agent Skill](#agent-skill)
- [测试](#测试)
- [排错与调试](#排错与调试)
- [项目文件结构](#项目文件结构)
- [工具命令](#工具命令)
- [兼容性计划与路线图](#兼容性计划与路线图)
- [近期修复](#近期修复)
- [使用指南](docs/USAGE_GUIDE.md)

## 组成部分

1. **C4D Plugin**：一个 socket 服务器，接收 MCP 服务器的命令，并在 Cinema 4D 环境中执行。
2. **MCP Server**：一个实现 MCP 协议的 Python 服务器，为 Cinema 4D 集成提供工具。

## 前置条件

- Cinema 4D（建议 R2024 或更高版本）
- Python 3.10 或更高版本（用于 MCP Server 部分）

## 安装

按以下步骤安装项目。

### 克隆仓库

```bash
git clone https://github.com/ttiimmaacc/cinema4d-mcp.git
cd cinema4d-mcp
```

### 安装 MCP Server 包

```bash
pip install -e .
```

### 赋予包装脚本可执行权限

```bash
chmod +x bin/cinema4d-mcp-wrapper
```

## 设置

### Cinema 4D 插件设置

按以下步骤设置 Cinema 4D 插件。

1. **复制插件文件**：将 `c4d_plugin/mcp_server_plugin.pyp` 复制到 Cinema 4D 的插件文件夹。不同操作系统的路径不同：

   - macOS：`/Users/USERNAME/Library/Preferences/Maxon/Maxon Cinema 4D/plugins/`
   - Windows：`C:\\Users\\USERNAME\\AppData\\Roaming\\Maxon\\Maxon Cinema 4D\\plugins\\`

2. **启动 Socket Server**：
   - 打开 Cinema 4D。
   - 前往 `Extensions > Socket Server Plugin`。
   - 应会看到 Socket Server Control 对话框，点击 `Start Server`。

### Claude Desktop 配置

要配置 Claude Desktop，需要修改它的配置文件。

1. **打开配置文件**：

   - macOS：`~/Library/Application Support/Claude/claude_desktop_config.json`
   - Windows：`%APPDATA%\\Claude\\claude_desktop_config.json`
   - 也可以通过设置菜单进入：`Settings > Developer > Edit Config`。

2. **添加 MCP Server 配置**：

   对于开发中或尚未发布的服务器，添加以下配置：

   ```json
   "mcpServers": {
     "cinema4d": {
       "command": "python3",
       "args": ["/Users/username/cinema4d-mcp/main.py"]
     }
   }
   ```

3. 更新配置后，**重启 Claude Desktop**。

<details>
  <summary>[TODO] 已发布服务器的配置</summary>

```json
{
  "mcpServers": {
    "cinema4d": {
      "command": "cinema4d-mcp-wrapper",
      "args": []
    }
  }
}
```

</details>

## 使用

1. 确认 Cinema 4D 的 Socket Server 正在运行。
2. 打开 Claude Desktop，在输入框中确认出现锤子图标，表示 MCP 工具已可用。
3. 使用下方的[工具命令](#工具命令)，通过 Claude 与 Cinema 4D 交互。

## Agent Skill

若你使用 agent skills，这个 MCP 对应的维护中 Skill 位于 [vladmdgolam/agent-skills](https://github.com/vladmdgolam/agent-skills/tree/main/skills/cinema4d-mcp)。

该 Skill 在原始 MCP 工具之上补充了更贴近制作流程的指导，包括：

- 何时应优先用 `inspect_redshift_materials` 检查 Redshift，而不是临时编写 Python
- 何时应回退到 `execute_python_script`，以使用完整的 C4D API
- 当运行环境或节点空间不可用时，Redshift 当前的限制
- 实用的 MoGraph 提取与调试流程

## 测试

### 命令行测试

可直接在命令行测试 Cinema 4D socket server：

```bash
python main.py
```

正常情况下会看到服务器启动成功并连接到 Cinema 4D 的确认信息。

### 使用 MCP Test Harness 测试

仓库包含一个简单的测试工具，可按顺序运行预定义命令：

1. **测试命令文件**（`tests/mcp_test_harness.jsonl`）：包含一系列按顺序执行的 JSONL 命令；每一行代表一个 MCP 命令及其参数。
2. **图形化测试工具**（`tests/mcp_test_harness_gui.py`）：一个用于运行测试命令的简单 Tkinter GUI：

   ```bash
   python tests/mcp_test_harness_gui.py
   ```

   此 GUI 可以：

   - 选择 JSONL 测试文件
   - 依次运行命令
   - 查看 Cinema 4D 的返回结果

该测试工具尤其适合：

- 快速测试新命令
- 验证插件功能
- 为调试复现场景
- 测试不同 Cinema 4D 版本的兼容性

## 排错与调试

1. 检查日志文件：

   ```bash
   tail -f ~/Library/Logs/Claude/mcp*.log
   ```

2. 打开 Claude Desktop 后，确认 Cinema 4D 控制台显示连接信息。
3. 直接测试包装脚本：

   ```bash
   cinema4d-mcp-wrapper
   ```

4. 若报错提示找不到 `mcp` 模块，可进行系统范围安装：

   ```bash
   pip install mcp
   ```

5. 需要深入调试时，使用 [MCP Inspector](https://github.com/modelcontextprotocol/inspector)：

   ```bash
   npx @modelcontextprotocol/inspector uv --directory /Users/username/cinema4d-mcp run cinema4d-mcp
   ```

## 项目文件结构

```
cinema4d-mcp/
├── .gitignore
├── LICENSE
├── README.md
├── README.zh-CN.md
├── main.py
├── pyproject.toml
├── setup.py
├── bin/
│   └── cinema4d-mcp-wrapper
├── c4d_plugin/
│   └── mcp_server_plugin.pyp
├── src/
│   └── cinema4d_mcp/
│       ├── __init__.py
│       ├── server.py
│       ├── config.py
│       └── utils.py
└── tests/
    ├── test_server.py
    ├── mcp_test_harness.jsonl
    └── mcp_test_harness_gui.py
```

## 工具命令

### 通用场景与执行

- `get_scene_info`：获取当前活动场景的摘要信息。可用。
- `list_objects`：列出全部场景对象及层级关系。可用。
- `group_objects`：将选中的对象归入新的 Null 对象。可用。
- `execute_python`：在 Cinema 4D 内执行自定义 Python 代码。可用。
- `save_scene`：将当前 Cinema 4D 项目保存到磁盘。可用。
- `load_scene`：将 `.c4d` 文件载入场景。可用。
- `set_keyframe`：为对象属性（位置、旋转等）设置关键帧。可用。

### 对象创建与修改

- `add_primitive`：向场景添加基础体（立方体、球体、圆锥等）。可用。
- `modify_object`：修改已有对象的变换或属性。可用。
- `create_abstract_shape`：创建有机、非标准的抽象形体。可用。

### 相机与动画

- `create_camera`：添加新相机。可用。
- `animate_camera`：让相机沿路径（线性或样条）运动。可用。

### 灯光与材质

- `create_light`：添加灯光（全向灯、聚光灯等）。可用。
- `create_material`：创建标准 Cinema 4D 材质。可用。
- `apply_material`：将材质应用到目标对象。可用。
- `apply_shader`：生成并应用风格化或程序化着色器。可用。

### Redshift 支持

- `inspect_redshift_materials`：只读检查 Redshift 材质；包含分配关系、预览颜色、可读取参数、类似 `renderEngine` 的节点材质探测，以及通过 `redshift.GetRSMaterialNodeMaster(...)` 使用 Redshift GraphView 的回退方式。可用。已知情况：即使单个材质的 GraphView 回退成功，某些版本中的顶层 `capabilities.redshift_module_available` 标记仍可能为 `false`。应以每个材质的 `graph.backend` 和 `graph.graphview.redshift_module_imported` 为准。
- `validate_redshift_materials`：检查 Redshift 材质的设置和连接。可用，但 Redshift 材质尚未完全实现。

### MoGraph 与 Fields

- `create_mograph_cloner`：添加 MoGraph Cloner（线性、放射、网格等）。可用。
- `add_effector`：添加 MoGraph Effector（Random、Plain 等）。可用。
- `apply_mograph_fields`：向对象添加并连接 MoGraph Field。可用。

### 动力学与物理

- `create_soft_body`：向对象添加 Soft Body 标签。可用。
- `apply_dynamics`：应用刚体或柔体物理效果。可用。

### 渲染与预览

- `render_frame`：渲染一帧并保存到磁盘，仅支持基于文件的输出。可用；但大分辨率下会因 `MemoryError: Bitmap Init failed` 失败，这是资源限制。
- `render_preview`：渲染快速预览，并返回 base64 图像供 AI 使用。可用。
- `snapshot_scene`：获取场景快照（对象与预览图像）。可用。

## 兼容性计划与路线图

| Cinema 4D 版本 | Python 版本 | 兼容状态 | 备注 |
| --- | --- | --- | --- |
| R21 / S22 | Python 2.7 | 不支持 | 旧版 API 与 Python 版本过低 |
| R23 | Python 3.7 | 未计划 | 当前未测试 |
| S24 / R25 / S26 | Python 3.9 | 可能（待定） | 需要测试并为缺失 API 加入回退处理 |
| 2023.0 / 2023.1 | Python 3.9 | 进行中 | 目标是为核心功能提供回退支持 |
| 2023.2 | Python 3.10 | 进行中 | 与计划的测试基础保持一致 |
| 2024.0 | Python 3.11 | 已支持 | 已验证 |
| 2025.0+ | Python 3.11 | 完全支持 | 主要开发目标 |

### 兼容性目标

- **短期**：确保兼容 C4D 2023.1+（Python 3.9 与 3.10）。
- **中期**：为缺失的 MoGraph 和 Field API 增加条件处理。
- **长期**：若有需求，考虑为 R23-S26 提供可选的旧版插件模块。

## 近期修复

- **上下文感知**：通过 GUID 实现了稳健的对象追踪。创建对象的命令会返回上下文（`guid`、实际名称等）；后续命令可正确使用测试工具或服务器传入的 GUID，可靠地找到对象。
- **对象查找**：重新实现 `find_object_by_name`，可正确处理 GUID（数字字符串格式），修复递归错误，并提高 `doc.SearchObject` 查找失败时的可靠性。
- **GUID 检测**：命令处理器（`apply_material`、`create_mograph_cloner`、`add_effector`、`apply_mograph_fields`、`set_keyframe`、`group_objects`）现在能正确检测不同参数（`object_name`、`target`、`target_name`、列表项）中的 GUID 并进行查找。
- **create_mograph_cloner**：通过 `getattr` 回退处理缺失的 MoGraph 参数（如 `MG_LINEAR_PERSTEP`），并修复找到的对象未被正确传入克隆流程的逻辑问题。
- **渲染**：修复 `render_frame` 中与 `doc.ExecutePasses` 有关的 `TypeError`；`snapshot_scene` 现在正确使用可工作的 base64 渲染逻辑。大尺寸 `render_frame` 仍受内存限制。
- **注册**：修复 `c4d.NilGuid` 的 `AttributeError`。

---

中文文档贡献：[@truman-t3](https://github.com/truman-t3)
