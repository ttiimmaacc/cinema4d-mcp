"""
Cinema 4D MCP Server Plugin
Updated for Cinema 4D R2025.1 compatibility
Version 0.1.9 - Fixed plugin registration for R2025.1
"""

import c4d
from c4d import gui
import socket
import threading
import json
import time
import queue
import os
import sys

PLUGIN_ID = 1057843  # Unique plugin ID for SpecialEventAdd

# Check Cinema 4D version and log compatibility info
C4D_VERSION = c4d.GetC4DVersion()
C4D_VERSION_MAJOR = C4D_VERSION // 1000
C4D_VERSION_MINOR = (C4D_VERSION // 100) % 10
print(f"[C4D MCP] Running on Cinema 4D R{C4D_VERSION_MAJOR}{C4D_VERSION_MINOR}")
print(f"[C4D MCP] Python version: {sys.version}")

# Warn if using unsupported version
if C4D_VERSION_MAJOR < 20:
    print(
        "[C4D MCP] WARNING: This plugin is designed for Cinema 4D 2025 or later. Some features may not work correctly."
    )


class C4DSocketServer(threading.Thread):
    """Socket Server running in a background thread, sending logs & status via queue."""

    def __init__(self, msg_queue, host="127.0.0.1", port=5555):
        super(C4DSocketServer, self).__init__()
        self.host = host
        self.port = port
        self.socket = None
        self.running = False
        self.msg_queue = msg_queue  # Queue to communicate with UI
        self.daemon = True  # Ensures cleanup on shutdown

    def log(self, message):
        """Send log messages to UI via queue and trigger an event."""
        self.msg_queue.put(("LOG", message))
        c4d.SpecialEventAdd(PLUGIN_ID)  # Notify UI thread

    def update_status(self, status):
        """Update status via queue and trigger an event."""
        self.msg_queue.put(("STATUS", status))
        c4d.SpecialEventAdd(PLUGIN_ID)

    def execute_on_main_thread(self, func, *args, **kwargs):
        """Execute a function on the main thread using a thread-safe queue and special event."""
        # Extract the timeout parameter if provided, or use default
        timeout = kwargs.pop("_timeout", None)

        # Set appropriate timeout based on operation type
        if timeout is None:
            # Use different default timeouts based on the function name
            func_name = func.__name__ if hasattr(func, "__name__") else str(func)

            if "render" in func_name.lower():
                timeout = 120  # 2 minutes for rendering
                self.log(f"[C4D] Using extended timeout (120s) for rendering operation")
            elif "save" in func_name.lower():
                timeout = 60  # 1 minute for saving
                self.log(f"[C4D] Using extended timeout (60s) for save operation")
            else:
                timeout = 15  # Default timeout increased to 15 seconds

        self.log(f"[C4D] Main thread execution will timeout after {timeout}s")

        # Create a thread-safe container for the result
        result_container = {"result": None, "done": False}

        # Define a wrapper that will be executed on the main thread
        def main_thread_exec():
            try:
                self.log(f"[C4D] Starting main thread execution")
                start_time = time.time()
                result_container["result"] = func(*args, **kwargs)
                execution_time = time.time() - start_time
                self.log(f"[C4D] Main thread execution completed in {execution_time:.2f}s")
            except Exception as e:
                self.log(f"[C4D] Error executing function on main thread: {str(e)}")
                result_container["result"] = {"error": str(e)}
            finally:
                result_container["done"] = True
            return True

        # Queue the request and signal the main thread
        self.log("[C4D] Queueing function for main thread execution")
        self.msg_queue.put(("EXEC", main_thread_exec))
        c4d.SpecialEventAdd(PLUGIN_ID)  # Notify UI thread

        # Wait for the function to complete (with timeout)
        start_time = time.time()
        poll_interval = 0.01  # Small sleep to prevent CPU overuse
        last_progress = 0

        while not result_container["done"]:
            time.sleep(poll_interval)

            # Calculate elapsed time
            elapsed = time.time() - start_time

            # Log progress periodically for long-running operations
            if int(elapsed) > last_progress:
                if elapsed > 5:  # Only start logging after 5 seconds
                    self.log(f"[C4D] Waiting for main thread execution ({elapsed:.1f}s elapsed)")
                last_progress = int(elapsed)

            # Check for timeout
            if elapsed > timeout:
                self.log(f"[C4D] Main thread execution timed out after {elapsed:.2f}s")
                return {"error": f"Execution on main thread timed out after {timeout}s"}

        return result_container["result"]

    def run(self):
        """Main server loop"""
        try:
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.socket.bind((self.host, self.port))
            self.socket.listen(5)
            self.running = True
            self.update_status("Online")
            self.log(f"[C4D] Server started on {self.host}:{self.port}")

            while self.running:
                client, addr = self.socket.accept()
                self.log(f"[C4D] Client connected from {addr}")
                threading.Thread(target=self.handle_client, args=(client,)).start()

        except Exception as e:
            self.log(f"[C4D] Server Error: {str(e)}")
            self.update_status("Offline")
            self.running = False

    def handle_client(self, client):
        """Handle incoming client connections."""
        buffer = ""
        try:
            while self.running:
                data = client.recv(4096)
                if not data:
                    break

                # Add received data to buffer
                buffer += data.decode("utf-8")

                # Process complete messages (separated by newlines)
                while "\n" in buffer:
                    message, buffer = buffer.split("\n", 1)
                    self.log(f"[C4D] Received command")

                    try:
                        # Parse the command
                        command = json.loads(message)
                        command_type = command.get("command", "")

                        # Process different command types
                        if command_type == "get_scene_info":
                            response = self.handle_get_scene_info()
                        elif command_type == "list_objects":
                            response = self.handle_list_objects()
                        elif command_type == "add_primitive":
                            response = self.handle_add_primitive(command)
                        elif command_type == "create_material":
                            response = self.handle_create_material(command)
                        elif command_type == "debug_redshift_material":
                            response = self.handle_debug_redshift_material(command)
                        elif command_type == "modify_object":
                            response = self.handle_modify_object(command)
                        elif command_type == "apply_material":
                            response = self.handle_apply_material(command) 
                        elif command_type == "set_keyframe":
                            response = self.handle_set_keyframe(command)
                        elif command_type == "render_frame":
                            response = self.handle_render_frame(command)
                        elif command_type == "save_scene":
                            response = self.handle_save_scene(command)
                        elif command_type == "load_scene":
                            response = self.handle_load_scene(command)
                        elif command_type == "create_mograph_cloner":
                            response = self.handle_create_mograph_cloner(command)
                        elif command_type == "apply_shader":
                            response = self.handle_apply_shader(command)
                        elif command_type == "execute_python":
                            response = self.handle_execute_python(command)
                        elif command_type == "add_effector":
                            response = self.handle_add_effector(command)
                        elif command_type == "create_light":
                            response = self.handle_create_light(command)
                        elif command_type == "animate_camera":
                            response = self.handle_animate_camera(command)
                        elif command_type == "apply_mograph_fields":
                            response = self.handle_apply_mograph_fields(command)
                        elif command_type == "apply_dynamics":
                            response = self.handle_apply_dynamics(command)
                        elif command_type == "create_abstract_shape":
                            response = self.handle_create_abstract_shape(command)
                        elif command_type == "create_soft_body":
                            response = self.handle_create_soft_body(command)
                        else:
                            response = {"error": f"Unknown command: {command_type}"}

                        # Send the response as JSON
                        response_json = json.dumps(response) + "\n"
                        client.sendall(response_json.encode("utf-8"))
                        self.log(f"[C4D] Sent response for {command_type}")

                    except json.JSONDecodeError:
                        error_response = {"error": "Invalid JSON format"}
                        client.sendall((json.dumps(error_response) + "\n").encode("utf-8"))
                    except Exception as e:
                        error_response = {"error": f"Error processing command: {str(e)}"}
                        client.sendall((json.dumps(error_response) + "\n").encode("utf-8"))
                        self.log(f"[C4D] Error processing command: {str(e)}")

        except Exception as e:
            self.log(f"[C4D] Client error: {str(e)}")
        finally:
            client.close()
            self.log("[C4D] Client disconnected")

    def stop(self):
        """Stop the server."""
        self.running = False
        if self.socket:
            self.socket.close()
        self.update_status("Offline")
        self.log("[C4D] Server stopped")

    def handle_get_scene_info(self):
        """Handle get_scene_info command."""
        doc = c4d.documents.GetActiveDocument()

        # Get scene information
        scene_info = {
            "filename": doc.GetDocumentName() or "Untitled",
            "object_count": self.count_objects(doc),
            "current_frame": doc.GetTime().GetFrame(doc.GetFps()),
            "fps": doc.GetFps(),
        }

        return {"scene_info": scene_info}

    def count_objects(self, doc):
        """Count all objects in the document."""
        count = 0
        obj = doc.GetFirstObject()
        while obj:
            count += 1
            obj = obj.GetNext()
        return count
        
    def set_position_keyframe(self, obj, frame, position):
        """Set a position keyframe for an object at a specific frame.

        Args:
            obj: The Cinema 4D object to keyframe
            frame: The frame number
            position: A list of [x, y, z] coordinates

        Returns:
            True if successful, False otherwise
        """
        if not obj or not isinstance(position, list) or len(position) < 3:
            self.log(f"[C4D] Invalid object or position for keyframe")
            return False

        try:
            # Get the active document and time
            doc = c4d.documents.GetActiveDocument()

            # Log what we're doing
            self.log(
                f"[C4D] Setting position keyframe for {obj.GetName()} at frame {frame} to {position}"
            )

            # Create the position vector from the list
            pos = c4d.Vector(position[0], position[1], position[2])

            # Set the object's position
            obj.SetAbsPos(pos)

            # Create track or get existing track for position
            track_x = obj.FindCTrack(
                c4d.DescID(
                    c4d.DescLevel(c4d.ID_BASEOBJECT_POSITION, c4d.DTYPE_VECTOR, 0),
                    c4d.DescLevel(c4d.VECTOR_X, c4d.DTYPE_REAL, 0),
                )
            )
            if track_x is None:
                track_x = c4d.CTrack(
                    obj,
                    c4d.DescID(
                        c4d.DescLevel(c4d.ID_BASEOBJECT_POSITION, c4d.DTYPE_VECTOR, 0),
                        c4d.DescLevel(c4d.VECTOR_X, c4d.DTYPE_REAL, 0),
                    ),
                )
                obj.InsertTrackSorted(track_x)

            track_y = obj.FindCTrack(
                c4d.DescID(
                    c4d.DescLevel(c4d.ID_BASEOBJECT_POSITION, c4d.DTYPE_VECTOR, 0),
                    c4d.DescLevel(c4d.VECTOR_Y, c4d.DTYPE_REAL, 0),
                )
            )
            if track_y is None:
                track_y = c4d.CTrack(
                    obj,
                    c4d.DescID(
                        c4d.DescLevel(c4d.ID_BASEOBJECT_POSITION, c4d.DTYPE_VECTOR, 0),
                        c4d.DescLevel(c4d.VECTOR_Y, c4d.DTYPE_REAL, 0),
                    ),
                )
                obj.InsertTrackSorted(track_y)

            track_z = obj.FindCTrack(
                c4d.DescID(
                    c4d.DescLevel(c4d.ID_BASEOBJECT_POSITION, c4d.DTYPE_VECTOR, 0),
                    c4d.DescLevel(c4d.VECTOR_Z, c4d.DTYPE_REAL, 0),
                )
            )
            if track_z is None:
                track_z = c4d.CTrack(
                    obj,
                    c4d.DescID(
                        c4d.DescLevel(c4d.ID_BASEOBJECT_POSITION, c4d.DTYPE_VECTOR, 0),
                        c4d.DescLevel(c4d.VECTOR_Z, c4d.DTYPE_REAL, 0),
                    ),
                )
                obj.InsertTrackSorted(track_z)

            # Create time object for the keyframe
            time = c4d.BaseTime(frame, doc.GetFps())

            # Set the keyframes for each axis
            curve_x = track_x.GetCurve()
            key_x = curve_x.AddKey(time)
            if key_x is not None and key_x["key"] is not None:
                key_x["key"].SetValue(curve_x, position[0])

            curve_y = track_y.GetCurve()
            key_y = curve_y.AddKey(time)
            if key_y is not None and key_y["key"] is not None:
                key_y["key"].SetValue(curve_y, position[1])

            curve_z = track_z.GetCurve()
            key_z = curve_z.AddKey(time)
            if key_z is not None and key_z["key"] is not None:
                key_z["key"].SetValue(curve_z, position[2])

            # Update the document
            c4d.EventAdd()

            self.log(
                f"[C4D] Successfully set keyframe for {obj.GetName()} at frame {frame}"
            )
            return True

        except Exception as e:
            self.log(f"[C4D] Error setting position keyframe: {str(e)}")
            return False
        
    def find_object_by_name(self, doc, name):
        """Find an object by name in the document.

        Args:
            doc: The active Cinema 4D document
            name: The name of the object to find

        Returns:
            The object if found, None otherwise
        """
        if not name:
            self.log(f"[C4D] Warning: Empty object name provided")
            return None

        # First pass: exact match
        obj = doc.GetFirstObject()
        while obj:
            if obj.GetName() == name:
                return obj
            obj = obj.GetNext()

        # Second pass: case-insensitive match (fallback)
        name_lower = name.lower()
        obj = doc.GetFirstObject()
        closest_match = None
        while obj:
            if obj.GetName().lower() == name_lower:
                closest_match = obj
                self.log(
                    f"[C4D] Found case-insensitive match for '{name}': '{obj.GetName()}'"
                )
                break
            obj = obj.GetNext()

        if closest_match:
            return closest_match

        self.log(f"[C4D] Object not found: '{name}'")
        return None
        
    def find_material_by_name(self, doc, name):
        """Find a material by name in the document.
        
        Args:
            doc: The active Cinema 4D document
            name: The name of the material to find
            
        Returns:
            The material if found, None otherwise
        """
        if not name:
            self.log("[C4D] Warning: Empty material name provided")
            return None
            
        # First pass: exact match
        materials = doc.GetMaterials()
        for mat in materials:
            if mat.GetName() == name:
                return mat
                
        # Second pass: case-insensitive match (fallback)
        name_lower = name.lower()
        for mat in materials:
            if mat.GetName().lower() == name_lower:
                self.log(f"[C4D] Found case-insensitive match for '{name}': '{mat.GetName()}'")
                return mat
                
        self.log(f"[C4D] Material not found: '{name}'")
        return None

    def handle_list_objects(self):
        """Handle list_objects command."""
        doc = c4d.documents.GetActiveDocument()
        objects = []
        
        # Get all objects
        obj = doc.GetFirstObject()
        while obj:
            obj_info = {
                "name": obj.GetName(),
                "type": obj.GetType(),
                "position": [obj.GetAbsPos().x, obj.GetAbsPos().y, obj.GetAbsPos().z],
            }
            objects.append(obj_info)
            obj = obj.GetNext()
        
        return {"objects": objects}

    def handle_add_primitive(self, command):
        """Handle add_primitive command."""
        doc = c4d.documents.GetActiveDocument()
        primitive_type = command.get("type", "cube").lower()
        name = command.get("name", primitive_type.capitalize())
        position = command.get("position", [0, 0, 0])
        size = command.get("size", [100, 100, 100])

        # Create the appropriate primitive object
        if primitive_type == "cube":
            obj = c4d.BaseObject(c4d.Ocube)
            obj[c4d.PRIM_CUBE_LEN] = c4d.Vector(*size)
        elif primitive_type == "sphere":
            obj = c4d.BaseObject(c4d.Osphere)
            obj[c4d.PRIM_SPHERE_RAD] = size[0] / 2
        elif primitive_type == "cylinder":
            obj = c4d.BaseObject(c4d.Ocylinder)
            obj[c4d.PRIM_CYLINDER_RADIUS] = size[0] / 2
            obj[c4d.PRIM_CYLINDER_HEIGHT] = size[1]
        else:
            # Default to cube if type not recognized
            obj = c4d.BaseObject(c4d.Ocube)

        # Set object name and position
        obj.SetName(name)
        if len(position) >= 3:
            obj.SetAbsPos(c4d.Vector(position[0], position[1], position[2]))

        # Insert object into document
        doc.InsertObject(obj)
        doc.SetActiveObject(obj)
        c4d.EventAdd()

        # Return information about the created object
        return {
            "object": {
                "name": obj.GetName(),
                "position": [obj.GetAbsPos().x, obj.GetAbsPos().y, obj.GetAbsPos().z],
            }
        }

    def handle_create_material(self, command):
        """Handle create_material command."""
        doc = c4d.documents.GetActiveDocument()
        
        # Extract parameters
        name = command.get("name", f"Material_{int(time.time())}")
        material_type = command.get("type", "standard").lower()  # "standard" or "redshift"
        color = command.get("color", [1.0, 1.0, 1.0])
        
        self.log(f"[C4D] Creating {material_type} material: {name}")
        
        # Create standard material
        mat = c4d.BaseMaterial(c4d.Mmaterial)
        mat.SetName(name)
        
        # Set color
        if len(color) >= 3:
            color_vector = c4d.Vector(color[0], color[1], color[2])
            mat[c4d.MATERIAL_COLOR_COLOR] = color_vector
            
        # Insert material into document
        doc.InsertMaterial(mat)
        c4d.EventAdd()
        
        return {
            "material": {
                "name": mat.GetName(),
                "color": [mat[c4d.MATERIAL_COLOR_COLOR].x, mat[c4d.MATERIAL_COLOR_COLOR].y, mat[c4d.MATERIAL_COLOR_COLOR].z],
                "type": "standard"
            }
        }

    def handle_modify_object(self, command):
        """Handle modify_object command."""
        doc = c4d.documents.GetActiveDocument()
        object_name = command.get("object_name", "")
        properties = command.get("properties", {})

        # Find the object by name
        obj = self.find_object_by_name(doc, object_name)
        if obj is None:
            return {"error": f"Object not found: {object_name}"}

        # Apply modifications
        modified = {}

        # Position
        if (
            "position" in properties
            and isinstance(properties["position"], list)
            and len(properties["position"]) >= 3
        ):
            pos = properties["position"]
            obj.SetAbsPos(c4d.Vector(pos[0], pos[1], pos[2]))
            modified["position"] = pos

        # Rotation (in degrees)
        if (
            "rotation" in properties
            and isinstance(properties["rotation"], list)
            and len(properties["rotation"]) >= 3
        ):
            rot = properties["rotation"]
            # Convert degrees to radians
            rot_rad = [c4d.utils.DegToRad(r) for r in rot]
            obj.SetRelRot(c4d.Vector(rot_rad[0], rot_rad[1], rot_rad[2]))
            modified["rotation"] = rot

        # Scale
        if (
            "scale" in properties
            and isinstance(properties["scale"], list)
            and len(properties["scale"]) >= 3
        ):
            scale = properties["scale"]
            obj.SetRelScale(c4d.Vector(scale[0], scale[1], scale[2]))
            modified["scale"] = scale

        # Color (if object has a base color channel)
        if (
            "color" in properties
            and isinstance(properties["color"], list)
            and len(properties["color"]) >= 3
        ):
            color = properties["color"]
            try:
                # Try to set base color if available
                obj[c4d.ID_BASEOBJECT_COLOR] = c4d.Vector(color[0], color[1], color[2])
                modified["color"] = color
            except:
                pass  # Silently fail if property doesn't exist

        # Update the document
        c4d.EventAdd()

        return {
            "object": {
                "name": obj.GetName(),
                "id": str(obj.GetGUID()),
                "modified": modified,
            }
        }
        
    def handle_apply_material(self, command):
        """Handle apply_material command."""
        doc = c4d.documents.GetActiveDocument()
        material_name = command.get("material_name", "")
        object_name = command.get("object_name", "")
        projection_type = command.get("projection_type", "cubic")
        
        self.log(f"[C4D] Applying material '{material_name}' to object '{object_name}'")
        
        # Define function to execute on main thread
        def apply_material_on_main_thread(doc, material_name, object_name, projection_type):
            try:
                # Find the object
                obj = self.find_object_by_name(doc, object_name)
                if obj is None:
                    self.log(f"[C4D] Object not found: '{object_name}'")
                    return {"error": f"Object not found: {object_name}"}

                # List all available materials for debugging
                all_materials = doc.GetMaterials()
                material_names = [mat.GetName() for mat in all_materials]
                self.log(f"[C4D] Available materials: {material_names}")

                # Find the material
                mat = None
                # First try direct lookup by name
                for m in all_materials:
                    if m.GetName() == material_name:
                        mat = m
                        break
                
                # If not found, try case-insensitive lookup
                if mat is None:
                    material_name_lower = material_name.lower()
                    for m in all_materials:
                        if m.GetName().lower() == material_name_lower:
                            mat = m
                            self.log(f"[C4D] Found material '{m.GetName()}' using case-insensitive match")
                            break
                
                if mat is None:
                    self.log(f"[C4D] Material not found: '{material_name}'")
                    # Create a new material as fallback
                    self.log(f"[C4D] Creating new material '{material_name}' as fallback")
                    mat = c4d.BaseMaterial(c4d.Mmaterial)
                    mat.SetName(material_name)
                    doc.InsertMaterial(mat)

                # Create a texture tag
                tag = c4d.TextureTag()
                tag.SetMaterial(mat)

                # Set projection type
                if projection_type == "cubic":
                    tag[c4d.TEXTURETAG_PROJECTION] = c4d.TEXTURETAG_PROJECTION_CUBIC
                elif projection_type == "spherical":
                    tag[c4d.TEXTURETAG_PROJECTION] = c4d.TEXTURETAG_PROJECTION_SPHERICAL
                elif projection_type == "flat":
                    tag[c4d.TEXTURETAG_PROJECTION] = c4d.TEXTURETAG_PROJECTION_FLAT
                elif projection_type == "cylindrical":
                    tag[c4d.TEXTURETAG_PROJECTION] = c4d.TEXTURETAG_PROJECTION_CYLINDRICAL
                elif projection_type == "frontal":
                    tag[c4d.TEXTURETAG_PROJECTION] = c4d.TEXTURETAG_PROJECTION_FRONTAL
                elif projection_type == "uvw":
                    tag[c4d.TEXTURETAG_PROJECTION] = c4d.TEXTURETAG_PROJECTION_UVW

                # Add the tag to the object
                obj.InsertTag(tag)
                doc.AddUndo(c4d.UNDOTYPE_NEW, tag)

                # Update the document
                c4d.EventAdd()

                return {
                    "material": {
                        "name": mat.GetName(),
                        "object": obj.GetName(),
                        "projection": projection_type,
                    }
                }
            except Exception as e:
                self.log(f"[C4D] Error applying material: {str(e)}")
                return {"error": f"Failed to apply material: {str(e)}"}
        
        # Execute on the main thread
        return self.execute_on_main_thread(
            apply_material_on_main_thread, doc, material_name, object_name, projection_type
        )
    
    def handle_set_keyframe(self, command):
        """Handle set_keyframe command."""
        doc = c4d.documents.GetActiveDocument()
        object_name = command.get("object_name", "")
        frame = command.get("frame", doc.GetTime().GetFrame(doc.GetFps()))
        property_type = command.get("property_type", "position")
        value = command.get("value", [0, 0, 0])

        # Find the object
        obj = self.find_object_by_name(doc, object_name)
        if obj is None:
            return {"error": f"Object not found: {object_name}"}

        try:
            # Handle different property types
            if property_type == "position":
                if not isinstance(value, list) or len(value) < 3:
                    return {"error": "Position must be a list of [x, y, z] values"}

                # Set the keyframe
                result = self.set_position_keyframe(obj, frame, value)

                if result:
                    return {
                        "keyframe": {
                            "object": obj.GetName(),
                            "frame": frame,
                            "property": property_type,
                            "value": value,
                        }
                    }
                else:
                    return {"error": "Failed to set position keyframe"}
            else:
                return {"error": f"Unsupported property type: {property_type}"}
        except Exception as e:
            return {"error": f"Failed to set keyframe: {str(e)}"}
            
    def handle_render_frame(self, command):
        """Handle render_frame command with improved timeout handling."""
        output_path = command.get("output_path", None)
        width = command.get("width", 800)  # Default width if not provided
        height = command.get("height", 600)  # Default height if not provided

        # Log the render request
        self.log(f"[C4D] Rendering frame at {width}x{height}")

        # Define the function to be executed on the main thread
        def render_on_main_thread(doc, output_path, width, height):
            try:
                # Clone active render settings
                rd = doc.GetActiveRenderData().GetClone()

                # Use reduced settings for faster rendering
                rd[c4d.RDATA_XRES] = width
                rd[c4d.RDATA_YRES] = height

                # Always use BaseContainer approach for R2025.1 compatibility
                self.log("[C4D] Using BaseContainer for render settings")
                settings = rd.GetDataInstance()  # Gets the BaseContainer

                # Ensure we have a BaseContainer
                if not isinstance(settings, c4d.BaseContainer):
                    self.log("[C4D] Creating a new BaseContainer for render settings")
                    settings = c4d.BaseContainer()
                    settings[c4d.RDATA_XRES] = width
                    settings[c4d.RDATA_YRES] = height
                    
                # Execute the rendering with proper BaseContainer settings
                try:
                    # Create bitmap for rendering
                    bmp = c4d.bitmaps.BaseBitmap()
                    if not bmp.Init(width, height, 24):  # 24 bit color depth
                        return {"error": "Failed to initialize bitmap"}
                    
                    # Use positional parameters instead of named parameters for R2025.1 compatibility
                    c4d.documents.RenderDocument(
                        doc, 
                        settings,  # Always pass BaseContainer
                        bmp,  # Must provide bitmap (positional parameter)
                        c4d.RENDERFLAGS_EXTERNAL,
                        None
                    )
                    self.log("[C4D] Render completed successfully")
                except Exception as e:
                    self.log(f"[C4D] Error rendering with BaseContainer: {str(e)}")
                    # We won't fall back to old method since it's known to be problematic in R2025.1
                    raise  # Re-raise to report error
                
                # Access the rendered image
                if output_path:
                    self.log(f"[C4D] Saving render to {output_path}")
                    # Check if the directory exists, create if it doesn't
                    directory = os.path.dirname(output_path)
                    if directory and not os.path.exists(directory):
                        os.makedirs(directory)

                    # Get the rendered bitmap
                    bitmap = doc.GetActiveRenderData().GetResult()
                    
                    if bitmap:
                        # Save the bitmap to the specified path
                        if output_path.lower().endswith('.jpg') or output_path.lower().endswith('.jpeg'):
                            bitmap.Save(output_path, c4d.FILTER_JPG)
                        elif output_path.lower().endswith('.png'):
                            bitmap.Save(output_path, c4d.FILTER_PNG)
                        elif output_path.lower().endswith('.tif') or output_path.lower().endswith('.tiff'):
                            bitmap.Save(output_path, c4d.FILTER_TIF)
                        else:
                            # Default to PNG if extension not recognized
                            if not '.' in os.path.basename(output_path):
                                output_path += '.png'
                            bitmap.Save(output_path, c4d.FILTER_PNG)
                        
                        self.log(f"[C4D] Render saved to {output_path}")
                        return {
                            "render": {
                                "width": width,
                                "height": height,
                                "output_path": output_path,
                                "success": True,
                            }
                        }
                    else:
                        self.log("[C4D] Failed to get rendered bitmap")
                        return {"error": "Failed to get rendered bitmap"}
                else:
                    # If no output path specified, just return success
                    self.log("[C4D] Render completed but no output path was specified")
                    return {
                        "render": {
                            "width": width,
                            "height": height,
                            "success": True,
                        }
                    }

            except Exception as e:
                self.log(f"[C4D] Error in render process: {str(e)}")
                return {"error": f"Error rendering: {str(e)}"}

        # Execute the rendering on the main thread with extended timeout (2 minutes)
        doc = c4d.documents.GetActiveDocument()
        result = self.execute_on_main_thread(
            render_on_main_thread, 
            doc, 
            output_path, 
            width, 
            height, 
            _timeout=120  # 2 minute timeout for rendering
        )
        
        return result
        
    def handle_save_scene(self, command):
        """Handle save_scene command."""
        file_path = command.get("file_path", "")
        if not file_path:
            return {"error": "No file path provided"}

        # Log the save request
        self.log(f"[C4D] Saving scene to: {file_path}")

        # Define function to execute on main thread
        def save_scene_on_main_thread(doc, file_path):
            try:
                # Ensure the directory exists
                directory = os.path.dirname(file_path)
                if directory and not os.path.exists(directory):
                    os.makedirs(directory)

                # Check file extension
                _, extension = os.path.splitext(file_path)
                if not extension:
                    file_path += ".c4d"  # Add default extension

                # Save the document
                if not c4d.documents.SaveDocument(
                    doc,
                    file_path,
                    c4d.SAVEDOCUMENTFLAGS_NONE,
                    c4d.FORMAT_C4DEXPORT,
                ):
                    return {"error": f"Failed to save document to {file_path}"}

                # R2025.1 fix: Update document name and path to fix "Untitled-1" issue
                try:
                    # Update the document name
                    doc.SetDocumentName(os.path.basename(file_path))

                    # Update document path
                    doc.SetDocumentPath(os.path.dirname(file_path))

                    # Ensure UI is updated
                    c4d.EventAdd()
                    self.log(f"[C4D] Updated document name and path for {file_path}")
                except Exception as e:
                    self.log(f"[C4D] Warning: Could not update document name/path: {str(e)}")

                return {
                    "success": True,
                    "file_path": file_path,
                }
            except Exception as e:
                return {"error": f"Failed to save document: {str(e)}"}

        # Execute on main thread with extended timeout
        doc = c4d.documents.GetActiveDocument()
        return self.execute_on_main_thread(save_scene_on_main_thread, doc, file_path)
        
    def handle_load_scene(self, command):
        """Handle load_scene command."""
        file_path = command.get("file_path", "")
        if not file_path:
            self.log("[C4D] Error: No file path provided for load_scene")
            return {"error": "No file path provided"}

        # Clean up and normalize the file path
        file_path = os.path.normpath(file_path)
        self.log(f"[C4D] Checking for file: {file_path}")
        
        # Check if file exists
        if not os.path.exists(file_path):
            self.log(f"[C4D] Error: File not found: {file_path}")
            
            # Try to provide helpful information about the current directory
            try:
                current_dir = os.getcwd()
                self.log(f"[C4D] Current working directory: {current_dir}")
                files = os.listdir(current_dir)
                self.log(f"[C4D] Files in current directory: {files[:10]}" + 
                        ("..." if len(files) > 10 else ""))
            except Exception as e:
                self.log(f"[C4D] Could not list directory: {str(e)}")
            
            # Also check if the file exists with .c4d extension
            if not file_path.lower().endswith('.c4d'):
                c4d_path = file_path + '.c4d'
                if os.path.exists(c4d_path):
                    self.log(f"[C4D] Found file with .c4d extension: {c4d_path}")
                    file_path = c4d_path
                else:
                    return {"error": f"File not found: {file_path}"}
            else:
                return {"error": f"File not found: {file_path}"}

        # Log the load request
        self.log(f"[C4D] Loading scene from: {file_path}")

        # Define function to execute on main thread
        def load_scene_on_main_thread(file_path):
            try:
                # Load the document
                self.log(f"[C4D] Attempting to load document from {file_path}")
                new_doc = c4d.documents.LoadDocument(file_path, c4d.SCENEFILTER_NONE)
                if not new_doc:
                    self.log(f"[C4D] Failed to load document from {file_path}")
                    return {"error": f"Failed to load document from {file_path}"}

                # Set as active document
                c4d.documents.SetActiveDocument(new_doc)
                
                # Update UI
                c4d.EventAdd()
                
                self.log(f"[C4D] Successfully loaded document: {new_doc.GetDocumentName()}")
                return {
                    "success": True,
                    "file_path": file_path,
                    "document_name": new_doc.GetDocumentName(),
                }
            except Exception as e:
                self.log(f"[C4D] Error loading document: {str(e)}")
                return {"error": f"Failed to load document: {str(e)}"}

        # Execute on main thread with extended timeout
        return self.execute_on_main_thread(
            load_scene_on_main_thread, 
            file_path,
            _timeout=60  # Give it a full minute to load larger scenes
        )
    
    def handle_create_mograph_cloner(self, command):
        """Handle create_mograph_cloner command."""
        doc = c4d.documents.GetActiveDocument()
        name = command.get("name") or "MoGraph Cloner"
        mode = command.get("mode", "grid").lower()  # grid, linear, radial, object
        count = command.get("count", 5)
        clone_object_name = command.get("clone_object", "")

        # Map string mode names to C4D constants
        mode_map = {
            "linear": 0,
            "grid": 1,
            "radial": 2,
            "object": 3,
        }
        mode_id = mode_map.get(mode, 1)  # Default to grid

        # Execute on main thread for reliability
        def create_mograph_cloner_safe(doc, name, mode, count, clone_obj_name):
            try:
                # Create MoGraph Cloner object
                cloner = None
                
                # In R2025.1, Omgcloner is accessed differently
                try:
                    # Try to get MoGraph module constant
                    cloner = c4d.BaseObject(c4d.Omgcloner)
                    self.log("[C4D] Created cloner using standard constant")
                except:
                    # Try using R2025.1 modules namespace
                    try:
                        if hasattr(c4d, "modules") and hasattr(c4d.modules, "mograph"):
                            cloner = c4d.BaseObject(c4d.modules.mograph.Omgcloner)
                            self.log("[C4D] Created cloner using R2025.1 modules namespace")
                    except Exception as mograph_error:
                        self.log(f"[C4D] Error creating MoGraph cloner: {str(mograph_error)}")
                        
                        # If all fails, use hardcoded ID as last resort
                        try:
                            # Hardcoded value as fallback (1018544 = Cloner)
                            cloner = c4d.BaseObject(1018544)
                            self.log("[C4D] Created cloner using hardcoded ID")
                        except:
                            return {"error": "Failed to create MoGraph Cloner object"}
                
                if not cloner:
                    return {"error": "Failed to create MoGraph Cloner object"}
                
                # Set the name
                cloner.SetName(name)
                
                # Set MoGraph mode
                cloner[c4d.ID_MG_MOTIONGENERATOR_MODE] = mode_id
                
                # Find clone object if specified
                if clone_obj_name:
                    clone_obj = self.find_object_by_name(doc, clone_obj_name)
                    if clone_obj:
                        # Add a link to the target object
                        cloner[c4d.MG_OBJECT_LINK] = clone_obj
                        self.log(f"[C4D] Linked cloner to {clone_obj_name}")
                    else:
                        # If object not found, fall back to grid mode
                        cloner[c4d.ID_MG_MOTIONGENERATOR_MODE] = 1  # Grid
                        
                        # Use correct R2025.1 MoGraph constants path
                        try:
                            # R2025.1 approach using modules.mograph namespace
                            cloner[c4d.modules.mograph.MG_GRID_COUNT_X] = 3
                            cloner[c4d.modules.mograph.MG_GRID_COUNT_Y] = 3
                            cloner[c4d.modules.mograph.MG_GRID_COUNT_Z] = 3
                            self.log(
                                "[C4D] Set default grid counts using c4d.modules.mograph namespace"
                            )
                        except Exception as e:
                            # Fallback to traditional constants if needed
                            self.log(f"[C4D] Error with mograph module: {str(e)}")
                            try:
                                cloner[c4d.MG_GRID_COUNT_X] = 3
                                cloner[c4d.MG_GRID_COUNT_Y] = 3
                                cloner[c4d.MG_GRID_COUNT_Z] = 3
                                self.log(
                                    "[C4D] Set default grid counts using traditional constants"
                                )
                            except Exception as e2:
                                self.log(f"[C4D] Could not set grid counts: {str(e2)}")
                else:
                    # If no object specified, use cube as child
                    if mode_id == 3:  # Object mode
                        # Create a cube for the cloner
                        cube = c4d.BaseObject(c4d.Ocube)
                        cube.SetName(f"{name} Source")
                        cube[c4d.PRIM_CUBE_LEN] = c4d.Vector(50, 50, 50)
                        doc.InsertObject(cube)
                        doc.AddUndo(c4d.UNDOTYPE_NEW, cube)
                        
                        # Add the cube under the cloner
                        cube.InsertUnder(cloner)
                    else:
                        # For grid/linear mode, set the count
                        if mode_id == 0:  # Linear
                            cloner[c4d.MG_LINEAR_COUNT] = count
                        elif mode_id == 2:  # Radial
                            cloner[c4d.MG_OBJECT_COUNT] = count
                        else:  # Grid or fallback
                            # For grid mode, calculate a reasonable grid size
                            grid_size = (
                                int(count ** (1 / 3)) or 1
                            )  # Cube root for even distribution
                            
                            # Use correct R2025.1 MoGraph constants path
                            try:
                                # R2025.1 approach using modules.mograph namespace
                                cloner[c4d.modules.mograph.MG_GRID_COUNT_X] = grid_size
                                cloner[c4d.modules.mograph.MG_GRID_COUNT_Y] = grid_size
                                cloner[c4d.modules.mograph.MG_GRID_COUNT_Z] = grid_size
                                self.log(
                                    "[C4D] Set grid counts using c4d.modules.mograph namespace"
                                )
                            except Exception as e:
                                # Fallback to traditional constants if needed
                                self.log(f"[C4D] Error with mograph module: {str(e)}")
                                try:
                                    cloner[c4d.MG_GRID_COUNT_X] = grid_size
                                    cloner[c4d.MG_GRID_COUNT_Y] = grid_size
                                    cloner[c4d.MG_GRID_COUNT_Z] = grid_size
                                    self.log(
                                        "[C4D] Set grid counts using traditional constants"
                                    )
                                except Exception as e2:
                                    self.log(f"[C4D] Could not set grid counts: {str(e2)}")
                            
                        # Create an object for the cloner
                        obj = c4d.BaseObject(c4d.Ocube)
                        obj.SetName(f"{name} Cube")
                        obj[c4d.PRIM_CUBE_LEN] = c4d.Vector(50, 50, 50)
                        doc.InsertObject(obj)
                        doc.AddUndo(c4d.UNDOTYPE_NEW, obj)
                        
                        # Add the object under the cloner
                        obj.InsertUnder(cloner)
                
                # Insert cloner into document
                doc.InsertObject(cloner)
                doc.AddUndo(c4d.UNDOTYPE_NEW, cloner)
                
                # Make it the active object
                doc.SetActiveObject(cloner)
                
                # Update the document
                c4d.EventAdd()
                
                return {
                    "mograph_cloner": {
                        "name": cloner.GetName(),
                        "id": str(cloner.GetGUID()),
                        "mode": mode,
                        "mode_id": mode_id,
                        "count": count,
                    }
                }
            except Exception as e:
                return {"error": f"Failed to create MoGraph Cloner: {str(e)}"}
        
        # Execute on the main thread
        doc = c4d.documents.GetActiveDocument()
        return self.execute_on_main_thread(
            create_mograph_cloner_safe, doc, name, mode_id, count, clone_object_name
        )
    
    def handle_execute_python(self, command):
        """Handle execute_python command."""
        code = command.get("code", "")
        script_name = command.get("script_name", "")
        
        # Report error for empty code
        if not code:
            self.log("[C4D] Error: No Python code provided")
            return {"error": "No Python code provided"}
            
        # Log that we're executing Python code
        self.log(f"[C4D] Executing Python code{' from ' + script_name if script_name else ''}")
        self.log(f"[C4D] Code length: {len(code)} characters")
        # Log first 100 chars of the code for debugging
        if len(code) > 100:
            self.log(f"[C4D] Code preview: {code[:100]}...")
        else:
            self.log(f"[C4D] Code: {code}")

        # For security, limit available modules
        allowed_imports = ["c4d", "math", "random", "time", "json", "os.path", "sys"]

        # Check for potentially harmful imports or functions
        for banned_keyword in [
            "os.system",
            "subprocess",
            "exec(",
            "eval(",
            "import os",
            "from os import",
            "__import__",
            "importlib",
        ]:
            if banned_keyword in code:
                self.log(f"[C4D] Security: Banned keyword found in code: {banned_keyword}")
                return {
                    "error": f"Security: Banned keyword found in code: {banned_keyword}"
                }

        # Function to execute code on the main thread
        def execute_python_on_main_thread(code):
            try:
                # Create a separate namespace for execution
                namespace = {
                    "c4d": c4d,
                    "doc": c4d.documents.GetActiveDocument(),
                    "math": __import__("math"),
                    "random": __import__("random"),
                    "time": __import__("time"),
                    "json": __import__("json"),
                    "result": None,  # For storing the result
                }

                # Execute the code in the controlled namespace
                self.log("[C4D] Executing Python code in controlled namespace")
                exec(code, namespace)
                self.log("[C4D] Python code executed successfully")

                # Return the result if set
                if "result" in namespace and namespace["result"] is not None:
                    result_str = str(namespace["result"])
                    self.log(f"[C4D] Python execution result: {result_str[:100]}" + 
                             ("..." if len(result_str) > 100 else ""))
                    return {
                        "success": True,
                        "result": result_str,
                        "result_type": type(namespace["result"]).__name__
                    }
                else:
                    self.log("[C4D] Python code executed with no explicit result")
                    return {
                        "success": True,
                        "result": "Code executed successfully (no result value returned)"
                    }
            except Exception as e:
                import traceback
                error_msg = str(e)
                trace = traceback.format_exc()
                self.log(f"[C4D] Python execution error: {error_msg}")
                self.log(f"[C4D] Traceback: {trace}")
                return {
                    "success": False,
                    "error": f"Python execution error: {error_msg}",
                    "traceback": trace,
                }

        # Execute on main thread for reliability and for accessing C4D API
        return self.execute_on_main_thread(execute_python_on_main_thread, code)
        
    def handle_apply_shader(self, command):
        """Handle apply_shader command with improved Redshift/Fresnel support."""
        doc = c4d.documents.GetActiveDocument()
        material_name = command.get("material_name", "")
        object_name = command.get("object_name", "")
        shader_type = command.get("shader_type", "noise").lower()
        channel = command.get("channel", "color").lower()
        parameters = command.get("parameters", {})

        # Debug logging
        self.log(f"[C4D] Applying {shader_type} shader to channel {channel}")
        if material_name:
            self.log(f"[C4D] Using material: '{material_name}'")
        else:
            self.log("[C4D] No material specified, will create a new one")

        try:
            # If no material name specified or material not found, create a new one
            mat = None
            created_new = False

            if material_name:
                mat = self.find_material_by_name(doc, material_name)

            # If material not found or no name specified, create a new one
            if mat is None:
                mat = c4d.BaseMaterial(c4d.Mmaterial)
                if material_name:
                    mat.SetName(material_name)
                else:
                    # Name the material after the shader type
                    mat.SetName(f"{shader_type.capitalize()} Material")

                # Insert the new material
                doc.InsertMaterial(mat)
                doc.AddUndo(c4d.UNDOTYPE_NEW, mat)
                created_new = True
                material_name = mat.GetName()
                self.log(f"[C4D] Created new material: '{material_name}'")
            
            # For standard materials
            # Map shader types to C4D constants
            shader_types = {
                "noise": 5832,
                "gradient": 5825,
                "fresnel": 5837,
                "layer": 5685,
                "checkerboard": 5831,
            }

            # Map channel names to C4D constants
            channel_map = {
                "color": c4d.MATERIAL_COLOR_SHADER,
                "luminance": c4d.MATERIAL_LUMINANCE_SHADER,
                "transparency": c4d.MATERIAL_TRANSPARENCY_SHADER,
                "reflection": c4d.MATERIAL_REFLECTION_SHADER,
            }

            # Get shader type ID and channel ID
            shader_type_id = shader_types.get(shader_type, 5832)  # Default to noise
            channel_id = channel_map.get(channel, c4d.MATERIAL_COLOR_SHADER)

            # Create shader with proper error handling
            try:
                shader = c4d.BaseShader(shader_type_id)
                if shader is None:
                    return {"error": f"Failed to create {shader_type} shader"}

                # Set shader parameters
                if shader_type == "noise":
                    if "scale" in parameters:
                        shader[c4d.SLA_NOISE_SCALE] = float(
                            parameters.get("scale", 1.0)
                        )
                    if "octaves" in parameters:
                        shader[c4d.SLA_NOISE_OCTAVES] = int(
                            parameters.get("octaves", 3)
                        )

                # Assign shader to material channel
                mat[channel_id] = shader

                # Enable channel
                enable_map = {
                    "color": c4d.MATERIAL_USE_COLOR,
                    "luminance": c4d.MATERIAL_USE_LUMINANCE,
                    "transparency": c4d.MATERIAL_USE_TRANSPARENCY,
                    "reflection": c4d.MATERIAL_USE_REFLECTION,
                }
                if channel in enable_map:
                    mat[enable_map[channel]] = True
            except Exception as e:
                return {"error": f"Error creating shader: {str(e)}"}

            # Update the material
            mat.Update(True, True)
            doc.AddUndo(c4d.UNDOTYPE_CHANGE, mat)

            # Apply to object if specified
            applied_to = "None"
            if object_name:
                obj = self.find_object_by_name(doc, object_name)
                if obj is None:
                    self.log(f"[C4D] Warning: Object '{object_name}' not found")
                else:
                    # Create and add texture tag
                    try:
                        tag = c4d.TextureTag()
                        tag.SetMaterial(mat)
                        obj.InsertTag(tag)
                        doc.AddUndo(c4d.UNDOTYPE_NEW, tag)
                        applied_to = obj.GetName()
                        self.log(f"[C4D] Applied material to object '{applied_to}'")
                    except Exception as e:
                        self.log(f"[C4D] Error applying material to object: {str(e)}")

            # Update Cinema 4D
            c4d.EventAdd()

            # Return shader info
            return {
                "shader": {
                    "material": material_name,
                    "type": shader_type,
                    "channel": channel,
                    "applied_to": applied_to,
                    "created_new": created_new,
                }
            }
        except Exception as e:
            self.log(f"[C4D] Error applying shader: {str(e)}")
            return {"error": f"Failed to apply shader: {str(e)}"}
    
    def handle_add_effector(self, command):
        """Handle add_effector command to add effectors to MoGraph objects."""
        doc = c4d.documents.GetActiveDocument()
        
        # Extract parameters
        target_object = command.get("target_object", "")
        effector_type = command.get("type", "random").lower()
        name = command.get("name", f"{effector_type.capitalize()} Effector")
        strength = command.get("strength", 100.0)
        parameters = command.get("parameters", {})
        
        # Log what we're doing
        self.log(f"[C4D] Adding {effector_type} effector to '{target_object}'")
        
        # Define function to execute on main thread
        def add_effector_on_main_thread(doc, target_name, effector_type, name, strength, parameters):
            try:
                # Find the target object (should be a MoGraph object)
                target = self.find_object_by_name(doc, target_name)
                if target is None:
                    return {"error": f"Target object not found: {target_name}"}
                
                # Map effector types to C4D constants
                effector_map = {
                    "random": None,    # Will set based on R2025.1 detection
                    "shader": None,    # Will set based on R2025.1 detection
                    "formula": None,   # Will set based on R2025.1 detection
                    "step": None,      # Will set based on R2025.1 detection
                    "time": None,      # Will set based on R2025.1 detection
                    "sound": None,     # Will set based on R2025.1 detection
                    "delay": None,     # Will set based on R2025.1 detection
                }
                
                # Update effector map based on R2025.1 detection
                try:
                    # Check if using R2025.1 module structure
                    if hasattr(c4d, "modules") and hasattr(c4d.modules, "mograph"):
                        self.log("[C4D] Using R2025.1 module structure for effectors")
                        effector_map.update({
                            "random": c4d.modules.mograph.Omgrandomeffector,
                            "shader": c4d.modules.mograph.Omgshadereffector,
                            "formula": c4d.modules.mograph.Omgformulaeffector,
                            "step": c4d.modules.mograph.Omgstepeffector,
                            "time": c4d.modules.mograph.Omgtimeeffector,
                            "sound": c4d.modules.mograph.Omgsoundeffector,
                            "delay": c4d.modules.mograph.Omgdelayeffector,
                        })
                    else:
                        # Fallback to traditional constants
                        self.log("[C4D] Using traditional constants for effectors")
                        effector_map.update({
                            "random": 1018643,    # Random Effector
                            "shader": 1018643,    # Shader Effector
                            "formula": 1019351,   # Formula Effector
                            "step": 1018881,      # Step Effector
                            "time": 1018596,      # Time Effector
                            "sound": 1018889,     # Sound Effector
                            "delay": 1018886,     # Delay Effector
                        })
                except Exception as e:
                    self.log(f"[C4D] Error setting up effector constants: {str(e)}")
                    # Hardcoded fallback values
                    effector_map.update({
                        "random": 1018643,    # Random Effector
                        "shader": 1018643,    # Shader Effector
                        "formula": 1019351,   # Formula Effector
                        "step": 1018881,      # Step Effector
                        "time": 1018596,      # Time Effector
                        "sound": 1018889,     # Sound Effector
                        "delay": 1018886,     # Delay Effector
                    })
                
                # Get the appropriate effector ID
                effector_id = effector_map.get(effector_type)
                if effector_id is None:
                    return {"error": f"Unsupported effector type: {effector_type}"}
                
                # Create the effector object
                effector = c4d.BaseObject(effector_id)
                if effector is None:
                    return {"error": f"Failed to create {effector_type} effector"}
                
                # Set the name
                effector.SetName(name)
                
                # Set the effector strength
                try:
                    # Try to set common parameters
                    if hasattr(c4d.modules, "mograph"):
                        # R2025.1 approach
                        effector[c4d.modules.mograph.ID_MG_BASEEFFECTOR_STRENGTH] = strength
                    else:
                        # Traditional approach
                        effector[c4d.ID_MG_BASEEFFECTOR_STRENGTH] = strength
                except Exception as e:
                    self.log(f"[C4D] Warning setting effector strength: {str(e)}")
                
                # Apply type-specific parameters
                if effector_type == "random":
                    # Set random effector parameters
                    seed = parameters.get("seed", 12345)
                    try:
                        effector[c4d.MG_RANDOM_SEED] = seed
                    except:
                        try:
                            effector[c4d.modules.mograph.MG_RANDOM_SEED] = seed
                        except Exception as e:
                            self.log(f"[C4D] Could not set random seed: {str(e)}")
                            
                    # Set position/rotation/scale influence
                    if "position_influence" in parameters:
                        pos_influence = parameters.get("position_influence", [100, 100, 100])
                        try:
                            effector[c4d.MG_EFFECTOR_POSITION_ACTIVE] = True
                            effector[c4d.MG_EFFECTOR_POSITION] = c4d.Vector(*pos_influence)
                        except:
                            try:
                                effector[c4d.modules.mograph.MG_EFFECTOR_POSITION_ACTIVE] = True
                                effector[c4d.modules.mograph.MG_EFFECTOR_POSITION] = c4d.Vector(*pos_influence)
                            except:
                                self.log("[C4D] Could not set position influence")
                                
                    if "rotation_influence" in parameters:
                        rot_influence = parameters.get("rotation_influence", [0, 0, 0])
                        try:
                            effector[c4d.MG_EFFECTOR_ROTATION_ACTIVE] = True
                            effector[c4d.MG_EFFECTOR_ROTATION] = c4d.Vector(*rot_influence)
                        except:
                            try:
                                effector[c4d.modules.mograph.MG_EFFECTOR_ROTATION_ACTIVE] = True
                                effector[c4d.modules.mograph.MG_EFFECTOR_ROTATION] = c4d.Vector(*rot_influence)
                            except:
                                self.log("[C4D] Could not set rotation influence")
                                
                    if "scale_influence" in parameters:
                        scale_influence = parameters.get("scale_influence", [0, 0, 0])
                        try:
                            effector[c4d.MG_EFFECTOR_SCALE_ACTIVE] = True
                            effector[c4d.MG_EFFECTOR_SCALE] = c4d.Vector(*scale_influence)
                        except:
                            try:
                                effector[c4d.modules.mograph.MG_EFFECTOR_SCALE_ACTIVE] = True
                                effector[c4d.modules.mograph.MG_EFFECTOR_SCALE] = c4d.Vector(*scale_influence)
                            except:
                                self.log("[C4D] Could not set scale influence")
                
                elif effector_type == "formula":
                    # Set formula effector parameters
                    formula = parameters.get("formula", "sin(x+time*10)")
                    try:
                        effector[c4d.MG_BASEEFFECTOR_STRENGTHMODE] = 2  # Formula mode
                        effector[c4d.MG_FORMULA_FORMULA] = formula
                    except:
                        try:
                            effector[c4d.modules.mograph.MG_BASEEFFECTOR_STRENGTHMODE] = 2  # Formula mode
                            effector[c4d.modules.mograph.MG_FORMULA_FORMULA] = formula
                        except Exception as e:
                            self.log(f"[C4D] Could not set formula: {str(e)}")
                
                # Insert the effector into the document
                doc.InsertObject(effector)
                doc.AddUndo(c4d.UNDOTYPE_NEW, effector)
                
                # Add the effector to the target MoGraph object's effector list
                self.log(f"[C4D] Adding effector to '{target.GetName()}'")
                try:
                    # Try R2025.1 approach first
                    try:
                        # Get the MoGraph object's effector list
                        effector_list = target[c4d.modules.mograph.MGCLONER_EFFECTORLIST]
                        if effector_list is None:
                            effector_list = c4d.InExcludeData()
                            target[c4d.modules.mograph.MGCLONER_EFFECTORLIST] = effector_list
                        
                        # Add the effector to the list
                        effector_list.InsertObject(effector, 1)
                    except:
                        # Fallback to traditional approach
                        try:
                            effector_list = target[c4d.MGCLONER_EFFECTORLIST]
                            if effector_list is None:
                                effector_list = c4d.InExcludeData()
                                target[c4d.MGCLONER_EFFECTORLIST] = effector_list
                            
                            # Add the effector to the list
                            effector_list.InsertObject(effector, 1)
                        except Exception as e:
                            self.log(f"[C4D] Error adding effector to list: {str(e)}")
                            return {"error": f"Failed to add effector to {target_name}: {str(e)}"}
                except Exception as e:
                    self.log(f"[C4D] Error setting up effector list: {str(e)}")
                    # Continue anyway, as the effector is at least created
                
                # Update Cinema 4D
                c4d.EventAdd()
                
                return {
                    "effector": {
                        "name": effector.GetName(),
                        "type": effector_type,
                        "target": target.GetName(),
                        "strength": strength,
                    }
                }
            except Exception as e:
                return {"error": f"Failed to add effector: {str(e)}"}
        
        # Execute on the main thread
        return self.execute_on_main_thread(
            add_effector_on_main_thread, doc, target_object, effector_type, name, strength, parameters
        )
        
    def handle_create_light(self, command):
        """Handle create_light command to create different types of lights."""
        doc = c4d.documents.GetActiveDocument()
        
        # Extract parameters
        light_type = command.get("type", "point").lower()
        name = command.get("name", f"{light_type.capitalize()} Light")
        position = command.get("position", [0, 150, 0]) 
        target = command.get("target", [0, 0, 0])  # For spot/area lights
        color = command.get("color", [1.0, 1.0, 1.0])
        intensity = command.get("intensity", 100.0)  # Brightness percentage
        shadow_enabled = command.get("shadow_enabled", True)
        parameters = command.get("parameters", {})
        
        # Log what we're doing
        self.log(f"[C4D] Creating {light_type} light: {name}")
        
        # Define function to execute on main thread
        def create_light_on_main_thread(doc, light_type, name, position, target, color, intensity, shadow_enabled, parameters):
            try:
                # Map light types to C4D constants - using dynamic detection for R2025.1 compatibility
                light_map = {}
                
                # Check if we're in R2025.1 with objects module
                if hasattr(c4d, "objects"):
                    self.log("[C4D] Using R2025.1 objects module for light types")
                    light_map = {
                        "point": c4d.objects.Olight,
                        "spot": c4d.objects.Ospotlight,
                        "area": c4d.objects.Oarealight,
                        "directional": c4d.objects.Odistantlight,
                        "infinite": c4d.objects.Oinfinitelight,
                    }
                else:
                    # Fallback to traditional constants
                    self.log("[C4D] Using traditional constants for light types")
                    light_map = {
                        "point": 5102,  # c4d.Olight
                        "spot": 5159,   # c4d.Ospotlight
                        "area": 5160,   # c4d.Oarealight
                        "directional": 5129,  # c4d.Odistantlight
                        "infinite": 5142,     # c4d.Oinfinitelight
                    }
                
                # Get the appropriate light type ID
                light_id = light_map.get(light_type)
                if light_id is None:
                    return {"error": f"Unsupported light type: {light_type}"}
                
                # Create the light object
                light = c4d.BaseObject(light_id)
                if light is None:
                    return {"error": f"Failed to create {light_type} light"}
                
                # Set the name
                light.SetName(name)
                
                # Set basic light properties
                # Position
                if len(position) >= 3:
                    light.SetAbsPos(c4d.Vector(position[0], position[1], position[2]))
                
                # Color 
                if len(color) >= 3:
                    light[c4d.LIGHT_COLOR] = c4d.Vector(color[0], color[1], color[2])
                
                # Intensity (brightness)
                light[c4d.LIGHT_BRIGHTNESS] = intensity
                
                # Shadow
                if shadow_enabled:
                    light[c4d.LIGHT_SHADOWTYPE] = 1  # Shadow enabled (0 = off, 1 = shadow maps)
                else:
                    light[c4d.LIGHT_SHADOWTYPE] = 0  # No shadow
                
                # Apply type-specific parameters
                if light_type == "spot":
                    # Spot light specific settings
                    inner_angle = parameters.get("inner_angle", 20.0)
                    outer_angle = parameters.get("outer_angle", 40.0)
                    
                    light[c4d.LIGHT_DETAILS_INNERANGLE] = inner_angle
                    light[c4d.LIGHT_DETAILS_OUTERANGLE] = outer_angle
                    
                    # Calculate rotation to target position
                    if len(target) >= 3:
                        target_pos = c4d.Vector(target[0], target[1], target[2])
                        light_pos = light.GetAbsPos()
                        
                        # Calculate direction vector
                        direction = target_pos - light_pos
                        if direction.GetLength() > 0:
                            # Create rotation matrix that points light along direction
                            up = c4d.Vector(0, 1, 0)  # Assume Y-up world
                            
                            # Calculate the rotation to look at target
                            look_at_matrix = c4d.utils.LookAtMatrix(light_pos, target_pos, up)
                            rotation = c4d.utils.MatrixToHPB(look_at_matrix)
                            
                            # Apply rotation to light
                            light.SetRotation(rotation)
                
                elif light_type == "area":
                    # Area light specific settings
                    width = parameters.get("width", 100.0)
                    height = parameters.get("height", 100.0)
                    
                    light[c4d.LIGHT_AREAWIDTH] = width
                    light[c4d.LIGHT_AREAHEIGHT] = height
                    
                    # Set sampling quality
                    samples = parameters.get("samples", 4)
                    light[c4d.LIGHT_AREASAMPLES] = samples
                    
                    # Calculate rotation to target position if provided
                    if len(target) >= 3:
                        target_pos = c4d.Vector(target[0], target[1], target[2])
                        light_pos = light.GetAbsPos()
                        
                        # Calculate direction vector
                        direction = target_pos - light_pos
                        if direction.GetLength() > 0:
                            # Create rotation matrix that points light along direction
                            up = c4d.Vector(0, 1, 0)  # Assume Y-up world
                            
                            # Calculate the rotation to look at target
                            look_at_matrix = c4d.utils.LookAtMatrix(light_pos, target_pos, up)
                            rotation = c4d.utils.MatrixToHPB(look_at_matrix)
                            
                            # Apply rotation to light
                            light.SetRotation(rotation)
                
                elif light_type == "directional" or light_type == "infinite":
                    # Directional light specific settings
                    # Calculate rotation to target if provided
                    if len(target) >= 3:
                        target_pos = c4d.Vector(target[0], target[1], target[2])
                        light_pos = light.GetAbsPos()
                        
                        # Calculate direction vector
                        direction = target_pos - light_pos
                        if direction.GetLength() > 0:
                            # Create rotation matrix that points light along direction
                            up = c4d.Vector(0, 1, 0)  # Assume Y-up world
                            
                            # Calculate the rotation to look at target
                            look_at_matrix = c4d.utils.LookAtMatrix(light_pos, target_pos, up)
                            rotation = c4d.utils.MatrixToHPB(look_at_matrix)
                            
                            # Apply rotation to light
                            light.SetRotation(rotation)
                
                # Check for light visibility settings
                if "visible" in parameters:
                    light[c4d.LIGHT_VISIBLE] = parameters.get("visible", True)
                
                # Check for falloff settings for point/spot lights
                if (light_type == "point" or light_type == "spot") and "falloff" in parameters:
                    light[c4d.LIGHT_DETAILS_LINEARFALLOFF] = parameters.get("falloff", 100.0)
                
                # Insert the light into the document
                doc.InsertObject(light)
                doc.AddUndo(c4d.UNDOTYPE_NEW, light)
                
                # Make it the active object
                doc.SetActiveObject(light)
                
                # Update Cinema 4D
                c4d.EventAdd()
                
                return {
                    "light": {
                        "name": light.GetName(),
                        "type": light_type,
                        "position": [position[0], position[1], position[2]],
                        "intensity": intensity,
                        "color": [color[0], color[1], color[2]],
                        "shadow_enabled": shadow_enabled
                    }
                }
            except Exception as e:
                return {"error": f"Failed to create light: {str(e)}"}
        
        # Execute on the main thread
        return self.execute_on_main_thread(
            create_light_on_main_thread, doc, light_type, name, position, target, color, intensity, shadow_enabled, parameters
        )
        
    def handle_animate_camera(self, command):
        """Handle animate_camera command to create and animate cameras."""
        doc = c4d.documents.GetActiveDocument()
        
        # Extract parameters
        camera_name = command.get("camera_name", "")  # Optional - will create new camera if not provided
        keyframes = command.get("keyframes", [])  # List of camera keyframes
        focal_length = command.get("focal_length", 36.0)  # Default to 36mm focal length
        target_object = command.get("target_object", "")  # Optional object to target/look at
        create_target = command.get("create_target", False)  # Whether to create a target null
        
        # Log what we're doing
        self.log(f"[C4D] Setting up camera animation with {len(keyframes)} keyframes")
        
        # Define function to execute on main thread
        def animate_camera_on_main_thread(doc, camera_name, keyframes, focal_length, target_object, create_target):
            try:
                # Get the camera to animate
                camera = None
                created_new = False
                
                # Try to find existing camera if name provided
                if camera_name:
                    camera = self.find_object_by_name(doc, camera_name)
                    if camera and camera.GetType() != c4d.Ocamera:
                        self.log(f"[C4D] Object '{camera_name}' exists but is not a camera")
                        camera = None
                
                # If no camera name provided or not found, create a new one
                if camera is None:
                    # Create a new camera
                    camera = c4d.BaseObject(c4d.Ocamera)
                    
                    # Set the name (either provided name or default)
                    if camera_name:
                        camera.SetName(camera_name)
                    else:
                        camera.SetName(f"Camera_{int(time.time())}")
                    
                    # Insert the camera into the document
                    doc.InsertObject(camera)
                    doc.AddUndo(c4d.UNDOTYPE_NEW, camera)
                    created_new = True
                    self.log(f"[C4D] Created new camera: {camera.GetName()}")
                
                # Set basic camera properties
                camera[c4d.CAMERA_FOCUS] = focal_length
                
                # Create a target object if requested
                target = None
                if create_target:
                    # Create a null as the camera target
                    target = c4d.BaseObject(c4d.Onull)
                    target.SetName(f"{camera.GetName()}_Target")
                    
                    # Set the target size to be easily visible
                    target[c4d.NULLOBJECT_DISPLAY] = 2  # Display as cross
                    target[c4d.NULLOBJECT_RADIUS] = 20  # Larger size
                    
                    # Insert the target into the document
                    doc.InsertObject(target)
                    doc.AddUndo(c4d.UNDOTYPE_NEW, target)
                    self.log(f"[C4D] Created target: {target.GetName()}")
                    
                    # Set up target tag on camera
                    target_tag = c4d.BaseTag(c4d.Ttargetexpression)
                    target_tag[c4d.TARGETEXPRESSIONTAG_LINK] = target
                    camera.InsertTag(target_tag)
                    self.log("[C4D] Added target tag to camera")
                
                # Look at specific object if requested
                elif target_object:
                    # Find the target object
                    look_at_obj = self.find_object_by_name(doc, target_object)
                    if look_at_obj:
                        # Set up target tag on camera
                        target_tag = c4d.BaseTag(c4d.Ttargetexpression)
                        target_tag[c4d.TARGETEXPRESSIONTAG_LINK] = look_at_obj
                        camera.InsertTag(target_tag)
                        self.log(f"[C4D] Set camera to look at '{look_at_obj.GetName()}'")
                        target = look_at_obj  # Store for use in keyframing
                    else:
                        self.log(f"[C4D] Target object '{target_object}' not found")
                
                # Process keyframes
                keyframe_data = []
                current_fps = doc.GetFps()
                
                # Create keyframes for camera and target
                for kf in keyframes:
                    frame = kf.get("frame", 0)
                    self.log(f"[C4D] Processing keyframe at frame {frame}")
                    
                    # Process camera position
                    if "position" in kf and len(kf["position"]) >= 3:
                        # Set keyframe for camera position
                        pos = kf["position"]
                        success = self.set_position_keyframe(camera, frame, pos)
                        keyframe_data.append({
                            "frame": frame,
                            "type": "camera_position",
                            "value": pos,
                            "success": success
                        })
                    
                    # Process target position if we have a target
                    if target and "target" in kf and len(kf["target"]) >= 3:
                        # Set keyframe for target position
                        target_pos = kf["target"]
                        success = self.set_position_keyframe(target, frame, target_pos)
                        keyframe_data.append({
                            "frame": frame,
                            "type": "target_position",
                            "value": target_pos,
                            "success": success
                        })
                    
                    # Process focal length
                    if "focal_length" in kf:
                        focal = kf["focal_length"]
                        time_obj = c4d.BaseTime(frame, current_fps)
                        
                        # Create track for focal length if doesn't exist
                        track = camera.FindCTrack(c4d.DescID(c4d.DescLevel(c4d.CAMERA_FOCUS, c4d.DTYPE_REAL, 0)))
                        if not track:
                            track = c4d.CTrack(camera, c4d.DescID(c4d.DescLevel(c4d.CAMERA_FOCUS, c4d.DTYPE_REAL, 0)))
                            camera.InsertTrackSorted(track)
                        
                        # Add the key
                        curve = track.GetCurve()
                        key = curve.AddKey(time_obj)
                        if key is not None and key["key"] is not None:
                            key["key"].SetValue(curve, focal)
                            
                            keyframe_data.append({
                                "frame": frame,
                                "type": "focal_length",
                                "value": focal,
                                "success": True
                            })
                        else:
                            self.log(f"[C4D] Failed to set focal length keyframe at frame {frame}")
                            keyframe_data.append({
                                "frame": frame,
                                "type": "focal_length",
                                "value": focal,
                                "success": False
                            })
                
                # Set this camera as active if it's new
                if created_new:
                    # Make it the active camera for the scene
                    doc.SetActiveObject(camera)
                    
                    # Try to set as render camera (handle R2025.1 compatibility)
                    try:
                        self.log("[C4D] Setting as render camera")
                        render_data = doc.GetActiveRenderData()
                        
                        # Set as preview renderer
                        try:
                            render_data[c4d.RDATA_RENDERENGINE] = c4d.RDATA_RENDERENGINE_PREVIEWHARDWARE
                        except:
                            self.log("[C4D] Could not set preview hardware render engine")
                        
                        # Try multiple approaches to set the camera
                        try:
                            # Standard approach
                            render_data[c4d.RDATA_SCENECAMERA] = camera
                            self.log("[C4D] Set as render camera using RDATA_SCENECAMERA")
                        except:
                            try:
                                # Alternative ID approach
                                render_data[300001590] = camera  # Known ID for scene camera
                                self.log("[C4D] Set as render camera using hard-coded ID")
                            except Exception as e2:
                                self.log(f"[C4D] Could not set as render camera: {str(e2)}")
                    except Exception as e:
                        self.log(f"[C4D] Error setting as render camera: {str(e)}")
                
                # Update Cinema 4D
                c4d.EventAdd()
                
                return {
                    "camera": {
                        "name": camera.GetName(),
                        "created_new": created_new,
                        "focal_length": camera[c4d.CAMERA_FOCUS],
                        "keyframes": keyframe_data,
                        "has_target": target is not None,
                        "target_name": target.GetName() if target else None
                    }
                }
            except Exception as e:
                self.log(f"[C4D] Error animating camera: {str(e)}")
                return {"error": f"Failed to animate camera: {str(e)}"}
        
        # Execute on the main thread
        return self.execute_on_main_thread(
            animate_camera_on_main_thread, doc, camera_name, keyframes, focal_length, target_object, create_target
        )
    
    def handle_apply_mograph_fields(self, command):
        """Handle apply_mograph_fields command to add a field to a MoGraph object."""
        doc = c4d.documents.GetActiveDocument()
        
        # Extract parameters
        target_object = command.get("target_object", "")
        field_type = command.get("type", "spherical").lower()
        name = command.get("name", f"{field_type.capitalize()} Field")
        position = command.get("position", [0, 0, 0])
        scale = command.get("scale", 200.0)  # Overall field size
        falloff = command.get("falloff", 0)  # Field falloff type (0=linear, 1=step, etc)
        strength = command.get("strength", 100.0)  # Field strength as a percentage
        parameters = command.get("parameters", {})
        
        # Log what we're doing
        self.log(f"[C4D] Creating {field_type} field for '{target_object}'")
        
        # Define function to execute on main thread
        def apply_field_on_main_thread(doc, target_name, field_type, name, position, scale, falloff, strength, parameters):
            try:
                # Find the target MoGraph object
                target = self.find_object_by_name(doc, target_name)
                if target is None:
                    return {"error": f"Target object not found: {target_name}"}
                
                # Map field types to C4D constants (resolve at runtime for R2025.1 compatibility)
                field_map = {}
                
                # Try to detect if we're in R2025.1 with modules structure
                if hasattr(c4d, "modules") and hasattr(c4d.modules, "mograph"):
                    self.log("[C4D] Using R2025.1 module structure for fields")
                    try:
                        # Use module structure if available
                        field_map = {
                            "spherical": c4d.modules.mograph.FieldLayer.SPHERICAL,
                            "box": c4d.modules.mograph.FieldLayer.BOX,
                            "linear": c4d.modules.mograph.FieldLayer.LINEAR,
                            "radial": c4d.modules.mograph.FieldLayer.RADIAL,
                            "torus": c4d.modules.mograph.FieldLayer.TORUS,
                            "capsule": c4d.modules.mograph.FieldLayer.CAPSULE,
                            "noise": c4d.modules.mograph.FieldLayer.NOISE,
                            "turbulence": c4d.modules.mograph.FieldLayer.TURBULENCE,
                            "formula": c4d.modules.mograph.FieldLayer.FORMULA,
                            "sound": c4d.modules.mograph.FieldLayer.SOUND,
                            "group": c4d.modules.mograph.FieldLayer.GROUP,
                        }
                    except Exception as e:
                        self.log(f"[C4D] Error setting up R2025.1 field map: {str(e)}")
                
                # Fallback to hardcoded IDs if needed
                if not field_map or field_type not in field_map:
                    self.log("[C4D] Using hardcoded IDs for fields")
                    field_map = {
                        "spherical": 440000280,
                        "box": 440000281,
                        "linear": 440000282,
                        "radial": 440000283,
                        "torus": 440000285,
                        "capsule": 440000286,
                        "noise": 440000267,
                        "turbulence": 440000269,
                        "formula": 440000261,
                        "sound": 440000265,
                        "group": 440000271,
                    }
                
                # Get the field type ID
                field_id = field_map.get(field_type)
                if field_id is None:
                    return {"error": f"Unsupported field type: {field_type}"}
                
                # Create a Fields object first (required for all field types)
                try:
                    # Try R2025.1 approach
                    if hasattr(c4d, "modules") and hasattr(c4d.modules, "mograph"):
                        fields_obj = c4d.modules.mograph.FieldObject()
                    else:
                        # Fallback to hardcoded ID
                        fields_obj = c4d.BaseObject(1040306)  # Mo Fields object
                except Exception as e:
                    self.log(f"[C4D] Error creating Fields object: {str(e)}")
                    return {"error": f"Failed to create Fields object: {str(e)}"}
                
                # Set the name
                fields_obj.SetName(name)
                
                # Set position
                if len(position) >= 3:
                    fields_obj.SetAbsPos(c4d.Vector(position[0], position[1], position[2]))
                
                # Create the field layer
                try:
                    field_layer = None
                    if hasattr(c4d, "modules") and hasattr(c4d.modules, "mograph"):
                        # R2025.1 approach
                        field_layer = c4d.modules.mograph.FieldLayer(field_id)
                    else:
                        # Legacy approach with hardcoded ID
                        field_layer = c4d.BaseList2D(field_id)
                        
                    if field_layer is None:
                        return {"error": f"Failed to create field layer of type {field_type}"}
                    
                    # Set field parameters based on type
                    if field_type == "spherical":
                        # Set radius
                        radius = parameters.get("radius", scale / 2)
                        try:
                            # Try R2025.1 first
                            if hasattr(c4d, "modules") and hasattr(c4d.modules, "mograph"):
                                field_layer[c4d.modules.mograph.FIELD_SPHERICAL_RADIUS] = radius
                            else:
                                # Fallback to hardcoded ID
                                field_layer[440000293] = radius  # FIELD_SPHERICAL_RADIUS
                        except Exception as e:
                            self.log(f"[C4D] Could not set spherical radius: {str(e)}")
                    
                    elif field_type == "box":
                        # Set box size
                        size_x = parameters.get("size_x", scale)
                        size_y = parameters.get("size_y", scale)
                        size_z = parameters.get("size_z", scale)
                        try:
                            # Try R2025.1 first
                            if hasattr(c4d, "modules") and hasattr(c4d.modules, "mograph"):
                                field_layer[c4d.modules.mograph.FIELD_BOX_SIZE_X] = size_x
                                field_layer[c4d.modules.mograph.FIELD_BOX_SIZE_Y] = size_y
                                field_layer[c4d.modules.mograph.FIELD_BOX_SIZE_Z] = size_z
                            else:
                                # Fallback to hardcoded IDs
                                field_layer[440000298] = size_x  # FIELD_BOX_SIZE_X
                                field_layer[440000299] = size_y  # FIELD_BOX_SIZE_Y
                                field_layer[440000300] = size_z  # FIELD_BOX_SIZE_Z
                        except Exception as e:
                            self.log(f"[C4D] Could not set box size: {str(e)}")
                    
                    elif field_type == "noise":
                        # Set noise parameters
                        scale_val = parameters.get("scale", 100.0)
                        octaves = parameters.get("octaves", 3)
                        seed = parameters.get("seed", 12345)
                        try:
                            # Try R2025.1 first
                            if hasattr(c4d, "modules") and hasattr(c4d.modules, "mograph"):
                                field_layer[c4d.modules.mograph.FIELD_NOISE_OCTAVES] = octaves
                                field_layer[c4d.modules.mograph.FIELD_NOISE_SCALE] = scale_val
                                field_layer[c4d.modules.mograph.FIELD_NOISE_SEED] = seed
                            else:
                                # Fallback to hardcoded IDs
                                field_layer[440000364] = octaves  # FIELD_NOISE_OCTAVES
                                field_layer[440000361] = scale_val  # FIELD_NOISE_SCALE
                                field_layer[440000362] = seed  # FIELD_NOISE_SEED
                        except Exception as e:
                            self.log(f"[C4D] Could not set noise parameters: {str(e)}")
                    
                    # Set general field parameters (strength, falloff, etc)
                    try:
                        # Set strength
                        if hasattr(c4d, "modules") and hasattr(c4d.modules, "mograph"):
                            field_layer[c4d.modules.mograph.FIELD_STRENGTH] = strength
                        else:
                            # Fallback to hardcoded ID
                            field_layer[440000293] = strength
                        
                        # Set falloff
                        if hasattr(c4d, "modules") and hasattr(c4d.modules, "mograph"):
                            field_layer[c4d.modules.mograph.FIELD_FALLOFF_TYPE] = falloff
                        else:
                            # Fallback to hardcoded ID
                            field_layer[440000246] = falloff
                    except Exception as e:
                        self.log(f"[C4D] Could not set general field parameters: {str(e)}")
                    
                    # Add the field layer to the field object
                    if hasattr(c4d, "modules") and hasattr(c4d.modules, "mograph"):
                        # R2025.1 approach
                        layerdata = fields_obj.GetFieldList()
                        if layerdata:
                            layerdata.InsertLayer(field_layer)
                    else:
                        # Legacy approach
                        fields_obj.InsertLayerList(field_layer)
                    
                except Exception as e:
                    self.log(f"[C4D] Error creating field layer: {str(e)}")
                    return {"error": f"Failed to set up field layer: {str(e)}"}
                
                # Insert the fields object into the document
                doc.InsertObject(fields_obj)
                doc.AddUndo(c4d.UNDOTYPE_NEW, fields_obj)
                
                # Add the field to the target MoGraph object
                try:
                    # Different approach based on target type
                    # For Cloner objects specifically
                    if target.CheckType(1018544):  # Mograph Cloner
                        self.log("[C4D] Applying field to MoGraph Cloner")
                        # Try R2025.1 approach first
                        try:
                            if hasattr(c4d, "modules") and hasattr(c4d.modules, "mograph"):
                                target[c4d.modules.mograph.MGCLONER_EFFECTOR_FIELDLIST] = fields_obj
                            else:
                                # Fallback to hardcoded ID
                                target[1018565] = fields_obj  # MGCLONER_EFFECTOR_FIELDLIST
                        except Exception as e:
                            self.log(f"[C4D] Error linking field to cloner: {str(e)}")
                    else:
                        # Generic approach for all objects
                        self.log("[C4D] Applying field using generic tag approach")
                        try:
                            # Create a Fields tag
                            fields_tag = None
                            if hasattr(c4d, "modules") and hasattr(c4d.modules, "mograph"):
                                fields_tag = c4d.modules.mograph.FieldsTag()
                            else:
                                # Use hardcoded ID as fallback
                                fields_tag = c4d.BaseTag(1040308)  # Fields Tag
                            
                            if fields_tag:
                                # Link the fields object to the tag
                                if hasattr(c4d, "modules") and hasattr(c4d.modules, "mograph"):
                                    fields_tag[c4d.modules.mograph.FIELDTAG_LINK] = fields_obj
                                else:
                                    # Use hardcoded ID as fallback
                                    fields_tag[450000101] = fields_obj  # FIELDTAG_LINK
                                
                                # Add the tag to the target object
                                target.InsertTag(fields_tag)
                                doc.AddUndo(c4d.UNDOTYPE_NEW, fields_tag)
                            else:
                                self.log("[C4D] Could not create Fields tag")
                        except Exception as e:
                            self.log(f"[C4D] Error creating Fields tag: {str(e)}")
                    
                except Exception as e:
                    self.log(f"[C4D] Error applying field to target: {str(e)}")
                
                # Update Cinema 4D
                c4d.EventAdd()
                
                return {
                    "field": {
                        "name": fields_obj.GetName(),
                        "type": field_type,
                        "target": target.GetName(),
                        "position": [position[0], position[1], position[2]],
                    }
                }
            except Exception as e:
                return {"error": f"Failed to create field: {str(e)}"}
        
        # Execute on the main thread
        return self.execute_on_main_thread(
            apply_field_on_main_thread, doc, target_object, field_type, name, position, scale, falloff, strength, parameters
        )
        
    def handle_create_abstract_shape(self, command):
        """Handle create_abstract_shape command to create advanced procedural shapes."""
        doc = c4d.documents.GetActiveDocument()
        
        # Extract parameters
        shape_type = command.get("type", "metaball").lower()
        name = command.get("name", f"{shape_type.capitalize()} Shape")
        position = command.get("position", [0, 0, 0])
        size = command.get("size", [100, 100, 100])
        parameters = command.get("parameters", {})
        
        # Log what we're doing
        self.log(f"[C4D] Creating abstract shape: {shape_type}")
        
        # Define function to execute on main thread
        def create_abstract_shape_on_main_thread(doc, shape_type, name, position, size, parameters):
            try:
                # Map shape types to C4D object IDs - using dynamic detection for R2025.1 compatibility
                shape_map = {}
                
                # Check if we're in R2025.1 with objects module
                if hasattr(c4d, "objects"):
                    self.log("[C4D] Using R2025.1 objects module for shape types")
                    shape_map = {
                        "metaball": c4d.objects.Ometaball,
                        "boolean": c4d.objects.Oboole,
                        "sweep": c4d.objects.Osweep,
                        "loft": c4d.objects.Oloft,
                        "extrude": c4d.objects.Oextrude,
                        "atom": c4d.objects.Oatom,
                        "platonic": c4d.objects.Oplatonic,
                        "formula": c4d.objects.Oformula,
                        "landscape": c4d.objects.Olandscape,
                        "fractal": c4d.objects.Ofractal
                    }
                else:
                    # Fallback to traditional constants or hardcoded IDs
                    self.log("[C4D] Using hardcoded IDs for shape types")
                    shape_map = {
                        "metaball": 5159,   # c4d.Ometaball
                        "boolean": 5142,    # c4d.Oboole
                        "sweep": 5118,      # c4d.Osweep
                        "loft": 5107,       # c4d.Oloft
                        "extrude": 5116,    # c4d.Oextrude
                        "atom": 5168,       # c4d.Oatom
                        "platonic": 5170,   # c4d.Oplatonic
                        "formula": 5179,    # c4d.Oformula
                        "landscape": 5119,  # c4d.Olandscape
                        "fractal": 5171     # c4d.Ofractal
                    }
                
                # Get the appropriate shape type ID
                shape_id = shape_map.get(shape_type)
                if shape_id is None:
                    return {"error": f"Unsupported shape type: {shape_type}"}
                
                # Create the shape object
                shape = c4d.BaseObject(shape_id)
                if shape is None:
                    return {"error": f"Failed to create {shape_type} object"}
                
                # Set name
                shape.SetName(name)
                
                # Set position
                if len(position) >= 3:
                    shape.SetAbsPos(c4d.Vector(position[0], position[1], position[2]))
                
                # Apply type-specific parameters
                if shape_type == "metaball":
                    # Set metaball parameters
                    subdivisions = parameters.get("subdivisions", 24)
                    editor_subdivisions = parameters.get("editor_subdivisions", 6)
                    hull_value = parameters.get("hull_value", 0.5)
                    
                    shape[c4d.METABALLOBJECT_SUBEDITOR] = editor_subdivisions
                    shape[c4d.METABALLOBJECT_SUBRAY] = subdivisions
                    shape[c4d.METABALLOBJECT_THRESHOLD] = hull_value
                    
                    # Create child elements if specified
                    elements = parameters.get("elements", [])
                    for i, elem in enumerate(elements):
                        # Create a sphere child for each element
                        elem_pos = elem.get("position", [0, 0, 0])
                        elem_radius = elem.get("radius", 50)
                        elem_weight = elem.get("weight", 1.0)
                        
                        # Create metaball element
                        element = c4d.BaseObject(c4d.Osphere)
                        element.SetName(f"Element_{i+1}")
                        element.SetRelPos(c4d.Vector(elem_pos[0], elem_pos[1], elem_pos[2]))
                        element[c4d.PRIM_SPHERE_RAD] = elem_radius
                        
                        # Add metaball-specific tag with weight
                        metatag = c4d.BaseTag(c4d.Tmetaball)
                        metatag[c4d.METABALLTAG_WEIGHT] = elem_weight
                        element.InsertTag(metatag)
                        
                        # Insert under the metaball object
                        element.InsertUnder(shape)
                        doc.AddUndo(c4d.UNDOTYPE_NEW, element)
                
                elif shape_type == "boolean":
                    # Set boolean parameters
                    operation = parameters.get("operation", 0)  # 0=union, 1=subtract, 2=intersect
                    shape[c4d.BOOLEOBJECT_TYPE] = operation
                    
                    # Create child objects if specified
                    children = parameters.get("children", [])
                    for i, child in enumerate(children):
                        child_type = child.get("type", "cube")
                        child_pos = child.get("position", [0, 0, 0])
                        child_size = child.get("size", [50, 50, 50])
                        
                        # Create appropriate primitive
                        child_obj = None
                        if child_type == "cube":
                            child_obj = c4d.BaseObject(c4d.Ocube)
                            child_obj[c4d.PRIM_CUBE_LEN] = c4d.Vector(child_size[0], child_size[1], child_size[2])
                        elif child_type == "sphere":
                            child_obj = c4d.BaseObject(c4d.Osphere)
                            child_obj[c4d.PRIM_SPHERE_RAD] = child_size[0] / 2
                        elif child_type == "cylinder":
                            child_obj = c4d.BaseObject(c4d.Ocylinder)
                            child_obj[c4d.PRIM_CYLINDER_RADIUS] = child_size[0] / 2
                            child_obj[c4d.PRIM_CYLINDER_HEIGHT] = child_size[1]
                        
                        if child_obj:
                            child_obj.SetName(f"Boolean_{child_type}_{i+1}")
                            child_obj.SetRelPos(c4d.Vector(child_pos[0], child_pos[1], child_pos[2]))
                            child_obj.InsertUnder(shape)
                            doc.AddUndo(c4d.UNDOTYPE_NEW, child_obj)
                
                elif shape_type == "platonic":
                    # Set platonic solid parameters
                    platonic_type = parameters.get("platonic_type", 0)  # 0=tetrahedron, 1=hexahedron, 2=octahedron, 3=dodecahedron, 4=icosahedron
                    segment_count = parameters.get("segments", 1)
                    radius = parameters.get("radius", size[0] / 2)
                    
                    shape[c4d.PRIM_PLATONIC_TYPE] = platonic_type
                    shape[c4d.PRIM_PLATONIC_RADIUS] = radius
                    shape[c4d.PRIM_PLATONIC_SEGMENTS] = segment_count
                
                elif shape_type == "landscape":
                    # Set landscape parameters
                    width = parameters.get("width", size[0])
                    height = parameters.get("height", size[1])
                    length = parameters.get("length", size[2])
                    seed = parameters.get("seed", 12345)
                    
                    shape[c4d.LANDSCAPEOBJECT_WIDTH] = width
                    shape[c4d.LANDSCAPEOBJECT_HEIGHT] = height
                    shape[c4d.LANDSCAPEOBJECT_LENGTH] = length
                    shape[c4d.LANDSCAPEOBJECT_SEED] = seed
                
                # Insert the shape into the document
                doc.InsertObject(shape)
                doc.AddUndo(c4d.UNDOTYPE_NEW, shape)
                
                # Make it the active object
                doc.SetActiveObject(shape)
                
                # Update the document
                c4d.EventAdd()
                
                return {
                    "abstract_shape": {
                        "name": shape.GetName(),
                        "type": shape_type,
                        "position": [position[0], position[1], position[2]]
                    }
                }
            except Exception as e:
                self.log(f"[C4D] Error creating abstract shape: {str(e)}")
                return {"error": f"Failed to create abstract shape: {str(e)}"}
        
        # Execute on the main thread
        return self.execute_on_main_thread(
            create_abstract_shape_on_main_thread, doc, shape_type, name, position, size, parameters
        )
        
    def handle_apply_dynamics(self, command):
        """Handle apply_dynamics command to add dynamic simulations to objects."""
        doc = c4d.documents.GetActiveDocument()
        
        # Extract parameters
        object_name = command.get("object_name", "")
        dynamics_type = command.get("type", "rigid_body").lower()
        properties = command.get("properties", {})
        
        # Log what we're doing
        self.log(f"[C4D] Applying {dynamics_type} dynamics to '{object_name}'")
        
        # Define function to execute on main thread
        def apply_dynamics_on_main_thread(doc, object_name, dynamics_type, properties):
            try:
                # Find the object
                obj = self.find_object_by_name(doc, object_name)
                if obj is None:
                    return {"error": f"Object not found: {object_name}"}
                
                # Map dynamics types to tag IDs
                dynamics_map = {
                    "rigid_body": 180000102,  # Rigid Body tag
                    "soft_body": 180000104,   # Soft Body tag
                    "cloth": 180000106,       # Cloth tag
                    "collider": 180000103,    # Collider Body tag
                    "connector": 180000105,   # Connector tag
                    "ghost": 180000107        # Ghost tag
                }
                
                # Get the appropriate tag ID
                tag_id = dynamics_map.get(dynamics_type)
                if tag_id is None:
                    return {"error": f"Unsupported dynamics type: {dynamics_type}"}
                
                # Check if the object already has this dynamics tag
                existing_tag = None
                tags = obj.GetTags()
                for tag in tags:
                    if tag.GetType() == tag_id:
                        existing_tag = tag
                        break
                
                # Use existing tag or create a new one
                tag = existing_tag or c4d.BaseTag(tag_id)
                if tag is None:
                    return {"error": f"Failed to create {dynamics_type} tag"}
                
                # Common dynamics properties
                mass = properties.get("mass", 1.0)
                is_dynamic = properties.get("is_dynamic", True)
                linear_damping = properties.get("linear_damping", 0.1)
                angular_damping = properties.get("angular_damping", 0.1)
                collision_margin = properties.get("collision_margin", 1.0)
                
                # Apply specific dynamics properties based on type
                if dynamics_type == "rigid_body":
                    # Rigid body specific settings
                    tag[c4d.RIGID_BODY_MASS] = mass
                    tag[c4d.RIGID_BODY_LINEAR_FOLLOW_STRENGTH] = properties.get("linear_follow", 1.0)
                    tag[c4d.RIGID_BODY_ANGULAR_FOLLOW_STRENGTH] = properties.get("angular_follow", 1.0)
                    tag[c4d.RIGID_BODY_DYNAMIC] = is_dynamic
                    tag[c4d.RIGID_BODY_LINEAR_DAMPING] = linear_damping
                    tag[c4d.RIGID_BODY_ANGULAR_DAMPING] = angular_damping
                    
                    # Set collision shape
                    collision_shape = properties.get("collision_shape", 0)  # 0=automatic, 1=static mesh, 2=moving mesh, etc.
                    tag[c4d.RIGID_BODY_COLLISION_SHAPE] = collision_shape
                    
                elif dynamics_type == "soft_body":
                    # Soft body specific settings
                    tag[c4d.SOFT_BODY_MASS] = mass
                    tag[c4d.SOFT_BODY_STRUCTURAL_STIFFNESS] = properties.get("stiffness", 0.5)
                    tag[c4d.SOFT_BODY_BENDING_STIFFNESS] = properties.get("bending", 0.5)
                    tag[c4d.SOFT_BODY_DAMPING] = properties.get("damping", 0.01)
                    tag[c4d.SOFT_BODY_PRESSURE] = properties.get("pressure", 0.0)
                    
                    # Set soft body subdivisions
                    subdivisions = properties.get("subdivisions", 3)
                    tag[c4d.SOFT_BODY_SUBHARDEDGES] = subdivisions
                    
                elif dynamics_type == "cloth":
                    # Cloth specific settings
                    tag[c4d.CLOTH_MASS] = mass
                    tag[c4d.CLOTH_STIFFNESS] = properties.get("stiffness", 1.0)
                    tag[c4d.CLOTH_BENDING] = properties.get("bending", 1.0)
                    tag[c4d.CLOTH_DAMPING] = properties.get("damping", 0.01)
                    tag[c4d.CLOTH_TEAR_RESISTANCE] = properties.get("tear_resistance", 100.0)
                    tag[c4d.CLOTH_PRESSURE] = properties.get("pressure", 0.0)
                    
                elif dynamics_type == "collider":
                    # Collider specific settings
                    tag[c4d.COLLIDER_BODY_MARGIN] = collision_margin
                    
                # Add the tag to the object if it's new
                if not existing_tag:
                    obj.InsertTag(tag)
                    doc.AddUndo(c4d.UNDOTYPE_NEW, tag)
                else:
                    # Mark existing tag as modified
                    doc.AddUndo(c4d.UNDOTYPE_CHANGE, tag)
                
                # Update the document
                c4d.EventAdd()
                
                return {
                    "dynamics": {
                        "object": obj.GetName(),
                        "type": dynamics_type,
                        "mass": mass,
                        "is_dynamic": is_dynamic
                    }
                }
            except Exception as e:
                self.log(f"[C4D] Error applying dynamics: {str(e)}")
                return {"error": f"Failed to apply dynamics: {str(e)}"}
        
        # Execute on the main thread
        return self.execute_on_main_thread(
            apply_dynamics_on_main_thread, doc, object_name, dynamics_type, properties
        )
        
    def handle_create_soft_body(self, command):
        """Handle create_soft_body command to create a soft body simulation."""
        doc = c4d.documents.GetActiveDocument()
        
        # Extract parameters
        name = command.get("name", "Soft Body")
        geometry_type = command.get("geometry", "sphere").lower()
        position = command.get("position", [0, 0, 0])
        size = command.get("size", [100, 100, 100])
        properties = command.get("properties", {})
        
        # Log what we're doing
        self.log(f"[C4D] Creating soft body: {name} with {geometry_type} geometry")
        
        # Define function to execute on main thread
        def create_soft_body_on_main_thread(doc, name, geometry_type, position, size, properties):
            try:
                # Create the base geometry
                geometry = None
                
                if geometry_type == "sphere":
                    geometry = c4d.BaseObject(c4d.Osphere)
                    radius = size[0] / 2
                    geometry[c4d.PRIM_SPHERE_RAD] = radius
                    geometry[c4d.PRIM_SPHERE_SUB] = properties.get("subdivisions", 16)
                    
                elif geometry_type == "cube":
                    geometry = c4d.BaseObject(c4d.Ocube)
                    geometry[c4d.PRIM_CUBE_LEN] = c4d.Vector(size[0], size[1], size[2])
                    
                elif geometry_type == "cylinder":
                    geometry = c4d.BaseObject(c4d.Ocylinder)
                    geometry[c4d.PRIM_CYLINDER_RADIUS] = size[0] / 2
                    geometry[c4d.PRIM_CYLINDER_HEIGHT] = size[1]
                    
                elif geometry_type == "plane":
                    geometry = c4d.BaseObject(c4d.Oplane)
                    geometry[c4d.PRIM_PLANE_WIDTH] = size[0]
                    geometry[c4d.PRIM_PLANE_HEIGHT] = size[1]
                    geometry[c4d.PRIM_PLANE_SUBW] = properties.get("width_subdivisions", 10)
                    geometry[c4d.PRIM_PLANE_SUBH] = properties.get("height_subdivisions", 10)
                
                elif geometry_type == "torus":
                    geometry = c4d.BaseObject(c4d.Otorus)
                    geometry[c4d.PRIM_TORUS_RADIUS] = size[0] / 2
                    geometry[c4d.PRIM_TORUS_PIPE] = size[1] / 2
                    geometry[c4d.PRIM_TORUS_HSUB] = properties.get("ring_subdivisions", 24)
                    geometry[c4d.PRIM_TORUS_PSUB] = properties.get("pipe_subdivisions", 12)
                
                if geometry is None:
                    return {"error": f"Unsupported geometry type: {geometry_type}"}
                
                # Set the name and position
                geometry.SetName(name)
                
                if len(position) >= 3:
                    geometry.SetAbsPos(c4d.Vector(position[0], position[1], position[2]))
                
                # Insert the geometry into the document
                doc.InsertObject(geometry)
                doc.AddUndo(c4d.UNDOTYPE_NEW, geometry)
                
                # Add a Soft Body dynamics tag
                tag = c4d.BaseTag(180000104)  # Soft Body tag
                
                # Set soft body properties
                mass = properties.get("mass", 1.0)
                stiffness = properties.get("stiffness", 0.5)
                bending = properties.get("bending", 0.5)
                damping = properties.get("damping", 0.01)
                pressure = properties.get("pressure", 0.0)
                
                tag[c4d.SOFT_BODY_MASS] = mass
                tag[c4d.SOFT_BODY_STRUCTURAL_STIFFNESS] = stiffness
                tag[c4d.SOFT_BODY_BENDING_STIFFNESS] = bending
                tag[c4d.SOFT_BODY_DAMPING] = damping
                tag[c4d.SOFT_BODY_PRESSURE] = pressure
                
                # Set soft body subdivisions
                subdivisions = properties.get("subdivisions", 3)
                tag[c4d.SOFT_BODY_SUBHARDEDGES] = subdivisions
                
                # Self collision
                self_collision = properties.get("self_collision", False)
                if self_collision:
                    tag[c4d.SOFT_BODY_SELF_COLLISION] = self_collision
                
                # Add the tag to the object
                geometry.InsertTag(tag)
                doc.AddUndo(c4d.UNDOTYPE_NEW, tag)
                
                # Create a floor (collision object) if requested
                if properties.get("add_floor", True):
                    floor = c4d.BaseObject(c4d.Oplane)
                    floor.SetName(f"{name}_Floor")
                    floor[c4d.PRIM_PLANE_WIDTH] = size[0] * 10
                    floor[c4d.PRIM_PLANE_HEIGHT] = size[1] * 10
                    
                    # Position the floor below the soft body
                    floor_pos = [position[0], position[1] - size[1], position[2]]
                    floor.SetAbsPos(c4d.Vector(floor_pos[0], floor_pos[1], floor_pos[2]))
                    
                    # Add a Collider Body tag
                    collider_tag = c4d.BaseTag(180000103)  # Collider Body tag
                    floor.InsertTag(collider_tag)
                    
                    # Insert the floor into the document
                    doc.InsertObject(floor)
                    doc.AddUndo(c4d.UNDOTYPE_NEW, floor)
                
                # Make the soft body the active object
                doc.SetActiveObject(geometry)
                
                # Update the document
                c4d.EventAdd()
                
                return {
                    "soft_body": {
                        "name": geometry.GetName(),
                        "geometry": geometry_type,
                        "position": [position[0], position[1], position[2]],
                        "mass": mass,
                        "stiffness": stiffness,
                        "has_floor": properties.get("add_floor", True)
                    }
                }
            except Exception as e:
                self.log(f"[C4D] Error creating soft body: {str(e)}")
                return {"error": f"Failed to create soft body: {str(e)}"}
        
        # Execute on the main thread
        return self.execute_on_main_thread(
            create_soft_body_on_main_thread, doc, name, geometry_type, position, size, properties
        )
    
    def handle_debug_redshift_material(self, command):
        """Debug handler to test Redshift material creation."""
        self.log("[C4D] DEBUG: Testing Redshift material creation...")

        try:
            doc = c4d.documents.GetActiveDocument()
            # Get diagnostic info
            diagnostic = {
                "c4d_version": c4d.GetC4DVersion(),
                "has_redshift_module": hasattr(c4d.modules, "redshift"),
                "plugin_info": [],
            }

            # Check for Redshift plugin
            plugins = c4d.plugins.FilterPluginList(c4d.PLUGINTYPE_MATERIAL, True)
            redshift_plugin_id = None

            for plugin in plugins:
                plugin_name = plugin.GetName()
                plugin_id = plugin.GetID()
                diagnostic["plugin_info"].append({"name": plugin_name, "id": plugin_id})

                if "redshift" in plugin_name.lower():
                    redshift_plugin_id = plugin_id
                    diagnostic["redshift_plugin_id"] = plugin_id

            # Create a test material
            test_name = f"Debug_RS_{int(time.time())}"
            mat = None
            
            # Create standard material as fallback
            mat = c4d.BaseMaterial(c4d.Mmaterial)
            mat.SetName(test_name)
            mat[c4d.MATERIAL_COLOR_COLOR] = c4d.Vector(1, 0, 0)

            # Insert material into document
            doc.InsertMaterial(mat)
            c4d.EventAdd()

            # Return detailed diagnostic information
            return {
                "status": "ok",
                "message": "Debug material test complete",
                "diagnostic": diagnostic,
                "material_type": "standard",
                "material_name": test_name,
            }

        except Exception as e:
            return {
                "status": "error",
                "message": f"Error in debug material: {str(e)}",
            }


class SocketServerDialog(gui.GeDialog):
    """GUI Dialog to control the server and display logs."""

    def __init__(self):
        super(SocketServerDialog, self).__init__()
        self.server = None
        self.msg_queue = queue.Queue()  # Thread-safe queue
        self.SetTimer(100)  # Update UI at 10 Hz

    def CreateLayout(self):
        self.SetTitle("Socket Server Control")

        self.status_text = self.AddStaticText(
            1002, c4d.BFH_SCALEFIT, name="Server: Offline"
        )

        self.GroupBegin(1010, c4d.BFH_SCALEFIT, 2, 1)
        self.AddButton(1011, c4d.BFH_SCALE, name="Start Server")
        self.AddButton(1012, c4d.BFH_SCALE, name="Stop Server")
        self.GroupEnd()

        self.log_box = self.AddMultiLineEditText(
            1004,
            c4d.BFH_SCALEFIT,
            initw=400,
            inith=250,
            style=c4d.DR_MULTILINE_READONLY,
        )

        self.Enable(1012, False)  # Disable "Stop" button initially
        return True

    def CoreMessage(self, id, msg):
        """Handles UI updates and main thread execution triggered by SpecialEventAdd()."""
        if id == PLUGIN_ID:
            try:
                # Process all pending messages in the queue
                while not self.msg_queue.empty():
                    try:
                        # Get next message from queue with timeout
                        msg_type, msg_value = self.msg_queue.get(timeout=0.1)

                        # Process based on message type
                        if msg_type == "STATUS":
                            self.UpdateStatusText(msg_value)
                        elif msg_type == "LOG":
                            self.AppendLog(msg_value)
                        elif msg_type == "EXEC":
                            # Execute function on main thread
                            if callable(msg_value):
                                try:
                                    msg_value()
                                except Exception as e:
                                    error_msg = f"[C4D] Error in main thread execution: {str(e)}"
                                    self.AppendLog(error_msg)
                            else:
                                self.AppendLog(
                                    f"[C4D] Warning: Non-callable value received: {type(msg_value)}"
                                )
                    except queue.Empty:
                        # Queue timeout - break the loop
                        break
                    except Exception as e:
                        error_msg = f"[C4D] Error processing message: {str(e)}"
                        self.AppendLog(error_msg)
            except Exception as e:
                # Catch all exceptions to prevent Cinema 4D from crashing
                error_msg = f"[C4D] Critical error in message processing: {str(e)}"
                print(error_msg)  # Print to console as UI might be unstable

        return True

    def Timer(self, msg):
        """Periodic UI update in case SpecialEventAdd() missed something."""
        if self.server:
            if not self.server.running:  # Detect unexpected crashes
                self.UpdateStatusText("Offline")
                self.Enable(1011, True)
                self.Enable(1012, False)
        return True

    def UpdateStatusText(self, status):
        """Update server status UI."""
        self.SetString(1002, f"Server: {status}")
        self.Enable(1011, status == "Offline")
        self.Enable(1012, status == "Online")

    def AppendLog(self, message):
        """Append log messages to UI."""
        existing_text = self.GetString(1004)
        new_text = (existing_text + "\n" + message).strip()
        self.SetString(1004, new_text)

    def Command(self, id, msg):
        if id == 1011:  # Start Server button
            self.StartServer()
            return True
        elif id == 1012:  # Stop Server button
            self.StopServer()
            return True
        return False

    def StartServer(self):
        """Start the socket server thread."""
        if not self.server:
            self.server = C4DSocketServer(msg_queue=self.msg_queue)
            self.server.start()
            self.Enable(1011, False)
            self.Enable(1012, True)

    def StopServer(self):
        """Stop the socket server."""
        if self.server:
            self.server.stop()
            self.server = None
            self.Enable(1011, True)
            self.Enable(1012, False)


class SocketServerPlugin(c4d.plugins.CommandData):
    """Cinema 4D Plugin Wrapper"""

    PLUGIN_ID = 1057843
    PLUGIN_NAME = "Socket Server Plugin"

    def __init__(self):
        self.dialog = None
        
    def GetSubContainer(self):
        """R2025.1 SDK - Return the sub container for the plugin"""
        return None
        
    def Message(self, type, data):
        """R2025.1 SDK - Process messages from Cinema 4D"""
        return True

    def Execute(self, doc):
        if self.dialog is None:
            self.dialog = SocketServerDialog()
        return self.dialog.Open(
            dlgtype=c4d.DLG_TYPE_ASYNC,
            pluginid=self.PLUGIN_ID,
            defaultw=400,
            defaulth=300,
        )

    def GetState(self, doc):
        return c4d.CMD_ENABLED


if __name__ == "__main__":
    # R2025.1 plugin registration
    c4d.plugins.RegisterCommandPlugin(
        SocketServerPlugin.PLUGIN_ID,
        SocketServerPlugin.PLUGIN_NAME,
        0,          # Flags
        None,       # Icon (must be None for R2025.1)
        None,       # Help text ID (must be None for R2025.1)
        SocketServerPlugin(),
    )