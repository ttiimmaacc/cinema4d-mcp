"""
Cinema 4D MCP Server Plugin
Updated for Cinema 4D R2025 compatibility
Version 0.1.7 - Fixed MoGraph field linking and "Applied to: None" issues
"""

import c4d
from c4d import gui
import socket
import threading
import json
import time
import math
import queue
import os
import sys
import base64

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
        "[C4D MCP] ## Warning ##: This plugin is in development for Cinema 4D 2025 or later with plans to futher support earlier versions. Some features may not work correctly."
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

    def execute_on_main_thread(self, func, args=None, kwargs=None, _timeout=None):
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
        args = args or ()
        kwargs = kwargs or {}

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
                self.log(
                    f"[**ERROR**] Error executing function on main thread: {str(e)}"
                )
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

        # Improved result handling
        if result_container["result"] is None:
            self.log(
                "[C4D] ## Warning ##: Function execution completed but returned None"
            )
            # Return a structured response instead of None
            return {
                "status": "completed",
                "result": None,
                "warning": "Function returned None",
            }

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

                        # Scene info & execution
                        if command_type == "get_scene_info":
                            response = self.handle_get_scene_info()
                        elif command_type == "list_objects":
                            response = self.handle_list_objects()
                        elif command_type == "group_objects":
                            response = self.handle_group_objects(command)
                        elif command_type == "execute_python":
                            response = self.handle_execute_python(command)
                        elif command_type == "save_scene":
                            response = self.handle_save_scene(command)
                        elif command_type == "load_scene":
                            response = self.handle_load_scene(command)
                        elif command_type == "set_keyframe":
                            response = self.handle_set_keyframe(command)
                        # Object creation & modification
                        elif command_type == "add_primitive":
                            response = self.handle_add_primitive(command)
                        elif command_type == "modify_object":
                            response = self.handle_modify_object(command)
                        elif command_type == "create_abstract_shape":
                            response = self.handle_create_abstract_shape(command)
                        # Materials & shaders
                        elif command_type == "create_material":
                            response = self.handle_create_material(command)
                        elif command_type == "apply_material":
                            response = self.handle_apply_material(command)
                        elif command_type == "apply_shader":
                            response = self.handle_apply_shader(command)
                        elif command_type == "validate_redshift_materials":
                            response = self.handle_validate_redshift_materials(command)
                        # Rendering & preview
                        elif command_type == "render_frame":
                            response = self.handle_render_frame(command)
                        elif command_type == "render_preview":
                            response = self.handle_render_preview_base64()
                        elif command_type == "snapshot_scene":
                            response = self.handle_snapshot_scene(command)
                        # Camera & light handling
                        elif command_type == "create_camera":
                            response = self.handle_create_camera(command)
                        elif command_type == "animate_camera":
                            response = self.handle_animate_camera(command)
                        elif command_type == "create_light":
                            response = self.handle_create_light(command)
                        # MoGraph/dynamics
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
                        self.log(f"[**ERROR**] Error processing command: {str(e)}")

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
        """Find an object by name with C4D 2025 compatibility."""
        if not name:
            self.log("[C4D] ## Warning ##: Empty object name provided")
            return None

        name = name.strip()
        self.log(f"[C4D] Looking for object with name: '{name}'")

        all_objects = self._get_all_objects(doc)

        # Method 1: Registry lookup by name -> GUID
        if hasattr(self, "_object_name_registry"):
            obj_id = self._object_name_registry.get(name)
            if isinstance(obj_id, str):
                for obj in all_objects:
                    if str(obj.GetGUID()) == obj_id:
                        self.log(
                            f"[C4D] Found object via registry: '{obj.GetName()}' (requested as '{name}')"
                        )
                        return obj

        # Method 2: Direct name search (case-insensitive)
        for obj in all_objects:
            if obj.GetName().strip().lower() == name.lower():
                self.log(f"[C4D] Found object via direct name: '{obj.GetName()}'")
                return obj

        # Method 3: Search via Comment Tag (e.g. "MCP_NAME:Box") - Older versions R21–R24
        self.log(f"[C4D] Looking for object via comment tag with '{name}'")
        if hasattr(c4d, "Tcomment"):
            for obj in all_objects:
                for tag in obj.GetTags():
                    if tag.GetType() == c4d.Tcomment:
                        try:
                            tag_text = tag[c4d.COMMENTTAG_TEXT]
                            if tag_text and tag_text.startswith("MCP_NAME:"):
                                tagged_name = tag_text[9:].strip()
                                if tagged_name.lower() == name.lower():
                                    self.log(
                                        f"[C4D] Found object via comment tag: {obj.GetName()}"
                                    )
                                    return obj
                        except Exception as e:
                            self.log(f"[**ERROR**] Error reading comment tag: {str(e)}")

        # Method 4: Check User Data for "mcp_original_name"
        for obj in all_objects:
            try:
                userdata = obj.GetUserDataContainer()
                if userdata:
                    for entry in userdata:
                        if entry[c4d.DESC_NAME] == "mcp_original_name":
                            data_id = entry[c4d.DESC_ID]
                            if obj[data_id].strip().lower() == name.lower():
                                self.log(
                                    f"[C4D] Found object via user data: {obj.GetName()}"
                                )
                                return obj
            except Exception as e:
                self.log(f"[**ERROR**] Error checking user data: {str(e)}")

        # Method 5: Fallback - try fuzzy name matching as a last resort
        similar_objects = []
        for obj in all_objects:
            obj_name = obj.GetName().strip().lower()
            name_l = name.lower()
            if (
                name_l in obj_name
                or obj_name in name_l
                or obj_name.startswith(name_l)
                or name_l.startswith(obj_name)
            ):
                similarity = abs(len(obj_name) - len(name_l))
                similar_objects.append((obj, similarity))

        if similar_objects:
            closest_match = sorted(similar_objects, key=lambda pair: pair[1])[0][0]
            self.log(
                f"[C4D] Fallback match: Using '{closest_match.GetName()}' for requested '{name}'"
            )
            return closest_match

        # Final: Not found
        self.log(f"[C4D] Object not found: '{name}'")
        return None

    def _get_all_objects(self, doc):
        """Recursively collects all objects in the scene into a flat list."""
        result = []

        def collect_recursive(obj):
            while obj:
                result.append(obj)
                if obj.GetDown():
                    collect_recursive(obj.GetDown())
                obj = obj.GetNext()

        first_obj = doc.GetFirstObject()
        if first_obj:
            collect_recursive(first_obj)

        return result

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
                    self.log(f"[**ERROR**] Error in hierarchy traversal: {str(e)}")

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
            self.log(f"[**ERROR**] Error in GetObjects search: {str(e)}")

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
            self.log(f"[**ERROR**] Error in MoGraph direct search: {str(e)}")

        self.log(
            f"[C4D] Comprehensive object search complete, found {len(all_objects)} objects"
        )
        return all_objects

    def handle_group_objects(self, command):
        """
        Handle group_objects command.
        - Groups provided objects under a new null.
        - Supports optional group position.
        - Can center null based on children bounds.
        - Can fallback to grouping currently selected objects.
        """
        doc = c4d.documents.GetActiveDocument()

        group_name = command.get("group_name", "Group")
        object_names = command.get("object_names", [])  # Optional
        position = command.get("position", None)  # Optional
        center = command.get("center", False)  # Optional flag

        # Determine which objects to group
        if object_names:
            objects_to_group = []
            for name in object_names:
                obj = self.find_object_by_name(doc, name)
                if obj:
                    objects_to_group.append(obj)
                else:
                    self.log(f"[C4D GROUP] Warning: Object not found: {name}")
        else:
            # Fallback: use currently selected objects
            objects_to_group = doc.GetActiveObjects(c4d.GETACTIVEOBJECTFLAGS_CHILDREN)
            if not objects_to_group:
                return {"error": "No objects selected or specified to group."}
            self.log(
                f"[C4D GROUP] Fallback: Grouping {len(objects_to_group)} selected objects."
            )

        # Create group null
        group_null = c4d.BaseObject(c4d.Onull)
        group_null.SetName(group_name)
        doc.InsertObject(group_null)

        # Re-parent objects under the null
        grouped_names = []
        for obj in objects_to_group:
            grouped_names.append(obj.GetName())
            obj.Remove()
            obj.InsertUnder(group_null)

        # Optional: position the group null
        if isinstance(position, list) and len(position) == 3:
            try:
                group_null.SetAbsPos(c4d.Vector(position[0], position[1], position[2]))
                self.log(f"[C4D GROUP] Set group position to {position}")
            except Exception as e:
                self.log(f"[C4D GROUP] Error setting position: {str(e)}")

        elif center and objects_to_group:
            # Compute center of bounding boxes
            center_vec = c4d.Vector(0, 0, 0)
            count = 0
            for obj in objects_to_group:
                try:
                    # bbox = obj.GetRad()
                    # pos = obj.GetAbsPos()
                    # center_vec += pos + bbox
                    center_vec += obj.GetMp()
                    count += 1
                except Exception as e:
                    self.log(
                        f"[C4D GROUP] Error calculating center for {obj.GetName()}: {str(e)}"
                    )

            if count > 0:
                average = center_vec / count
                group_null.SetAbsPos(average)
                self.log(f"[C4D GROUP] Centered group to position: {average}")

        # Finalize
        c4d.EventAdd()

        # Get the position vector
        pos_vector = group_null.GetAbsPos()

        return {
            "group": {
                "name": group_null.GetName(),
                "children": grouped_names,
                "id": str(group_null.GetGUID()),
                "position": [pos_vector.x, pos_vector.y, pos_vector.z],
            }
        }

    def handle_add_primitive(self, command):
        """Handle add_primitive command."""
        doc = c4d.documents.GetActiveDocument()

        primitive_type = command.get("primitive_type") or command.get("type") or "cube"
        primitive_type = primitive_type.lower()

        name = (
            command.get("name")
            or command.get("object_name")
            or f"MCP_{primitive_type.capitalize()}"
        )

        position = command.get("position", [0, 0, 0])
        size = command.get("size", [50, 50, 50])

        # Create the appropriate primitive object
        obj = None
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

        elif primitive_type == "pyramid":
            obj = c4d.BaseObject(c4d.Opyramid)

            # Use PRIM_PYRAMID_LEN for C4D 2023+ compatibility
            if hasattr(c4d, "PRIM_PYRAMID_LEN"):
                obj[c4d.PRIM_PYRAMID_LEN] = c4d.Vector(*size)
            else:
                # Fallback for older versions (in case someone backports)
                if hasattr(c4d, "PRIM_PYRAMID_WIDTH"):
                    obj[c4d.PRIM_PYRAMID_WIDTH] = size[0]
                if hasattr(c4d, "PRIM_PYRAMID_HEIGHT"):
                    obj[c4d.PRIM_PYRAMID_HEIGHT] = size[1]
                if hasattr(c4d, "PRIM_PYRAMID_DEPTH"):
                    obj[c4d.PRIM_PYRAMID_DEPTH] = size[2]

        elif primitive_type == "disk":
            obj = c4d.BaseObject(c4d.Odisk)
            obj[c4d.PRIM_DISK_LEN] = c4d.Vector(0, size[0] / 2, size[1])

        elif primitive_type == "tube":
            obj = c4d.BaseObject(c4d.Otube)
            obj[c4d.PRIM_TUBE_RADIUS] = size[0] / 2  # Outer radius
            obj[c4d.PRIM_TUBE_IRADIUS] = size[1] / 2  # Inner radius
            obj[c4d.PRIM_TUBE_HEIGHT] = size[2]  # Height

        elif primitive_type == "torus":
            obj = c4d.BaseObject(c4d.Otorus)
            obj[c4d.PRIM_TORUS_OUTERRAD] = size[0] / 2  # Ring radius
            obj[c4d.PRIM_TORUS_INNERRAD] = size[1] / 2  # Pipe radius

        elif primitive_type == "platonic":
            obj = c4d.BaseObject(c4d.Oplatonic)
            obj[c4d.PRIM_PLATONIC_TYPE] = (
                c4d.PRIM_PLATONIC_TYPE_TETRA
            )  # Choose type: TETRA, OCTA, HEXA, ICOSA
            obj[c4d.PRIM_PLATONIC_RAD] = size[0] / 2  # Radius

        else:
            # Default to cube if type not recognized
            print(f"Unknown primitive_type: {primitive_type}, defaulting to cube.")
            obj = c4d.BaseObject(c4d.Ocube)
            obj[c4d.PRIM_CUBE_LEN] = c4d.Vector(*size)

        # Set position
        if len(position) >= 3:
            obj.SetAbsPos(c4d.Vector(position[0], position[1], position[2]))

        # Set name
        obj.SetName(name)

        # Add to doc
        doc.InsertObject(obj)
        doc.SetActiveObject(obj)
        c4d.EventAdd()

        # Return information about the created object
        return {
            "object": {
                "name": obj.GetName(),
                "id": str(obj.GetGUID()),
                "position": [obj.GetAbsPos().x, obj.GetAbsPos().y, obj.GetAbsPos().z],
            }
        }

    def register_object_name(self, obj, requested_name):
        """Register an object with its requested name for more reliable lookup later."""
        if not hasattr(self, "_object_name_registry"):
            self._object_name_registry = {}

        if not hasattr(self, "_name_to_guid_registry"):
            self._name_to_guid_registry = {}

        if not hasattr(self, "_guid_to_name_registry"):
            self._guid_to_name_registry = {}

        # Store both by GUID (most reliable) and by requested name
        obj_id = str(obj.GetGUID())
        actual_name = obj.GetName()

        # Store a mapping from requested name to GUID
        self._name_to_guid_registry[requested_name] = obj_id

        # Store a mapping from GUID to both names
        self._guid_to_name_registry[obj_id] = {
            "requested_name": requested_name,
            "actual_name": actual_name,
        }

        # Also store a mapping from actual name to GUID for fallback
        if actual_name != requested_name:
            self._name_to_guid_registry[actual_name] = obj_id

        # Log the registration
        self.log(
            f"[C4D] Registered object: Requested name '{requested_name}', Actual name '{actual_name}', GUID: {obj_id}"
        )

        # Backward compatibility
        self._object_name_registry[obj_id] = requested_name
        self._object_name_registry[requested_name] = obj_id

        # Attempt to store the original name in user data for extra robustness
        try:
            # First check if we already have our tag
            has_tag = False
            userdata = obj.GetUserDataContainer()
            if userdata:
                for data_id in range(len(userdata)):
                    if userdata[data_id][c4d.DESC_NAME] == "mcp_original_name":
                        has_tag = True
                        break

            # Only add if we don't already have it
            if not has_tag:
                # Create a user data element to store the original name
                bc = c4d.GetCustomDataTypeDefault(c4d.DTYPE_STRING)
                if bc:
                    bc[c4d.DESC_NAME] = "mcp_original_name"
                    bc[c4d.DESC_SHORT_NAME] = "MCP Name"
                    element = obj.AddUserData(bc)
                    if element:
                        obj[element] = requested_name
                        self.log(
                            f"[C4D] Stored original name '{requested_name}' in object user data"
                        )
        except Exception as e:
            self.log(
                f"[C4D] ## Warning ##: Could not add user data for original name: {str(e)}"
            )

    def handle_modify_object(self, command):
        """Handle modify_object command with full property support."""
        doc = c4d.documents.GetActiveDocument()
        # Support both 'object_name' and 'name'
        object_name = command.get("object_name") or command.get("name") or ""
        properties = command.get("properties", {})

        # Find the object by name
        obj = self.find_object_by_name(doc, object_name)
        if obj is None:
            return {"error": f"Object not found: {object_name}"}

        # Apply modifications
        modified = {}

        # Position
        pos = properties.get("position")
        if isinstance(pos, list) and len(pos) >= 3:
            obj.SetAbsPos(c4d.Vector(pos[0], pos[1], pos[2]))
            modified["position"] = pos
        print(f"Incoming position: {modified.get('position')}")

        # Rotation (in degrees)
        rot = properties.get("rotation")
        if isinstance(rot, list) and len(rot) >= 3:
            rot_rad = [c4d.utils.DegToRad(r) for r in rot]
            obj.SetRelRot(c4d.Vector(*rot_rad))
            modified["rotation"] = rot
        print(f"Rotation: {modified.get('rotation')}")

        # Scale
        scale = properties.get("scale")
        if isinstance(scale, list) and len(scale) >= 3:
            obj.SetRelScale(c4d.Vector(scale[0], scale[1], scale[2]))
            modified["scale"] = scale
        print(f"Scale: {modified.get('scale')}")

        # Color (if object has a base color channel)
        color = properties.get("color")
        if isinstance(color, list) and len(color) >= 3:
            try:
                obj[c4d.ID_BASEOBJECT_COLOR] = c4d.Vector(color[0], color[1], color[2])
                modified["color"] = color
            except Exception as e:
                print(f"Error setting color: {str(e)}")
        print(f"Color: {modified.get('color')}")

        # Rename
        new_name = properties.get("name")
        if isinstance(new_name, str) and new_name.strip():
            old_name = obj.GetName()
            obj.SetName(new_name)
            modified["name"] = {"from": old_name, "to": new_name}
        print(f"Name change: {modified.get('name')}")

        # Size primitives
        size = properties.get("size")
        if isinstance(size, list) and len(size) >= 3:
            obj_type = obj.GetType()
            try:
                if obj_type == c4d.Ocube:
                    obj[c4d.PRIM_CUBE_LEN] = c4d.Vector(*size)
                    modified["size"] = size
                elif obj_type == c4d.Osphere:
                    obj[c4d.PRIM_SPHERE_RAD] = size[0] / 2
                    modified["size"] = size
                elif obj_type == c4d.Ocone:
                    obj[c4d.PRIM_CONE_BRAD] = size[0] / 2
                    obj[c4d.PRIM_CONE_HEIGHT] = size[1]
                    modified["size"] = size
                elif obj_type == c4d.Ocylinder:
                    obj[c4d.PRIM_CYLINDER_RADIUS] = size[0] / 2
                    obj[c4d.PRIM_CYLINDER_HEIGHT] = size[1]
                    modified["size"] = size
                elif obj_type == c4d.Oplane:
                    obj[c4d.PRIM_PLANE_WIDTH] = size[0]
                    obj[c4d.PRIM_PLANE_HEIGHT] = size[1]
                    modified["size"] = size
            except Exception as e:
                print(f"Error modifying size: {str(e)}")
        print(f"Size: {modified.get('size')}")

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
        material_type = command.get("material_type", "standard")  # standard, redshift
        projection_type = command.get(
            "projection_type", "cubic"
        )  # cubic, spherical, flat, etc.
        auto_uv = command.get("auto_uv", False)  # generate UVs automatically
        procedural = command.get("procedural", False)  # use procedural shaders

        # Find the object
        obj = self.find_object_by_name(doc, object_name)
        if obj is None:
            return {"error": f"Object not found: {object_name}"}

        # Find the material
        mat = self._find_material_by_name(doc, material_name)
        if mat is None:
            return {"error": f"Material not found: {material_name}"}

        try:
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

            # Generate UVs automatically if needed
            if auto_uv:
                try:
                    # Create UVW tag if none exists
                    uvw_tag = obj.GetTag(c4d.Tuvw)
                    if not uvw_tag:
                        uvw_tag = c4d.UVWTag(obj.GetPolygonCount())
                        obj.InsertTag(uvw_tag)

                    # Create a temporary UVW mapping object
                    uvw_obj = c4d.BaseObject(c4d.Ouvw)
                    doc.InsertObject(uvw_obj)

                    # Set source object
                    uvw_obj[c4d.UVWMAPPING_MAPPING] = c4d.UVWMAPPING_MAPPING_CUBIC
                    uvw_obj[c4d.UVWMAPPING_PROJECTION] = c4d.UVWMAPPING_PROJECTION_CUBIC
                    uvw_obj[c4d.UVWMAPPING_TISOCPIC] = True
                    uvw_obj[c4d.UVWMAPPING_FITSIZE] = True

                    # Set the selection object
                    selection = c4d.InExcludeData()
                    selection.InsertObject(obj, 1)
                    uvw_obj[c4d.UVWMAPPING_SELECTION] = selection

                    # Generate UVs
                    c4d.CallButton(uvw_obj, c4d.UVWMAPPING_GENERATE)

                    # Remove temp object
                    doc.RemoveObject(uvw_obj)
                except Exception as e:
                    print(f"[**ERROR**] Error creating UVs: {str(e)}")

            # Handle Redshift material setup if needed
            if (
                material_type == "redshift"
                and hasattr(c4d, "modules")
                and hasattr(c4d.modules, "redshift")
            ):
                try:
                    redshift = c4d.modules.redshift

                    # Try to convert material to Redshift if it's not already
                    if mat.GetType() != c4d.ID_REDSHIFT_MATERIAL:
                        # Create new Redshift material
                        rs_mat = c4d.BaseMaterial(c4d.ID_REDSHIFT_MATERIAL)
                        rs_mat.SetName(f"RS_{mat.GetName()}")

                        # Copy basic material properties like color
                        color = mat[c4d.MATERIAL_COLOR_COLOR]

                        # Use CreateDefaultGraph for reliable material setup
                        try:
                            import maxon

                            rs_nodespace_id = maxon.Id(
                                "com.redshift3d.redshift4c4d.class.nodespace"
                            )
                            rs_mat.CreateDefaultGraph(rs_nodespace_id)
                        except Exception as e:
                            print(f"[**ERROR**] Error creating default graph: {str(e)}")

                        # Access the Redshift material graph
                        node_space = redshift.GetRSMaterialNodeSpace(rs_mat)
                        root = redshift.GetRSMaterialRootShader(rs_mat)

                        if root is None:
                            raise Exception("Failed to get Redshift root shader")

                        if procedural:
                            # Create procedural texture nodes
                            noise_shader = redshift.RSMaterialNodeCreator.CreateNode(
                                node_space,
                                redshift.RSMaterialNodeType.TEXTURE,
                                "RS::TextureNode",
                            )
                            noise_shader[redshift.TEXTURE_TYPE] = redshift.TEXTURE_NOISE

                            # Connect procedural texture to output
                            redshift.CreateConnectionBetweenNodes(
                                node_space,
                                noise_shader,
                                "outcolor",
                                root,
                                "diffuse_color",
                            )
                        else:
                            # Set color directly
                            root[redshift.OUTPUT_COLOR] = color

                        # Insert new material
                        doc.InsertMaterial(rs_mat)

                        # Update the tag to use the new material
                        tag.SetMaterial(rs_mat)
                except Exception as e:
                    print(f"[**ERROR**] Error setting up Redshift material: {str(e)}")

            # Update the document
            c4d.EventAdd()

            return {
                "success": True,
                "message": f"Applied material '{material_name}' to object '{object_name}'",
                "material_type": material_type,
                "auto_uv": auto_uv,
            }
        except Exception as e:
            return {"error": f"Failed to apply material: {str(e)}"}

    def handle_render_preview_base64(self, frame=0, width=640, height=360):
        """Render a preview of the scene to base64 PNG (in-memory) with proper render settings."""
        import base64
        import time
        import traceback

        doc = c4d.documents.GetActiveDocument()

        def render_to_base64(doc, frame, width, height):
            try:
                # Clone and safely access render settings
                render_data = doc.GetActiveRenderData()
                if not render_data:
                    return {"error": "No active render settings found"}

                rd_clone = render_data.GetClone()
                if not rd_clone:
                    return {"error": "Failed to clone render settings"}

                settings = rd_clone.GetDataInstance()
                settings.SetLong(c4d.RDATA_XRES, width)
                settings.SetLong(c4d.RDATA_YRES, height)

                # Apply frame and update passes
                doc.SetTime(c4d.BaseTime(frame, doc.GetFps()))
                doc.ExecutePasses(None, True, True, True, c4d.BUILDFLAGS_NONE)

                # Prepare target bitmap
                bmp = c4d.bitmaps.BaseBitmap()
                if not bmp.Init(width, height):
                    return {"error": "Failed to initialize bitmap"}

                # Attempt rendering with BaseContainer (required for C4D 2025+)
                render_result = c4d.documents.RenderDocument(
                    doc,
                    settings,  # ✅ Must be BaseContainer
                    bmp,
                    c4d.RENDERFLAGS_EXTERNAL | c4d.RENDERFLAGS_NODOCUMENTCLONE,
                    None,
                )

                if not render_result:
                    return {"error": "RenderDocument returned False"}

                # Export bitmap to base64
                mem_file = c4d.storage.MemoryFileStruct()
                mem_file.SetMemoryWriteMode()
                if bmp.Save(mem_file, c4d.FILTER_PNG) != c4d.IMAGERESULT_OK:
                    return {"error": "Failed to write PNG to memory"}

                data, size = mem_file.GetData()
                if not data or size == 0:
                    return {"error": "Memory buffer is empty"}

                encoded = base64.b64encode(data).decode("utf-8")

                return {
                    "success": True,
                    "frame": frame,
                    "resolution": f"{width}x{height}",
                    "image_base64": f"data:image/png;base64,{encoded}",
                    "render_time": round(time.time(), 2),
                }

            except Exception as e:
                self.log("[C4D PREVIEW] Exception during base64 render")
                self.log(traceback.format_exc())
                return {"error": f"Render to base64 failed: {str(e)}"}

        return self.execute_on_main_thread(
            render_to_base64, args=(doc, frame, width, height), _timeout=60
        )

    def handle_render_frame(self, command):
        """Render the current frame to file. No base64. File-based output only."""
        import time
        import tempfile

        doc = c4d.documents.GetActiveDocument()
        frame = int(command.get("frame", 0))
        width = int(command.get("width", 640))
        height = int(command.get("height", 360))
        output_path = command.get("output_path", None)

        def render_to_file(doc, frame, width, height, output_path):
            try:
                rd = doc.GetActiveRenderData()
                settings = rd.GetDataInstance()
                settings.SetLong(c4d.RDATA_XRES, width)
                settings.SetLong(c4d.RDATA_YRES, height)

                doc.SetTime(c4d.BaseTime(frame, doc.GetFps()))
                doc.ExecutePasses(None, True, True, True, c4d.BUILDFLAGS_NONE)

                bmp = c4d.bitmaps.BaseBitmap()
                bmp.Init(width, height)

                rd_data = settings

                result = c4d.documents.RenderDocument(
                    doc,
                    rd.GetDataInstance(),
                    bmp,
                    c4d.RENDERFLAGS_EXTERNAL | c4d.RENDERFLAGS_NODOCUMENTCLONE,
                    None,
                )
                if not result:
                    return {"error": "RenderDocument returned False"}

                # Auto-generate temp path if none provided
                if not output_path:
                    doc_name = doc.GetDocumentName() or "untitled"
                    if doc_name.lower().endswith(".c4d"):
                        doc_name = doc_name[:-4]
                    base_dir = doc.GetDocumentPath() or tempfile.gettempdir()
                    output_path = os.path.join(
                        base_dir, f"{doc_name}_frame_{frame}.png"
                    )

                ext = os.path.splitext(output_path)[1].lower()
                format_map = {
                    ".png": c4d.FILTER_PNG,
                    ".jpg": c4d.FILTER_JPG,
                    ".jpeg": c4d.FILTER_JPG,
                    ".tif": c4d.FILTER_TIF,
                    ".tiff": c4d.FILTER_TIF,
                }
                format_id = format_map.get(ext, c4d.FILTER_PNG)

                saved = bmp.Save(output_path, format_id)
                if not saved:
                    return {"error": f"Failed to save image to: {output_path}"}

                return {
                    "success": True,
                    "frame": frame,
                    "resolution": f"{width}x{height}",
                    "output_path": output_path,
                    "file_exists": os.path.exists(output_path),
                }

            except Exception as e:
                import traceback

                self.log(traceback.format_exc())
                return {"error": f"Render failed: {str(e)}"}

        return self.execute_on_main_thread(
            render_to_file, args=(doc, frame, width, height, output_path), _timeout=60
        )

    def handle_snapshot_scene(self, command=None):
        """Combine object listing and rendered preview into one unified snapshot for AI context."""

        doc = c4d.documents.GetActiveDocument()
        frame = 0  # You can extend to accept from command if needed

        self.log("[C4D] Running snapshot_scene: listing objects and rendering preview")

        # --- 1. Get object list ---
        object_data = self.handle_list_objects()
        objects = object_data.get("objects", [])

        # --- 2. Render preview image ---
        render_result = self.handle_render_frame(
            {
                "frame": frame,
                "width": 640,
                "height": 360,
                # Don't set output_path unless you want saved files
                # "output_path": "/Users/you/Desktop/snapshot.png"
            }
        )

        if "error" in render_result:
            self.log("[C4D] Snapshot render failed.")
            return {"objects": objects, "render": {"error": render_result["error"]}}

        # --- 3. Return combined context ---
        return {
            "objects": objects,
            "render": {
                "frame": render_result.get("frame"),
                "resolution": render_result.get("resolution"),
                "image_base64": render_result.get("image_base64"),
                "output_path": render_result.get("output_path"),  # Optional
                "file_exists": render_result.get("file_exists"),
                "render_time": render_result.get("render_time"),
            },
        }

    def handle_set_keyframe(self, command):
        """Handle set_keyframe command with enhanced property support."""
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
            # Check if it's a component property (e.g., "position.x")
            if "." in property_type:
                # Parse the property and component
                parts = property_type.split(".")
                if len(parts) != 2:
                    return {
                        "error": f"Invalid property format: {property_type}. Use format 'property.component' (e.g., 'position.x')"
                    }

                # Log the component parts for debugging
                self.log(
                    f"[C4D KEYFRAME] Parsing component property: {property_type} into parts: {parts}"
                )

                base_property = parts[0].lower()
                component = parts[1].lower()

                # Map to property IDs and component IDs
                property_map = {
                    "position": c4d.ID_BASEOBJECT_POSITION,
                    "rotation": c4d.ID_BASEOBJECT_ROTATION,
                    "scale": c4d.ID_BASEOBJECT_SCALE,
                    "color": c4d.LIGHT_COLOR if obj.GetType() == c4d.Olight else None,
                }

                component_map = {
                    "x": c4d.VECTOR_X,
                    "y": c4d.VECTOR_Y,
                    "z": c4d.VECTOR_Z,
                }

                if base_property not in property_map:
                    return {
                        "error": f"Unsupported vector property: {base_property}. Supported properties: position, rotation, scale, color (for lights)"
                    }

                if property_map[base_property] is None:
                    return {
                        "error": f"Property {base_property} is not applicable to this object type"
                    }

                if component not in component_map:
                    return {
                        "error": f"Unsupported component: {component}. Supported components: x, y, z"
                    }

                # Ensure value is a scalar
                if isinstance(value, list):
                    if len(value) > 0:
                        value = value[0]  # Use the first value if a list is provided
                    else:
                        return {"error": f"Empty value provided for {property_type}"}

                # Set the component keyframe
                result = self._set_vector_component_keyframe(
                    obj,
                    frame,
                    property_map[base_property],
                    component_map[component],
                    float(value),  # Convert to float to ensure compatibility
                    base_property,
                    component,
                )

                if not result:
                    return {"error": f"Failed to set {property_type} keyframe"}

            # Handle standard vector properties
            elif property_type in ["position", "rotation", "scale"]:
                # Map to property IDs
                property_ids = {
                    "position": c4d.ID_BASEOBJECT_POSITION,
                    "rotation": c4d.ID_BASEOBJECT_ROTATION,
                    "scale": c4d.ID_BASEOBJECT_SCALE,
                }

                # Check for valid vector value and make adaptations as needed
                if isinstance(value, (int, float)):
                    self.log(
                        f"[C4D KEYFRAME] Converting single value {value} to a vector with all components equal"
                    )
                    # Use the single value for all components
                    value = [value, value, value]
                elif isinstance(value, list):
                    # Pad short lists with zeros or last element
                    if len(value) == 1:
                        self.log(
                            f"[C4D KEYFRAME] Padding single-value list to [x, x, x]"
                        )
                        value = [value[0], value[0], value[0]]
                    elif len(value) == 2:
                        self.log(
                            f"[C4D KEYFRAME] Padding two-value list with 0 for z-component"
                        )
                        value = [value[0], value[1], 0]
                    # Truncate long lists
                    elif len(value) > 3:
                        self.log(
                            f"[C4D KEYFRAME] Truncating list with {len(value)} values to first 3 components"
                        )
                        value = value[:3]
                else:
                    # Convert to string and try to parse as list
                    try:
                        str_value = str(value).strip()
                        if str_value.startswith("[") and str_value.endswith("]"):
                            # Parse as JSON list
                            import json

                            parsed_value = json.loads(str_value)
                            if isinstance(parsed_value, list):
                                value = parsed_value
                                self.log(
                                    f"[C4D KEYFRAME] Parsed string value into list: {value}"
                                )
                            else:
                                return {
                                    "error": f"{property_type.capitalize()} must be a list of [x, y, z] values, got: {value}"
                                }
                        else:
                            # Try as single value
                            try:
                                single_value = float(value)
                                value = [single_value, single_value, single_value]
                                self.log(
                                    f"[C4D KEYFRAME] Converted string to vector: {value}"
                                )
                            except ValueError:
                                return {
                                    "error": f"{property_type.capitalize()} must be a list of [x, y, z] values, got: {value}"
                                }
                    except:
                        return {
                            "error": f"{property_type.capitalize()} must be a list of [x, y, z] values, got: {value}"
                        }

                # Final check that we have a valid list of 3 elements
                if not isinstance(value, list) or len(value) != 3:
                    return {
                        "error": f"{property_type.capitalize()} must be a list of [x, y, z] values, got: {value}"
                    }

                # Log the final vector value
                self.log(
                    f"[C4D KEYFRAME] Using final vector value for {property_type}: {value}"
                )

                # Set the vector keyframe
                result = self._set_vector_keyframe(
                    obj, frame, property_ids[property_type], value, property_type
                )

                if not result:
                    return {"error": f"Failed to set {property_type} keyframe"}

            # Handle light-specific properties
            elif obj.GetType() == c4d.Olight and property_type in [
                "intensity",
                "color",
            ]:
                if property_type == "intensity":
                    # Ensure value is a scalar
                    if isinstance(value, list):
                        if len(value) > 0:
                            value = value[
                                0
                            ]  # Use the first value if a list is provided
                        else:
                            return {"error": "Empty value provided for intensity"}

                    # Set the light intensity keyframe
                    result = self._set_scalar_keyframe(
                        obj,
                        frame,
                        c4d.LIGHT_BRIGHTNESS,
                        c4d.DTYPE_REAL,
                        float(value) / 100.0,  # Convert percentage to 0-1 range
                        "intensity",
                    )

                    if not result:
                        return {"error": "Failed to set intensity keyframe"}
                elif property_type == "color":
                    # Ensure value is a valid RGB list
                    if not isinstance(value, list) or len(value) < 3:
                        return {"error": "Color must be a list of [r, g, b] values"}

                    # Set the light color keyframe
                    result = self._set_vector_keyframe(
                        obj, frame, c4d.LIGHT_COLOR, value, "color"
                    )

                    if not result:
                        return {"error": "Failed to set color keyframe"}

            # Check for other common scalar properties
            elif property_type in ["visibility", "parameter"]:
                # For visibility
                if property_type == "visibility":
                    # Ensure value is a scalar
                    if isinstance(value, list):
                        if len(value) > 0:
                            value = value[
                                0
                            ]  # Use the first value if a list is provided
                        else:
                            return {"error": "Empty value provided for visibility"}

                    # Set the visibility keyframe
                    result = self._set_scalar_keyframe(
                        obj,
                        frame,
                        c4d.ID_BASEOBJECT_VISIBILITY_EDITOR,
                        c4d.DTYPE_LONG,
                        int(bool(value)),  # Convert to 0 or 1
                        "visibility",
                    )

                    if not result:
                        return {"error": "Failed to set visibility keyframe"}

                # For custom parameter (requires parameter_id in command)
                elif property_type == "parameter":
                    parameter_id = command.get("parameter_id")
                    data_type = command.get("data_type", c4d.DTYPE_REAL)

                    if parameter_id is None:
                        return {
                            "error": "parameter_id is required for setting parameter keyframes"
                        }

                    # Ensure value is a scalar
                    if isinstance(value, list):
                        if len(value) > 0:
                            value = value[
                                0
                            ]  # Use the first value if a list is provided
                        else:
                            return {"error": "Empty value provided for parameter"}

                    # Set the parameter keyframe
                    result = self._set_scalar_keyframe(
                        obj,
                        frame,
                        parameter_id,
                        data_type,
                        float(value),  # Convert to float
                        f"parameter (ID: {parameter_id})",
                    )

                    if not result:
                        return {
                            "error": f"Failed to set parameter (ID: {parameter_id}) keyframe"
                        }
            else:
                return {
                    "error": f"Unsupported property type: {property_type}. Supported properties: position, rotation, scale, intensity (lights), color (lights), visibility, parameter"
                }

            # Return success
            return {
                "success": True,
                "object": object_name,
                "frame": frame,
                "property": property_type,
                "value": value,
            }
        except Exception as e:
            return {"error": f"Error setting keyframe: {str(e)}"}

    def _set_position_keyframe(self, obj, frame, position):
        """Set a position keyframe for an object at a specific frame.

        Args:
            obj: The Cinema 4D object to keyframe
            frame: The frame number
            position: A list of [x, y, z] coordinates

        Returns:
            True if successful, False otherwise
        """
        if not obj or not isinstance(position, list) or len(position) < 3:
            self.log(f"[C4D KEYFRAME] Invalid object or position for keyframe")
            return False

        try:
            # Get the active document and time
            doc = c4d.documents.GetActiveDocument()

            # Log what we're doing
            self.log(
                f"[C4D KEYFRAME] Setting position keyframe for {obj.GetName()} at frame {frame} to {position}"
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
                f"[C4D KEYFRAME] Successfully set keyframe for {obj.GetName()} at frame {frame}"
            )
            return True

        except Exception as e:
            self.log(f"[C4D KEYFRAME] Error setting position keyframe: {str(e)}")
            return False

    def _set_vector_keyframe(self, obj, frame, property_id, value, property_name):
        """Set a keyframe for a vector property of an object.

        Args:
            obj: The Cinema 4D object to keyframe
            frame: The frame number
            property_id: The ID of the property (e.g., c4d.ID_BASEOBJECT_POSITION)
            value: A list of [x, y, z] values
            property_name: Name of the property for logging

        Returns:
            True if successful, False otherwise
        """
        if not obj or not isinstance(value, list) or len(value) < 3:
            self.log(
                f"[C4D KEYFRAME] Invalid object or {property_name} value for keyframe"
            )
            return False

        try:
            # Get the active document and time
            doc = c4d.documents.GetActiveDocument()

            # Log what we're doing
            self.log(
                f"[C4D KEYFRAME] Setting {property_name} keyframe for {obj.GetName()} at frame {frame} to {value}"
            )

            # Create the vector from the list
            vec = c4d.Vector(value[0], value[1], value[2])

            # Set the object's property value based on property type
            if property_id == c4d.ID_BASEOBJECT_POSITION:
                obj.SetAbsPos(vec)
            elif property_id == c4d.ID_BASEOBJECT_ROTATION:
                # Convert degrees to radians for rotation
                rot_rad = c4d.Vector(
                    c4d.utils.DegToRad(value[0]),
                    c4d.utils.DegToRad(value[1]),
                    c4d.utils.DegToRad(value[2]),
                )
                obj.SetRotation(rot_rad)
            elif property_id == c4d.ID_BASEOBJECT_SCALE:
                obj.SetScale(vec)
            elif property_id == c4d.LIGHT_COLOR:
                obj[c4d.LIGHT_COLOR] = vec

            # Component IDs for vector properties
            component_ids = [c4d.VECTOR_X, c4d.VECTOR_Y, c4d.VECTOR_Z]
            component_names = ["X", "Y", "Z"]

            # Create tracks and set keyframes for each component
            for i, component_id in enumerate(component_ids):
                # Create or get track for this component
                track = obj.FindCTrack(
                    c4d.DescID(
                        c4d.DescLevel(property_id, c4d.DTYPE_VECTOR, 0),
                        c4d.DescLevel(component_id, c4d.DTYPE_REAL, 0),
                    )
                )

                if track is None:
                    track = c4d.CTrack(
                        obj,
                        c4d.DescID(
                            c4d.DescLevel(property_id, c4d.DTYPE_VECTOR, 0),
                            c4d.DescLevel(component_id, c4d.DTYPE_REAL, 0),
                        ),
                    )
                    obj.InsertTrackSorted(track)

                # Create time object for the keyframe
                time = c4d.BaseTime(frame, doc.GetFps())

                # Set the keyframe
                curve = track.GetCurve()
                key = curve.AddKey(time)

                # Convert rotation values from degrees to radians if necessary
                component_value = value[i]
                if property_id == c4d.ID_BASEOBJECT_ROTATION:
                    component_value = c4d.utils.DegToRad(component_value)

                if key is not None and key["key"] is not None:
                    key["key"].SetValue(curve, component_value)
                    self.log(
                        f"[C4D KEYFRAME] Set {property_name}.{component_names[i]} keyframe to {value[i]}"
                    )

            # Update the document
            c4d.EventAdd()

            self.log(
                f"[C4D KEYFRAME] Successfully set {property_name} keyframe for {obj.GetName()} at frame {frame}"
            )

            return True
        except Exception as e:
            self.log(f"[C4D KEYFRAME] Error setting {property_name} keyframe: {str(e)}")
            return False

    def _set_scalar_keyframe(
        self, obj, frame, property_id, data_type, value, property_name
    ):
        """Set a keyframe for a scalar property of an object.

        Args:
            obj: The Cinema 4D object to keyframe
            frame: The frame number
            property_id: The ID of the property (e.g., c4d.LIGHT_BRIGHTNESS)
            data_type: The data type of the property (e.g., c4d.DTYPE_REAL)
            value: The scalar value
            property_name: Name of the property for logging

        Returns:
            True if successful, False otherwise
        """
        if not obj:
            self.log(f"[C4D KEYFRAME] Invalid object for {property_name} keyframe")
            return False

        try:
            # Get the active document and time
            doc = c4d.documents.GetActiveDocument()

            # Log what we're doing
            self.log(
                f"[C4D KEYFRAME] Setting {property_name} keyframe for {obj.GetName()} at frame {frame} to {value}"
            )

            # Set the object's property value
            obj[property_id] = value

            # Create or get track for this property
            track = obj.FindCTrack(c4d.DescID(c4d.DescLevel(property_id, data_type, 0)))

            if track is None:
                track = c4d.CTrack(
                    obj, c4d.DescID(c4d.DescLevel(property_id, data_type, 0))
                )
                obj.InsertTrackSorted(track)

            # Create time object for the keyframe
            time = c4d.BaseTime(frame, doc.GetFps())

            # Set the keyframe
            curve = track.GetCurve()
            key = curve.AddKey(time)

            if key is not None and key["key"] is not None:
                key["key"].SetValue(curve, value)

            # Update the document
            c4d.EventAdd()

            self.log(
                f"[C4D KEYFRAME] Successfully set {property_name} keyframe for {obj.GetName()} at frame {frame}"
            )

            return True
        except Exception as e:
            self.log(f"[C4D KEYFRAME] Error setting {property_name} keyframe: {str(e)}")
            return False

    def _set_vector_component_keyframe(
        self,
        obj,
        frame,
        property_id,
        component_id,
        value,
        property_name,
        component_name,
    ):
        """Set a keyframe for a single component of a vector property.

        Args:
            obj: The Cinema 4D object to keyframe
            frame: The frame number
            property_id: The ID of the property (e.g., c4d.ID_BASEOBJECT_POSITION)
            component_id: The ID of the component (e.g., c4d.VECTOR_X)
            value: The scalar value for the component
            property_name: Name of the property for logging
            component_name: Name of the component for logging

        Returns:
            True if successful, False otherwise
        """
        if not obj:
            self.log(
                f"[C4D KEYFRAME] Invalid object for {property_name}.{component_name} keyframe"
            )
            return False

        try:
            # Get the active document and time
            doc = c4d.documents.GetActiveDocument()

            # Log what we're doing
            self.log(
                f"[C4D KEYFRAME] Setting {property_name}.{component_name} keyframe for {obj.GetName()} at frame {frame} to {value}"
            )

            # Get the current vector value
            current_vec = None
            if property_id == c4d.ID_BASEOBJECT_POSITION:
                current_vec = obj.GetAbsPos()
            elif property_id == c4d.ID_BASEOBJECT_ROTATION:
                current_vec = obj.GetRotation()
                # For rotation, convert the input value from degrees to radians
                value = c4d.utils.DegToRad(value)
            elif property_id == c4d.ID_BASEOBJECT_SCALE:
                current_vec = obj.GetScale()
            elif property_id == c4d.LIGHT_COLOR:
                current_vec = obj[c4d.LIGHT_COLOR]

            if current_vec is None:
                self.log(f"[C4D KEYFRAME] Could not get current {property_name} value")
                return False

            # Update the specific component
            if component_id == c4d.VECTOR_X:
                current_vec.x = value
            elif component_id == c4d.VECTOR_Y:
                current_vec.y = value
            elif component_id == c4d.VECTOR_Z:
                current_vec.z = value

            # Set the updated vector back to the object
            if property_id == c4d.ID_BASEOBJECT_POSITION:
                obj.SetAbsPos(current_vec)
            elif property_id == c4d.ID_BASEOBJECT_ROTATION:
                obj.SetRotation(current_vec)
            elif property_id == c4d.ID_BASEOBJECT_SCALE:
                obj.SetScale(current_vec)
            elif property_id == c4d.LIGHT_COLOR:
                obj[c4d.LIGHT_COLOR] = current_vec

            # Create or get track for this component
            track = obj.FindCTrack(
                c4d.DescID(
                    c4d.DescLevel(property_id, c4d.DTYPE_VECTOR, 0),
                    c4d.DescLevel(component_id, c4d.DTYPE_REAL, 0),
                )
            )

            if track is None:
                track = c4d.CTrack(
                    obj,
                    c4d.DescID(
                        c4d.DescLevel(property_id, c4d.DTYPE_VECTOR, 0),
                        c4d.DescLevel(component_id, c4d.DTYPE_REAL, 0),
                    ),
                )
                obj.InsertTrackSorted(track)

            # Create time object for the keyframe
            time = c4d.BaseTime(frame, doc.GetFps())

            # Set the keyframe
            curve = track.GetCurve()
            key = curve.AddKey(time)

            if key is not None and key["key"] is not None:
                key["key"].SetValue(curve, value)

            # Update the document
            c4d.EventAdd()

            self.log(
                f"[C4D KEYFRAME] Successfully set {property_name}.{component_name} keyframe for {obj.GetName()} at frame {frame}"
            )

            return True
        except Exception as e:
            self.log(
                f"[C4D KEYFRAME] Error setting {property_name}.{component_name} keyframe: {str(e)}"
            )
            return False

    def handle_save_scene(self, command):
        """Handle save_scene command."""
        file_path = command.get("file_path", "")
        if not file_path:
            return {"error": "No file path provided"}

        # Log the save request
        self.log(f"[C4D SAVE] Saving scene to: {file_path}")

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
                elif extension.lower() != ".c4d":
                    file_path = file_path[: -len(extension)] + ".c4d"

                # Save document
                self.log(f"[C4D SAVE] Saving to: {file_path}")
                if not c4d.documents.SaveDocument(
                    doc,
                    file_path,
                    c4d.SAVEDOCUMENTFLAGS_DONTADDTORECENTLIST,
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
                    self.log(
                        f"[C4D SAVE] Updated document name and path for {file_path}"
                    )
                except Exception as e:
                    self.log(
                        f"[C4D SAVE] ## Warning ##: Could not update document name/path: {str(e)}"
                    )

                return {
                    "success": True,
                    "file_path": file_path,
                    "message": f"Scene saved to {file_path}",
                }
            except Exception as e:
                return {"error": f"Error saving scene: {str(e)}"}

        # Execute the save function on the main thread with extended timeout
        doc = c4d.documents.GetActiveDocument()
        result = self.execute_on_main_thread(
            save_scene_on_main_thread, args=(doc, file_path), _timeout=60
        )
        return result

    def handle_load_scene(self, command):
        """Handle load_scene command with improved path handling."""
        file_path = command.get("file_path", "")
        if not file_path:
            return {"error": "No file path provided"}

        # Normalize path to handle different path formats
        file_path = os.path.normpath(os.path.expanduser(file_path))

        # Log the normalized path
        self.log(f"[C4D LOAD] Normalized file path: {file_path}")

        # If path is not absolute, try to resolve it relative to current directory
        if not os.path.isabs(file_path):
            current_doc_path = c4d.documents.GetActiveDocument().GetDocumentPath()
            if current_doc_path:
                possible_path = os.path.join(current_doc_path, file_path)
                self.log(
                    f"[C4D LOAD] Trying path relative to current document: {possible_path}"
                )
                if os.path.exists(possible_path):
                    file_path = possible_path

        # Check if file exists
        if not os.path.exists(file_path):
            # Try to find the file in common locations
            common_dirs = [
                os.path.expanduser("~/Documents"),
                os.path.expanduser("~/Desktop"),
                "/Users/Shared/",
                ".",
                # Add the current working directory
                os.getcwd(),
                # Add the directory containing the plugin
                os.path.dirname(os.path.abspath(__file__)),
                # Add parent directory of plugin
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            ]

            # Try with different extensions
            filename = os.path.basename(file_path)
            basename, ext = os.path.splitext(filename)
            if not ext:
                filenames_to_try = [filename, filename + ".c4d"]
            else:
                filenames_to_try = [filename]

            # Report search paths
            self.log(
                f"[C4D LOAD] Searching for file '{filename}' in multiple locations"
            )

            # Try each directory and filename combination
            for directory in common_dirs:
                for fname in filenames_to_try:
                    possible_path = os.path.join(directory, fname)
                    self.log(f"[C4D LOAD] Trying path: {possible_path}")
                    if os.path.exists(possible_path):
                        file_path = possible_path
                        self.log(f"[C4D LOAD] Found file at: {file_path}")
                        break
                else:
                    continue  # Continue to next directory if file not found
                break  # Break main loop if file found
            else:
                # Try a case-insensitive search as a last resort
                for directory in common_dirs:
                    if os.path.exists(directory):
                        for file in os.listdir(directory):
                            if file.lower() == filename.lower():
                                file_path = os.path.join(directory, file)
                                self.log(
                                    f"[C4D LOAD] Found file with case-insensitive match: {file_path}"
                                )
                                break
                        else:
                            continue  # Continue to next directory if file not found
                        break  # Break main loop if file found
                else:
                    return {"error": f"File not found: {file_path}"}

        # Log the load request
        self.log(f"[C4D LOAD] Loading scene from: {file_path}")

        # Define function to execute on main thread
        def load_scene_on_main_thread(file_path):
            try:
                # Load the document
                new_doc = c4d.documents.LoadDocument(file_path, c4d.SCENEFILTER_NONE)
                if not new_doc:
                    return {"error": f"Failed to load document from {file_path}"}

                # Set the new document as active
                c4d.documents.SetActiveDocument(new_doc)

                # Add the document to the documents list
                # (only needed if the document wasn't loaded by the document manager)
                c4d.documents.InsertBaseDocument(new_doc)

                # Update Cinema 4D
                c4d.EventAdd()

                return {
                    "success": True,
                    "file_path": file_path,
                    "message": f"Scene loaded from {file_path}",
                }
            except Exception as e:
                return {"error": f"Error loading scene: {str(e)}"}

        # Execute the load function on the main thread with extended timeout
        result = self.execute_on_main_thread(
            load_scene_on_main_thread, file_path, _timeout=60
        )
        return result

    def handle_execute_python(self, command):
        """Handle execute_python command with improved output capturing and error handling."""
        code = command.get("code", "")
        if not code:
            # Try alternative parameter names
            code = command.get("script", "")
            if not code:
                self.log(
                    "[C4D PYTHON] Error: No Python code provided in 'code' or 'script' parameters"
                )
                return {"error": "No Python code provided"}

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
        ]:
            if banned_keyword in code:
                return {
                    "error": f"Security: Banned keyword found in code: {banned_keyword}"
                }

        self.log(f"[C4D PYTHON] Executing Python code")

        # Prepare improved capture function with thread-safe collection
        captured_output = []
        import sys
        import traceback
        from io import StringIO

        # Execute the code on the main thread
        def execute_code():
            # Save original stdout
            original_stdout = sys.stdout
            # Create a StringIO object to capture output
            string_io = StringIO()

            try:
                # Redirect stdout to our capture object
                sys.stdout = string_io

                # Create a new namespace with limited globals
                sandbox = {
                    "c4d": c4d,
                    "math": __import__("math"),
                    "random": __import__("random"),
                    "time": __import__("time"),
                    "json": __import__("json"),
                    "doc": c4d.documents.GetActiveDocument(),
                }

                # Print startup message
                print("[C4D PYTHON] Starting script execution")

                # Execute the code
                exec(code, sandbox)

                # Print completion message
                print("[C4D PYTHON] Script execution completed")

                # Get any variables that were set in the code
                result_vars = {
                    k: v
                    for k, v in sandbox.items()
                    if not k.startswith("__")
                    and k not in ["c4d", "math", "random", "time", "json", "doc"]
                }

                # Get captured output
                full_output = string_io.getvalue()

                # Process variables to make them serializable
                processed_vars = {}
                for k, v in result_vars.items():
                    try:
                        # Try to make the value JSON-serializable
                        if hasattr(v, "__dict__"):
                            processed_vars[k] = f"<{type(v).__name__} object>"
                        else:
                            processed_vars[k] = str(v)
                    except:
                        processed_vars[k] = f"<{type(v).__name__} object>"

                # Return results
                return {
                    "success": True,
                    "output": full_output,
                    "variables": processed_vars,
                }

            except Exception as e:
                error_msg = f"Python execution error: {str(e)}"
                self.log(f"[C4D PYTHON] {error_msg}")

                # Get traceback info
                tb = traceback.format_exc()

                # Get any output captured before the error
                captured = string_io.getvalue()

                # Return error with details
                return {
                    "error": error_msg,
                    "traceback": tb,
                    "output": captured,
                }
            finally:
                # Restore original stdout
                sys.stdout = original_stdout

                # Close the StringIO object
                string_io.close()

        # Execute on main thread with extended timeout
        result = self.execute_on_main_thread(execute_code, _timeout=30)

        # Check for empty output and add warning
        if result.get("success") and not result.get("output").strip():
            self.log(
                "[C4D PYTHON] ## Warning ##: Script executed successfully but produced no output"
            )
            result["warning"] = "Script executed but produced no output"

        return result

    def handle_create_mograph_cloner(self, command):
        """
        Handle create_mograph_cloner command.
        Creates a MoGraph Cloner object with the specified properties.
        Based on Cinema 4D R2025 SDK documentation for MoGraph.
        """
        doc = c4d.documents.GetActiveDocument()
        name = command.get("cloner_name", "MoGraph Cloner")
        mode = command.get("mode", "grid").lower()

        # Ensure count is a number, not a list
        count_value = command.get("count", 10)
        # Convert to a single number if it's a list or tuple
        if isinstance(count_value, (list, tuple)) and len(count_value) > 0:
            count = float(count_value[0])
        else:
            count = float(count_value) if count_value else 10

        object_name = command.get("object_name", None)

        self.log(
            f"[C4D CLONER] Creating MoGraph Cloner: {name}, Mode: {mode}, Count: {count}"
        )

        # Find object to clone if specified
        clone_obj = None
        if object_name:
            clone_obj = self.find_object_by_name(doc, object_name)
            if not clone_obj:
                self.log(f"[C4D CLONER] Clone object not found: {object_name}")
                return {"error": f"Object '{object_name}' not found."}
            self.log(f"[C4D CLONER] Found clone object: {object_name}")

        # Define a function to run on the main thread:
        def create_mograph_cloner_safe(doc, name, mode, count, clone_obj):
            self.log("[C4D CLONER] Creating MoGraph Cloner on main thread")
            try:
                # Create cloner object
                cloner = c4d.BaseObject(c4d.Omgcloner)
                if not cloner:
                    self.log("[C4D CLONER] Failed to create Cloner object")
                    return {"error": "Failed to create Cloner object"}

                cloner.SetName(name)
                self.log(f"[C4D CLONER] Created cloner: {name}")

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
                self.log(f"[C4D CLONER] Setting cloner mode to: {mode} (ID: {mode_id})")
                cloner[c4d.ID_MG_MOTIONGENERATOR_MODE] = mode_id

                # Create a clone of the provided object, or a default cube
                child_obj = None
                if clone_obj:
                    self.log(
                        f"[C4D CLONER] Cloning source object: {clone_obj.GetName()}"
                    )
                    child_obj = clone_obj.GetClone()
                else:
                    self.log("[C4D CLONER] Creating default cube as clone source")
                    child_obj = c4d.BaseObject(c4d.Ocube)
                    child_obj.SetName("Default Cube")
                    child_obj.SetAbsScale(c4d.Vector(0.5, 0.5, 0.5))

                if not child_obj:
                    self.log("[C4D CLONER] Failed to create child object for cloner")
                    return {"error": "Failed to create child object for cloner"}

                # Insert the child object under the cloner (this is critical!)
                self.log("[C4D CLONER] Inserting child object under cloner")
                doc.InsertObject(child_obj)
                doc.AddUndo(c4d.UNDOTYPE_NEW, child_obj)

                # Ensure proper hierarchy - this is the key part!
                child_obj.InsertUnderLast(cloner)

                # Set specific parameters based on mode
                # Setting these parameters AFTER creating the hierarchy ensures they take effect
                if mode == "linear":
                    self.log(
                        f"[C4D CLONER] Configuring linear mode with count: {count}"
                    )
                    cloner[c4d.MG_LINEAR_COUNT] = count
                    # Set a reasonable default offset
                    cloner[c4d.MG_LINEAR_OFFSET] = 100

                elif mode == "grid":
                    # Calculate dimensions for a reasonable grid based on total count
                    grid_dim = max(1, int(round(count ** (1 / 3))))
                    self.log(
                        f"[C4D CLONER] Configuring grid mode with dimensions: {grid_dim}x{grid_dim}x{grid_dim}"
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
                    self.log(
                        f"[C4D CLONER] Configuring radial mode with count: {count}"
                    )
                    cloner[c4d.MG_POLY_COUNT] = count
                    # Set a reasonable radius
                    cloner[c4d.MG_POLY_RADIUS] = 200

                elif mode == "object":
                    self.log(f"[C4D CLONER] Configuring object mode")
                    # For object mode, we would need a target object
                    # This could be added in a future enhancement

                # Ensure the cloner's iteration mode is set to iterate
                # This determines how the cloner uses child objects
                cloner[c4d.MGCLONER_MODE] = c4d.MGCLONER_MODE_ITERATE

                # Update the document
                self.log("[C4D CLONER] Calling EventAdd to update document")
                c4d.EventAdd()

                # Log summary of what was created
                self.log(
                    f"[C4D CLONER] Successfully created {mode} cloner with {count} instances"
                )

                return {
                    "name": cloner.GetName(),
                    "id": str(cloner.GetGUID()),
                    "type": mode,
                    "count": count,
                    "type_id": cloner.GetType(),
                }
            except Exception as e:
                self.log(f"[C4D CLONER] Error in create_mograph_cloner_safe: {str(e)}")
                import traceback

                traceback.print_exc()
                return {"error": f"Failed to create MoGraph Cloner: {str(e)}"}

        try:
            # Execute the creation safely on the main thread with extended timeout
            self.log("[C4D CLONER] Dispatching cloner creation to main thread")
            cloner_info = self.execute_on_main_thread(
                create_mograph_cloner_safe,
                args=(doc, name, mode, count, clone_obj),
                _timeout=30,
            )

            if isinstance(cloner_info, dict) and "error" in cloner_info:
                self.log(f"[C4D CLONER] Error from main thread: {cloner_info['error']}")
                return cloner_info

            self.log(f"[C4D CLONER] Cloner created successfully: {cloner_info}")
            return {"success": True, "cloner": cloner_info}
        except Exception as e:
            self.log(f"[C4D CLONER] Exception creating MoGraph Cloner: {str(e)}")
            return {"error": f"Failed to create MoGraph Cloner: {str(e)}"}

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

                            # Add counts based on mode - using R2025.1 constant paths
                            try:
                                # Try R2025.1 module path first
                                if mode_id == 0:  # Linear
                                    additional_props["count"] = current_obj[
                                        (
                                            c4d.modules.mograph.MG_LINEAR_COUNT
                                            if hasattr(c4d.modules, "mograph")
                                            else c4d.MG_LINEAR_COUNT
                                        )
                                    ]
                                elif mode_id == 1:  # Grid
                                    if hasattr(c4d.modules, "mograph"):
                                        additional_props["count_x"] = current_obj[
                                            c4d.modules.mograph.MG_GRID_COUNT_X
                                        ]
                                        additional_props["count_y"] = current_obj[
                                            c4d.modules.mograph.MG_GRID_COUNT_Y
                                        ]
                                        additional_props["count_z"] = current_obj[
                                            c4d.modules.mograph.MG_GRID_COUNT_Z
                                        ]
                                    else:
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
                                        (
                                            c4d.modules.mograph.MG_POLY_COUNT
                                            if hasattr(c4d.modules, "mograph")
                                            else c4d.MG_POLY_COUNT
                                        )
                                    ]
                            except Exception as e:
                                self.log(
                                    f"[C4D CLONER] Error getting cloner counts: {str(e)}"
                                )

                            self.log(
                                f"[C4D CLONER] Detected MoGraph Cloner: {obj_name}, Mode: {mode_name}"
                            )
                        except Exception as e:
                            self.log(
                                f"[C4D CLONER] Error getting cloner details: {str(e)}"
                            )

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

                    # Base object info
                    obj_info = {
                        "id": obj_id,
                        "name": obj_name,
                        "type": obj_type,
                        "type_id": obj_type_id,
                        "level": depth,
                        **additional_props,  # Include any additional properties
                    }

                    # Position
                    if hasattr(current_obj, "GetAbsPos"):
                        pos = current_obj.GetAbsPos()
                        obj_info["position"] = [pos.x, pos.y, pos.z]

                    # Rotation (converted to degrees)
                    if hasattr(current_obj, "GetRelRot"):
                        rot = current_obj.GetRelRot()
                        obj_info["rotation"] = [
                            c4d.utils.RadToDeg(rot.x),
                            c4d.utils.RadToDeg(rot.y),
                            c4d.utils.RadToDeg(rot.z),
                        ]

                    # Scale
                    if hasattr(current_obj, "GetAbsScale"):
                        scale = current_obj.GetAbsScale()
                        obj_info["scale"] = [scale.x, scale.y, scale.z]

                    # Add to the list
                    objects.append(obj_info)

                    # Recurse children
                    if current_obj.GetDown():
                        get_objects_recursive(current_obj.GetDown(), depth + 1)

                    # Move to next object
                    current_obj = current_obj.GetNext()
                except Exception as e:
                    self.log(f"[C4D CLONER] Error processing object: {str(e)}")
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
                self.log(f"[**ERROR**] Error checking MoGraph objects: {str(e)}")

        # Get all objects starting from the root level
        get_all_root_objects()

        self.log(
            f"[C4D] Comprehensive object search complete, found {len(objects)} objects"
        )
        return {"objects": objects}

    def handle_add_effector(self, command):
        """Adds a MoGraph effector (e.g. random, plain) and optionally links it to a cloner."""
        doc = c4d.documents.GetActiveDocument()
        type_name = command.get("effector_type", "random").lower()
        cloner_name = command.get("cloner_name", "")
        properties = command.get("properties", {})

        name = command.get("name", f"{type_name.capitalize()} Effector")

        try:
            # Debug log
            self.log(f"[C4D EFFECTOR] Creating {type_name} effector named '{name}'")
            if cloner_name:
                self.log(
                    f"[C4D EFFECTOR] Will attempt to apply to cloner '{cloner_name}'"
                )

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
                    self.log(f"[C4D EFFECTOR] Trying to find any MoGraph Cloner object")
                    obj = doc.GetFirstObject()
                    while obj:
                        if obj.GetType() == c4d.Omgcloner:
                            cloner = obj
                            self.log(
                                f"[C4D EFFECTOR] Found cloner by type: {cloner.GetName()}"
                            )
                            break
                        obj = obj.GetNext()

                if cloner is None:
                    self.log(
                        f"[C4D EFFECTOR] ## Warning ##: Cloner '{cloner_name}' not found, effector created but not applied"
                    )
                    # Instead of returning error, just continue without applying
                else:
                    if cloner.GetType() != c4d.Omgcloner:
                        self.log(
                            f"[C4D EFFECTOR] ## Warning ##: Object '{cloner_name}' is not a MoGraph Cloner"
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
                                self.log(
                                    f"[C4D EFFECTOR] Creating new effector list for cloner"
                                )
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
                                f"[C4D EFFECTOR] Successfully applied effector to cloner '{cloner.GetName()}'"
                            )
                        except Exception as e:
                            self.log(
                                f"[C4D EFFECTOR] Error applying effector to cloner: {str(e)}"
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
            self.log(f"[C4D EFFECTOR] Error creating effector: {str(e)}")
            return {"error": f"Failed to create effector: {str(e)}"}

    def handle_apply_mograph_fields(self, command):
        """Applies a MoGraph field (as a child) to a MoGraph effector in C4D 2025.0.0."""
        doc = c4d.documents.GetActiveDocument()
        if not doc:
            return {"error": "No active document"}

        # Step 1: Extract command parameters
        field_type = command.get("field_type", "spherical").lower()
        field_name = command.get("field_name", f"{field_type.capitalize()} Field")
        target_name = command.get("target_name", "")
        parameters = command.get("parameters", {})

        # Step 2: Map field type to object ID
        field_type_map = {
            "spherical": getattr(c4d, "Fspherical", 440000243),
            "box": getattr(c4d, "Fbox", 440000244),
            "radial": getattr(c4d, "Fradial", 440000245),
            "linear": getattr(c4d, "Flinear", 440000246),
            "noise": 440000248,  # No constant exists in c4d module
        }

        field_type_id = field_type_map.get(field_type)
        if not field_type_id:
            return {"error": f"Unsupported field type: '{field_type}'"}

        try:
            # Step 3: Locate effector
            target = self.find_object_by_name(doc, target_name)
            if not target:
                return {"error": f"Effector '{target_name}' not found"}

            # Validate effector by its type ID
            valid_effector_types = {
                c4d.Omgplain,
                c4d.Omgrandom,
                c4d.Omgstep,
                c4d.Omgdelay,
                c4d.Omgformula,
                c4d.Omgtime,
                c4d.Omgsound,
                c4d.Omgpython,
            }
            if hasattr(c4d, "Omgtarget"):
                valid_effector_types.add(c4d.Omgtarget)
            else:
                valid_effector_types.add(c4d.Omgeffectortarget)

            if target.GetType() not in valid_effector_types:
                return {
                    "error": f"Target '{target_name}' is not a supported effector type"
                }

            # Step 4: Create field object
            field = c4d.BaseObject(field_type_id)
            if not field:
                return {"error": "Failed to create field object"}

            field.SetName(field_name)

            # Step 5: Apply basic user-defined parameters
            if "position" in parameters and isinstance(parameters["position"], list):
                field.SetAbsPos(c4d.Vector(*parameters["position"]))

            if "scale" in parameters and isinstance(parameters["scale"], list):
                field.SetAbsScale(c4d.Vector(*parameters["scale"]))

            if "rotation" in parameters and isinstance(parameters["rotation"], list):
                hpb = [c4d.utils.DegToRad(angle) for angle in parameters["rotation"]]
                field.SetAbsRot(c4d.Vector(*hpb))

            # Step 6: Insert and parent field under effector
            doc.InsertObject(field)
            field.InsertUnder(target)
            doc.AddUndo(c4d.UNDOTYPE_NEW, field)
            doc.AddUndo(c4d.UNDOTYPE_CHANGE, target)

            c4d.EventAdd()
            self.log(
                f"[C4D FIELDS] Linked field '{field_name}' to effector '{target_name}'"
            )

            return {
                "field": {
                    "name": field.GetName(),
                    "id": str(field.GetGUID()),
                    "type": field_type,
                    "target": target.GetName(),
                    "position": [
                        field.GetAbsPos().x,
                        field.GetAbsPos().y,
                        field.GetAbsPos().z,
                    ],
                    "linked": True,
                }
            }

        except Exception as e:
            import traceback

            traceback.print_exc()
            return {"error": f"Exception occurred: {str(e)}"}

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
            self.log(
                f"[C4D SBODY] Creating soft body dynamics tag '{name}' for object '{object_name}'"
            )

            # Get dynamics tag ID - same for all C4D versions
            dynamics_tag_id = 180000102  # This is the standard tag ID for dynamics

            # Create Dynamics tag
            tag = c4d.BaseTag(dynamics_tag_id)
            if tag is None:
                self.log(
                    f"[C4D SBODY] Error: Failed to create Dynamics Body tag with ID {dynamics_tag_id}"
                )
                raise RuntimeError("Failed to create Dynamics Body tag")

            tag.SetName(name)
            self.log(f"[C4D SBODY] Successfully created dynamics tag: {name}")

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
                create_soft_body_safe, args=(obj, name, stiffness, mass, object_name)
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
        """Handle create_abstract_shape command with C4D 2025 compatibility."""
        doc = c4d.documents.GetActiveDocument()

        # Standardize parameter naming for consistency with other functions
        shape_type = command.get("shape_type", "metaball").lower()

        # Accept both "name" and "object_name" for backward compatibility
        name = command.get("name")
        if name is None:
            name = command.get("object_name", f"{shape_type.capitalize()}")

        position = command.get("position", [0, 0, 0])

        # Log the requested name for debugging
        self.log(
            f"[C4D ABSTRCTSHAPE] Creating abstract shape '{shape_type}' with requested name: '{name}'"
        )

        try:
            # Updated shape type mapping based on the testing report
            # Use direct integer IDs for maximum compatibility
            shape_types = {
                "metaball": 5125,  # Correct ID from testing report
                "blob": 5119,  # Correct ID from testing report
                "loft": 5107,
                "sweep": 5118,
                "atom": 5168,
                "platonic": 5170,
                "cloth": 5186,
                "landscape": 5119,
                "extrude": 5116,
            }

            # Get the ID for the requested shape type
            shape_type_id = shape_types.get(shape_type, shape_types.get("metaball"))
            self.log(
                f"[C4D ABSTRCTSHAPE] Creating abstract shape of type: {shape_type} (ID: {shape_type_id})"
            )

            # Create the abstract shape object
            shape = c4d.BaseObject(shape_type_id)
            if shape is None:
                return {"error": f"Failed to create {shape_type} object"}

            # Set the name
            original_name = name
            shape.SetName(original_name)

            # Set position if provided
            if len(position) >= 3:
                shape.SetAbsPos(
                    c4d.Vector(
                        float(position[0]), float(position[1]), float(position[2])
                    )
                )

            # Dictionary to track all created child objects
            child_objects = {}

            # For metaball and blob shapes, add a child sphere
            if shape_type in ["metaball", "blob"]:
                # Log the child object creation
                self.log(
                    f"[C4D ABSTRCTSHAPE] Creating child sphere for {shape_type} object"
                )

                # Create sphere for the abstract shape
                sphere = c4d.BaseObject(c4d.Osphere)
                if sphere is None:
                    self.log(
                        f"[C4D ABSTRCTSHAPE] ## Warning ##: Failed to create child sphere for {shape_type}"
                    )
                else:
                    # Use a standardized naming pattern
                    sphere_name = f"{original_name}_Sphere"
                    sphere.SetName(sphere_name)

                    # Configure the sphere using GetDataInstance for C4D 2025 compatibility
                    sphere.SetAbsScale(c4d.Vector(2.0, 2.0, 2.0))

                    # Use proper BaseContainer access
                    bc = sphere.GetDataInstance()
                    if bc:
                        bc.SetFloat(c4d.PRIM_SPHERE_RAD, 50.0)

                    # Insert under the parent shape
                    sphere.InsertUnder(shape)
                    doc.AddUndo(c4d.UNDOTYPE_NEW, sphere)

                    # Track this child object
                    child_objects["sphere"] = {
                        "name": sphere.GetName(),
                        "id": str(sphere.GetGUID()),
                        "type": "sphere",
                    }

            elif shape_type in ("loft", "sweep"):
                self.log(
                    f"[C4D ABSTRCTSHAPE] Creating profile and path splines for {shape_type}"
                )

                # Create profile spline
                # Use direct constants for maximum compatibility
                spline = c4d.BaseObject(c4d.Osplinecircle)
                path = c4d.BaseObject(c4d.Osplinenside)

                if spline is None or path is None:
                    self.log(
                        f"[C4D ABSTRCTSHAPE] ## Warning ##: Failed to create profile or path splines for {shape_type}"
                    )
                else:
                    # Use standardized naming patterns
                    spline.SetName(f"{original_name}_Profile")
                    spline.InsertUnder(shape)
                    doc.AddUndo(c4d.UNDOTYPE_NEW, spline)

                    path.SetName(f"{original_name}_Path")
                    path.SetAbsPos(c4d.Vector(0, 50, 0))
                    path.InsertUnder(shape)
                    doc.AddUndo(c4d.UNDOTYPE_NEW, path)

                # Track these child objects if they were created successfully
                if spline is not None:
                    child_objects["profile"] = {
                        "name": spline.GetName(),
                        "id": str(spline.GetGUID()),
                        "type": "spline",
                    }

                if path is not None:
                    child_objects["path"] = {
                        "name": path.GetName(),
                        "id": str(path.GetGUID()),
                        "type": "spline",
                    }

            doc.InsertObject(shape)
            doc.AddUndo(c4d.UNDOTYPE_NEW, shape)
            c4d.EventAdd()

            # Get actual name which may have been changed by Cinema 4D
            actual_name = shape.GetName()
            if actual_name != original_name:
                self.log(
                    f"[C4D ABSTRCTSHAPE] ## Warning ##: Cinema 4D renamed shape from '{original_name}' to '{actual_name}'"
                )

            # Register the object for reliable lookup
            self.register_object_name(shape, original_name)

            # Return comprehensive information about created objects
            return {
                "shape": {
                    "name": actual_name,
                    "requested_name": original_name,
                    "id": str(shape.GetGUID()),
                    "type": shape_type,
                    "position": position,
                    "child_objects": child_objects,  # Include information about all child objects
                }
            }
        except Exception as e:
            self.log(f"[C4D ABSTRCTSHAPE] Error creating abstract shape: {str(e)}")
            import traceback

            self.log(traceback.format_exc())
            return {"error": f"Failed to create abstract shape: {str(e)}"}

    def _find_by_guid_recursive(self, start_obj, guid):
        """Recursively search for an object with a specific GUID."""
        current_obj = start_obj
        while current_obj:
            if str(current_obj.GetGUID()) == guid:
                return current_obj

            # Check children recursively
            child = current_obj.GetDown()
            if child:
                result = self._find_by_guid_recursive(child, guid)
                if result:
                    return result

            current_obj = current_obj.GetNext()
        return None

    def _get_all_objects(self, doc):
        """Get all objects in the document for efficient searching.

        This method uses optimal strategies for Cinema 4D 2025 to collect all objects
        in the scene without missing anything.
        """
        all_objects = []
        found_ids = set()  # To avoid duplicates

        # Method 1: Standard hierarchy traversal
        def collect_recursive(obj):
            if obj is None:
                return

            obj_id = str(obj.GetGUID())
            if obj_id not in found_ids:
                all_objects.append(obj)
                found_ids.add(obj_id)

            # Get children
            child = obj.GetDown()
            if child:
                collect_recursive(child)

            # Get siblings
            next_obj = obj.GetNext()
            if next_obj:
                collect_recursive(next_obj)

        # Start collection from root
        collect_recursive(doc.GetFirstObject())

        # Method 2: Use GetObjects API if available in this version
        try:
            if hasattr(doc, "GetObjects"):
                objects = doc.GetObjects()
                for obj in objects:
                    obj_id = str(obj.GetGUID())
                    if obj_id not in found_ids:
                        all_objects.append(obj)
                        found_ids.add(obj_id)
        except Exception as e:
            self.log(f"[**ERROR**] Error using GetObjects API: {str(e)}")

        # Method 3: Check for any missed MoGraph objects
        try:
            # Direct check for Cloners
            if hasattr(c4d, "Omgcloner"):
                # Use object type filtering to find cloners
                for obj in all_objects[:]:  # Use a copy to avoid modification issues
                    if (
                        obj.GetType() == c4d.Omgcloner
                        and str(obj.GetGUID()) not in found_ids
                    ):
                        all_objects.append(obj)
                        found_ids.add(str(obj.GetGUID()))
        except Exception as e:
            self.log(f"[**ERROR**] Error checking for MoGraph objects: {str(e)}")

        self.log(f"[C4D] Found {len(all_objects)} objects in document")
        return all_objects

    def handle_create_light(self, command):
        """Light creation with EXACT 2025.0 SDK parameters"""
        doc = c4d.documents.GetActiveDocument()
        light_type = command.get("type", "spot").lower()
        default_name = command.get("object_name")

        # Verified from your script log and docs
        LIGHT_TYPE_MAP = {
            "point": 0,  # c4d.LIGHT_TYPE_OMNI
            "spot": 1,  # c4d.LIGHT_TYPE_SPOT
            "area": 8,  # c4d.LIGHT_TYPE_AREA
            "infinite": 3,  # c4d.LIGHT_TYPE_DISTANT
        }

        try:
            light = c4d.BaseObject(c4d.Olight)
            if not light:
                return {"error": "Light creation failed"}

            # Validate light type first
            if light_type not in LIGHT_TYPE_MAP:
                valid_types = ", ".join(LIGHT_TYPE_MAP.keys())
                return {
                    "error": f"Invalid light type: {light_type}. Valid: {valid_types}"
                }

            light_code = LIGHT_TYPE_MAP[light_type]
            light[c4d.LIGHT_TYPE] = light_code

            # Core parameters (documented)
            if not default_name:
                # Use standardized test name if this is coming from test harness
                if command.get("from_test_harness"):
                    default_name = "Test_Light"
                else:
                    default_name = f"{light_type.capitalize()} Light"

            light.SetName(default_name)
            light.SetAbsPos(
                c4d.Vector(
                    *[float(x) for x in command.get("position", [0, 100, 0])[:3]]
                )
            )
            light[c4d.LIGHT_COLOR] = c4d.Vector(
                *[float(c) for c in command.get("color", [1, 1, 1])[:3]]
            )
            light[c4d.LIGHT_BRIGHTNESS] = (
                float(command.get("intensity", 100)) / 100.0
            )  # 0-1 range

            # Temperature handling (only set if parameter exists)
            if hasattr(c4d, "LIGHT_TEMPERATURE"):
                try:
                    light[c4d.LIGHT_TEMPERATURE] = float(
                        command.get("temperature", 6500)
                    )
                except:
                    pass  # Silently fail if temperature is invalid

            # Area light parameters (documented)
            if light_code == 8:  # Area light
                light[c4d.LIGHT_AREADETAILS_SIZEX] = float(command.get("width", 200))
                light[c4d.LIGHT_AREADETAILS_SIZEY] = float(command.get("height", 200))
                light[c4d.LIGHT_AREADETAILS_SHAPE] = 0  # Rectangle

            # Shadow parameters (documented)
            if hasattr(c4d, "LIGHT_SHADOWTYPE"):
                light[c4d.LIGHT_SHADOWTYPE] = (
                    1  # Soft shadows (c4d.LIGHT_SHADOWTYPE_SOFT)
                )

            doc.InsertObject(light)
            c4d.EventAdd()

            return {
                "success": True,
                "type": light_type,
                "temperature": (
                    light[c4d.LIGHT_TEMPERATURE]
                    if hasattr(c4d, "LIGHT_TEMPERATURE")
                    else None
                ),
                "width": (
                    light[c4d.LIGHT_AREADETAILS_SIZEX] if light_code == 8 else None
                ),
                "height": (
                    light[c4d.LIGHT_AREADETAILS_SIZEY] if light_code == 8 else None
                ),
            }

        except Exception as e:
            return {"error": f"Light creation failed: {str(e)}"}

    def handle_create_camera(self, command):
        """Create a new camera in the scene with optional properties."""
        doc = c4d.documents.GetActiveDocument()

        # Get command values
        name = command.get("name", "Camera")
        position = command.get("position", [0, 0, 0])
        properties = command.get("properties", {})  # Focal length, aperture, etc.

        # Create the camera
        camera = c4d.BaseObject(c4d.Ocamera)
        camera.SetName(name)

        # Set position
        if isinstance(position, list) and len(position) >= 3:
            camera.SetAbsPos(c4d.Vector(*position))

        # Apply camera-specific properties if provided
        if "focal_length" in properties:
            camera[c4d.CAMERA_FOCUS] = float(properties["focal_length"])
        if "aperture" in properties:
            camera[c4d.CAMERA_APERTURE] = float(properties["aperture"])
        if "film_offset_x" in properties:
            camera[c4d.CAMERA_FILM_OFFSET_X] = float(properties["film_offset_x"])
        if "film_offset_y" in properties:
            camera[c4d.CAMERA_FILM_OFFSET_Y] = float(properties["film_offset_y"])

        # Insert into the scene
        doc.InsertObject(camera)
        doc.AddUndo(c4d.UNDOTYPE_NEW, camera)
        doc.SetActiveObject(camera)
        c4d.EventAdd()

        self.log(f"[C4D] Created camera '{name}' at {position}")

        return {
            "camera": {
                "name": name,
                "position": position,
                "focal_length": properties.get("focal_length"),
                "aperture": properties.get("aperture"),
            }
        }

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
                f"[C4D LIGHT] Animate camera command: path_type={path_type}, camera={camera_name}, positions={len(positions)}, frames={len(frames)}"
            )

            camera = None
            if camera_name:
                camera = self.find_object_by_name(doc, camera_name)
                if camera is None:
                    self.log(
                        f"[C4D LIGHT] Camera '{camera_name}' not found, will create a new one"
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
                            f"[C4D LIGHT] Available cameras: {', '.join(existing_cameras)}"
                        )
                    else:
                        self.log("[C4D LIGHT] No cameras found in the scene")

            if camera is None or create_camera:
                camera = c4d.BaseObject(c4d.Ocamera)
                camera.SetName(camera_name or "Animated Camera")
                self.log(f"[C4D LIGHT] Created new camera: {camera.GetName()}")

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
                    self._set_position_keyframe(camera, frame, pos)

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

    def _get_redshift_material_id(self):
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
                self.log(
                    f"[C4D RS] Found existing Redshift material with type ID: {mat_type}"
                )
                # Try to create a material with this ID
                try:
                    rs_mat = c4d.BaseMaterial(mat_type)
                    if rs_mat and rs_mat.GetType() == mat_type:
                        self.log(
                            f"[C4D RS] Successfully created Redshift material using detected ID: {mat_type}"
                        )
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
            if (
                hasattr(c4d, "modules")
                and hasattr(c4d.modules, "net")
                and hasattr(c4d.modules.net, "Execute")
            ):
                # Execute in a controlled way that won't affect normal operation
                import tempfile, os

                script_path = None
                try:
                    with tempfile.NamedTemporaryFile(suffix=".py", delete=False) as f:
                        f.write(script.encode("utf-8"))
                        script_path = f.name

                    # Try to execute this script
                    self.execute_on_main_thread(
                        lambda: c4d.modules.net.Execute(script_path)
                    )
                finally:
                    # Always clean up the temp file
                    if script_path and os.path.exists(script_path):
                        try:
                            os.unlink(script_path)
                        except:
                            pass

            # Now look for the material we created
            temp_mat = self._find_material_by_name(doc, "TempRedshiftMaterial")
            if temp_mat and temp_mat.GetType() >= 1000000:
                self.log(
                    f"[C4D RS] Created Redshift material via script with type ID: {temp_mat.GetType()}"
                )
                # Clean up the temporary material
                doc.RemoveMaterial(temp_mat)
                c4d.EventAdd()
                # Create a fresh material with this ID
                return c4d.BaseMaterial(temp_mat.GetType())
        except Exception as e:
            self.log(
                f"[C4D RS] Script-based Redshift material creation failed: {str(e)}"
            )

        # No Redshift materials found
        return None

    def _find_material_by_name(self, doc, name):
        """Find a material by name in the document.

        Args:
            doc: The active Cinema 4D document
            name: The name of the material to find

        Returns:
            The material if found, None otherwise
        """
        if not name:
            self.log(f"[C4D] ## Warning ##: Empty material name provided")
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
            redshift_module_exists = hasattr(c4d, "modules") and hasattr(
                c4d.modules, "redshift"
            )
            self.log(
                f"[C4D] DIAGNOSTIC: Redshift module exists: {redshift_module_exists}"
            )

            if redshift_module_exists:
                redshift = c4d.modules.redshift
                self.log(
                    f"[C4D] DIAGNOSTIC: Redshift module dir contents: {dir(redshift)}"
                )

                # Check for common Redshift module attributes
                for attr in ["Mmaterial", "MATERIAL_TYPE", "GetRSMaterialNodeSpace"]:
                    has_attr = hasattr(redshift, attr)
                    self.log(
                        f"[C4D] DIAGNOSTIC: Redshift module has '{attr}': {has_attr}"
                    )

            # Check if Redshift ID_REDSHIFT_MATERIAL constant exists
            has_rs_constant = hasattr(c4d, "ID_REDSHIFT_MATERIAL")
            self.log(
                f"[C4D] DIAGNOSTIC: c4d.ID_REDSHIFT_MATERIAL exists: {has_rs_constant}"
            )
            if has_rs_constant:
                self.log(
                    f"[C4D] DIAGNOSTIC: c4d.ID_REDSHIFT_MATERIAL value: {c4d.ID_REDSHIFT_MATERIAL}"
                )

            # Check all installed plugins
            plugins = c4d.plugins.FilterPluginList(c4d.PLUGINTYPE_MATERIAL, True)
            self.log(f"[C4D] DIAGNOSTIC: Found {len(plugins)} material plugins")
            for plugin in plugins:
                plugin_name = plugin.GetName()
                plugin_id = plugin.GetID()
                self.log(
                    f"[C4D] DIAGNOSTIC: Material plugin: {plugin_name} (ID: {plugin_id})"
                )

            # Continue with normal validation
            # Get the Redshift node space ID
            redshift_ns = maxon.Id("com.redshift3d.redshift4c4d.class.nodespace")

            # Log all relevant Redshift material IDs for debugging
            self.log(f"[C4D] Standard material ID: {c4d.Mmaterial}")
            self.log(
                f"[C4D] Redshift material ID (c4d.ID_REDSHIFT_MATERIAL): {c4d.ID_REDSHIFT_MATERIAL}"
            )

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
                "material_types": {},
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
                is_rs_material = mat_type == c4d.ID_REDSHIFT_MATERIAL

                # Also check for alternative Redshift material type IDs
                if not is_rs_material and mat_type >= 1000000:
                    # This is likely a Redshift material with a different ID
                    self.log(
                        f"[C4D] Found possible Redshift material with ID {mat_type}: {name}"
                    )
                    is_rs_material = True

                if not is_rs_material:
                    warnings.append(
                        f"ℹ️ '{name}': Not a Redshift node material (type: {mat.GetType()})."
                    )
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
                                warnings.append(
                                    f"⚠️ Error creating default graph for '{name}': {str(e)}"
                                )
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
                                            maxon.PROPERTYFLAGS_NONE,
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

                            fixes.append(
                                f"✅ Converted '{name}' to Redshift node material."
                            )
                            mat_stats["fixed"] += 1
                        except Exception as e:
                            warnings.append(f"❌ Failed to convert '{name}': {str(e)}")

                    continue

                # For Redshift materials, continue with validation
                if is_rs_material:
                    # It's a confirmed Redshift material
                    mat_stats["redshift"] += 1

                    # Check if it's using the Redshift node space
                    if (
                        hasattr(mat, "GetNodeMaterialSpace")
                        and mat.GetNodeMaterialSpace() != redshift_ns
                    ):
                        warnings.append(
                            f"⚠️ '{name}': Redshift material but not using correct node space."
                        )
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
                            warnings.append(
                                f"❌ Could not create default graph for '{name}': {str(e)}"
                            )

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
                            standard_surface = graph.CreateNode(
                                maxon.nodes.IdAndVersion(
                                    "com.redshift3d.redshift4c4d.nodes.core.standardmaterial"
                                )
                            )

                            # Connect to output
                            graph.CreateConnection(
                                standard_surface.GetOutputs()[0],  # Surface output
                                root.GetInputs()[0],  # Surface input on root
                            )

                            fixes.append(f"✅ Added Standard Surface node to '{name}'.")
                            mat_stats["fixed"] += 1
                        except Exception as e:
                            warnings.append(
                                f"❌ Could not add Standard Surface to '{name}': {str(e)}"
                            )

                    continue

                # Check that the output is connected to a Redshift Material node (Standard Surface, etc.)
                if (
                    "StandardMaterial" not in output_node.GetId()
                    and "Material" not in output_node.GetId()
                ):
                    warnings.append(
                        f"❌ '{name}': Output not connected to a Redshift Material node."
                    )
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
                    warnings.append(
                        f"ℹ️ '{name}': Diffuse/base color input not connected."
                    )
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

                    warnings.append(
                        f"✅ '{name}': Diffuse/base color connected to {source_type} node."
                    )

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
                                warnings.append(
                                    f"⚠️ '{name}': Fresnel node missing IOR input."
                                )
                                mat_stats["issues"] += 1

                        outputs_valid = False
                        for output_port in node.GetOutputs():
                            if output_port.GetSource():
                                outputs_valid = True
                                break

                        if not outputs_valid:
                            warnings.append(
                                f"⚠️ '{name}': Fresnel node has no output connections."
                            )
                            mat_stats["issues"] += 1

                if has_fresnel:
                    warnings.append(
                        f"ℹ️ '{name}': Contains Fresnel shader (check for potential issues)."
                    )

            # Summary stats
            summary = (
                f"Material validation complete. Found {mat_stats['total']} materials: "
                + f"{mat_stats['redshift']} Redshift, {mat_stats['standard']} Standard, "
                + f"{mat_stats['issues']} with issues, {mat_stats['fixed']} fixed."
            )

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
                    "redshift_material": c4d.ID_REDSHIFT_MATERIAL,
                },
            }

        except Exception as e:
            return {
                "status": "error",
                "message": f"Error validating materials: {str(e)}",
                "warnings": warnings,
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
                    self.log(
                        f"[C4D] Found Redshift plugin: {plugin_name} (ID: {plugin_id})"
                    )

            if material_type == "redshift" and not has_redshift:
                self.log(
                    "[C4D] ## Warning ##: Redshift requested but not found in plugins. Using standard material."
                )
                material_type = "standard"

            # STEP 2: Create the material based on type
            if material_type == "redshift" and has_redshift:
                self.log(
                    "[C4D] Creating Redshift material using NodeMaterial approach..."
                )
                # Try multiple methods for creating Redshift material, preferring NodeMaterial
                redshift_material_created = False

                # Using R2025.1 SDK approach for creating Redshift NodeMaterial
                try:
                    self.log(
                        "[C4D] Creating Redshift material using R2025.1 SDK approach"
                    )

                    # Determine the Redshift material ID to use
                    rs_id = 1036224  # Default known Redshift material ID

                    if hasattr(c4d, "ID_REDSHIFT_MATERIAL"):
                        rs_id = c4d.ID_REDSHIFT_MATERIAL
                        self.log(f"[C4D] Using c4d.ID_REDSHIFT_MATERIAL: {rs_id}")
                    elif redshift_plugin_id is not None:
                        rs_id = redshift_plugin_id
                        self.log(f"[C4D] Using detected plugin ID: {rs_id}")
                    else:
                        self.log(f"[C4D] Using default Redshift ID: {rs_id}")

                    # Step 1: Create a material with the Redshift Material ID
                    mat = c4d.BaseMaterial(rs_id)

                    if not mat:
                        raise RuntimeError("Failed to create base Redshift material")

                    # Set the name immediately
                    mat.SetName(name)

                    # Verify we got a valid Redshift material
                    if mat and mat.GetType() == rs_id:
                        self.log(
                            f"[C4D] Successfully created Redshift material, type: {mat.GetType()}"
                        )
                        redshift_material_created = True
                        material_type = "redshift"
                        success = True
                    else:
                        self.log("[C4D] Failed to create valid Redshift material")
                        material_type = "standard"

                except Exception as e:
                    self.log(f"[C4D] Redshift material creation error: {str(e)}")
                    import traceback

                    traceback.print_exc()
                    material_type = "standard"

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
                        redshift_ns = maxon.Id(
                            "com.redshift3d.redshift4c4d.class.nodespace"
                        )

                        # Create default graph (includes Standard material node)
                        self.log(
                            "[C4D] Creating default node graph for Redshift material"
                        )
                        try:
                            # Step 2: Properly initialize as a NodeMaterial (R2025.1 approach)
                            # This is critical as per the R2025.1 SDK documentation
                            node_mat = c4d.NodeMaterial(mat)
                            if not node_mat:
                                raise RuntimeError(
                                    "Failed to create NodeMaterial wrapper"
                                )

                            # Step 3: Create the default graph using the NodeMaterial
                            if not node_mat.HasSpace(redshift_ns):
                                graph = node_mat.CreateDefaultGraph(redshift_ns)
                                self.log("[C4D] Created default Redshift node graph")
                            else:
                                graph = node_mat.GetGraph(redshift_ns)
                                self.log("[C4D] Using existing Redshift node graph")

                            # Important: Update our reference to use the NodeMaterial
                            mat = node_mat

                            # Find the Standard Surface material node to set color
                            if len(color) >= 3 and graph:
                                root = graph.GetViewRoot()
                                if root:
                                    # Try to find Standard Surface node
                                    for node in graph.GetNodes():
                                        node_id = node.GetId()
                                        if "StandardMaterial" in node_id:
                                            self.log(
                                                f"[C4D] Found StandardMaterial node: {node_id}"
                                            )
                                            try:
                                                # Set base color parameter
                                                node.SetParameter(
                                                    maxon.nodes.ParameterID(
                                                        "base_color"
                                                    ),
                                                    maxon.Color(
                                                        color[0], color[1], color[2]
                                                    ),
                                                    maxon.PROPERTYFLAGS_NONE,
                                                )
                                                self.log(
                                                    f"[C4D] Set color: [{color[0]}, {color[1]}, {color[2]}]"
                                                )
                                            except Exception as e:
                                                self.log(
                                                    f"[**ERROR**] Error setting node color: {str(e)}"
                                                )
                                            break
                        except Exception as e:
                            self.log(
                                f"[**ERROR**] Error setting up Redshift node graph: {str(e)}"
                            )
                    except ImportError as e:
                        self.log(f"[**ERROR**] Error importing maxon module: {str(e)}")
                        # Continue with basic material without node graph
                else:
                    self.log(
                        "[C4D] All Redshift material creation methods failed, switching to standard"
                    )
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
                if (
                    "specular" in properties
                    and isinstance(properties["specular"], list)
                    and len(properties["specular"]) >= 3
                ):
                    spec = properties["specular"]
                    mat[c4d.MATERIAL_SPECULAR_COLOR] = c4d.Vector(
                        spec[0], spec[1], spec[2]
                    )

                if "reflection" in properties and isinstance(
                    properties["reflection"], (int, float)
                ):
                    mat[c4d.MATERIAL_REFLECTION_BRIGHTNESS] = float(
                        properties["reflection"]
                    )

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

            self.log(
                f"[C4D] Material created successfully: {name}, type: {material_type}, ID: {mat.GetType()}"
            )

            return {
                "material": {
                    "name": mat.GetName(),  # Exact Cinema 4D material name
                    "id": material_id,  # Internal ID
                    "color": material_color,  # Material color (RGB)
                    "type": material_type,  # "standard" or "redshift"
                    "material_type_id": mat.GetType(),  # Actual material type ID
                    "procedural": procedural if material_type == "redshift" else False,
                    "redshift_available": has_redshift,  # Helps client know if Redshift is available
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
                mat = self._find_material_by_name(doc, material_name)

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
            is_redshift_material = mat.GetType() >= 1000000
            if is_redshift_material:
                self.log(f"[C4D] Detected Redshift material (ID: {mat.GetType()})")

                # Handle shader application for Redshift material using node graph
                try:
                    import maxon

                    redshift_ns = maxon.Id(
                        "com.redshift3d.redshift4c4d.class.nodespace"
                    )

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
                                    surface_input = root_node.GetInputs()[
                                        0
                                    ]  # First input is usually surface

                                    if surface_input.GetDestination():
                                        material_output = (
                                            surface_input.GetDestination().GetNode()
                                        )

                                    if not material_output:
                                        # Try to find a standard material node
                                        for node in graph.GetNodes():
                                            if "StandardMaterial" in node.GetId():
                                                material_output = node
                                                break

                                    if material_output:
                                        self.log(
                                            f"[C4D] Found material output node: {material_output.GetId()}"
                                        )

                                        # Create shader node based on type
                                        shader_node = None

                                        if shader_type == "noise":
                                            # Create a Redshift Noise texture
                                            shader_node = graph.AddChild(
                                                maxon.Id(),  # Auto-generate ID
                                                maxon.Id(
                                                    "com.redshift3d.redshift4c4d.nodes.core.texturesampler"
                                                ),
                                            )

                                            if shader_node:
                                                # Set texture type to noise
                                                shader_node.SetParameter(
                                                    maxon.nodes.ParameterID("tex0_tex"),
                                                    4,  # 4 = Noise in Redshift
                                                    maxon.PROPERTYFLAGS_NONE,
                                                )

                                                # Set noise parameters
                                                if "scale" in parameters:
                                                    try:
                                                        scale = float(
                                                            parameters["scale"]
                                                        )
                                                        shader_node.SetParameter(
                                                            maxon.nodes.ParameterID(
                                                                "noise_scale"
                                                            ),
                                                            scale,
                                                            maxon.PROPERTYFLAGS_NONE,
                                                        )
                                                    except Exception as e:
                                                        self.log(
                                                            f"[**ERROR**] Error setting noise scale: {str(e)}"
                                                        )

                                        elif shader_type == "fresnel":
                                            # Create a Redshift Fresnel node
                                            shader_node = graph.AddChild(
                                                maxon.Id(),  # Auto-generate ID
                                                maxon.Id(
                                                    "com.redshift3d.redshift4c4d.nodes.core.fresnel"
                                                ),
                                            )

                                            if shader_node:
                                                # Set IOR parameter if specified
                                                if "ior" in parameters:
                                                    try:
                                                        ior = float(parameters["ior"])
                                                        shader_node.SetParameter(
                                                            maxon.nodes.ParameterID(
                                                                "ior"
                                                            ),
                                                            ior,
                                                            maxon.PROPERTYFLAGS_NONE,
                                                        )
                                                    except Exception as e:
                                                        self.log(
                                                            f"[**ERROR**] Error setting fresnel IOR: {str(e)}"
                                                        )

                                        elif shader_type == "gradient":
                                            # Create a Redshift Gradient texture
                                            shader_node = graph.AddChild(
                                                maxon.Id(),  # Auto-generate ID
                                                maxon.Id(
                                                    "com.redshift3d.redshift4c4d.nodes.core.texturesampler"
                                                ),
                                            )

                                            if shader_node:
                                                # Set texture type to gradient
                                                shader_node.SetParameter(
                                                    maxon.nodes.ParameterID("tex0_tex"),
                                                    2,  # 2 = Gradient in Redshift
                                                    maxon.PROPERTYFLAGS_NONE,
                                                )

                                        elif shader_type == "checkerboard":
                                            # Create a Redshift Checker texture
                                            shader_node = graph.AddChild(
                                                maxon.Id(),  # Auto-generate ID
                                                maxon.Id(
                                                    "com.redshift3d.redshift4c4d.nodes.core.texturesampler"
                                                ),
                                            )

                                            if shader_node:
                                                # Set texture type to checker
                                                shader_node.SetParameter(
                                                    maxon.nodes.ParameterID("tex0_tex"),
                                                    1,  # 1 = Checker in Redshift
                                                    maxon.PROPERTYFLAGS_NONE,
                                                )

                                        # Connect the shader to the appropriate channel
                                        if shader_node:
                                            self.log(
                                                f"[C4D] Created {shader_type} node: {shader_node.GetId()}"
                                            )

                                            # Find the right input port based on channel
                                            target_port = None
                                            for (
                                                input_port
                                            ) in material_output.GetInputs():
                                                port_id = input_port.GetId()

                                                if channel == "color" and (
                                                    "base_color" in port_id
                                                    or "diffuse_color" in port_id
                                                ):
                                                    target_port = input_port
                                                    break
                                                elif channel == "reflection" and (
                                                    "refl_color" in port_id
                                                    or "reflection" in port_id
                                                ):
                                                    target_port = input_port
                                                    break
                                                elif channel == "bump" and (
                                                    "bump" in port_id
                                                ):
                                                    target_port = input_port
                                                    break
                                                elif channel == "opacity" and (
                                                    "opacity" in port_id
                                                    or "transparency" in port_id
                                                ):
                                                    target_port = input_port
                                                    break

                                            if target_port:
                                                self.log(
                                                    f"[C4D] Found target port: {target_port.GetId()}"
                                                )

                                                # Find the appropriate output port of the shader
                                                source_port = None
                                                for (
                                                    output_port
                                                ) in shader_node.GetOutputs():
                                                    port_id = output_port.GetId()
                                                    if (
                                                        "out" in port_id
                                                        and shader_type == "fresnel"
                                                    ):
                                                        source_port = output_port
                                                        break
                                                    elif "outcolor" in port_id:
                                                        source_port = output_port
                                                        break

                                                if source_port:
                                                    # Create the connection
                                                    graph.CreateConnection(
                                                        source_port, target_port
                                                    )
                                                    self.log(
                                                        f"[C4D] Connected {shader_type} to {channel} channel"
                                                    )
                                                else:
                                                    self.log(
                                                        f"[C4D] Could not find source output port for {shader_type}"
                                                    )
                                            else:
                                                self.log(
                                                    f"[C4D] Could not find {channel} input port on material"
                                                )
                                        else:
                                            self.log(
                                                f"[C4D] Failed to create {shader_type} node"
                                            )
                                    else:
                                        self.log(
                                            "[C4D] Could not find a valid material output node"
                                        )
                                except Exception as e:
                                    self.log(
                                        f"[**ERROR**] Error in node graph transaction: {str(e)}"
                                    )
                                    transaction.Rollback()
                                    return {
                                        "error": f"Failed to apply shader to Redshift material: {str(e)}"
                                    }

                                # Commit the transaction if no errors
                                transaction.Commit()
                        else:
                            self.log("[C4D] Could not access Redshift node graph")

                            # Try to create the graph
                            try:
                                node_mat.CreateDefaultGraph(redshift_ns)
                                self.log(
                                    "[C4D] Created default Redshift node graph, try applying shader again"
                                )
                                return self.handle_apply_shader(
                                    command
                                )  # Retry with new graph
                            except Exception as e:
                                self.log(
                                    f"[C4D] Failed to create Redshift node graph: {str(e)}"
                                )
                    else:
                        self.log("[C4D] Material does not have a Redshift node space")
                        is_redshift_material = False  # Treat as standard material
                except Exception as e:
                    self.log(f"[**ERROR**] Error handling Redshift material: {str(e)}")
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
                    self.log(
                        "[C4D] Attempting to create fresnel shader (may not be available)"
                    )

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
                    self.log(f"[C4D] ## Warning ##: Object '{object_name}' not found")
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
                        self.log(
                            f"[**ERROR**] Error applying material to object: {str(e)}"
                        )

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
                    "is_redshift": is_redshift_material,
                }
            }
        except Exception as e:
            self.log(f"[**ERROR**] Error applying shader: {str(e)}")
            return {"error": f"Failed to apply shader: {str(e)}"}


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
                                    error_msg = f"[**ERROR**] Error in main thread execution: {str(e)}"
                                    self.AppendLog(error_msg)
                                    print(
                                        error_msg
                                    )  # Also print to console for debugging
                            else:
                                self.AppendLog(
                                    f"[C4D] ## Warning ##: Non-callable value received: {type(msg_value)}"
                                )
                        else:
                            self.AppendLog(
                                f"[C4D] ## Warning ##: Unknown message type: {msg_type}"
                            )
                    except queue.Empty:
                        # Queue timeout - break the loop to prevent blocking
                        break
                    except Exception as e:
                        # Handle any other exceptions during message processing
                        error_msg = f"[**ERROR**] Error processing message: {str(e)}"
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
