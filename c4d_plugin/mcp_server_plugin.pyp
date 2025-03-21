"""
Cinema 4D MCP Server Plugin
Updated for Cinema 4D R2025 compatibility
Version 0.1.6 - Improved Redshift material creation with NodeMaterial approach
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
        "[C4D MCP] WARNING: This plugin is designed for Cinema 4D R20 or later. Some features may not work correctly."
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
        """Execute a function on the main thread using a thread-safe queue and special event.

        Since CallMainThread is not available in the Python SDK (R2025), we use
        a thread-safe approach by queuing the function and triggering it via SpecialEventAdd.

        Args:
            func: The function to execute on the main thread
            *args: Arguments to pass to the function
            **kwargs: Keyword arguments to pass to the function
                      Special keyword '_timeout': Override default timeout (in seconds)

        Returns:
            The result of executing the function on the main thread
        """
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
            elif "field" in func_name.lower():
                timeout = 30  # 30 seconds for field operations
                self.log(f"[C4D] Using extended timeout (30s) for field operation")
            else:
                timeout = 15  # Default timeout increased to 15 seconds

        self.log(f"[C4D] Main thread execution will timeout after {timeout}s")

        # Create a thread-safe container for the result
        result_container = {"result": None, "done": False}

        # Define a wrapper that will be executed on the main thread
        def main_thread_exec():
            try:
                self.log(
                    f"[C4D] Starting main thread execution of {func.__name__ if hasattr(func, '__name__') else 'function'}"
                )
                start_time = time.time()
                result_container["result"] = func(*args, **kwargs)
                execution_time = time.time() - start_time
                self.log(
                    f"[C4D] Main thread execution completed in {execution_time:.2f}s"
                )
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
        progress_interval = 1.0  # Log progress every second
        last_progress = 0

        while not result_container["done"]:
            time.sleep(poll_interval)

            # Calculate elapsed time
            elapsed = time.time() - start_time

            # Log progress periodically for long-running operations
            if int(elapsed) > last_progress:
                if elapsed > 5:  # Only start logging after 5 seconds
                    self.log(
                        f"[C4D] Waiting for main thread execution ({elapsed:.1f}s elapsed)"
                    )
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
                    self.log(f"[C4D] Received: {message}")

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
                        elif command_type == "modify_object":
                            response = self.handle_modify_object(command)
                        elif command_type == "create_material":
                            response = self.handle_create_material(command)
                        elif command_type == "apply_material":
                            response = self.handle_apply_material(command)
                        elif command_type == "render_frame":
                            response = self.handle_render_frame(command)
                        elif command_type == "set_keyframe":
                            response = self.handle_set_keyframe(command)
                        elif command_type == "save_scene":
                            response = self.handle_save_scene(command)
                        elif command_type == "load_scene":
                            response = self.handle_load_scene(command)
                        elif command_type == "execute_python":
                            response = self.handle_execute_python(command)
                        # advanced commands
                        elif command_type == "create_mograph_cloner":
                            response = self.handle_create_mograph_cloner(command)
                        elif command_type == "add_effector":
                            response = self.handle_add_effector(command)
                        elif command_type == "apply_mograph_fields":
                            response = self.handle_apply_mograph_fields(command)
                        elif command_type == "create_soft_body":
                            response = self.handle_create_soft_body(command)
                        elif command_type == "apply_dynamics":
                            response = self.handle_apply_dynamics(command)
                        elif command_type == "create_abstract_shape":
                            response = self.handle_create_abstract_shape(command)
                        elif command_type == "create_light":
                            response = self.handle_create_light(command)
                        elif command_type == "apply_shader":
                            response = self.handle_apply_shader(command)
                        elif command_type == "animate_camera":
                            response = self.handle_animate_camera(command)
                        elif command["command"] == "validate_redshift_materials":
                            response = self.handle_validate_redshift_materials(command)
                        else:
                            response = {"error": f"Unknown command: {command_type}"}

                        # Send the response as JSON
                        response_json = json.dumps(response) + "\n"
                        client.sendall(response_json.encode("utf-8"))
                        self.log(f"[C4D] Sent response for {command_type}")

                    except json.JSONDecodeError:
                        error_response = {"error": "Invalid JSON format"}
                        client.sendall(
                            (json.dumps(error_response) + "\n").encode("utf-8")
                        )
                    except Exception as e:
                        error_response = {
                            "error": f"Error processing command: {str(e)}"
                        }
                        client.sendall(
                            (json.dumps(error_response) + "\n").encode("utf-8")
                        )
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

    # Basic commands
    def handle_get_scene_info(self):
        """Handle get_scene_info command."""
        doc = c4d.documents.GetActiveDocument()

        # Get scene information
        scene_info = {
            "filename": doc.GetDocumentName() or "Untitled",
            "object_count": self.count_objects(doc),
            "polygon_count": self.count_polygons(doc),
            "material_count": len(doc.GetMaterials()),
            "current_frame": doc.GetTime().GetFrame(doc.GetFps()),
            "fps": doc.GetFps(),
            "frame_start": doc.GetMinTime().GetFrame(doc.GetFps()),
            "frame_end": doc.GetMaxTime().GetFrame(doc.GetFps()),
        }

        return {"scene_info": scene_info}

    def handle_list_objects(self):
        """Handle list_objects command with comprehensive object detection including MoGraph objects."""
        doc = c4d.documents.GetActiveDocument()
        objects = []
        found_ids = set()  # Track object IDs to avoid duplicates

        # Function to recursively get all objects including children with improved traversal
        def get_objects_recursive(start_obj, depth=0):
            current_obj = start_obj
            while current_obj:
                try:
                    # Get object ID to avoid duplicates
                    obj_id = str(current_obj.GetGUID())

                    # Skip if we've already processed this object
                    if obj_id in found_ids:
                        current_obj = current_obj.GetNext()
                        continue

                    found_ids.add(obj_id)

                    # Get object name and type
                    obj_name = current_obj.GetName()
                    obj_type_id = current_obj.GetType()

                    # Get basic object info with enhanced MoGraph detection
                    obj_type = self.get_object_type_name(current_obj)

                    # Additional properties dictionary for specific object types
                    additional_props = {}

                    # MoGraph Cloner enhanced detection - explicitly check for cloner type
                    if obj_type_id == c4d.Omgcloner:
                        obj_type = "MoGraph Cloner"
                        try:
                            # Get the cloner mode
                            mode_id = current_obj[c4d.ID_MG_MOTIONGENERATOR_MODE]
                            modes = {0: "Linear", 1: "Grid", 2: "Radial", 3: "Object"}
                            mode_name = modes.get(mode_id, f"Mode {mode_id}")
                            additional_props["cloner_mode"] = mode_name

                            # Add counts based on mode
                            if mode_id == 0:  # Linear
                                additional_props["count"] = current_obj[
                                    c4d.MG_LINEAR_COUNT
                                ]
                            elif mode_id == 1:  # Grid
                                additional_props["count_x"] = current_obj[
                                    c4d.MG_GRID_COUNT_X
                                ]
                                additional_props["count_y"] = current_obj[
                                    c4d.MG_GRID_COUNT_Y
                                ]
                                additional_props["count_z"] = current_obj[
                                    c4d.MG_GRID_COUNT_Z
                                ]
                            elif mode_id == 2:  # Radial
                                additional_props["count"] = current_obj[
                                    c4d.MG_POLY_COUNT
                                ]

                            self.log(
                                f"[C4D] Detected MoGraph Cloner: {obj_name}, Mode: {mode_name}"
                            )
                        except Exception as e:
                            self.log(f"[C4D] Error getting cloner details: {str(e)}")

                    # MoGraph Effector enhanced detection
                    elif 1019544 <= obj_type_id <= 1019644:
                        if obj_type_id == c4d.Omgrandom:
                            obj_type = "Random Effector"
                        elif obj_type_id == c4d.Omgformula:
                            obj_type = "Formula Effector"
                        elif hasattr(c4d, "Omgstep") and obj_type_id == c4d.Omgstep:
                            obj_type = "Step Effector"
                        else:
                            obj_type = "MoGraph Effector"

                        # Try to get effector strength
                        try:
                            if hasattr(c4d, "ID_MG_BASEEFFECTOR_STRENGTH"):
                                additional_props["strength"] = current_obj[
                                    c4d.ID_MG_BASEEFFECTOR_STRENGTH
                                ]
                        except:
                            pass

                    # Field objects enhanced detection
                    elif 1039384 <= obj_type_id <= 1039484:
                        field_types = {
                            1039384: "Spherical Field",
                            1039385: "Box Field",
                            1039386: "Cylindrical Field",
                            1039387: "Torus Field",
                            1039388: "Cone Field",
                            1039389: "Linear Field",
                            1039390: "Radial Field",
                            1039394: "Noise Field",
                        }
                        obj_type = field_types.get(obj_type_id, "Field")

                        # Try to get field strength
                        try:
                            if hasattr(c4d, "FIELD_STRENGTH"):
                                additional_props["strength"] = current_obj[
                                    c4d.FIELD_STRENGTH
                                ]
                        except:
                            pass

                    # Basic object information
                    obj_info = {
                        "id": obj_id,
                        "name": obj_name,
                        "type": obj_type,
                        "type_id": obj_type_id,
                        "level": depth,
                        **additional_props,  # Include any additional properties
                    }

                    # Add position and scale if applicable
                    if hasattr(current_obj, "GetAbsPos"):
                        pos = current_obj.GetAbsPos()
                        obj_info["position"] = [pos.x, pos.y, pos.z]
                    if hasattr(current_obj, "GetAbsScale"):
                        scale = current_obj.GetAbsScale()
                        obj_info["scale"] = [scale.x, scale.y, scale.z]

                    # Add to the list
                    objects.append(obj_info)

                    # Process children
                    if current_obj.GetDown():
                        get_objects_recursive(current_obj.GetDown(), depth + 1)

                    # Move to next object
                    current_obj = current_obj.GetNext()
                except Exception as e:
                    self.log(f"[C4D] Error processing object: {str(e)}")
                    if current_obj:
                        current_obj = current_obj.GetNext()

        def get_all_root_objects():
            # Start with standard objects
            get_objects_recursive(doc.GetFirstObject())

            # Also check for MoGraph objects that might not be in main hierarchy
            # (This is more for thoroughness as get_objects_recursive should find everything)
            try:
                if hasattr(c4d, "GetMoData"):
                    mograph_data = c4d.GetMoData(doc)
                    if mograph_data:
                        for i in range(mograph_data.GetCount()):
                            obj = mograph_data.GetObject(i)
                            if obj and obj.GetType() == c4d.Omgcloner:
                                if str(obj.GetGUID()) not in found_ids:
                                    get_objects_recursive(obj)
            except Exception as e:
                self.log(f"[C4D] Error checking MoGraph objects: {str(e)}")

        # Get all objects starting from the root level
        get_all_root_objects()

        self.log(
            f"[C4D] Comprehensive object search complete, found {len(objects)} objects"
        )
        return {"objects": objects}

    def get_redshift_material_id(self):
        """Detect Redshift material ID by examining existing materials.
        
        This function scans the active document for materials with type IDs
        in the range typical for Redshift materials (over 1,000,000).
        
        Returns:
            A BaseMaterial with the detected Redshift material type or None if not found
        """
        doc = c4d.documents.GetActiveDocument()
        
        # Look for existing Redshift materials to detect the proper ID
        for mat in doc.GetMaterials():
            mat_type = mat.GetType()
            if mat_type >= 1000000:
                self.log(f"[C4D] Found existing Redshift material with type ID: {mat_type}")
                # Try to create a material with this ID
                try:
                    rs_mat = c4d.BaseMaterial(mat_type)
                    if rs_mat and rs_mat.GetType() == mat_type:
                        self.log(f"[C4D] Successfully created Redshift material using detected ID: {mat_type}")
                        return rs_mat
                except:
                    pass
                    
        # If Python scripting can create Redshift materials, try this method
        try:
            # Execute a Python script to create a Redshift material
            script = """
