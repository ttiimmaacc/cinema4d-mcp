# cinema4d_mcp/server.py
import socket
import json
from dataclasses import dataclass
from mcp.server.fastmcp import FastMCP, Context
from typing import Any, Dict, List, Optional, Union
from starlette.routing import Route
from starlette.responses import JSONResponse
from contextlib import asynccontextmanager

# Configuration for Cinema 4D connection
C4D_HOST = '127.0.0.1'  # localhost
C4D_PORT = 5555  # default port, can be changed

@dataclass
class C4DConnection:
    sock: Optional[socket.socket] = None
    connected: bool = False

# Asynchronous context manager for Cinema 4D connection
@asynccontextmanager
async def c4d_connection_context():
    """Asynchronous context manager for Cinema 4D connection."""
    connection = C4DConnection()
    try:
        # Initialize connection to Cinema 4D
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.connect((C4D_HOST, C4D_PORT))
        connection.sock = sock
        connection.connected = True
        print(f"✅ Connected to Cinema 4D at {C4D_HOST}:{C4D_PORT}")
        yield connection  # Yield the connection
    except Exception as e:
        print(f"❌ Failed to connect to Cinema 4D: {str(e)}")
        connection.connected = False  # Ensure connection is marked as not connected
        yield connection  # Still yield the connection object
    finally:
        # Clean up on server shutdown
        if connection.sock:
            connection.sock.close()
            print("🔌 Disconnected from Cinema 4D")

def send_to_c4d(connection: C4DConnection, command: Dict[str, Any]) -> Dict[str, Any]:
    """Send a command to Cinema 4D and get the response."""
    if not connection.connected or not connection.sock:
        return {"error": "Not connected to Cinema 4D"}
    
    try:
        # Convert command to JSON and send it
        command_json = json.dumps(command) + "\n"  # Add newline as message delimiter
        connection.sock.sendall(command_json.encode('utf-8'))
        
        # Receive response
        response_data = b""
        while True:
            chunk = connection.sock.recv(4096)
            if not chunk:
                break
            response_data += chunk
            if b'\n' in chunk:  # Message complete when we see a newline
                break
        
        # Parse and return response
        response_text = response_data.decode('utf-8').strip()
        return json.loads(response_text)
    
    except Exception as e:
        return {"error": f"Communication error: {str(e)}"}

async def homepage(request):
    return JSONResponse({'hello': 'world'})

routes = [
    Route("/", endpoint=homepage)
]

# Initialize our FastMCP server
mcp = FastMCP(
    title="Cinema4D",
    routes=routes
)

@mcp.tool()
async def get_scene_info(ctx: Context) -> str:
    """Get information about the current Cinema 4D scene."""
    async with c4d_connection_context() as connection:
        if not connection.connected:
            return "❌ Not connected to Cinema 4D"
        
        response = send_to_c4d(connection, {
            "command": "get_scene_info"
        })
        
        if "error" in response:
            return f"❌ Error: {response['error']}"
        
        # Format scene info nicely
        scene_info = response.get("scene_info", {})
        return f"""
    # Cinema 4D Scene Information
    - **Filename**: {scene_info.get('filename', 'Untitled')}
    - **Objects**: {scene_info.get('object_count', 0)}
    - **Polygons**: {scene_info.get('polygon_count', 0):,}
    - **Materials**: {scene_info.get('material_count', 0)}
    - **Current Frame**: {scene_info.get('current_frame', 0)}
    - **FPS**: {scene_info.get('fps', 30)}
    - **Frame Range**: {scene_info.get('frame_start', 0)} - {scene_info.get('frame_end', 90)}
    """

@mcp.tool()
async def add_primitive(primitive_type: str, name: Optional[str] = None, position: Optional[List[float]] = None, 
                    size: Optional[List[float]] = None, ctx: Context = None) -> str:
    """
    Add a primitive object to the Cinema 4D scene.
    
    Args:
        primitive_type: Type of primitive (cube, sphere, cone, cylinder, etc.)
        name: Optional name for the new object
        position: Optional [x, y, z] position
        size: Optional [x, y, z] size or dimensions
    """
    async with c4d_connection_context() as connection:
        if not connection.connected:
            return "❌ Not connected to Cinema 4D"
        
        # Prepare command
        command = {
            "command": "add_primitive",
            "type": primitive_type,
        }
        
        if name:
            command["name"] = name
        if position:
            command["position"] = position
        if size:
            command["size"] = size
        
        # Send command to Cinema 4D
        response = send_to_c4d(connection, command)
        
        if "error" in response:
            return f"❌ Error: {response['error']}"
        
        object_info = response.get("object", {})
        return f"""
    ✅ Added {primitive_type} to scene
    - **Name**: {object_info.get('name', primitive_type)}
    - **ID**: {object_info.get('id', 'Unknown')}
    - **Position**: {object_info.get('position', [0, 0, 0])}
    """

