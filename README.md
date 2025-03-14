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

- Cinema 4D
- Python 3.10 or higher

## Development Installation

To install the project, follow these steps:

### Clone the Repository

```bash
git clone https://github.com/ttiimmaacc/cinema4d-mcp.git
cd cinema4d-mcp
```

### Install the Package

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

1. **Copy the Plugin File**: Copy the `c4d_plugins/cinema4d_socket_plugin.py` file to Cinema 4D's scripts folder. The path varies depending on your operating system:
   - macOS: `/Users/USERNAME/Library/Preferences/Maxon/Maxon Cinema 4D RXXX_XXXXXXXX/library/scripts/`
   - Windows: `C:\Users\USERNAME\AppData\Roaming\Maxon\Maxon Cinema 4D RXXX_XXXXXXXX\library\scripts\`

2. **Start the Socket Server**:
   - Open Cinema 4D.
   - Open the Script Manager and Console.
   - Find and run `cinema4d_socket_plugin.py`.
   - You should see a message in the console indicating that the socket server has started.

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

## Test directly from the command line

To test the Cinema 4D socket server directly from the command line:

```bash
python main.py
```
---
You should see output confirming the server's successful start and connection to Cinema 4D.

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
├── c4d_plugins/
│   └── cinema4d_socket_plugin.py
├── src/
│   └── cinema4d_mcp/
│       ├── __init__.py
│       ├── server.py
│       ├── config.py
│       └── utils.py
└── tests/
    └── test_server.py
```

## Tool Commands

- `add_primitive`: Add a primitive object to the Cinema 4D scene.
- `apply_material`: Apply a material to an object.
- `create_material`: Create a new material in Cinema 4D.
- `execute_python_script`: Execute a Python script in Cinema 4D.
- `get_scene_info`: Get information about the current Cinema 4D scene.
- `list_objects`: List all objects in the current Cinema 4D scene.
- `load_scene`: Load a Cinema 4D scene file.
- `modify_object`: Modify properties of an existing object.
- `render_frame`: Render the current frame.
- `save_scene`: Save the current Cinema 4D scene.
- `set_keyframe`: Set a keyframe for an object property.