import c4d
doc = c4d.documents.GetActiveDocument()
# Try with known Redshift ID
rs_mat = c4d.BaseMaterial(1036224)
if rs_mat:
    rs_mat.SetName("TempRedshiftMaterial")
    doc.InsertMaterial(rs_mat)
    c4d.EventAdd()
"""
            # Only try script-based approach if explicitly allowed
            if hasattr(c4d, "modules") and hasattr(c4d.modules, "net") and hasattr(c4d.modules.net, "Execute"):
                # Execute in a controlled way that won't affect normal operation
                import tempfile, os
                script_path = None
                try:
                    with tempfile.NamedTemporaryFile(suffix='.py', delete=False) as f:
                        f.write(script.encode('utf-8'))
                        script_path = f.name
                        
                    # Try to execute this script
                    self.execute_on_main_thread(lambda: c4d.modules.net.Execute(script_path))
                finally:
                    # Always clean up the temp file
                    if script_path and os.path.exists(script_path):
                        try:
                            os.unlink(script_path)
                        except:
                            pass
            
            # Now look for the material we created
            temp_mat = self.find_material_by_name(doc, "TempRedshiftMaterial")
            if temp_mat and temp_mat.GetType() >= 1000000:
                self.log(f"[C4D] Created Redshift material via script with type ID: {temp_mat.GetType()}")
                # Clean up the temporary material
                doc.RemoveMaterial(temp_mat)
                c4d.EventAdd()
                # Create a fresh material with this ID
                return c4d.BaseMaterial(temp_mat.GetType())
        except Exception as e:
            self.log(f"[C4D] Script-based Redshift material creation failed: {str(e)}")
            
        # No Redshift materials found
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
            self.log(f"[C4D] Warning: Empty material name provided")
            return None

        # Get all materials in the document
        materials = doc.GetMaterials()

        # First pass: exact match
        for mat in materials:
            if mat.GetName() == name:
                return mat

        # Second pass: case-insensitive match
        name_lower = name.lower()
        closest_match = None
        for mat in materials:
            if mat.GetName().lower() == name_lower:
                closest_match = mat
                self.log(
                    f"[C4D] Found case-insensitive match for material '{name}': '{mat.GetName()}'"
                )
                break

        if closest_match:
            return closest_match

        self.log(f"[C4D] Material not found: '{name}'")

        # If material not found, list available materials to aid debugging
        if materials:
            material_names = [mat.GetName() for mat in materials]
            self.log(f"[C4D] Available materials: {', '.join(material_names)}")

        return None

    def handle_validate_redshift_materials(self, command):
        """Validate Redshift node materials in the scene and fix issues when possible."""
        import maxon
        
        warnings = []
        fixes = []
        doc = c4d.documents.GetActiveDocument()
        
        try:
            # Advanced Redshift detection diagnostics
            self.log(f"[C4D] DIAGNOSTIC: Cinema 4D version: {c4d.GetC4DVersion()}")
            self.log(f"[C4D] DIAGNOSTIC: Python version: {sys.version}")
            
            # Check for Redshift modules more comprehensively
            redshift_module_exists = hasattr(c4d, "modules") and hasattr(c4d.modules, "redshift")
            self.log(f"[C4D] DIAGNOSTIC: Redshift module exists: {redshift_module_exists}")
            
            if redshift_module_exists:
                redshift = c4d.modules.redshift
                self.log(f"[C4D] DIAGNOSTIC: Redshift module dir contents: {dir(redshift)}")
                
                # Check for common Redshift module attributes
                for attr in ["Mmaterial", "MATERIAL_TYPE", "GetRSMaterialNodeSpace"]:
                    has_attr = hasattr(redshift, attr)
                    self.log(f"[C4D] DIAGNOSTIC: Redshift module has '{attr}': {has_attr}")
            
            # Check if Redshift ID_REDSHIFT_MATERIAL constant exists
            has_rs_constant = hasattr(c4d, "ID_REDSHIFT_MATERIAL")
            self.log(f"[C4D] DIAGNOSTIC: c4d.ID_REDSHIFT_MATERIAL exists: {has_rs_constant}")
            if has_rs_constant:
                self.log(f"[C4D] DIAGNOSTIC: c4d.ID_REDSHIFT_MATERIAL value: {c4d.ID_REDSHIFT_MATERIAL}")
            
            # Check all installed plugins
            plugins = c4d.plugins.FilterPluginList(c4d.PLUGINTYPE_MATERIAL, True)
            self.log(f"[C4D] DIAGNOSTIC: Found {len(plugins)} material plugins")
            for plugin in plugins:
                plugin_name = plugin.GetName()
                plugin_id = plugin.GetID()
                self.log(f"[C4D] DIAGNOSTIC: Material plugin: {plugin_name} (ID: {plugin_id})")
            
            # Continue with normal validation
            # Get the Redshift node space ID
            redshift_ns = maxon.Id("com.redshift3d.redshift4c4d.class.nodespace")
            
            # Log all relevant Redshift material IDs for debugging
            self.log(f"[C4D] Standard material ID: {c4d.Mmaterial}")
            self.log(f"[C4D] Redshift material ID (c4d.ID_REDSHIFT_MATERIAL): {c4d.ID_REDSHIFT_MATERIAL}")
            
            # Check if Redshift module has its own material type constant
            if hasattr(c4d, "modules") and hasattr(c4d.modules, "redshift"):
                redshift = c4d.modules.redshift
                rs_material_id = getattr(redshift, "Mmaterial", None)
                if rs_material_id is not None:
                    self.log(f"[C4D] Redshift module material ID: {rs_material_id}")
                rs_material_type = getattr(redshift, "MATERIAL_TYPE", None)
                if rs_material_type is not None:
                    self.log(f"[C4D] Redshift MATERIAL_TYPE: {rs_material_type}")
            
            # Count of materials by type
            mat_stats = {
                "total": 0,
                "redshift": 0,
                "standard": 0, 
                "fixed": 0,
                "issues": 0,
                "material_types": {}
            }
            
            # Validate all materials in the document
            for mat in doc.GetMaterials():
                mat_stats["total"] += 1
                name = mat.GetName()
                
                # Track all material types encountered
                mat_type = mat.GetType()
                if mat_type not in mat_stats["material_types"]:
                    mat_stats["material_types"][mat_type] = 1
                else:
                    mat_stats["material_types"][mat_type] += 1
                
                # Check if it's a Redshift node material (should be c4d.ID_REDSHIFT_MATERIAL)
                is_rs_material = (mat_type == c4d.ID_REDSHIFT_MATERIAL)
                
                # Also check for alternative Redshift material type IDs
                if not is_rs_material and mat_type >= 1000000:
                    # This is likely a Redshift material with a different ID
                    self.log(f"[C4D] Found possible Redshift material with ID {mat_type}: {name}")
                    is_rs_material = True
                
                if not is_rs_material:
                    warnings.append(f"ℹ️ '{name}': Not a Redshift node material (type: {mat.GetType()}).")
                    mat_stats["standard"] += 1
                    
                    # Auto-fix option: convert standard materials to Redshift if requested
                    if command.get("auto_convert", False):
                        try:
                            # Create new Redshift material
                            rs_mat = c4d.BaseMaterial(c4d.ID_REDSHIFT_MATERIAL)
                            rs_mat.SetName(f"RS_{name}")
                            
                            # Copy basic properties
                            color = mat[c4d.MATERIAL_COLOR_COLOR]
                            
                            # Set up default graph using CreateDefaultGraph
                            try:
                                rs_mat.CreateDefaultGraph(redshift_ns)
                            except Exception as e:
                                warnings.append(f"⚠️ Error creating default graph for '{name}': {str(e)}")
                                # Continue anyway and try to work with what we have
                            
                            # Get the graph and root
                            graph = rs_mat.GetGraph(redshift_ns)
                            root = graph.GetRoot()
                            
                            # Find the Standard Surface output
                            for node in graph.GetNodes():
                                if "StandardMaterial" in node.GetId():
                                    # Set diffuse color
                                    try:
                                        node.SetParameter(
                                            maxon.nodes.ParameterID("base_color"), 
                                            maxon.Color(color.x, color.y, color.z),
                                            maxon.PROPERTYFLAGS_NONE
                                        )
                                    except:
                                        pass
                                    break
                            
                            # Insert the new material
                            doc.InsertMaterial(rs_mat)
                            
                            # Find and update texture tags
                            if command.get("update_references", False):
                                obj = doc.GetFirstObject()
                                while obj:
                                    tag = obj.GetFirstTag()
                                    while tag:
                                        if tag.GetType() == c4d.Ttexture:
                                            if tag[c4d.TEXTURETAG_MATERIAL] == mat:
                                                tag[c4d.TEXTURETAG_MATERIAL] = rs_mat
                                        tag = tag.GetNext()
                                    obj = obj.GetNext()
                            
                            fixes.append(f"✅ Converted '{name}' to Redshift node material.")
                            mat_stats["fixed"] += 1
                        except Exception as e:
                            warnings.append(f"❌ Failed to convert '{name}': {str(e)}")
                    
                    continue
                
                # For Redshift materials, continue with validation
                if is_rs_material:
                    # It's a confirmed Redshift material
                    mat_stats["redshift"] += 1
                    
                    # Check if it's using the Redshift node space
                    if hasattr(mat, 'GetNodeMaterialSpace') and mat.GetNodeMaterialSpace() != redshift_ns:
                        warnings.append(f"⚠️ '{name}': Redshift material but not using correct node space.")
                        mat_stats["issues"] += 1
                        continue
                else:
                    # Skip further validation for non-Redshift materials
                    continue
                
                # Validate the node graph
                graph = mat.GetGraph(redshift_ns)
                if not graph:
                    warnings.append(f"❌ '{name}': No node graph.")
                    mat_stats["issues"] += 1
                    
                    # Try to fix by creating a default graph
                    if command.get("auto_fix", False):
                        try:
                            mat.CreateDefaultGraph(redshift_ns)
                            fixes.append(f"✅ Created default graph for '{name}'.")
                            mat_stats["fixed"] += 1
                        except Exception as e:
                            warnings.append(f"❌ Could not create default graph for '{name}': {str(e)}")
                    
                    continue
                
                # Check the root node connections
                root = graph.GetRoot()
                if not root:
                    warnings.append(f"❌ '{name}': No root node in graph.")
                    mat_stats["issues"] += 1
                    continue
                
                # Check if we have inputs
                inputs = root.GetInputs()
                if not inputs or len(inputs) == 0:
                    warnings.append(f"❌ '{name}': Root has no input ports.")
                    mat_stats["issues"] += 1
                    continue
                
                # Check the output connection
                output_port = inputs[0]  # First input is typically the main output
                output_node = output_port.GetDestination()
                
                if not output_node:
                    warnings.append(f"⚠️ '{name}': Output not connected.")
                    mat_stats["issues"] += 1
                    
                    # Try to fix by creating a Standard Surface node
                    if command.get("auto_fix", False):
                        try:
                            # Create Standard Surface node
                            standard_surface = graph.CreateNode(maxon.nodes.IdAndVersion("com.redshift3d.redshift4c4d.nodes.core.standardmaterial"))
                            
                            # Connect to output
                            graph.CreateConnection(
                                standard_surface.GetOutputs()[0],  # Surface output
                                root.GetInputs()[0]  # Surface input on root
                            )
                            
                            fixes.append(f"✅ Added Standard Surface node to '{name}'.")
                            mat_stats["fixed"] += 1
                        except Exception as e:
                            warnings.append(f"❌ Could not add Standard Surface to '{name}': {str(e)}")
                    
                    continue
                
                # Check that the output is connected to a Redshift Material node (Standard Surface, etc.)
                if "StandardMaterial" not in output_node.GetId() and "Material" not in output_node.GetId():
                    warnings.append(f"❌ '{name}': Output not connected to a Redshift Material node.")
                    mat_stats["issues"] += 1
                    continue
                
                # Now check specific material inputs
                rs_mat_node = output_node
                
                # Check diffuse/base color
                base_color = None
                for input_port in rs_mat_node.GetInputs():
                    port_id = input_port.GetId()
                    if "diffuse_color" in port_id or "base_color" in port_id:
                        base_color = input_port
                        break
                
                if base_color is None:
                    warnings.append(f"⚠️ '{name}': No diffuse/base color input found.")
                    mat_stats["issues"] += 1
                    continue
                
                if not base_color.GetDestination():
                    warnings.append(f"ℹ️ '{name}': Diffuse/base color input not connected.")
                    # This is not necessarily an issue, just informational
                else:
                    source_node = base_color.GetDestination().GetNode()
                    source_type = "unknown"
                    
                    # Identify the type of source
                    if "ColorTexture" in source_node.GetId():
                        source_type = "texture"
                    elif "Noise" in source_node.GetId():
                        source_type = "noise"
                    elif "Checker" in source_node.GetId():
                        source_type = "checker"
                    elif "Gradient" in source_node.GetId():
                        source_type = "gradient"
                    elif "ColorConstant" in source_node.GetId():
                        source_type = "color"
                    
                    warnings.append(f"✅ '{name}': Diffuse/base color connected to {source_type} node.")
                
                # Check for common issues in other ports
                # Detect if there's a fresnel node present
                has_fresnel = False
                for node in graph.GetNodes():
                    if "Fresnel" in node.GetId():
                        has_fresnel = True
                        
                        # Verify the Fresnel node has proper connections
                        inputs_valid = True
                        for input_port in node.GetInputs():
                            port_id = input_port.GetId()
                            if "ior" in port_id and not input_port.GetDestination():
                                inputs_valid = False
                                warnings.append(f"⚠️ '{name}': Fresnel node missing IOR input.")
                                mat_stats["issues"] += 1
                        
                        outputs_valid = False
                        for output_port in node.GetOutputs():
                            if output_port.GetSource():
                                outputs_valid = True
                                break
                                
                        if not outputs_valid:
                            warnings.append(f"⚠️ '{name}': Fresnel node has no output connections.")
                            mat_stats["issues"] += 1
                
                if has_fresnel:
                    warnings.append(f"ℹ️ '{name}': Contains Fresnel shader (check for potential issues).")
            
            # Summary stats
            summary = f"Material validation complete. Found {mat_stats['total']} materials: " + \
                      f"{mat_stats['redshift']} Redshift, {mat_stats['standard']} Standard, " + \
                      f"{mat_stats['issues']} with issues, {mat_stats['fixed']} fixed."
            
            # Update the document to apply any changes
            c4d.EventAdd()
            
            # Format material_types for better readability
            material_types_formatted = {}
            for type_id, count in mat_stats["material_types"].items():
                if type_id == c4d.Mmaterial:
                    name = "Standard Material"
                elif type_id == c4d.ID_REDSHIFT_MATERIAL:
                    name = "Redshift Material (using c4d.ID_REDSHIFT_MATERIAL)"
                elif type_id == 1036224:
                    name = "Redshift Material (1036224)"
                elif type_id >= 1000000:
                    name = f"Possible Redshift Material ({type_id})"
                else:
                    name = f"Unknown Type ({type_id})"
                
                material_types_formatted[name] = count
            
            # Replace the original dictionary with the formatted one
            mat_stats["material_types"] = material_types_formatted
            
            return {
                "status": "ok", 
                "warnings": warnings,
                "fixes": fixes,
                "summary": summary,
                "stats": mat_stats,
                "ids": {
                    "standard_material": c4d.Mmaterial,
                    "redshift_material": c4d.ID_REDSHIFT_MATERIAL
                }
            }
        
        except Exception as e:
            return {
                "status": "error",
                "message": f"Error validating materials: {str(e)}",
                "warnings": warnings
            }

    def handle_create_material(self, command):
        """Handle create_material command with proper NodeMaterial support for Redshift."""
        doc = c4d.documents.GetActiveDocument()
        name = command.get("name") or command.get("material_name") or "New Material"
        color = command.get("color", [1, 1, 1])
        properties = command.get("properties", {})
        material_type = command.get("material_type", "standard")  # standard, redshift
        procedural = command.get("procedural", False)
        shader_type = command.get("shader_type", "noise")
        
        self.log(f"[C4D] Starting material creation: {name}, type: {material_type}")
        
        # Set default result
        mat = None
        material_id = f"mat_{name}_{int(time.time())}"
        success = False
        has_redshift = False
        redshift_plugin_id = None
        
        try:
            # DIAGNOSTIC STEP 1: Check for Redshift plugin
            self.log("[C4D] Checking for Redshift plugin availability...")
            plugins = c4d.plugins.FilterPluginList(c4d.PLUGINTYPE_MATERIAL, True)
            self.log(f"[C4D] Found {len(plugins)} material plugins")
            
            for plugin in plugins:
                plugin_name = plugin.GetName()
                plugin_id = plugin.GetID()
                self.log(f"[C4D] Material plugin: {plugin_name} (ID: {plugin_id})")
                
                if "redshift" in plugin_name.lower():
                    has_redshift = True
                    redshift_plugin_id = plugin_id
                    self.log(f"[C4D] Found Redshift plugin: {plugin_name} (ID: {plugin_id})")
            
            if material_type == "redshift" and not has_redshift:
                self.log("[C4D] WARNING: Redshift requested but not found in plugins. Using standard material.")
                material_type = "standard"
            
            # STEP 2: Create the material based on type
            if material_type == "redshift" and has_redshift:
                self.log("[C4D] Creating Redshift material using NodeMaterial approach...")
                # Try multiple methods for creating Redshift material, preferring NodeMaterial
                redshift_material_created = False
                
                # Method 1: Use the detected plugin ID directly
                if redshift_plugin_id is not None:
                    try:
                        self.log(f"[C4D] Method 1: Creating Redshift material with plugin ID: {redshift_plugin_id}")
                        mat = c4d.BaseMaterial(redshift_plugin_id)
                        if mat and mat.GetType() >= 1000000:  # Redshift materials have high IDs
                            self.log(f"[C4D] Successfully created Redshift material with plugin ID method, type: {mat.GetType()}")
                            redshift_material_created = True
                        else:
                            self.log("[C4D] Plugin ID method failed to create valid Redshift material")
                    except Exception as e:
                        self.log(f"[C4D] Method 1 error: {str(e)}")
                
                # Method 2: Try using ID_REDSHIFT_MATERIAL constant if available
                if not redshift_material_created and hasattr(c4d, "ID_REDSHIFT_MATERIAL"):
                    try:
                        rs_id = c4d.ID_REDSHIFT_MATERIAL
                        self.log(f"[C4D] Method 2: Using c4d.ID_REDSHIFT_MATERIAL: {rs_id}")
                        mat = c4d.BaseMaterial(rs_id)
                        if mat and mat.GetType() == rs_id:
                            self.log(f"[C4D] Successfully created Redshift material with ID_REDSHIFT_MATERIAL")
                            redshift_material_created = True
                        else:
                            self.log("[C4D] ID_REDSHIFT_MATERIAL method failed to create valid Redshift material")
                    except Exception as e:
                        self.log(f"[C4D] Method 2 error: {str(e)}")
                
                # Method 3: Try using direct ID 1036224 (known Redshift material ID)
                if not redshift_material_created:
                    try:
                        self.log("[C4D] Method 3: Using direct ID 1036224 for Redshift material")
                        mat = c4d.BaseMaterial(1036224)
                        if mat and mat.GetType() == 1036224:
                            self.log("[C4D] Successfully created Redshift material with direct ID 1036224")
                            redshift_material_created = True
                        else:
                            self.log("[C4D] Direct ID method failed to create valid Redshift material")
                    except Exception as e:
                        self.log(f"[C4D] Method 3 error: {str(e)}")
                
                # If we have a Redshift material at this point, set up its node graph
                if redshift_material_created and mat:
                    try:
                        self.log("[C4D] Setting up Redshift node graph...")
                        mat.SetName(name)
                        material_type = "redshift"
                        success = True
                        
                        # Import maxon module for node material handling
                        import maxon
                        
                        # Get the Redshift node space ID
                        redshift_ns = maxon.Id("com.redshift3d.redshift4c4d.class.nodespace")
                        
                        # Create default graph (includes Standard material node)
                        self.log("[C4D] Creating default node graph for Redshift material")
                        try:
                            # Create the node graph with proper error handling
                            mat = c4d.NodeMaterial(mat)  # Ensure we're working with NodeMaterial
                            if not mat.HasSpace(redshift_ns):
                                graph = mat.CreateDefaultGraph(redshift_ns)
                                self.log("[C4D] Created default Redshift node graph")
                            else:
                                graph = mat.GetGraph(redshift_ns)
                                self.log("[C4D] Using existing Redshift node graph")
                            
                            # Find the Standard Surface material node to set color
                            if len(color) >= 3 and graph:
                                root = graph.GetViewRoot()
                                if root:
                                    # Try to find Standard Surface node
                                    for node in graph.GetNodes():
                                        node_id = node.GetId()
                                        if "StandardMaterial" in node_id:
                                            self.log(f"[C4D] Found StandardMaterial node: {node_id}")
                                            try:
                                                # Set base color parameter
                                                node.SetParameter(
                                                    maxon.nodes.ParameterID("base_color"),
                                                    maxon.Color(color[0], color[1], color[2]),
                                                    maxon.PROPERTYFLAGS_NONE
                                                )
                                                self.log(f"[C4D] Set color: [{color[0]}, {color[1]}, {color[2]}]")
                                            except Exception as e:
                                                self.log(f"[C4D] Error setting node color: {str(e)}")
                                            break
                        except Exception as e:
                            self.log(f"[C4D] Error setting up Redshift node graph: {str(e)}")
                    except ImportError as e:
                        self.log(f"[C4D] Error importing maxon module: {str(e)}")
                        # Continue with basic material without node graph
                else:
                    self.log("[C4D] All Redshift material creation methods failed, switching to standard")
                    material_type = "standard"
            
            # Create a standard material if needed
            if material_type == "standard" or not mat:
                self.log("[C4D] Creating standard material")
                mat = c4d.BaseMaterial(c4d.Mmaterial)
                mat.SetName(name)
                material_type = "standard"
                success = True
            
            # Set base properties for the material (if standard)
            if material_type == "standard":
                # Standard material properties
                if len(color) >= 3:
                    color_vector = c4d.Vector(color[0], color[1], color[2])
                    mat[c4d.MATERIAL_COLOR_COLOR] = color_vector
                
                # Apply additional properties
                if "specular" in properties and isinstance(properties["specular"], list) and len(properties["specular"]) >= 3:
                    spec = properties["specular"]
                    mat[c4d.MATERIAL_SPECULAR_COLOR] = c4d.Vector(spec[0], spec[1], spec[2])
                
                if "reflection" in properties and isinstance(properties["reflection"], (int, float)):
                    mat[c4d.MATERIAL_REFLECTION_BRIGHTNESS] = float(properties["reflection"])
            
            # Insert material into document
            doc.InsertMaterial(mat)
            doc.AddUndo(c4d.UNDOTYPE_NEW, mat)
            c4d.EventAdd()
            
            # Determine material color for response
            if material_type == "redshift":
                material_color = color  # Use requested color
            else:
                material_color = [
                    mat[c4d.MATERIAL_COLOR_COLOR].x,
                    mat[c4d.MATERIAL_COLOR_COLOR].y,
                    mat[c4d.MATERIAL_COLOR_COLOR].z,
                ]
            
            self.log(f"[C4D] Material created successfully: {name}, type: {material_type}, ID: {mat.GetType()}")
            
            return {
                "material": {
                    "name": mat.GetName(),  # Exact Cinema 4D material name
                    "id": material_id,      # Internal ID 
                    "color": material_color, # Material color (RGB)
                    "type": material_type,  # "standard" or "redshift"
                    "material_type_id": mat.GetType(),  # Actual material type ID
                    "procedural": procedural if material_type == "redshift" else False,
                    "redshift_available": has_redshift  # Helps client know if Redshift is available
                }
            }
            
        except Exception as e:
            error_msg = f"Failed to create material: {str(e)}"
            self.log(f"[C4D] {error_msg}")
            return {"error": error_msg}
    
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
            
            # Check if this is a Redshift material
            is_redshift_material = (mat.GetType() >= 1000000)
            if is_redshift_material:
                self.log(f"[C4D] Detected Redshift material (ID: {mat.GetType()})")
                
                # Handle shader application for Redshift material using node graph
                try:
                    import maxon
                    redshift_ns = maxon.Id("com.redshift3d.redshift4c4d.class.nodespace")
                    
                    # Check if the material has a node graph
                    # Ensure we're dealing with a NodeMaterial
                    node_mat = c4d.NodeMaterial(mat)
                    if node_mat and node_mat.HasSpace(redshift_ns):
                        self.log("[C4D] Accessing Redshift node graph...")
                        graph = node_mat.GetGraph(redshift_ns)
                        
                        if graph:
                            # Begin transaction to modify the graph
                            with graph.BeginTransaction() as transaction:
                                try:
                                    # Find the material output node (usually StandardMaterial)
                                    material_output = None
                                    root_node = graph.GetViewRoot()
                                    surface_input = root_node.GetInputs()[0]  # First input is usually surface
                                    
                                    if surface_input.GetDestination():
                                        material_output = surface_input.GetDestination().GetNode()
                                    
                                    if not material_output:
                                        # Try to find a standard material node
                                        for node in graph.GetNodes():
                                            if "StandardMaterial" in node.GetId():
                                                material_output = node
                                                break
                                    
                                    if material_output:
                                        self.log(f"[C4D] Found material output node: {material_output.GetId()}")
                                        
                                        # Create shader node based on type
                                        shader_node = None
                                        
                                        if shader_type == "noise":
                                            # Create a Redshift Noise texture
                                            shader_node = graph.AddChild(
                                                maxon.Id(),  # Auto-generate ID
                                                maxon.Id("com.redshift3d.redshift4c4d.nodes.core.texturesampler")
                                            )
                                            
                                            if shader_node:
                                                # Set texture type to noise
                                                shader_node.SetParameter(
                                                    maxon.nodes.ParameterID("tex0_tex"), 
                                                    4,  # 4 = Noise in Redshift
                                                    maxon.PROPERTYFLAGS_NONE
                                                )
                                                
                                                # Set noise parameters
                                                if "scale" in parameters:
                                                    try:
                                                        scale = float(parameters["scale"])
                                                        shader_node.SetParameter(
                                                            maxon.nodes.ParameterID("noise_scale"),
                                                            scale,
                                                            maxon.PROPERTYFLAGS_NONE
                                                        )
                                                    except Exception as e:
                                                        self.log(f"[C4D] Error setting noise scale: {str(e)}")
                                        
                                        elif shader_type == "fresnel":
                                            # Create a Redshift Fresnel node
                                            shader_node = graph.AddChild(
                                                maxon.Id(),  # Auto-generate ID
                                                maxon.Id("com.redshift3d.redshift4c4d.nodes.core.fresnel")
                                            )
                                            
                                            if shader_node:
                                                # Set IOR parameter if specified
                                                if "ior" in parameters:
                                                    try:
                                                        ior = float(parameters["ior"])
                                                        shader_node.SetParameter(
                                                            maxon.nodes.ParameterID("ior"),
                                                            ior,
                                                            maxon.PROPERTYFLAGS_NONE
                                                        )
                                                    except Exception as e:
                                                        self.log(f"[C4D] Error setting fresnel IOR: {str(e)}")
                                        
                                        elif shader_type == "gradient":
                                            # Create a Redshift Gradient texture
                                            shader_node = graph.AddChild(
                                                maxon.Id(),  # Auto-generate ID
                                                maxon.Id("com.redshift3d.redshift4c4d.nodes.core.texturesampler")
                                            )
                                            
                                            if shader_node:
                                                # Set texture type to gradient
                                                shader_node.SetParameter(
                                                    maxon.nodes.ParameterID("tex0_tex"), 
                                                    2,  # 2 = Gradient in Redshift
                                                    maxon.PROPERTYFLAGS_NONE
                                                )
                                        
                                        elif shader_type == "checkerboard":
                                            # Create a Redshift Checker texture
                                            shader_node = graph.AddChild(
                                                maxon.Id(),  # Auto-generate ID
                                                maxon.Id("com.redshift3d.redshift4c4d.nodes.core.texturesampler")
                                            )
                                            
                                            if shader_node:
                                                # Set texture type to checker
                                                shader_node.SetParameter(
                                                    maxon.nodes.ParameterID("tex0_tex"), 
                                                    1,  # 1 = Checker in Redshift
                                                    maxon.PROPERTYFLAGS_NONE
                                                )
                                        
                                        # Connect the shader to the appropriate channel
                                        if shader_node:
                                            self.log(f"[C4D] Created {shader_type} node: {shader_node.GetId()}")
                                            
                                            # Find the right input port based on channel
                                            target_port = None
                                            for input_port in material_output.GetInputs():
                                                port_id = input_port.GetId()
                                                
                                                if channel == "color" and ("base_color" in port_id or "diffuse_color" in port_id):
                                                    target_port = input_port
                                                    break
                                                elif channel == "reflection" and ("refl_color" in port_id or "reflection" in port_id):
                                                    target_port = input_port
                                                    break
                                                elif channel == "bump" and ("bump" in port_id):
                                                    target_port = input_port
                                                    break
                                                elif channel == "opacity" and ("opacity" in port_id or "transparency" in port_id):
                                                    target_port = input_port
                                                    break
                                            
                                            if target_port:
                                                self.log(f"[C4D] Found target port: {target_port.GetId()}")
                                                
                                                # Find the appropriate output port of the shader
                                                source_port = None
                                                for output_port in shader_node.GetOutputs():
                                                    port_id = output_port.GetId()
                                                    if "out" in port_id and shader_type == "fresnel":
                                                        source_port = output_port
                                                        break
                                                    elif "outcolor" in port_id:
                                                        source_port = output_port
                                                        break
                                                
                                                if source_port:
                                                    # Create the connection
                                                    graph.CreateConnection(source_port, target_port)
                                                    self.log(f"[C4D] Connected {shader_type} to {channel} channel")
                                                else:
                                                    self.log(f"[C4D] Could not find source output port for {shader_type}")
                                            else:
                                                self.log(f"[C4D] Could not find {channel} input port on material")
                                        else:
                                            self.log(f"[C4D] Failed to create {shader_type} node")
                                    else:
                                        self.log("[C4D] Could not find a valid material output node")
                                except Exception as e:
                                    self.log(f"[C4D] Error in node graph transaction: {str(e)}")
                                    transaction.Rollback()
                                    return {"error": f"Failed to apply shader to Redshift material: {str(e)}"}
                                
                                # Commit the transaction if no errors
                                transaction.Commit()
                        else:
                            self.log("[C4D] Could not access Redshift node graph")
                            
                            # Try to create the graph
                            try:
                                node_mat.CreateDefaultGraph(redshift_ns)
                                self.log("[C4D] Created default Redshift node graph, try applying shader again")
                                return self.handle_apply_shader(command)  # Retry with new graph
                            except Exception as e:
                                self.log(f"[C4D] Failed to create Redshift node graph: {str(e)}")
                    else:
                        self.log("[C4D] Material does not have a Redshift node space")
                        is_redshift_material = False  # Treat as standard material
                except Exception as e:
                    self.log(f"[C4D] Error handling Redshift material: {str(e)}")
                    is_redshift_material = False  # Fall back to standard approach
            
            # For standard materials or if Redshift handling failed
            if not is_redshift_material:
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
                
                # Handle fresnel shader carefully
                if shader_type == "fresnel":
                    self.log("[C4D] Attempting to create fresnel shader (may not be available)")
                
                # Create shader with proper error handling
                try:
                    shader = c4d.BaseShader(shader_type_id)
                    if shader is None:
                        return {"error": f"Failed to create {shader_type} shader"}
                    
                    # Set shader parameters
                    if shader_type == "noise":
                        if "scale" in parameters:
                            shader[c4d.SLA_NOISE_SCALE] = float(parameters.get("scale", 1.0))
                        if "octaves" in parameters:
                            shader[c4d.SLA_NOISE_OCTAVES] = int(parameters.get("octaves", 3))
                    
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
                    "is_redshift": is_redshift_material
                }
            }
        except Exception as e:
            self.log(f"[C4D] Error applying shader: {str(e)}")
            return {"error": f"Failed to apply shader: {str(e)}"}


# Class definition for SocketServerDialog and plugin registration would follow here