@mcp.tool()
async def modify_object(object_name: str, properties: Dict[str, Any], ctx: Context) -> str:
    """
    Modify properties of an existing object.
    
    Args:
        object_name: Name of the object to modify
        properties: Dictionary of properties to modify (position, rotation, scale, etc.)
    """
    async with c4d_connection_context() as connection:
        if not connection.connected:
            return "❌ Not connected to Cinema 4D"
        
        # Send command to Cinema 4D
        response = send_to_c4d(connection, {
            "command": "modify_object",
            "object_name": object_name,
            "properties": properties
        })
        
        if "error" in response:
            return f"❌ Error: {response['error']}"
        
        # Generate summary of what was modified
        modified_props = []
        for prop, value in properties.items():
            modified_props.append(f"- **{prop}**: {value}")
        
        return f"""
    ✅ Modified object: {object_name}
    {chr(10).join(modified_props)}
    """

@mcp.tool()
async def list_objects(ctx: Context) -> str:
    """List all objects in the current Cinema 4D scene."""
    async with c4d_connection_context() as connection:
        if not connection.connected:
            return "❌ Not connected to Cinema 4D"
        
        response = send_to_c4d(connection, {
            "command": "list_objects"
        })
        
        if "error" in response:
            return f"❌ Error: {response['error']}"
        
        objects = response.get("objects", [])
        if not objects:
            return "No objects found in the scene."
        
        # Format objects as a list
        object_list = []
        for obj in objects:
            object_list.append(f"- **{obj['name']}** ({obj['type']})")
        
        return f"""
    # Objects in Scene ({len(objects)})
    {chr(10).join(object_list)}
    """

@mcp.tool()
async def create_material(name: str, color: Optional[List[float]] = None, 
                    properties: Optional[Dict[str, Any]] = None, ctx: Context = None) -> str:
    """
    Create a new material in Cinema 4D.
    
    Args:
        name: Name for the new material
        color: Optional [R, G, B] color (values 0-1)
        properties: Optional additional material properties
    """
    async with c4d_connection_context() as connection:
        if not connection.connected:
            return "❌ Not connected to Cinema 4D"
        
        # Prepare command
        command = {
            "command": "create_material",
            "name": name
        }
        
        if color:
            command["color"] = color
        if properties:
            command["properties"] = properties
        
        # Send command to Cinema 4D
        response = send_to_c4d(connection, command)
        
        if "error" in response:
            return f"❌ Error: {response['error']}"
        
        material_info = response.get("material", {})
        return f"""
    ✅ Created material: {name}
    - **ID**: {material_info.get('id', 'Unknown')}
    - **Color**: {material_info.get('color', [1, 1, 1])}
    """

@mcp.tool()
async def apply_material(material_name: str, object_name: str, ctx: Context) -> str:
    """
    Apply a material to an object.
    
    Args:
        material_name: Name of the material to apply
        object_name: Name of the object to apply the material to
    """
    async with c4d_connection_context() as connection:
        if not connection.connected:
            return "❌ Not connected to Cinema 4D"
        
        # Send command to Cinema 4D
        response = send_to_c4d(connection, {
            "command": "apply_material",
            "material_name": material_name,
            "object_name": object_name
        })
        
        if "error" in response:
            return f"❌ Error: {response['error']}"
        
        return f"✅ Applied material '{material_name}' to object '{object_name}'"

@mcp.tool()
async def render_frame(output_path: Optional[str] = None, width: Optional[int] = None, 
                    height: Optional[int] = None, ctx: Context = None) -> str:
    """
    Render the current frame.
    
    Args:
        output_path: Optional path to save the rendered image
        width: Optional render width in pixels
        height: Optional render height in pixels
    """
    async with c4d_connection_context() as connection:
        if not connection.connected:
            return "❌ Not connected to Cinema 4D"
        
        # Prepare command
        command = {
            "command": "render_frame"
        }
        
        if output_path:
            command["output_path"] = output_path
        if width:
            command["width"] = width
        if height:
            command["height"] = height
        
        # Send command to Cinema 4D
        response = send_to_c4d(connection, command)
        
        if "error" in response:
            return f"❌ Error: {response['error']}"
        
        render_info = response.get("render_info", {})
        return f"""
    ✅ Rendered frame
    - **Path**: {render_info.get('path', 'Unknown')}
    - **Resolution**: {render_info.get('width', 0)} x {render_info.get('height', 0)}
    - **Render Time**: {render_info.get('render_time', 0):.2f} seconds
    """

