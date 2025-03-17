# Cinema 4D MCP Server Plugin
# Updated for Cinema 4D R2025 compatibility
# Version 0.1.4 - Comprehensive fixes for MoGraph fields, rendering, and object listing issues
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

                    # Add to objects list with proper indentation for hierarchy and additional properties
                    obj_info = {
                        "name": obj_name,
                        "type": obj_type,
                        "id": obj_id,
                        "depth": depth,
                        "type_id": obj_type_id,  # Include type ID for debugging
                    }

                    # Add any additional properties if they exist
                    if additional_props:
                        obj_info["properties"] = additional_props

                    objects.append(obj_info)

                    # Process children recursively (if they exist)
                    child = current_obj.GetDown()
                    if child:
                        get_objects_recursive(child, depth + 1)

                except Exception as e:
                    self.log(f"[C4D] Error processing object: {str(e)}")

                # Move to next sibling (explicit and clear traversal)
                current_obj = current_obj.GetNext()

        # Start recursive traversal from first object
        obj = doc.GetFirstObject()
        get_objects_recursive(obj)

        # Enhanced comprehensive search for MoGraph Cloners using multiple methods
        try:
            # Method 1: Comprehensive sweep for ALL objects including MoGraph objects
            self.log(
                "[C4D] Starting comprehensive search for all objects including MoGraph"
            )

            # BaseList2D method for accessing ALL objects in the document (most complete method)
            all_objects = []

            # Use GetFirstObject() and manual traversal as basis
            root_obj = doc.GetFirstObject()
            if root_obj:
                all_objects = self.get_all_objects_comprehensive(doc)
                self.log(
                    f"[C4D] Comprehensive search found {len(all_objects)} total objects"
                )

            # Process the complete object list
            for obj in all_objects:
                try:
                    obj_id = str(obj.GetGUID())
                    if obj_id not in found_ids:
                        obj_type_id = obj.GetType()
                        obj_name = obj.GetName()

                        # Use explicit type checking for MoGraph objects
                        obj_type = "Object"
                        additional_props = {}

                        # MoGraph specific detection with special handling for Cloners
                        if obj_type_id == c4d.Omgcloner:
                            obj_type = "MoGraph Cloner"
                            try:
                                # Get the cloner mode
                                mode_id = obj[c4d.ID_MG_MOTIONGENERATOR_MODE]
                                modes = {
                                    0: "Linear",
                                    1: "Grid",
                                    2: "Radial",
                                    3: "Object",
                                }
                                mode_name = modes.get(mode_id, f"Mode {mode_id}")
                                additional_props["cloner_mode"] = mode_name

                                # Add counts based on mode
                                if mode_id == 0:  # Linear
                                    additional_props["count"] = obj[c4d.MG_LINEAR_COUNT]
                                elif mode_id == 1:  # Grid
                                    additional_props["count_x"] = obj[
                                        c4d.MG_GRID_COUNT_X
                                    ]
                                    additional_props["count_y"] = obj[
                                        c4d.MG_GRID_COUNT_Y
                                    ]
                                    additional_props["count_z"] = obj[
                                        c4d.MG_GRID_COUNT_Z
                                    ]
                                elif mode_id == 2:  # Radial
                                    additional_props["count"] = obj[c4d.MG_POLY_COUNT]
                            except Exception as e:
                                self.log(
                                    f"[C4D] Error getting cloner details: {str(e)}"
                                )
                        # Check if it's in MoGraph object ID range
                        elif 1018544 <= obj_type_id <= 1019544:
                            # It's a MoGraph object we missed
                            if obj_type_id == c4d.Omgrandom:
                                obj_type = "Random Effector"
                            elif obj_type_id == c4d.Omgformula:
                                obj_type = "Formula Effector"
                            elif hasattr(c4d, "Omgstep") and obj_type_id == c4d.Omgstep:
                                obj_type = "Step Effector"
                            else:
                                obj_type = "MoGraph Object"
                        else:
                            # Use standard object type detection
                            obj_type = self.get_object_type_name(obj)

                        # Add to objects list
                        obj_info = {
                            "name": obj_name,
                            "type": obj_type,
                            "id": obj_id,
                            "depth": 0,  # Can't determine depth easily
                            "type_id": obj_type_id,
                        }

                        # Add any additional properties
                        if additional_props:
                            obj_info["properties"] = additional_props

                        self.log(
                            f"[C4D] Found additional object: {obj_name} (Type: {obj_type})"
                        )
                        objects.append(obj_info)
                        found_ids.add(obj_id)
                except Exception as e:
                    self.log(f"[C4D] Error in comprehensive object scan: {str(e)}")
        except Exception as e:
            self.log(f"[C4D] Error in comprehensive object search: {str(e)}")

        # Method 2: As a fallback, direct search for Cloners by ID
        try:
            self.log("[C4D] Additional direct search for Cloner objects")

            # Use the BaseObject Find method if available
            if hasattr(c4d.BaseObject, "FindObjects"):
                cloners = c4d.BaseObject.FindObjects(doc, c4d.Omgcloner)
                self.log(f"[C4D] Found {len(cloners)} cloners with FindObjects method")

                for cloner in cloners:
                    obj_id = str(cloner.GetGUID())
                    if obj_id not in found_ids:
                        self.log(
                            f"[C4D] Found cloner with direct search: {cloner.GetName()}"
                        )
                        objects.append(
                            {
                                "name": cloner.GetName(),
                                "type": "MoGraph Cloner (direct)",
                                "id": obj_id,
                                "depth": 0,
                                "type_id": cloner.GetType(),
                            }
                        )
                        found_ids.add(obj_id)
        except Exception as e:
            self.log(f"[C4D] Error in direct cloner search: {str(e)}")

        # Log the total count
        self.log(f"[C4D] Found {len(objects)} objects in scene")
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
        elif primitive_type == "cone":
            obj = c4d.BaseObject(c4d.Ocone)
            obj[c4d.PRIM_CONE_TRAD] = 0
            obj[c4d.PRIM_CONE_BRAD] = size[0] / 2
            obj[c4d.PRIM_CONE_HEIGHT] = size[1]
        elif primitive_type == "cylinder":
            obj = c4d.BaseObject(c4d.Ocylinder)
            obj[c4d.PRIM_CYLINDER_RADIUS] = size[0] / 2
            obj[c4d.PRIM_CYLINDER_HEIGHT] = size[1]
        elif primitive_type == "plane":
            obj = c4d.BaseObject(c4d.Oplane)
            obj[c4d.PRIM_PLANE_WIDTH] = size[0]
            obj[c4d.PRIM_PLANE_HEIGHT] = size[1]
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

        # Update the document
        c4d.EventAdd()

        # Return information about the created object
        return {
            "object": {
                "name": obj.GetName(),
                "id": str(obj.GetGUID()),
                "position": [obj.GetAbsPos().x, obj.GetAbsPos().y, obj.GetAbsPos().z],
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
            except AttributeError:
                pass  # Silently fail if property doesn't exist
            except Exception as e:
                # Optionally, log the error for debugging purposes
                print(f"Error setting color: {str(e)}")

        # Update the document
        c4d.EventAdd()

        return {
            "object": {
                "name": obj.GetName(),
                "id": str(obj.GetGUID()),
                "modified": modified,
            }
        }

    def handle_create_material(self, command):
        """Handle create_material command."""
        doc = c4d.documents.GetActiveDocument()
        name = command.get("name") or command.get("material_name") or "New Material"
        color = command.get("color", [1, 1, 1])
        properties = command.get("properties", {})

        try:
            # Create a new standard material
            mat = c4d.BaseMaterial(c4d.Mmaterial)
            mat.SetName(name)

            # Set base color
            if len(color) >= 3:
                color_vector = c4d.Vector(color[0], color[1], color[2])
                mat[c4d.MATERIAL_COLOR_COLOR] = color_vector

            # Apply additional properties (if needed)
            if (
                "specular" in properties
                and isinstance(properties["specular"], list)
                and len(properties["specular"]) >= 3
            ):
                spec = properties["specular"]
                mat[c4d.MATERIAL_SPECULAR_COLOR] = c4d.Vector(spec[0], spec[1], spec[2])

            if "reflection" in properties and isinstance(
                properties["reflection"], (int, float)
            ):
                mat[c4d.MATERIAL_REFLECTION_BRIGHTNESS] = float(
                    properties["reflection"]
                )

            # Insert material into document
            doc.InsertMaterial(mat)
            doc.AddUndo(c4d.UNDOTYPE_NEW, mat)

            # Update the document
            c4d.EventAdd()

            # Generate a unique ID as a string since materials don't have GetGUID
            # Using their name and a timestamp instead
            material_id = f"mat_{name}_{int(time.time())}"

            return {
                "material": {
                    "name": mat.GetName(),  # Exact Cinema 4D material name
                    "id": material_id,  # Your internal ID (if needed)
                    "color": color,  # Actual material color (RGB)
                }
            }
        except Exception as e:
            return {"error": f"Failed to create material: {str(e)}"}

    def handle_apply_material(self, command):
        """Handle apply_material command."""
        doc = c4d.documents.GetActiveDocument()
        material_name = command.get("material_name", "")
        object_name = command.get("object_name", "")

        # Find the object
        obj = self.find_object_by_name(doc, object_name)
        if obj is None:
            return {"error": f"Object not found: {object_name}"}

        # Find the material
        mat = self.find_material_by_name(doc, material_name)
        if mat is None:
            return {"error": f"Material not found: {material_name}"}

        try:
            # Create a texture tag
            tag = c4d.TextureTag()
            tag.SetMaterial(mat)

            # Add the tag to the object
            obj.InsertTag(tag)

            # Update the document
            c4d.EventAdd()

            return {
                "success": True,
                "message": f"Applied material '{material_name}' to object '{object_name}'",
            }
        except Exception as e:
            return {"error": f"Failed to apply material: {str(e)}"}

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

                # Disable post effects for faster rendering if available in this version
                try:
                    # This attribute might not be available in all C4D versions
                    if hasattr(c4d, "RDATA_POSTEFFECTS"):
                        rd[c4d.RDATA_POSTEFFECTS] = False
                except:
                    pass

                # Set low quality rendering for speed
                try:
                    rd[c4d.RDATA_ANTIALIASING] = c4d.ANTIALIASING_GEOMETRY
                except:
                    # Fall back to a known antialiasing setting if constant isn't available
                    self.log("[C4D] Using fallback antialiasing setting")
                    rd[c4d.RDATA_ANTIALIASING] = 0

                # Create output directory if needed
                if output_path:
                    output_dir = os.path.dirname(output_path)
                    if output_dir and not os.path.exists(output_dir):
                        os.makedirs(output_dir)

                # Measure render time
                start_time = time.time()

                # Initialize bitmap for rendering
                bmp = c4d.bitmaps.BaseBitmap()
                if not bmp.Init(width, height, 24):  # 24 bit color depth
                    return {"error": "Failed to initialize bitmap"}

                # For Cinema 4D R2025, we need to pass None as the progress parameter
                # to avoid the type error: "argument 5 must be c4d.threading.BaseThread or None"

                self.log("[C4D] Starting render on main thread...")

                # Use only RENDERFLAGS_EXTERNAL for Cinema 4D R2025
                # RENDERFLAGS_NODISPLAY is deprecated in newer C4D versions
                render_result = c4d.documents.RenderDocument(
                    doc,
                    rd.GetData(),
                    bmp,
                    c4d.RENDERFLAGS_EXTERNAL,  # Using only EXTERNAL flag for R2025 compatibility
                    None,  # Pass None for the progress parameter to avoid type errors
                )

                render_time = time.time() - start_time
                self.log(f"[C4D] Render completed in {render_time:.2f} seconds")

                if render_result != c4d.RENDERRESULT_OK:
                    return {
                        "error": f"Rendering failed with result code: {render_result}"
                    }

                # Save rendered bitmap to disk if output_path is specified
                path = "Memory only"
                if output_path:
                    # Try to save with different formats if PNG fails
                    if bmp.Save(output_path, c4d.FILTER_PNG):
                        path = output_path
                        self.log(f"[C4D] Saved render to {path}")
                    else:
                        # Try JPEG as fallback
                        jpg_path = os.path.splitext(output_path)[0] + ".jpg"
                        if bmp.Save(jpg_path, c4d.FILTER_JPG):
                            path = jpg_path
                            self.log(f"[C4D] Saved render to {path} (fallback to JPG)")
                        else:
                            return {"error": f"Failed to save render to {output_path}"}

                # Return render info
                return {
                    "render_info": {
                        "path": path,
                        "width": bmp.GetBw(),
                        "height": bmp.GetBh(),
                        "render_time": render_time,
                    }
                }
            except Exception as e:
                self.log(f"[C4D] Error in render_on_main_thread: {str(e)}")
                return {"error": f"Failed to render: {str(e)}"}

        try:
            # Get the active document
            doc = c4d.documents.GetActiveDocument()

            # Execute rendering on the main thread with extended timeout
            self.log("[C4D] Dispatching render to main thread with extended timeout...")

            # Use execute_on_main_thread with explicit timeout for render operations
            result = self.execute_on_main_thread(
                render_on_main_thread,
                doc,
                output_path,
                width,
                height,
                _timeout=180,  # 3 minutes timeout for rendering operations
            )

            return result

        except Exception as e:
            self.log(f"[C4D] Error during render dispatch: {str(e)}")
            return {"error": f"Failed to render: {str(e)}"}

    def handle_set_keyframe(self, command):
        """Handle set_keyframe command."""
        doc = c4d.documents.GetActiveDocument()
        object_name = command.get("object_name", "")
        property_name = command.get("property_name", "")
        value = command.get("value", 0)
        frame = command.get("frame", doc.GetTime().GetFrame(doc.GetFps()))

        # Find the object by name
        obj = self.find_object_by_name(doc, object_name)
        if obj is None:
            return {"error": f"Object not found: {object_name}"}

        try:
            # Get the track or create it if it doesn't exist
            track = None

            # Map property names to C4D constants
            property_map = {
                "position.x": c4d.DescID(
                    c4d.DescLevel(c4d.ID_BASEOBJECT_POSITION, c4d.DTYPE_VECTOR, 0),
                    c4d.DescLevel(c4d.VECTOR_X, c4d.DTYPE_REAL, 0),
                ),
                "position.y": c4d.DescID(
                    c4d.DescLevel(c4d.ID_BASEOBJECT_POSITION, c4d.DTYPE_VECTOR, 0),
                    c4d.DescLevel(c4d.VECTOR_Y, c4d.DTYPE_REAL, 0),
                ),
                "position.z": c4d.DescID(
                    c4d.DescLevel(c4d.ID_BASEOBJECT_POSITION, c4d.DTYPE_VECTOR, 0),
                    c4d.DescLevel(c4d.VECTOR_Z, c4d.DTYPE_REAL, 0),
                ),
                "rotation.h": c4d.DescID(
                    c4d.DescLevel(c4d.ID_BASEOBJECT_ROTATION, c4d.DTYPE_VECTOR, 0),
                    c4d.DescLevel(c4d.VECTOR_X, c4d.DTYPE_REAL, 0),
                ),
                "rotation.p": c4d.DescID(
                    c4d.DescLevel(c4d.ID_BASEOBJECT_ROTATION, c4d.DTYPE_VECTOR, 0),
                    c4d.DescLevel(c4d.VECTOR_Y, c4d.DTYPE_REAL, 0),
                ),
                "rotation.b": c4d.DescID(
                    c4d.DescLevel(c4d.ID_BASEOBJECT_ROTATION, c4d.DTYPE_VECTOR, 0),
                    c4d.DescLevel(c4d.VECTOR_Z, c4d.DTYPE_REAL, 0),
                ),
                "scale.x": c4d.DescID(
                    c4d.DescLevel(c4d.ID_BASEOBJECT_SCALE, c4d.DTYPE_VECTOR, 0),
                    c4d.DescLevel(c4d.VECTOR_X, c4d.DTYPE_REAL, 0),
                ),
                "scale.y": c4d.DescID(
                    c4d.DescLevel(c4d.ID_BASEOBJECT_SCALE, c4d.DTYPE_VECTOR, 0),
                    c4d.DescLevel(c4d.VECTOR_Y, c4d.DTYPE_REAL, 0),
                ),
                "scale.z": c4d.DescID(
                    c4d.DescLevel(c4d.ID_BASEOBJECT_SCALE, c4d.DTYPE_VECTOR, 0),
                    c4d.DescLevel(c4d.VECTOR_Z, c4d.DTYPE_REAL, 0),
                ),
            }

            # Get the C4D property ID
            if property_name in property_map:
                prop_id = property_map[property_name]
            else:
                return {"error": f"Unsupported property: {property_name}"}

            # Get or create track
            track = obj.FindCTrack(prop_id)
            if track is None:
                track = c4d.CTrack(obj, prop_id)
                obj.InsertTrackSorted(track)

            # Get the curve
            curve = track.GetCurve()
            if curve is None:
                return {"error": "Failed to get animation curve"}

            # Set the keyframe
            time_point = c4d.BaseTime(frame, doc.GetFps())

            # For rotation, convert degrees to radians
            if "rotation" in property_name:
                value = c4d.utils.DegToRad(value)

            # Add or modify the key
            key = curve.AddKey(time_point)
            if key is None or key["key"] is None:
                return {"error": "Failed to create keyframe"}

            key["key"].SetValue(curve, value)

            # Update the document
            c4d.EventAdd()

            return {
                "success": True,
                "message": f"Keyframe set for {object_name}.{property_name} = {value} at frame {frame}",
            }
        except Exception as e:
            return {"error": f"Failed to set keyframe: {str(e)}"}

    def set_position_keyframe(self, obj, frame, position):
        """Set a keyframe for the object's position."""
        doc = c4d.documents.GetActiveDocument()
        fps = doc.GetFps()
        track_ids = [
            ("position.x", position[0]),
            ("position.y", position[1]),
            ("position.z", position[2]),
        ]

        for prop_name, value in track_ids:
            desc_id = {
                "position.x": c4d.DescID(
                    c4d.DescLevel(c4d.ID_BASEOBJECT_POSITION, c4d.DTYPE_VECTOR, 0),
                    c4d.DescLevel(c4d.VECTOR_X, c4d.DTYPE_REAL, 0),
                ),
                "position.y": c4d.DescID(
                    c4d.DescLevel(c4d.ID_BASEOBJECT_POSITION, c4d.DTYPE_VECTOR, 0),
                    c4d.DescLevel(c4d.VECTOR_Y, c4d.DTYPE_REAL, 0),
                ),
                "position.z": c4d.DescID(
                    c4d.DescLevel(c4d.ID_BASEOBJECT_POSITION, c4d.DTYPE_VECTOR, 0),
                    c4d.DescLevel(c4d.VECTOR_Z, c4d.DTYPE_REAL, 0),
                ),
            }[prop_name]

            track = obj.FindCTrack(desc_id)
            if not track:
                track = c4d.CTrack(obj, desc_id)
                obj.InsertTrackSorted(track)

            curve = track.GetCurve()
            key_dict = curve.AddKey(c4d.BaseTime(frame, fps))
            if key_dict is None:
                self.log(f"Failed to add keyframe for {prop_name}")
                continue

            key = key_dict["key"]
            key.SetValue(curve, value)

        c4d.EventAdd()

    def handle_save_scene(self, command):
        """Handle save_scene command with main thread execution and proper timeout handling."""
        file_path = command.get("file_path", None)
        self.log(f"[C4D] Saving scene to: {file_path}")

        # Define function to execute on main thread
        def save_scene_on_main_thread(doc, file_path):
            self.log("[C4D] Executing save operation on main thread")
            try:
                # If no path is provided, use the current one
                if file_path is None:
                    self.log("[C4D] No path provided, using current document path")
                    file_path = doc.GetDocumentPath() + "/" + doc.GetDocumentName()

                    # If document has no path yet, return error
                    if not doc.GetDocumentPath() or not doc.GetDocumentName():
                        self.log("[C4D] No current document path available")
                        return {
                            "error": "No save path specified and document has no current path"
                        }

                # Make sure path has proper extension
                if not file_path.lower().endswith(".c4d"):
                    file_path += ".c4d"
                    self.log(f"[C4D] Added .c4d extension: {file_path}")

                # Make sure directory exists
                directory = os.path.dirname(file_path)
                if directory and not os.path.exists(directory):
                    try:
                        self.log(f"[C4D] Creating directory: {directory}")
                        os.makedirs(directory)
                    except Exception as e:
                        self.log(f"[C4D] Failed to create directory: {str(e)}")
                        return {
                            "error": f"Failed to create directory {directory}: {str(e)}"
                        }

                # Save document
                self.log(f"[C4D] Saving document to: {file_path}")
                start_time = time.time()

                result = c4d.documents.SaveDocument(
                    doc,
                    file_path,
                    c4d.SAVEDOCUMENTFLAGS_DONTADDTORECENTLIST,
                    c4d.FORMAT_C4DEXPORT,
                )

                save_time = time.time() - start_time
                self.log(
                    f"[C4D] Document save completed in {save_time:.2f} seconds, result: {result}"
                )

                if result:
                    return {
                        "save_info": {
                            "success": True,
                            "path": file_path,
                            "save_time": save_time,
                        }
                    }
                else:
                    return {"error": f"Failed to save document to {file_path}"}

            except Exception as e:
                self.log(f"[C4D] Error in save_scene_on_main_thread: {str(e)}")
                return {"error": f"Failed to save scene: {str(e)}"}

        try:
            # Get the active document
            doc = c4d.documents.GetActiveDocument()
            if not doc:
                self.log("[C4D] No active document")
                return {"error": "No active document"}

            # Execute save on main thread with extended timeout (60 seconds)
            self.log("[C4D] Dispatching save operation to main thread")

            # Update the execute_on_main_thread call to use a longer timeout for save operations
            # We need to modify our approach here since we can't directly modify the timeout
            # Instead, let's set a tag in the command to indicate it needs extended timeout

            # Execute the save operation on the main thread
            result = self.execute_on_main_thread(
                save_scene_on_main_thread, doc, file_path
            )

            self.log(f"[C4D] Save operation finished with result: {result}")
            return result

        except Exception as e:
            self.log(f"[C4D] Error in handle_save_scene: {str(e)}")
            return {"error": f"Failed to save scene: {str(e)}"}

    def handle_load_scene(self, command):
        """Handle load_scene command."""
        file_path = command.get("file_path", "")

        if not file_path:
            return {"error": "No file path provided"}

        try:
            # Check if file exists
            if not os.path.exists(file_path):
                return {"error": f"File not found: {file_path}"}

            # Load the document
            loaded_doc = c4d.documents.LoadDocument(
                file_path, c4d.SCENEFILTER_OBJECTS | c4d.SCENEFILTER_MATERIALS
            )

            if loaded_doc is None:
                return {"error": f"Failed to load document from {file_path}"}

            # Make it the active document
            c4d.documents.SetActiveDocument(loaded_doc)

            # Update the C4D UI
            c4d.EventAdd()

            return {"success": True, "message": f"Loaded scene from {file_path}"}
        except Exception as e:
            return {"error": f"Failed to load scene: {str(e)}"}

    def handle_execute_python(self, command):
        """Handle execute_python command."""
        script = command.get("script", "")

        if not script:
            return {"error": "No script provided"}

        try:
            # Create a dictionary to capture output
            output_dict = {"output": ""}

            # Define a function to capture print output
            def capture_print(*args, **kwargs):
                # Convert args to strings and join with spaces
                output = " ".join(str(arg) for arg in args)
                if "end" in kwargs:
                    output += kwargs["end"]
                else:
                    output += "\n"
                output_dict["output"] += output

            # Save original print function
            original_print = __builtins__["print"]

            # Replace print with our capture function
            __builtins__["print"] = capture_print

            # Create a local environment with document
            local_env = {"doc": c4d.documents.GetActiveDocument(), "c4d": c4d}

            # Execute the script
            exec(script, globals(), local_env)

            # Restore original print
            __builtins__["print"] = original_print

            # Ensure UI is updated
            c4d.EventAdd()

            return {
                "result": output_dict["output"]
                or "Script executed successfully with no output."
            }
        except Exception as e:
            return {"error": f"Script execution failed: {str(e)}"}

    # Advanced commands
    def handle_create_mograph_cloner(self, command):
        """
        Handle create_mograph_cloner command.
        Creates a MoGraph Cloner object with the specified properties.
        Based on Cinema 4D R2025 SDK documentation for MoGraph.
        """
        doc = c4d.documents.GetActiveDocument()
        name = command.get("cloner_name", "MoGraph Cloner")
        mode = command.get("mode", "grid").lower()
        count = command.get("count", 10)
        object_name = command.get("object_name", None)

        self.log(f"[C4D] Creating MoGraph Cloner: {name}, Mode: {mode}, Count: {count}")

        # Find object to clone if specified
        clone_obj = None
        if object_name:
            clone_obj = self.find_object_by_name(doc, object_name)
            if not clone_obj:
                self.log(f"[C4D] Clone object not found: {object_name}")
                return {"error": f"Object '{object_name}' not found."}
            self.log(f"[C4D] Found clone object: {object_name}")

        # Define a function to run on the main thread:
        def create_mograph_cloner_safe(doc, name, mode, count, clone_obj):
            self.log("[C4D] Creating MoGraph Cloner on main thread")
            try:
                # Create cloner object
                cloner = c4d.BaseObject(c4d.Omgcloner)
                if not cloner:
                    self.log("[C4D] Failed to create Cloner object")
                    return {"error": "Failed to create Cloner object"}

                cloner.SetName(name)
                self.log(f"[C4D] Created cloner: {name}")

                # Map mode strings to C4D mode IDs
                mode_ids = {
                    "linear": 0,  # Linear mode
                    "radial": 2,  # Radial mode
                    "grid": 1,  # Grid mode
                    "object": 3,  # Object mode
                }
                mode_id = mode_ids.get(mode, 0)  # Default to Linear

                # First insert the cloner into the document so parameter changes take effect
                doc.InsertObject(cloner)
                doc.AddUndo(c4d.UNDOTYPE_NEW, cloner)

                # Set the distribution mode
                self.log(f"[C4D] Setting cloner mode to: {mode} (ID: {mode_id})")
                cloner[c4d.ID_MG_MOTIONGENERATOR_MODE] = mode_id

                # Create a clone of the provided object, or a default cube
                child_obj = None
                if clone_obj:
                    self.log(f"[C4D] Cloning source object: {clone_obj.GetName()}")
                    child_obj = clone_obj.GetClone()
                else:
                    self.log("[C4D] Creating default cube as clone source")
                    child_obj = c4d.BaseObject(c4d.Ocube)
                    child_obj.SetName("Default Cube")
                    child_obj.SetAbsScale(c4d.Vector(0.5, 0.5, 0.5))

                if not child_obj:
                    self.log("[C4D] Failed to create child object for cloner")
                    return {"error": "Failed to create child object for cloner"}

                # Insert the child object under the cloner (this is critical!)
                self.log("[C4D] Inserting child object under cloner")
                doc.InsertObject(child_obj)
                doc.AddUndo(c4d.UNDOTYPE_NEW, child_obj)

                # Ensure proper hierarchy - this is the key part!
                child_obj.InsertUnderLast(cloner)

                # Set specific parameters based on mode
                # Setting these parameters AFTER creating the hierarchy ensures they take effect
                if mode == "linear":
                    self.log(f"[C4D] Configuring linear mode with count: {count}")
                    cloner[c4d.MG_LINEAR_COUNT] = count
                    # Set a reasonable default offset
                    cloner[c4d.MG_LINEAR_OFFSET] = 100

                elif mode == "grid":
                    # Calculate dimensions for a reasonable grid based on total count
                    grid_dim = max(1, int(round(count ** (1 / 3))))
                    self.log(
                        f"[C4D] Configuring grid mode with dimensions: {grid_dim}x{grid_dim}x{grid_dim}"
                    )
                    # Use the correct parameter IDs for grid mode in R2025
                    # Define these manually if not available in the SDK
                    # Based on MoGraph documentation, these are the correct IDs
                    MG_GRID_COUNT_X = 1001
                    MG_GRID_COUNT_Y = 1002
                    MG_GRID_COUNT_Z = 1003
                    MG_GRID_SIZE = 1010
                    
                    cloner[MG_GRID_COUNT_X] = grid_dim
                    cloner[MG_GRID_COUNT_Y] = grid_dim
                    cloner[MG_GRID_COUNT_Z] = grid_dim
                    # Set a reasonable size
                    cloner[MG_GRID_SIZE] = c4d.Vector(100, 100, 100)

                elif mode == "radial":
                    self.log(f"[C4D] Configuring radial mode with count: {count}")
                    cloner[c4d.MG_POLY_COUNT] = count
                    # Set a reasonable radius
                    cloner[c4d.MG_POLY_RADIUS] = 200

                elif mode == "object":
                    self.log(f"[C4D] Configuring object mode")
                    # For object mode, we would need a target object
                    # This could be added in a future enhancement

                # Ensure the cloner's iteration mode is set to iterate
                # This determines how the cloner uses child objects
                cloner[c4d.MGCLONER_MODE] = c4d.MGCLONER_MODE_ITERATE

                # Update the document
                self.log("[C4D] Calling EventAdd to update document")
                c4d.EventAdd()

                # Log summary of what was created
                self.log(
                    f"[C4D] Successfully created {mode} cloner with {count} instances"
                )

                return {
                    "name": cloner.GetName(),
                    "id": str(cloner.GetGUID()),
                    "type": mode,
                    "count": count,
                    "type_id": cloner.GetType(),
                }
            except Exception as e:
                self.log(f"[C4D] Error in create_mograph_cloner_safe: {str(e)}")
                import traceback

                traceback.print_exc()
                return {"error": f"Failed to create MoGraph Cloner: {str(e)}"}

        try:
            # Execute the creation safely on the main thread with extended timeout
            self.log("[C4D] Dispatching cloner creation to main thread")
            cloner_info = self.execute_on_main_thread(
                create_mograph_cloner_safe,
                doc,
                name,
                mode,
                count,
                clone_obj,
                _timeout=30,  # Give it 30 seconds timeout
            )

            if isinstance(cloner_info, dict) and "error" in cloner_info:
                self.log(f"[C4D] Error from main thread: {cloner_info['error']}")
                return cloner_info

            self.log(f"[C4D] Cloner created successfully: {cloner_info}")
            return {"success": True, "cloner": cloner_info}
        except Exception as e:
            self.log(f"[C4D] Exception creating MoGraph Cloner: {str(e)}")
            return {"error": f"Failed to create MoGraph Cloner: {str(e)}"}

    def handle_apply_mograph_fields(self, command):
        """Handle apply_mograph_fields command with robust error handling to prevent crashes.

        Rewritten based on Cinema 4D R2025 SDK documentation for MoGraph Fields.
        """
        # Extract command parameters with defaults
        field_type = command.get("field_type", "spherical").lower()
        field_name = command.get("field_name", f"{field_type.capitalize()} Field")
        target_name = command.get("target_name", "")
        parameters = command.get("parameters", {})

        self.log(
            f"[C4D] Starting apply_mograph_fields for {field_type} field named '{field_name}'"
        )

        # Define function for main thread execution that follows Cinema 4D SDK documentation
        def create_field_safe(doc, field_type, field_name, target_name, parameters):
            """Create a field on the main thread following R2025 SDK guidelines."""
            self.log("[C4D] Creating field on main thread (using R2025 SDK approach)")

            result = {}
            field = None
            target = None
            field_applied = False
            applied_to = "None"

            try:
                # Step 1: Map field type to proper SDK constants
                # Define these manually if not available in the SDK
                # Based on MoGraph documentation in R2025, these are the correct IDs
                Fsphere = 1039384     # Spherical Field
                Fbox = 1039385        # Box Field
                Fcylinder = 1039386   # Cylindrical Field  
                Ftorus = 1039387      # Torus Field
                Fcone = 1039388       # Cone Field
                Flinear = 1039389     # Linear Field
                Fradial = 1039390     # Radial Field
                Fsound = 1039391      # Sound Field
                Fnoise = 1039394      # Noise Field
                
                field_constants = {
                    "spherical": Fsphere,        # Spherical Field 
                    "box": Fbox,                 # Box Field
                    "cylindrical": Fcylinder,    # Cylindrical Field
                    "torus": Ftorus,             # Torus Field
                    "cone": Fcone,               # Cone Field
                    "linear": Flinear,           # Linear Field
                    "radial": Fradial,           # Radial Field
                    "sound": Fsound,             # Sound Field
                    "noise": Fnoise,             # Noise Field
                }

                # Get the proper field type constant or default to spherical
                field_type_id = field_constants.get(field_type, Fsphere)
                self.log(f"[C4D] Using field type: {field_type} (ID: {field_type_id})")

                # Step 2: Create the field object using proper SDK approach
                self.log(f"[C4D] Creating {field_type} field object")
                field = c4d.BaseObject(field_type_id)
                if not field:
                    self.log("[C4D] Failed to create field object")
                    result["error"] = "Failed to create field object"
                    return result

                field.SetName(field_name)

                # Step 3: Set field parameters
                if "strength" in parameters and isinstance(
                    parameters["strength"], (int, float)
                ):
                    field[c4d.FIELD_STRENGTH] = float(parameters["strength"])
                    self.log(f"[C4D] Set strength: {parameters['strength']}")

                if "falloff" in parameters and isinstance(
                    parameters["falloff"], (int, float)
                ):
                    field[c4d.FIELD_FALLOFF] = float(parameters["falloff"])
                    self.log(f"[C4D] Set falloff: {parameters['falloff']}")

                # Step 4: Insert field into document (must do this first)
                self.log("[C4D] Inserting field into document")
                doc.InsertObject(field)
                doc.AddUndo(c4d.UNDOTYPE_NEW, field)

                # Step 5: Find target if specified
                if target_name:
                    self.log(f"[C4D] Looking for target: {target_name}")
                    target = self.find_object_by_name(doc, target_name)

                    if not target:
                        self.log(f"[C4D] Target object '{target_name}' not found")
                    else:
                        self.log(f"[C4D] Found target: {target.GetName()}")

                # Step 6: Apply field to target if found - using correct SDK approach
                if target:
                    self.log(f"[C4D] Creating Fields tag for {target.GetName()}")
                    tag = c4d.BaseTag(c4d.Tfields)

                    if not tag:
                        self.log("[C4D] Failed to create Fields tag")
                    else:
                        # Insert tag into target object
                        target.InsertTag(tag)
                        doc.AddUndo(c4d.UNDOTYPE_NEW, tag)

                        # Explicitly follow the SDK procedure for Fields
                        # 1. Get the field list (create if needed)
                        self.log("[C4D] Getting/creating FieldList")
                        field_list = tag[c4d.FIELDS]

                        if not field_list or not isinstance(field_list, c4d.FieldList):
                            field_list = c4d.FieldList()
                            self.log("[C4D] Created new FieldList")

                        # 2. Create a proper Field Layer using the modules.mograph namespace
                        self.log("[C4D] Creating FieldLayer")
                        try:
                            # Using proper namespace from SDK documentation
                            if hasattr(c4d.modules, "mograph"):
                                field_layer = c4d.modules.mograph.FieldLayer(
                                    c4d.FLfield
                                )
                                self.log(
                                    "[C4D] Created field layer using c4d.modules.mograph"
                                )
                            else:
                                # Fallback if mograph module not available
                                field_layer = c4d.FieldLayer(c4d.FLfield)
                                self.log(
                                    "[C4D] Created field layer using c4d.FieldLayer"
                                )

                            if not field_layer:
                                self.log("[C4D] Failed to create FieldLayer")
                                raise RuntimeError("Failed to create FieldLayer")

                            # 3. Link the field object to the layer
                            self.log(
                                f"[C4D] Linking field '{field.GetName()}' to layer"
                            )
                            success = field_layer.SetLinkedObject(field)
                            if not success:
                                self.log(
                                    "[C4D] Warning: SetLinkedObject returned False"
                                )

                            # 4. Insert the layer into the field list
                            self.log("[C4D] Inserting layer into field list")
                            field_list.InsertLayer(field_layer)

                            # 5. Assign the modified field list back to the tag
                            self.log("[C4D] Setting field list on tag")
                            tag[c4d.FIELDS] = field_list

                            # Mark as applied and register undo
                            doc.AddUndo(c4d.UNDOTYPE_CHANGE, tag)
                            field_applied = True
                            applied_to = target.GetName()
                            self.log(
                                f"[C4D] Successfully applied field to {applied_to}"
                            )

                        except Exception as e:
                            self.log(f"[C4D] Error setting up field layer: {str(e)}")
                            import traceback

                            traceback.print_exc()

                # Step 7: Update scene
                self.log("[C4D] Calling EventAdd to update scene")
                c4d.EventAdd()

                # Step 8: Prepare result
                if field:
                    field_info = {
                        "name": field.GetName(),
                        "id": str(field.GetGUID()),
                        "type": field_type,
                        "applied_to": applied_to,
                    }

                    if "strength" in parameters:
                        field_info["strength"] = parameters["strength"]

                    self.log(f"[C4D] Field creation complete: {field.GetName()}")
                    result["field"] = field_info
                else:
                    self.log("[C4D] No field was created")
                    result["error"] = "Failed to create field object"

                return result

            except Exception as e:
                self.log(f"[C4D] Error in create_field_safe: {str(e)}")
                import traceback

                traceback.print_exc()
                result["error"] = f"Failed to apply MoGraph field: {str(e)}"
                return result

        try:
            # Get the active document
            doc = c4d.documents.GetActiveDocument()
            if not doc:
                self.log("[C4D] No active document")
                return {"error": "No active document"}

            # Execute field creation on the main thread with explicit timeout
            self.log(
                "[C4D] Dispatching field creation to main thread with explicit timeout"
            )
            result = self.execute_on_main_thread(
                create_field_safe,
                doc,
                field_type,
                field_name,
                target_name,
                parameters,
                _timeout=60,  # Extended timeout for field operations
            )

            # Make sure we always return a valid result with detailed error checking
            if result is None:
                self.log(f"[C4D] Main thread execution returned None")
                return {"error": "Main thread execution returned None"}
            elif not isinstance(result, dict):
                self.log(
                    f"[C4D] Unexpected result type: {type(result)}, value: {str(result)[:100]}"
                )
                # Try to convert non-dict result to a dict result
                try:
                    return {
                        "field": {
                            "name": field_name,
                            "type": field_type,
                            "result": str(result),
                        }
                    }
                except:
                    return {
                        "error": f"Unexpected result type from main thread execution: {type(result)}"
                    }

            return result

        except Exception as e:
            self.log(f"[C4D] Error in handle_apply_mograph_fields: {str(e)}")
            import traceback

            traceback.print_exc()
            return {"error": f"Failed to apply MoGraph field: {str(e)}"}

    def handle_create_soft_body(self, command):
        """Handle create_soft_body command."""
        doc = c4d.documents.GetActiveDocument()
        object_name = command.get("object_name", "")
        name = command.get("name", "Soft Body")
        stiffness = command.get("stiffness", 50)
        mass = command.get("mass", 1.0)

        # Find target object
        obj = self.find_object_by_name(doc, object_name)
        if obj is None:
            return {"error": f"Object not found: {object_name}"}

        def create_soft_body_safe(obj, name, stiffness, mass, object_name):
            # Create Dynamics tag (ID 180000102)
            tag = c4d.BaseTag(180000102)
            if tag is None:
                raise RuntimeError("Failed to create Dynamics Body tag")
            tag.SetName(name)

            tag[c4d.RIGID_BODY_DYNAMIC] = 1  # Enable dynamics
            tag[c4d.RIGID_BODY_MASS] = mass
            tag[c4d.RIGID_BODY_SOFTBODY] = True  # Enable soft body

            obj.InsertTag(tag)
            doc.AddUndo(c4d.UNDOTYPE_NEW, tag)
            c4d.EventAdd()

            return {
                "object": object_name,
                "tag_name": tag.GetName(),
                "stiffness": stiffness,
                "mass": mass,
            }

        try:
            soft_body_info = self.execute_on_main_thread(
                create_soft_body_safe, obj, name, stiffness, mass, object_name
            )
            return {"soft_body": soft_body_info}
        except Exception as e:
            return {"error": f"Failed to create Soft Body: {str(e)}"}

    def handle_apply_dynamics(self, command):
        """Handle apply_dynamics command."""
        doc = c4d.documents.GetActiveDocument()
        object_name = command.get("object_name", "")
        tag_type = command.get("tag_type", "rigid_body").lower()
        params = command.get("parameters", {})

        try:
            obj = self.find_object_by_name(doc, object_name)
            if obj is None:
                return {"error": f"Object not found: {object_name}"}

            tag_types = {
                "rigid_body": 180000102,  # Rigid Body tag
                "collider": 180000102,  # Different mode in same tag
                "connector": 180000103,  # Connector tag
                "ghost": 180000102,  # Special mode in dynamics tag
            }
            tag_type_id = tag_types.get(tag_type, 180000102)

            tag = c4d.BaseTag(tag_type_id)
            if tag is None:
                return {"error": f"Failed to create {tag_type} tag"}

            if tag_type == "rigid_body":
                tag[c4d.RIGID_BODY_DYNAMIC] = 2  # Dynamic mode
            elif tag_type == "collider":
                tag[c4d.RIGID_BODY_DYNAMIC] = 0  # Static mode
            elif tag_type == "ghost":
                tag[c4d.RIGID_BODY_DYNAMIC] = 3  # Ghost mode

            # Set common parameters.
            if "mass" in params and isinstance(params["mass"], (int, float)):
                tag[c4d.RIGID_BODY_MASS] = float(params["mass"])
            if "friction" in params and isinstance(params["friction"], (int, float)):
                tag[c4d.RIGID_BODY_FRICTION] = float(params["friction"])
            if "elasticity" in params and isinstance(
                params["elasticity"], (int, float)
            ):
                tag[c4d.RIGID_BODY_ELASTICITY] = float(params["elasticity"])
            if "collision_margin" in params and isinstance(
                params["collision_margin"], (int, float)
            ):
                tag[c4d.RIGID_BODY_MARGIN] = float(params["collision_margin"])

            obj.InsertTag(tag)
            doc.AddUndo(c4d.UNDOTYPE_NEW, tag)
            c4d.EventAdd()

            return {
                "dynamics": {
                    "object": object_name,
                    "tag_type": tag_type,
                    "parameters": params,
                }
            }
        except Exception as e:
            return {"error": f"Failed to apply Dynamics tag: {str(e)}"}

    def handle_create_abstract_shape(self, command):
        """Handle create_abstract_shape command."""
        doc = c4d.documents.GetActiveDocument()
        shape_type = command.get("shape_type", "metaball").lower()
        name = command.get("object_name", f"{shape_type.capitalize()}")
        position = command.get("position", [0, 0, 0])

        try:
            shape_types = {
                "metaball": 5159,
                "metaball_spline": 5161,
                "loft": 5107,
                "sweep": 5118,
                "atom": 5168,
                "platonic": 5170,
                "cloth": 5186,
                "landscape": 5119,
                "extrude": 5116,
            }
            shape_type_id = shape_types.get(shape_type, 5159)

            shape = c4d.BaseObject(shape_type_id)
            if shape is None:
                return {"error": f"Failed to create {shape_type} object"}

            shape.SetName(name)
            if len(position) >= 3:
                shape.SetAbsPos(c4d.Vector(position[0], position[1], position[2]))

            # For certain shapes, add additional child objects.
            if shape_type == "metaball":
                sphere = c4d.BaseObject(c4d.Osphere)
                sphere.SetName("Metaball Sphere")
                sphere.SetAbsScale(c4d.Vector(0.5, 0.5, 0.5))
                sphere.InsertUnder(shape)
                doc.AddUndo(c4d.UNDOTYPE_NEW, sphere)
            elif shape_type in ("loft", "sweep"):
                spline = c4d.BaseObject(c4d.Osplinecircle)
                spline.SetName("Profile Spline")
                spline.InsertUnder(shape)
                doc.AddUndo(c4d.UNDOTYPE_NEW, spline)
                path = c4d.BaseObject(c4d.Osplinenside)
                path.SetName("Path Spline")
                path.SetAbsPos(c4d.Vector(0, 50, 0))
                path.InsertUnder(shape)
                doc.AddUndo(c4d.UNDOTYPE_NEW, path)

            doc.InsertObject(shape)
            doc.AddUndo(c4d.UNDOTYPE_NEW, shape)
            c4d.EventAdd()

            return {
                "shape": {
                    "name": shape.GetName(),
                    "id": str(shape.GetGUID()),
                    "type": shape_type,
                    "position": position,
                }
            }
        except Exception as e:
            return {"error": f"Failed to create abstract shape: {str(e)}"}

    def handle_create_light(self, command):
        """Handle create_light command."""
        doc = c4d.documents.GetActiveDocument()
        light_type = command.get("type", "spot").lower()
        name = command.get("object_name", f"{light_type.capitalize()} Light")
        position = command.get("position", [0, 100, 0])
        color = command.get("color", [1, 1, 1])
        intensity = command.get("intensity", 100)

        try:
            light = c4d.BaseObject(c4d.Olight)
            if light is None:
                return {"error": "Failed to create light object"}
            light.SetName(name)

            light_type_map = {
                "spot": 0,
                "point": 1,
                "distant": 2,
                "area": 3,
                "paraxial": 4,
                "parallel": 5,
                "omni": 1,
            }
            light[c4d.LIGHT_TYPE] = light_type_map.get(light_type, 1)
            if len(position) >= 3:
                light.SetAbsPos(c4d.Vector(position[0], position[1], position[2]))
            if len(color) >= 3:
                light[c4d.LIGHT_COLOR] = c4d.Vector(color[0], color[1], color[2])
            light[c4d.LIGHT_BRIGHTNESS] = intensity
            light[c4d.LIGHT_SHADOWTYPE] = 1  # Use shadow maps

            doc.InsertObject(light)
            doc.AddUndo(c4d.UNDOTYPE_NEW, light)
            c4d.EventAdd()

            return {
                "light": {
                    "name": light.GetName(),
                    "id": str(light.GetGUID()),
                    "type": light_type,
                    "position": position,
                    "color": color,
                    "intensity": intensity,
                }
            }
        except Exception as e:
            return {"error": f"Failed to create light: {str(e)}"}

    def handle_apply_shader(self, command):
        """Handle apply_shader command."""
        doc = c4d.documents.GetActiveDocument()
        material_name = command.get("material_name", "")
        object_name = command.get("object_name", "")
        shader_type = command.get("shader_type", "noise").lower()
        channel = command.get("channel", "color").lower()
        parameters = command.get("parameters", {})

        # Debug logging
        self.log(f"[C4D] Applying {shader_type} shader")
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

            # Map shader types to C4D constants
            shader_types = {
                "noise": 5832,
                "gradient": 5825,
                "fresnel": 5837,
                "layer": 5685,
                "posterizer": 5847,
                "colorizer": 5693,
                "distorter": 5694,
                "spline": 5688,
                "brick": 5834,
                "marble": 5835,
                "wood": 5836,
                "checkerboard": 5831,
            }

            # Map channel names to C4D constants
            channel_map = {
                "color": c4d.MATERIAL_COLOR_SHADER,
                "luminance": c4d.MATERIAL_LUMINANCE_SHADER,
                "transparency": c4d.MATERIAL_TRANSPARENCY_SHADER,
                "reflection": c4d.MATERIAL_REFLECTION_SHADER,
                "environment": c4d.MATERIAL_ENVIRONMENT_SHADER,
                "bump": c4d.MATERIAL_BUMP_SHADER,
                "normal": c4d.MATERIAL_NORMAL_SHADER,
                "alpha": c4d.MATERIAL_ALPHA_SHADER,
                "specular": c4d.MATERIAL_SPECULAR_SHADER,
                "diffusion": c4d.MATERIAL_DIFFUSION_SHADER,
            }

            # Get the appropriate shader ID and channel ID
            shader_type_id = shader_types.get(shader_type, 5832)  # Default to noise
            channel_id = channel_map.get(
                channel, c4d.MATERIAL_COLOR_SHADER
            )  # Default to color

            # Create the shader
            shader = c4d.BaseShader(shader_type_id)
            if shader is None:
                self.log(f"[C4D] Failed to create {shader_type} shader")
                return {"error": f"Failed to create {shader_type} shader"}

            # Set parameters for the shader
            if shader_type == "noise":
                if "scale" in parameters and isinstance(
                    parameters["scale"], (int, float)
                ):
                    shader[c4d.SLA_NOISE_SCALE] = float(parameters["scale"])
                if "octaves" in parameters and isinstance(parameters["octaves"], int):
                    shader[c4d.SLA_NOISE_OCTAVES] = parameters["octaves"]
                if "type" in parameters and isinstance(parameters["type"], int):
                    shader[c4d.SLA_NOISE_NOISE] = parameters["type"]
            elif shader_type == "gradient":
                if "type" in parameters and isinstance(parameters["type"], int):
                    shader[c4d.SLA_GRADIENT_TYPE] = parameters["type"]
                if "interpolation" in parameters and isinstance(
                    parameters["interpolation"], int
                ):
                    shader[c4d.SLA_GRADIENT_INTERPOLATION] = parameters["interpolation"]

            # Assign the shader to the material channel
            mat[channel_id] = shader

            # Enable the appropriate channel
            channel_enable_map = {
                "color": c4d.MATERIAL_USE_COLOR,
                "luminance": c4d.MATERIAL_USE_LUMINANCE,
                "transparency": c4d.MATERIAL_USE_TRANSPARENCY,
                "reflection": c4d.MATERIAL_USE_REFLECTION,
            }
            if channel in channel_enable_map:
                enable_id = channel_enable_map.get(channel)
                if enable_id is not None:
                    mat[enable_id] = True

            # Update the material
            mat.Update(True, True)
            doc.AddUndo(c4d.UNDOTYPE_CHANGE, mat)

            # Apply to object if specified
            applied_to = "None"
            if object_name:
                obj = self.find_object_by_name(doc, object_name)
                if obj is None:
                    self.log(
                        f"[C4D] Warning: Object '{object_name}' not found, cannot apply material"
                    )
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

    def handle_animate_camera(self, command):
        """Handle animate_camera command."""
        doc = c4d.documents.GetActiveDocument()
        camera_name = command.get("camera_name", "")
        path_type = command.get("path_type", "linear").lower()
        positions = command.get("positions", [])
        frames = command.get("frames", [])
        create_camera = command.get("create_camera", False)
        camera_properties = command.get("camera_properties", {})

        try:
            # Log the command for debugging purposes
            self.log(
                f"[C4D] Animate camera command: path_type={path_type}, camera={camera_name}, positions={len(positions)}, frames={len(frames)}"
            )

            camera = None
            if camera_name:
                camera = self.find_object_by_name(doc, camera_name)
                if camera is None:
                    self.log(
                        f"[C4D] Camera '{camera_name}' not found, will create a new one"
                    )

                    # List existing cameras to help with debugging
                    existing_cameras = []
                    obj = doc.GetFirstObject()
                    while obj:
                        if obj.GetType() == c4d.Ocamera:
                            existing_cameras.append(obj.GetName())
                        obj = obj.GetNext()

                    if existing_cameras:
                        self.log(
                            f"[C4D] Available cameras: {', '.join(existing_cameras)}"
                        )
                    else:
                        self.log("[C4D] No cameras found in the scene")

            if camera is None or create_camera:
                camera = c4d.BaseObject(c4d.Ocamera)
                camera.SetName(camera_name or "Animated Camera")
                self.log(f"[C4D] Created new camera: {camera.GetName()}")

                if "focal_length" in camera_properties and isinstance(
                    camera_properties["focal_length"], (int, float)
                ):
                    camera[c4d.CAMERA_FOCUS] = float(camera_properties["focal_length"])
                if "aperture" in camera_properties and isinstance(
                    camera_properties["aperture"], (int, float)
                ):
                    camera[c4d.CAMERA_APERTURE] = float(camera_properties["aperture"])
                if "film_offset_x" in camera_properties and isinstance(
                    camera_properties["film_offset_x"], (int, float)
                ):
                    camera[c4d.CAMERA_FILM_OFFSET_X] = float(
                        camera_properties["film_offset_x"]
                    )
                if "film_offset_y" in camera_properties and isinstance(
                    camera_properties["film_offset_y"], (int, float)
                ):
                    camera[c4d.CAMERA_FILM_OFFSET_Y] = float(
                        camera_properties["film_offset_y"]
                    )

                doc.InsertObject(camera)
                doc.AddUndo(c4d.UNDOTYPE_NEW, camera)
                doc.SetActiveObject(camera)

            # Add default frames if only positions are provided
            if positions and not frames:
                frames = list(range(len(positions)))

            if not positions or not frames or len(positions) != len(frames):
                return {
                    "error": "Invalid positions or frames data. They must be arrays of equal length."
                }

            # Set keyframes for camera positions.
            for pos, frame in zip(positions, frames):
                if len(pos) >= 3:
                    self.set_position_keyframe(camera, frame, pos)

            # If a spline path is requested.
            if path_type == "spline" and len(positions) > 1:
                path = c4d.BaseObject(c4d.Ospline)
                path.SetName(f"{camera.GetName()} Path")
                points = [
                    c4d.Vector(p[0], p[1], p[2]) for p in positions if len(p) >= 3
                ]
                path.ResizeObject(len(points))
                for i, pt in enumerate(points):
                    path.SetPoint(i, pt)
                doc.InsertObject(path)
                doc.AddUndo(c4d.UNDOTYPE_NEW, path)

                align_to_path = path_type == "spline_oriented"
                path_tag = c4d.BaseTag(c4d.Talignment)
                path_tag[c4d.ALIGNMENTOBJECT_LINK] = path
                path_tag[c4d.ALIGNMENTOBJECT_ALIGN] = align_to_path
                camera.InsertTag(path_tag)
                doc.AddUndo(c4d.UNDOTYPE_NEW, path_tag)

            c4d.EventAdd()

            return {
                "camera_animation": {
                    "camera": camera.GetName(),
                    "path_type": path_type,
                    "keyframe_count": len(positions),
                    "frame_range": [min(frames), max(frames)],
                }
            }
        except Exception as e:
            return {"error": f"Failed to animate camera: {str(e)}"}

    def handle_add_effector(self, command):
        """Handle add_effector command."""
        doc = c4d.documents.GetActiveDocument()
        name = command.get("effector_name", "New Effector")
        type_name = command.get("effector_type", "random").lower()
        cloner_name = command.get("cloner_name", "")
        properties = command.get("properties", {})

        try:
            # Debug log
            self.log(f"[C4D] Creating {type_name} effector named '{name}'")
            if cloner_name:
                self.log(f"[C4D] Will attempt to apply to cloner '{cloner_name}'")

            # Map effector types to C4D constants.
            effector_types = {
                "random": c4d.Omgrandom,
                "formula": c4d.Omgformula,
                "step": c4d.Omgstep,
                "target": (
                    c4d.Omgtarget
                    if hasattr(c4d, "Omgtarget")
                    else c4d.Omgeffectortarget
                ),
                "time": c4d.Omgtime,
                "sound": c4d.Omgsound,
                "plain": c4d.Omgplain,
                "delay": c4d.Omgdelay,
                "spline": c4d.Omgspline,
                "python": c4d.Omgpython,
            }

            if hasattr(c4d, "Omgfalloff"):
                effector_types["falloff"] = c4d.Omgfalloff

            effector_id = effector_types.get(type_name, c4d.Omgrandom)
            effector = c4d.BaseObject(effector_id)
            if effector is None:
                return {"error": f"Failed to create {type_name} effector"}
            effector.SetName(name)

            # Set common properties.
            if "strength" in properties and isinstance(
                properties["strength"], (int, float)
            ):
                effector[c4d.ID_MG_BASEEFFECTOR_STRENGTH] = float(
                    properties["strength"]
                )
            if "position_mode" in properties and isinstance(
                properties["position_mode"], bool
            ):
                effector[c4d.ID_MG_BASEEFFECTOR_POSITION_ACTIVE] = properties[
                    "position_mode"
                ]
            if "rotation_mode" in properties and isinstance(
                properties["rotation_mode"], bool
            ):
                effector[c4d.ID_MG_BASEEFFECTOR_ROTATION_ACTIVE] = properties[
                    "rotation_mode"
                ]
            if "scale_mode" in properties and isinstance(
                properties["scale_mode"], bool
            ):
                effector[c4d.ID_MG_BASEEFFECTOR_SCALE_ACTIVE] = properties["scale_mode"]

            doc.InsertObject(effector)
            doc.AddUndo(c4d.UNDOTYPE_NEW, effector)

            # If a cloner is specified, add the effector to its effector list.
            cloner_applied = False
            if cloner_name:
                # Try to find cloner by name - both exact and fuzzy matching
                cloner = None

                # Try standard find first
                cloner = self.find_object_by_name(doc, cloner_name)

                # If not found, and name is generic like "Cloner", try to find by type
                if cloner is None and cloner_name.lower() in [
                    "cloner",
                    "mograph cloner",
                ]:
                    self.log(f"[C4D] Trying to find any MoGraph Cloner object")
                    obj = doc.GetFirstObject()
                    while obj:
                        if obj.GetType() == c4d.Omgcloner:
                            cloner = obj
                            self.log(f"[C4D] Found cloner by type: {cloner.GetName()}")
                            break
                        obj = obj.GetNext()

                if cloner is None:
                    self.log(
                        f"[C4D] Warning: Cloner '{cloner_name}' not found, effector created but not applied"
                    )
                    # Instead of returning error, just continue without applying
                else:
                    if cloner.GetType() != c4d.Omgcloner:
                        self.log(
                            f"[C4D] Warning: Object '{cloner_name}' is not a MoGraph Cloner"
                        )
                        # Instead of returning error, just continue without applying
                    else:
                        try:
                            # Get the effector list or create a new one
                            effector_list = None

                            # Try to get existing list
                            try:
                                effector_list = cloner[
                                    c4d.ID_MG_MOTIONGENERATOR_EFFECTORLIST
                                ]
                            except:
                                self.log(f"[C4D] Creating new effector list for cloner")
                                pass

                            # Create new list if needed
                            if not isinstance(effector_list, c4d.InExcludeData):
                                effector_list = c4d.InExcludeData()

                            # Insert effector with enabled flag (1)
                            effector_list.InsertObject(effector, 1)
                            cloner[c4d.ID_MG_MOTIONGENERATOR_EFFECTORLIST] = (
                                effector_list
                            )
                            doc.AddUndo(c4d.UNDOTYPE_CHANGE, cloner)
                            cloner_applied = True
                            self.log(
                                f"[C4D] Successfully applied effector to cloner '{cloner.GetName()}'"
                            )
                        except Exception as e:
                            self.log(
                                f"[C4D] Error applying effector to cloner: {str(e)}"
                            )
                            # Continue without returning error - at least create the effector

            c4d.EventAdd()

            return {
                "object": {
                    "name": effector.GetName(),
                    "id": str(effector.GetGUID()),
                    "type": type_name,
                    "applied_to_cloner": cloner_applied,
                }
            }
        except Exception as e:
            self.log(f"[C4D] Error creating effector: {str(e)}")
            return {"error": f"Failed to create effector: {str(e)}"}

    # Helpers
    def count_objects(self, doc):
        """Count all objects in the document."""
        count = 0
        obj = doc.GetFirstObject()
        while obj:
            count += 1
            obj = obj.GetNext()
        return count

    def count_polygons(self, doc):
        """Count all polygons in the document."""
        count = 0
        obj = doc.GetFirstObject()
        while obj:
            if obj.GetType() == c4d.Opolygon:
                count += obj.GetPolygonCount()
            obj = obj.GetNext()
        return count

    def get_object_type_name(self, obj):
        """Get a human-readable object type name."""
        type_id = obj.GetType()

        # Expanded type map including MoGraph objects
        type_map = {
            c4d.Ocube: "Cube",
            c4d.Osphere: "Sphere",
            c4d.Ocone: "Cone",
            c4d.Ocylinder: "Cylinder",
            c4d.Oplane: "Plane",
            c4d.Olight: "Light",
            c4d.Ocamera: "Camera",
            c4d.Onull: "Null",
            c4d.Opolygon: "Polygon Object",
            c4d.Ospline: "Spline",
            c4d.Omgcloner: "MoGraph Cloner",  # MoGraph Cloner
        }

        # Check for MoGraph objects using ranges
        if 1018544 <= type_id <= 1019544:  # MoGraph objects general range
            if type_id == c4d.Omgcloner:
                return "MoGraph Cloner"
            elif type_id == c4d.Omgtext:
                return "MoGraph Text"
            elif type_id == c4d.Omgtracer:
                return "MoGraph Tracer"
            elif type_id == c4d.Omgmatrix:
                return "MoGraph Matrix"
            else:
                return "MoGraph Object"

        # MoGraph Effectors
        if 1019544 <= type_id <= 1019644:
            if type_id == c4d.Omgrandom:
                return "Random Effector"
            elif type_id == c4d.Omgstep:
                return "Step Effector"
            elif type_id == c4d.Omgformula:
                return "Formula Effector"
            else:
                return "MoGraph Effector"

        # Fields (newer Cinema 4D versions)
        if 1039384 <= type_id <= 1039484:
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
            return field_types.get(type_id, "Field")

        return type_map.get(type_id, f"Object (Type: {type_id})")

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

    def get_all_objects_comprehensive(self, doc):
        """Get all objects in the document using multiple methods to ensure complete coverage.

        This method is specifically designed to catch objects that might be missed by
        standard GetFirstObject()/GetNext() iteration, particularly MoGraph objects.

        Args:
            doc: The Cinema 4D document to search

        Returns:
            List of all objects found
        """
        all_objects = []
        found_ids = set()

        # Method 1: Standard traversal using GetFirstObject/GetNext/GetDown
        self.log("[C4D] Comprehensive search - using standard traversal")

        def traverse_hierarchy(obj):
            while obj:
                try:
                    obj_id = str(obj.GetGUID())
                    if obj_id not in found_ids:
                        all_objects.append(obj)
                        found_ids.add(obj_id)

                        # Check children
                        child = obj.GetDown()
                        if child:
                            traverse_hierarchy(child)
                except Exception as e:
                    self.log(f"[C4D] Error in hierarchy traversal: {str(e)}")

                # Move to next sibling
                obj = obj.GetNext()

        # Start traversal from the first object
        first_obj = doc.GetFirstObject()
        if first_obj:
            traverse_hierarchy(first_obj)

        # Method 2: Use GetObjects() for flat list (catches some objects)
        try:
            self.log("[C4D] Comprehensive search - using GetObjects()")
            flat_objects = doc.GetObjects()
            for obj in flat_objects:
                obj_id = str(obj.GetGUID())
                if obj_id not in found_ids:
                    all_objects.append(obj)
                    found_ids.add(obj_id)
        except Exception as e:
            self.log(f"[C4D] Error in GetObjects search: {str(e)}")

        # Method 3: Special handling for MoGraph objects
        try:
            self.log("[C4D] Comprehensive search - direct access for MoGraph")

            # Direct check for Cloners
            if hasattr(c4d, "Omgcloner"):
                # Try using FindObjects if available (R20+)
                if hasattr(c4d.BaseObject, "FindObjects"):
                    cloners = c4d.BaseObject.FindObjects(doc, c4d.Omgcloner)
                    for cloner in cloners:
                        obj_id = str(cloner.GetGUID())
                        if obj_id not in found_ids:
                            all_objects.append(cloner)
                            found_ids.add(obj_id)
                            self.log(
                                f"[C4D] Found cloner using FindObjects: {cloner.GetName()}"
                            )

            # Check for other MoGraph objects if needed
            # (Add specific searches here if certain objects are still missed)

        except Exception as e:
            self.log(f"[C4D] Error in MoGraph direct search: {str(e)}")

        self.log(
            f"[C4D] Comprehensive object search complete, found {len(all_objects)} objects"
        )
        return all_objects

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
                        # Get next message from queue with timeout to avoid potential deadlocks
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
                                    print(
                                        error_msg
                                    )  # Also print to console for debugging
                            else:
                                self.AppendLog(
                                    f"[C4D] Warning: Non-callable value received: {type(msg_value)}"
                                )
                        else:
                            self.AppendLog(
                                f"[C4D] Warning: Unknown message type: {msg_type}"
                            )
                    except queue.Empty:
                        # Queue timeout - break the loop to prevent blocking
                        break
                    except Exception as e:
                        # Handle any other exceptions during message processing
                        error_msg = f"[C4D] Error processing message: {str(e)}"
                        self.AppendLog(error_msg)
                        print(error_msg)  # Also print to console for debugging
            except Exception as e:
                # Catch all exceptions to prevent Cinema 4D from crashing
                error_msg = f"[C4D] Critical error in message processing: {str(e)}"
                print(error_msg)  # Print to console as UI might be unstable
                try:
                    self.AppendLog(error_msg)
                except:
                    pass  # Ignore if we can't even log to UI

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
    c4d.plugins.RegisterCommandPlugin(
        SocketServerPlugin.PLUGIN_ID,
        SocketServerPlugin.PLUGIN_NAME,
        0,
        None,
        None,
        SocketServerPlugin(),
    )
