# Cinema4D MCP — Model Context Protocol (MCP) Server

Cinema4D MCP Server connects Cinema 4D to Claude, enabling prompt-assisted 3D manipulation.

## Table of Contents

- [Components](#components)
- [Prerequisites](#prerequisites)
- [Installation](#installation)
- [Setup](#setup)
- [Usage](#usage)
- [Development](#development)
- [Troubleshooting & Debugging](#troubleshooting--debugging)
- [File Structure](#file-structure)
- [Tool Commands](#tool-commands)

## Components

1. **C4D Plugin**: A socket server that listens for commands from the MCP server and executes them in the Cinema 4D environment.
2. **MCP Server**: A Python server that implements the MCP protocol and provides tools for Cinema 4D integration.

## Prerequisites

- Cinema 4D (R2024+ recommended)
- Python 3.10 or higher (for the MCP Server component)

## Installation

To install the project, follow these steps:

### Clone the Repository

```bash
git clone https://github.com/ttiimmaacc/cinema4d-mcp.git
cd cinema4d-mcp
```

### Install the MCP Server Package

```bash
pip install -e .
```

### Make the Wrapper Script Executable

```bash
chmod +x bin/cinema4d-mcp-wrapper
```

## Setup

### Cinema 4D Plugin Setup

To set up the Cinema 4D plugin, follow these steps:

1. **Copy the Plugin File**: Copy the `c4d_plugin/mcp_server_plugin.pyp` file to Cinema 4D's plugin folder. The path varies depending on your operating system:

   - macOS: `/Users/USERNAME/Library/Preferences/Maxon/Maxon Cinema 4D/plugins/`
   - Windows: `C:\Users\USERNAME\AppData\Roaming\Maxon\Maxon Cinema 4D\plugins\`

2. **Start the Socket Server**:
   - Open Cinema 4D.
   - Go to Extensions > Socket Server Plugin
   - You should see a Socket Server Control dialog window. Click Start Server.

### Claude Desktop Configuration

To configure Claude Desktop, you need to modify its configuration file:

1. **Open the Configuration File**:

   - macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
   - Windows: `%APPDATA%\Claude\claude_desktop_config.json`
   - Alternatively, use the Settings menu in Claude Desktop (Settings > Developer > Edit Config).

2. **Add MCP Server Configuration**:
   For development/unpublished server, add the following configuration:
   ```json
   "mcpServers": {
     "cinema4d": {
       "command": "python3",
       "args": ["/Users/username/cinema4d-mcp/main.py"]
     }
   }
   ```
3. **Restart Claude Desktop** after updating the configuration file.
<details>

  <summary>[TODO] For published server</summary>

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

## Usage

1. Ensure the Cinema 4D Socket Server is running.
2. Open Claude Desktop and look for the hammer icon 🔨 in the input box, indicating MCP tools are available.
3. Use the available [Tool Commands](#tool-commands) to interact with Cinema 4D through Claude.

## Testing

### Command Line Testing

To test the Cinema 4D socket server directly from the command line:

```bash
python main.py
```

You should see output confirming the server's successful start and connection to Cinema 4D.

### Testing with MCP Test Harness

The repository includes a simple test harness for running predefined command sequences:

1. **Test Command File** (`tests/mcp_test_harness.jsonl`): Contains a sequence of commands in JSONL format that can be executed in order. Each line represents a single MCP command with its parameters.

2. **GUI Test Runner** (`tests/mcp_test_harness_gui.py`): A simple Tkinter GUI for running the test commands:

   ```bash
   python tests/mcp_test_harness_gui.py
   ```

   The GUI allows you to:

   - Select a JSONL test file
   - Run the commands in sequence
   - View the responses from Cinema 4D

This test harness is particularly useful for:

- Rapidly testing new commands
- Verifying plugin functionality after updates
- Recreating complex scenes for debugging
- Testing compatibility across different Cinema 4D versions

## Troubleshooting & Debugging

1. Check the log files:

   ```bash
   tail -f ~/Library/Logs/Claude/mcp*.log
   ```

2. Verify Cinema 4D shows connections in its console after you open Claude Desktop.

3. Test the wrapper script directly:

   ```bash
   cinema4d-mcp-wrapper
   ```

4. If there are errors finding the mcp module, install it system-wide:

   ```bash
   pip install mcp
   ```

5. For advanced debugging, use the [MCP Inspector](https://github.com/modelcontextprotocol/inspector):
   ```bash
   npx @modelcontextprotocol/inspector uv --directory /Users/username/cinema4d-mcp run cinema4d-mcp
   ```

## Project File Structure

```
cinema4d-mcp/
├── .gitignore
├── LICENSE
├── README.md
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

## Tool Commands

### General Scene & Execution

- `get_scene_info`: Get summary info about the active Cinema 4D scene. ✅
- `list_objects`: List all scene objects (with hierarchy). ✅
- `group_objects`: Group selected objects under a new null. ✅
- `execute_python`: Execute custom Python code inside Cinema 4D. ✅
- `save_scene`: Save the current Cinema 4D project to disk. ✅
- `load_scene`: Load a `.c4d` file into the scene. ✅
- `set_keyframe`: Set a keyframe on an objects property (position, rotation, etc.). ✅

### Object Creation & Modification

- `add_primitive`: Add a primitive (cube, sphere, cone, etc.) to the scene. ✅
- `modify_object`: Modify transform or attributes of an existing object. ✅
- `create_abstract_shape`: Create an organic, non-standard abstract form. ✅

### Cameras & Animation

- `create_camera`: Add a new camera to the scene. ✅
- `animate_camera`: Animate a camera along a path (linear or spline-based). ✅

### Lighting & Materials

- `create_light`: Add a light (omni, spot, etc.) to the scene. ✅
- `create_material`: Create a standard Cinema 4D material. ✅
- `apply_material`: Apply a material to a target object. ✅
- `apply_shader`: Generate and apply a stylized or procedural shader. ✅

### Redshift Support

- `validate_redshift_materials`: Check Redshift material setup and connections. ✅ ⚠️ (Redshift materials not fully implemented)

### MoGraph & Fields

- `create_mograph_cloner`: Add a MoGraph Cloner (linear, radial, grid, etc.). ✅
- `add_effector`: Add a MoGraph Effector (Random, Plain, etc.). ✅
- `apply_mograph_fields`: Add and link a MoGraph Field to objects. ✅

### Dynamics & Physics

- `create_soft_body`: Add a Soft Body tag to an object. ✅
- `apply_dynamics`: Apply Rigid or Soft Body physics. ✅

### Rendering & Preview

- `render_frame`: Render a frame and save it to disk (file-based output only). ⚠️ (Works, but fails on large resolutions due to MemoryError: Bitmap Init failed. This is a resource limitation.)
- `render_preview`: Render a quick preview and return base64 image (for AI). ✅
- `snapshot_scene`: Capture a snapshot of the scene (objects + preview image). ✅

## Compatibility Plan & Roadmap

| Cinema 4D Version | Python Version | Compatibility Status | Notes                                             |
| ----------------- | -------------- | -------------------- | ------------------------------------------------- |
| R21 / S22         | Python 2.7     | ❌ Not supported     | Legacy API and Python version too old             |
| R23               | Python 3.7     | 🔍 Not planned       | Not currently tested                              |
| S24 / R25 / S26   | Python 3.9     | ⚠️ Possible (TBD)    | Requires testing and fallbacks for missing APIs   |
| 2023.0 / 2023.1   | Python 3.9     | 🧪 In progress       | Targeting fallback support for core functionality |
| 2023.2            | Python 3.10    | 🧪 In progress       | Aligns with planned testing base                  |
| 2024.0            | Python 3.11    | ✅ Supported         | Verified                                          |
| 2025.0+           | Python 3.11    | ✅ Fully Supported   | Primary development target                        |

### Compatibility Goals

- **Short Term**: Ensure compatibility with C4D 2023.1+ (Python 3.9 and 3.10)
- **Mid Term**: Add conditional handling for missing MoGraph and Field APIs
- **Long Term**: Consider optional legacy plugin module for R23–S26 support if demand arises

## Recent Fixes

- Context Awareness: Implemented robust object tracking using GUIDs. Commands creating objects return context (guid, actual_name, etc.). Subsequent commands correctly use GUIDs passed by the test harness/server to find objects reliably.
- Object Finding: Reworked find_object_by_name to correctly handle GUIDs (numeric string format), fixed recursion errors, and improved reliability when doc.SearchObject fails.
- GUID Detection: Command handlers (apply_material, create_mograph_cloner, add_effector, apply_mograph_fields, set_keyframe, group_objects) now correctly detect if identifiers passed in various parameters (object_name, target, target_name, list items) are GUIDs and search accordingly.
- create_mograph_cloner: Fixed AttributeError for missing MoGraph parameters (like MG_LINEAR_PERSTEP) by using getattr fallbacks. Fixed logic bug where the found object wasn't correctly passed for cloning.
- Rendering: Fixed TypeError in render_frame related to doc.ExecutePasses. snapshot_scene now correctly uses the working base64 render logic. Large render_frame still faces memory limits.
- Registration: Fixed AttributeError for c4d.NilGuid.