@mcp.tool()
async def set_keyframe(object_name: str, property_name: str, value: Any, frame: int, ctx: Context) -> str:
    """
    Set a keyframe for an object property.
    
    Args:
        object_name: Name of the object
        property_name: Name of the property to keyframe (e.g., 'position.x')
        value: Value to set at the keyframe
        frame: Frame number to set the keyframe at
    """
    async with c4d_connection_context() as connection:
        if not connection.connected:
            return "❌ Not connected to Cinema 4D"
        
        # Send command to Cinema 4D
        response = send_to_c4d(connection, {
            "command": "set_keyframe",
            "object_name": object_name,
            "property_name": property_name,
            "value": value,
            "frame": frame
        })
        
        if "error" in response:
            return f"❌ Error: {response['error']}"
        
        return f"✅ Set keyframe for '{object_name}.{property_name}' = {value} at frame {frame}"

@mcp.tool()
async def save_scene(file_path: Optional[str] = None, ctx: Context = None) -> str:
    """
    Save the current Cinema 4D scene.
    
    Args:
        file_path: Optional path to save the scene to
    """
    async with c4d_connection_context() as connection:
        if not connection.connected:
            return "❌ Not connected to Cinema 4D"
        
        # Prepare command
        command = {
            "command": "save_scene"
        }
        
        if file_path:
            command["file_path"] = file_path
        
        # Send command to Cinema 4D
        response = send_to_c4d(connection, command)
        
        if "error" in response:
            return f"❌ Error: {response['error']}"
        
        save_info = response.get("save_info", {})
        return f"✅ Scene saved to: {save_info.get('path', 'Default location')}"

@mcp.tool()
async def load_scene(file_path: str, ctx: Context) -> str:
    """
    Load a Cinema 4D scene file.
    
    Args:
        file_path: Path to the scene file to load
    """
    async with c4d_connection_context() as connection:
        if not connection.connected:
            return "❌ Not connected to Cinema 4D"
        
        # Send command to Cinema 4D
        response = send_to_c4d(connection, {
            "command": "load_scene",
            "file_path": file_path
        })
        
        if "error" in response:
            return f"❌ Error: {response['error']}"
        
        return f"✅ Loaded scene from: {file_path}"

@mcp.tool()
async def execute_python_script(script: str, ctx: Context) -> str:
    """
    Execute a Python script in Cinema 4D.
    
    Args:
        script: Python code to execute in Cinema 4D
    """
    async with c4d_connection_context() as connection:
        if not connection.connected:
            return "❌ Not connected to Cinema 4D"
        
        # Send command to Cinema 4D
        response = send_to_c4d(connection, {
            "command": "execute_python",
            "script": script
        })
        
        if "error" in response:
            return f"❌ Error: {response['error']}"
        
        result = response.get("result", "No output")
        return f"""
    ✅ Python script executed
    **Output**:
    {result}
    """

@mcp.resource("c4d://primitives")
def get_primitives_info() -> str:
    """Get information about available Cinema 4D primitives."""
    # This is static documentation that doesn't need the Cinema 4D connection
    return """
# Cinema 4D Primitive Objects

## Cube
- **Parameters**: size, segments

## Sphere
- **Parameters**: radius, segments

## Cylinder
- **Parameters**: radius, height, segments

## Cone
- **Parameters**: radius, height, segments

## Plane
- **Parameters**: width, height, segments

## Torus
- **Parameters**: outer radius, inner radius, segments

## Pyramid
- **Parameters**: width, height, depth

## Platonic
- **Parameters**: radius, type (tetrahedron, hexahedron, octahedron, dodecahedron, icosahedron)
"""

@mcp.resource("c4d://material_types")
def get_material_types() -> str:
    """Get information about available Cinema 4D material types and their properties."""
    # Static documentation about material types
    return """
# Cinema 4D Material Types

## Standard Material
- **Color**: Base diffuse color
- **Specular**: Highlight color and intensity
- **Reflection**: Surface reflectivity
- **Transparency**: Surface transparency
- **Bump**: Surface bumpiness or displacement

## Physical Material
- **Base Color**: Main surface color
- **Specular**: Surface glossiness and reflectivity
- **Roughness**: Surface irregularity
- **Metallic**: Metal-like properties
- **Transparency**: Light transmission properties
- **Emission**: Self-illumination properties
- **Normal**: Surface detail without geometry
- **Displacement**: Surface geometry modification
"""

@mcp.resource("c4d://status")
def get_connection_status() -> str:
    """Get the current connection status to Cinema 4D."""
    return """
# Cinema 4D Connection Status
Connection Status needs to be implemented!
"""

mcp_app = mcp

def main():
    """Main entry point for the server module."""
    # Run the server using stdio transport by default
    mcp_app.run()

if __name__ == "__main__":
    main()