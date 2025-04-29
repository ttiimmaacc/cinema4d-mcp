"""
Cinema 4D MCP Server Plugin
Updated for Cinema 4D R2025 compatibility
Version 0.1.8 - Added core logic for context-awareness
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
import traceback

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

        # --- ADDED FOR CONTEXT AWARENESS ---
        self._object_name_registry = (
            {}
        )  # Maps GUID -> requested_name AND requested_name -> GUID (less robust) Keep for compatibility

        self._name_to_guid_registry = (
            {}
        )  # Maps requested_name.lower() or actual_name.lower() -> guid
        self._guid_to_name_registry = (
            {}
        )  # Maps guid -> {'requested_name': str, 'actual_name': str}

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

    def find_object_by_name(self, doc, name_or_guid, use_guid=False):
        """Find object by GUID (preferred) or name, using local registry first. FIX for recursion and GUID format check."""
        if not name_or_guid:
            self.log("[C4D FIND] Cannot find object: No name or GUID provided.")
            return None
        if not doc:
            self.log("[C4D FIND] ## Error ##: No document provided for search.")
            return None
        if not hasattr(self, "_name_to_guid_registry"):
            self._name_to_guid_registry = {}
        if not hasattr(self, "_guid_to_name_registry"):
            self._guid_to_name_registry = {}

        search_term = str(name_or_guid).strip()
        self.log(
            f"[C4D FIND] Attempting to find: '{search_term}' (Treat as GUID: {use_guid})"
        )

        # --- GUID Search Logic ---
        if use_guid:
            guid_to_find = search_term
            # --- GUID format check ---
            # C4D GUIDs converted with str() are typically long numbers (sometimes negative).
            # Check if it's likely numeric and long enough. Hyphen is NOT required.
            is_valid_guid_format = False
            if guid_to_find:  # Check if not empty
                try:
                    int(guid_to_find)  # Check if it can be interpreted as an integer
                    if len(guid_to_find) > 10:  # Check if it's reasonably long
                        is_valid_guid_format = True
                except ValueError:
                    is_valid_guid_format = False  # Not purely numeric

            if not is_valid_guid_format:
                self.log(
                    f"[C4D FIND] ## Warning ##: Invalid format/length for GUID search: '{guid_to_find}'. Treating as name."
                )
                use_guid = False  # Fallback to name search
            else:
                # Try direct C4D SearchObject
                obj_from_search = doc.SearchObject(guid_to_find)
                if obj_from_search:
                    self.log(
                        f"[C4D FIND] Success (GUID Scene Search): Found '{obj_from_search.GetName()}' (GUID: {guid_to_find})"
                    )
                    current_actual_name = obj_from_search.GetName()
                    reg_entry = self._guid_to_name_registry.get(guid_to_find)
                    if (
                        not reg_entry
                        or reg_entry.get("actual_name") != current_actual_name
                    ):
                        req_name = (
                            reg_entry.get("requested_name", current_actual_name)
                            if reg_entry
                            else current_actual_name
                        )
                        self.register_object_name(obj_from_search, req_name)
                    return obj_from_search

                # Manual iteration fallback
                self.log(
                    f"[C4D FIND] Info: doc.SearchObject failed for GUID {guid_to_find}. Iterating manually..."
                )
                all_objects = self._get_all_objects(doc)
                found_obj_manual = None
                for obj_iter in all_objects:
                    try:
                        iter_guid = str(obj_iter.GetGUID())
                        if iter_guid == guid_to_find:
                            self.log(
                                f"[C4D FIND] Success (GUID Manual Iteration): Found '{obj_iter.GetName()}' (GUID: {guid_to_find})"
                            )
                            found_obj_manual = obj_iter
                            break
                    except Exception as e_iter:
                        self.log(
                            f"[C4D FIND] Error checking GUID during iteration for '{obj_iter.GetName()}': {e_iter}"
                        )

                if found_obj_manual:
                    current_actual_name = found_obj_manual.GetName()
                    reg_entry = self._guid_to_name_registry.get(guid_to_find)
                    if (
                        reg_entry
                        and reg_entry.get("actual_name") != current_actual_name
                    ):
                        req_name = reg_entry.get("requested_name", current_actual_name)
                        self.register_object_name(found_obj_manual, req_name)
                    elif not reg_entry:
                        self.register_object_name(
                            found_obj_manual, found_obj_manual.GetName()
                        )
                    return found_obj_manual

                # If both failed, cleanup registry
                self.log(
                    f"[C4D FIND] Failed (GUID): Object with GUID '{guid_to_find}' not found by SearchObject or Manual Iteration."
                )
                if guid_to_find in self._guid_to_name_registry:
                    self.log(
                        f"[C4D FIND] Cleaning registry for supposedly existing but unfound GUID {guid_to_find}."
                    )
                    reg_entry = self._guid_to_name_registry.pop(guid_to_find, None)
                    if reg_entry:
                        req_name_lower = reg_entry.get("requested_name", "").lower()
                        act_name_lower = reg_entry.get("actual_name", "").lower()
                        if req_name_lower:
                            self._name_to_guid_registry.pop(req_name_lower, None)
                        if act_name_lower and act_name_lower != req_name_lower:
                            self._name_to_guid_registry.pop(act_name_lower, None)
                return None

        # --- Name Search Logic (Keep as is from previous correction) ---
        name_to_find_lower = search_term.lower()

        # Check registry by name -> GUID -> Object
        guid_from_registry = self._name_to_guid_registry.get(name_to_find_lower)
        if guid_from_registry:
            obj_from_guid_lookup = self.find_object_by_name(
                doc, guid_from_registry, use_guid=True
            )
            if obj_from_guid_lookup:
                stored_names = self._guid_to_name_registry.get(guid_from_registry, {})
                actual_name_reg = stored_names.get("actual_name", "").lower()
                requested_name_reg = stored_names.get("requested_name", "").lower()
                found_name_actual = obj_from_guid_lookup.GetName().lower()
                if name_to_find_lower in [
                    actual_name_reg,
                    requested_name_reg,
                    found_name_actual,
                ]:
                    self.log(
                        f"[C4D FIND] Success (Registry Name '{search_term}' -> GUID {guid_from_registry}): Found '{obj_from_guid_lookup.GetName()}'"
                    )
                    if found_name_actual != actual_name_reg:
                        self.register_object_name(
                            obj_from_guid_lookup,
                            stored_names.get("requested_name", search_term),
                        )
                    return obj_from_guid_lookup
                else:
                    self.log(
                        f"[C4D FIND] ## Warning ## Registry inconsistency for name '{search_term}'. Continuing search."
                    )
            else:
                self.log(
                    f"[C4D FIND] ## Warning ## Name '{search_term}' maps to non-existent GUID. Cleaning registry."
                )
                self._name_to_guid_registry.pop(name_to_find_lower, None)
                reg_entry = self._guid_to_name_registry.pop(guid_from_registry, None)
                if reg_entry:
                    other_name_key = (
                        "actual_name"
                        if name_to_find_lower
                        == reg_entry.get("requested_name", "").lower()
                        else "requested_name"
                    )
                    other_name_val = reg_entry.get(other_name_key, "").lower()
                    if other_name_val:
                        self._name_to_guid_registry.pop(other_name_val, None)

        # Direct name search
        all_objects_name = self._get_all_objects(doc)
        for obj in all_objects_name:
            if obj.GetName().strip().lower() == name_to_find_lower:
                self.log(
                    f"[C4D FIND] Success (Direct Name Search): Found '{obj.GetName()}'"
                )
                self.register_object_name(obj, search_term)
                return obj

        # Comment Tag Search
        self.log(f"[C4D FIND] Trying comment tag search for '{search_term}'")
        if hasattr(c4d, "Tcomment"):
            for obj in all_objects_name:
                for tag in obj.GetTags():
                    if tag.GetType() == c4d.Tcomment:
                        try:
                            tag_text = tag[c4d.COMMENTTAG_TEXT]
                            if tag_text and tag_text.startswith("MCP_NAME:"):
                                tagged_name = tag_text[9:].strip()
                                if tagged_name.lower() == name_to_find_lower:
                                    self.log(
                                        f"[C4D FIND] Success (Comment Tag): Found '{obj.GetName()}'"
                                    )
                                    self.register_object_name(obj, search_term)
                                    return obj
                        except Exception as e:
                            self.log(f"Error reading comment tag: {e}")

        # User Data Search
        self.log(f"[C4D FIND] Trying user data search for '{search_term}'")
        for obj in all_objects_name:
            try:
                userdata = obj.GetUserDataContainer()
                if userdata:
                    for i in range(len(userdata)):
                        desc_id_tuple = obj.GetUserDataContainer()[i]
                        if (
                            isinstance(desc_id_tuple, tuple)
                            and len(desc_id_tuple) > c4d.DESC_NAME
                        ):
                            if desc_id_tuple[c4d.DESC_NAME] == "mcp_original_name":
                                desc_id = desc_id_tuple[c4d.DESC_ID]
                                if obj[desc_id].strip().lower() == name_to_find_lower:
                                    self.log(
                                        f"[C4D FIND] Success (User Data): Found '{obj.GetName()}'"
                                    )
                                    self.register_object_name(obj, search_term)
                                    return obj
            except Exception as e:
                self.log(f"Error checking user data for '{obj.GetName()}': {e}")

        # Fuzzy Name Search
        self.log(f"[C4D FIND] Trying fuzzy name matching for '{search_term}'")
        similar_objects = []
        for obj in all_objects_name:
            obj_name_lower = obj.GetName().strip().lower()
            if (
                name_to_find_lower in obj_name_lower
                or obj_name_lower in name_to_find_lower
                or obj_name_lower.startswith(name_to_find_lower)
                or name_to_find_lower.startswith(obj_name_lower)
            ):
                similarity = abs(len(obj_name_lower) - len(name_to_find_lower))
                similar_objects.append((obj, similarity))
        if similar_objects:
            similar_objects.sort(key=lambda pair: pair[1])
            closest_match = similar_objects[0][0]
            self.log(
                f"[C4D FIND] Success (Fuzzy Fallback): Using '{closest_match.GetName()}' for '{search_term}'"
            )
            self.register_object_name(closest_match, search_term)
            return closest_match

        # Final failure
        self.log(
            f"[C4D FIND] Failed: Object '{search_term}' not found after all checks."
        )
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
        except Exception as e:
            self.log(f"[**ERROR**] Error in MoGraph direct search: {str(e)}")

        self.log(
            f"[C4D] Comprehensive object search complete, found {len(all_objects)} objects"
        )
        return all_objects

    def handle_group_objects(self, command):
        """Handle group_objects command with GUID support."""
        doc = c4d.documents.GetActiveDocument()
        if not doc:
            return {"error": "No active document"}

        requested_group_name = command.get("group_name", "Group")
        object_identifiers = command.get("object_names", [])
        position = command.get("position", None)
        center = command.get("center", False)
        keep_world_pos = command.get("keep_world_position", True)

        objects_to_group = []
        identifiers_found_guids = set()
        identifiers_not_found = []

        if object_identifiers:
            self.log(f"[GROUP] Received identifiers: {object_identifiers}")
            for identifier in object_identifiers:
                if not identifier:
                    continue

                identifier_str = str(identifier).strip()
                # --- REVISED: Detect GUID format correctly ---
                use_current_id_as_guid = False
                if "-" in identifier_str and len(identifier_str) > 30:
                    use_current_id_as_guid = True
                elif identifier_str.isdigit() or (
                    identifier_str.startswith("-") and identifier_str[1:].isdigit()
                ):
                    if len(identifier_str) > 10:
                        use_current_id_as_guid = True

                self.log(
                    f"[GROUP] Finding object by identifier: '{identifier_str}' (Treat as GUID: {use_current_id_as_guid})"
                )
                # --- Pass the correct flag to find_object_by_name ---
                obj = self.find_object_by_name(
                    doc, identifier_str, use_guid=use_current_id_as_guid
                )

                if obj:
                    obj_guid = str(obj.GetGUID())
                    if obj_guid not in identifiers_found_guids:
                        objects_to_group.append(obj)
                        identifiers_found_guids.add(obj_guid)
                        self.log(
                            f"[GROUP] Found object: '{obj.GetName()}' (GUID: {obj_guid})"
                        )
                    else:
                        self.log(
                            f"[GROUP] Info: Object '{obj.GetName()}' (GUID: {obj_guid}) already added."
                        )
                else:
                    self.log(
                        f"[GROUP] ## Warning ##: Object identifier not found: '{identifier_str}' (Searched as GUID: {use_current_id_as_guid})"
                    )
                    identifiers_not_found.append(identifier_str)
        else:
            objects_to_group = doc.GetActiveObjects(
                c4d.GETACTIVEOBJECTFLAGS_SELECTIONORDER
                | c4d.GETACTIVEOBJECTFLAGS_TOPLEVEL
            )
            if not objects_to_group:
                return {
                    "error": "No objects selected (top-level) or specified via 'object_names'."
                }
            self.log(
                f"[GROUP] Fallback: Grouping {len(objects_to_group)} selected top-level objects."
            )

        if not objects_to_group:
            error_msg = "No valid objects found to group."
            if identifiers_not_found:
                error_msg += f" Identifiers not found: {identifiers_not_found}"
            return {"error": error_msg}

        # --- Grouping Logic ---
        group_null = None
        try:
            doc.StartUndo()
            group_null = c4d.BaseObject(c4d.Onull)
            group_null.SetName(requested_group_name)
            doc.InsertObject(group_null, None, None)
            doc.AddUndo(c4d.UNDOTYPE_NEW, group_null)

            grouped_actual_names = []
            grouped_guids = []
            original_matrices = {}

            # Calculate center
            group_center_pos = c4d.Vector(0)
            if center:
                min_vec, max_vec = c4d.Vector(float("inf")), c4d.Vector(float("-inf"))
                count = 0
                for obj in objects_to_group:
                    try:
                        rad, mp = obj.GetRad(), obj.GetMp()
                        min_vec.x, min_vec.y, min_vec.z = (
                            min(min_vec.x, mp.x - rad.x),
                            min(min_vec.y, mp.y - rad.y),
                            min(min_vec.z, mp.z - rad.z),
                        )
                        max_vec.x, max_vec.y, max_vec.z = (
                            max(max_vec.x, mp.x + rad.x),
                            max(max_vec.y, mp.y + rad.y),
                            max(max_vec.z, mp.z + rad.z),
                        )
                        count += 1
                    except Exception as e_bounds:
                        self.log(
                            f"[GROUP] Warning: Error getting bounds for '{obj.GetName()}': {e_bounds}"
                        )
                if count > 0:
                    group_center_pos = (min_vec + max_vec) * 0.5
                    self.log(f"[GROUP] Calculated center for null: {group_center_pos}")
                else:
                    center = False
                    self.log(
                        "[GROUP] Warning: Could not calculate center, disabling centering."
                    )

            # Reparent
            for obj in reversed(objects_to_group):
                try:
                    obj_name = obj.GetName()
                    obj_guid = str(obj.GetGUID())
                    grouped_actual_names.append(obj_name)
                    grouped_guids.append(obj_guid)
                    if keep_world_pos:
                        original_matrices[obj_guid] = obj.GetMg()
                    obj.Remove()
                    obj.InsertUnder(group_null)
                    doc.AddUndo(c4d.UNDOTYPE_CHANGE, obj)
                except Exception as e_reparent:
                    self.log(
                        f"[**ERROR**] Failed to reparent object '{obj_name}': {e_reparent}"
                    )

            # Set Position
            if isinstance(position, list) and len(position) == 3:
                try:
                    target_pos = c4d.Vector(
                        float(position[0]), float(position[1]), float(position[2])
                    )
                    group_null.SetAbsPos(target_pos)
                    doc.AddUndo(c4d.UNDOTYPE_CHANGE, group_null)
                except (ValueError, TypeError) as e_pos:
                    self.log(
                        f"[GROUP] Warning: Invalid position value '{position}': {e_pos}"
                    )
            elif center:
                group_null.SetAbsPos(group_center_pos)
                doc.AddUndo(c4d.UNDOTYPE_CHANGE, group_null)

            # Adjust children
            if keep_world_pos:
                null_mg_inv = ~group_null.GetMg()
                for child in group_null.GetChildren():
                    child_guid = str(child.GetGUID())
                    if child_guid in original_matrices:
                        new_ml = null_mg_inv * original_matrices[child_guid]
                        child.SetMl(new_ml)
                        doc.AddUndo(c4d.UNDOTYPE_CHANGE, child)
                    else:
                        self.log(
                            f"[GROUP] ## Warning ## Original matrix not found for child '{child.GetName()}'."
                        )

            doc.EndUndo()
            c4d.EventAdd()

            # --- Contextual Return ---
            actual_group_name = group_null.GetName()
            group_guid = str(group_null.GetGUID())
            pos_vector = group_null.GetAbsPos()
            self.register_object_name(group_null, requested_group_name)
            response = {
                "group": {
                    "requested_name": requested_group_name,
                    "actual_name": actual_group_name,
                    "guid": group_guid,
                    "children_actual_names": grouped_actual_names,
                    "children_guids": grouped_guids,
                    "position": [pos_vector.x, pos_vector.y, pos_vector.z],
                    "centered": center,
                    "kept_world_position": keep_world_pos,
                }
            }
            if identifiers_not_found:
                response["warnings"] = [
                    f"Object identifier not found: '{idf}'"
                    for idf in identifiers_not_found
                ]
            return response

        except Exception as e:
            doc.EndUndo()
            error_msg = f"Error during grouping: {str(e)}"
            self.log(f"[**ERROR**] {error_msg}\n{traceback.format_exc()}")
            if group_null and group_null.GetDown() is None:
                try:
                    doc.AddUndo(c4d.UNDOTYPE_DELETE, group_null)
                    group_null.Remove()
                except:
                    pass
            return {"error": error_msg, "traceback": traceback.format_exc()}

    def handle_add_primitive(self, command):
        """Handle add_primitive command."""
        doc = c4d.documents.GetActiveDocument()
        if not doc:
            return {"error": "No active document"}  # Added check

        primitive_type = command.get("primitive_type") or command.get("type") or "cube"
        primitive_type = primitive_type.lower()

        # Use provided name or generate one
        requested_name = (
            command.get("name")
            or command.get("object_name")
            or f"MCP_{primitive_type.capitalize()}_{int(time.time()) % 1000}"  # Generate unique name
        )

        position_list = command.get("position", [0, 0, 0])
        size_list = command.get("size", [50, 50, 50])  # Default size

        # --- Safely parse position and size ---
        position = [0.0, 0.0, 0.0]
        if isinstance(position_list, list) and len(position_list) >= 3:
            try:
                position = [float(p) for p in position_list[:3]]
            except (ValueError, TypeError):
                self.log(f"Warning: Invalid position data {position_list}")
        else:
            self.log(f"Warning: Position data not a list of 3: {position_list}")

        size = [50.0, 50.0, 50.0]
        if isinstance(size_list, list) and len(size_list) > 0:
            try:
                size_raw = [float(s) for s in size_list if s is not None]
                if not size_raw:
                    raise ValueError("Empty size list after filtering None")
                sx = size_raw[0]
                sy = size_raw[1] if len(size_raw) > 1 else sx
                sz = size_raw[2] if len(size_raw) > 2 else sx
                size = [sx, sy, sz]
            except (ValueError, TypeError):
                self.log(f"Warning: Invalid size data {size_list}")
        elif isinstance(size_list, (int, float)):  # Allow single size value
            size = [float(size_list)] * 3
        else:
            self.log(f"Warning: Size data not a list or number: {size_list}")

        obj = None
        try:  # Wrap object creation/setting in try-except
            # Create the appropriate primitive object
            if primitive_type == "cube":
                obj = c4d.BaseObject(c4d.Ocube)
                obj[c4d.PRIM_CUBE_LEN] = c4d.Vector(*size)
            elif primitive_type == "sphere":
                obj = c4d.BaseObject(c4d.Osphere)
                obj[c4d.PRIM_SPHERE_RAD] = size[0] / 2.0  # Use float division
            elif primitive_type == "cone":
                obj = c4d.BaseObject(c4d.Ocone)
                obj[c4d.PRIM_CONE_TRAD] = 0
                obj[c4d.PRIM_CONE_BRAD] = size[0] / 2.0
                obj[c4d.PRIM_CONE_HEIGHT] = size[1]
            elif primitive_type == "cylinder":
                obj = c4d.BaseObject(c4d.Ocylinder)
                obj[c4d.PRIM_CYLINDER_RADIUS] = size[0] / 2.0
                obj[c4d.PRIM_CYLINDER_HEIGHT] = size[1]
            elif primitive_type == "plane":
                obj = c4d.BaseObject(c4d.Oplane)
                obj[c4d.PRIM_PLANE_WIDTH] = size[0]
                obj[c4d.PRIM_PLANE_HEIGHT] = size[1]
            elif primitive_type == "pyramid":
                obj = c4d.BaseObject(c4d.Opyramid)
                if hasattr(c4d, "PRIM_PYRAMID_LEN"):
                    obj[c4d.PRIM_PYRAMID_LEN] = c4d.Vector(*size)
                else:
                    if hasattr(c4d, "PRIM_PYRAMID_WIDTH"):
                        obj[c4d.PRIM_PYRAMID_WIDTH] = size[0]
                    if hasattr(c4d, "PRIM_PYRAMID_HEIGHT"):
                        obj[c4d.PRIM_PYRAMID_HEIGHT] = size[1]
                    if hasattr(c4d, "PRIM_PYRAMID_DEPTH"):
                        obj[c4d.PRIM_PYRAMID_DEPTH] = size[2]
            elif primitive_type == "disk":
                obj = c4d.BaseObject(c4d.Odisk)
                # Use ORAD/IRAD for Disk
                obj[c4d.PRIM_DISK_ORAD] = size[0] / 2.0
                obj[c4d.PRIM_DISK_IRAD] = 0  # Default inner radius
            elif primitive_type == "tube":
                obj = c4d.BaseObject(c4d.Otube)
                obj[c4d.PRIM_TUBE_RADIUS] = size[0] / 2.0
                obj[c4d.PRIM_TUBE_IRADIUS] = size[1] / 2.0
                obj[c4d.PRIM_TUBE_HEIGHT] = size[2]
            elif primitive_type == "torus":
                obj = c4d.BaseObject(c4d.Otorus)
                # Use RINGRAD/PIPERAD for Torus
                obj[c4d.PRIM_TORUS_RINGRAD] = size[0] / 2.0
                obj[c4d.PRIM_TORUS_PIPERAD] = size[1] / 2.0
            elif primitive_type == "platonic":
                obj = c4d.BaseObject(c4d.Oplatonic)
                obj[c4d.PRIM_PLATONIC_TYPE] = c4d.PRIM_PLATONIC_TYPE_TETRA
                obj[c4d.PRIM_PLATONIC_RAD] = size[0] / 2.0
            else:
                self.log(
                    f"Unknown primitive_type: {primitive_type}, defaulting to cube."
                )
                obj = c4d.BaseObject(c4d.Ocube)
                obj[c4d.PRIM_CUBE_LEN] = c4d.Vector(*size)

            if obj is None:  # Check if object creation failed
                return {
                    "error": f"Failed to create base object for type '{primitive_type}'"
                }

            # Set common properties
            obj.SetName(requested_name)
            obj.SetAbsPos(c4d.Vector(*position))

            # Add to doc and finalize
            doc.InsertObject(obj)
            doc.AddUndo(c4d.UNDOTYPE_NEW, obj)  # Add Undo step
            doc.SetActiveObject(obj)  # Make it active
            c4d.EventAdd()

            # --- MODIFIED FOR CONTEXT ---
            actual_name = obj.GetName()
            guid = str(obj.GetGUID())
            pos_vec = obj.GetAbsPos()
            obj_type_name = self.get_object_type_name(obj)

            # Register the object
            self.register_object_name(obj, requested_name)

            # Return contextual information
            return {
                "object": {
                    "requested_name": requested_name,
                    "actual_name": actual_name,
                    "guid": guid,
                    "type": obj_type_name,
                    "type_id": obj.GetType(),
                    "position": [pos_vec.x, pos_vec.y, pos_vec.z],
                }
            }

        except Exception as e:
            # Catch errors during object creation or property setting
            self.log(
                f"[**ERROR**] Error adding primitive '{requested_name}': {str(e)}\n{traceback.format_exc()}"
            )
            # Clean up object if created but not inserted
            if obj and not obj.GetDocument():
                try:
                    obj.Remove()
                except:
                    pass
            return {
                "error": f"Failed to add primitive: {str(e)}",
                "traceback": traceback.format_exc(),
            }

    def register_object_name(self, obj, requested_name):
        """Register object GUID, actual name, and requested name for context tracking."""
        if not obj or not isinstance(obj, c4d.BaseObject):
            self.log("[C4D REG] Invalid object provided for registration.")
            return
        # Ensure registries exist (redundant if __init__ is correct, but safe)
        if not hasattr(self, "_name_to_guid_registry"):
            self._name_to_guid_registry = {}
        if not hasattr(self, "_guid_to_name_registry"):
            self._guid_to_name_registry = {}
        # Keep original for compatibility if needed
        if not hasattr(self, "_object_name_registry"):
            self._object_name_registry = {}

        try:
            # Ensure the object is part of a document before getting GUID
            if not obj.GetDocument():
                self.log(
                    f"[C4D REG] ## Warning ##: Object '{obj.GetName()}' not in document, cannot get reliable GUID."
                )
                return  # Skip registration if not in doc

            obj_id = str(obj.GetGUID())
            actual_name = obj.GetName()

            if not obj_id or len(obj_id) < 10:  # Basic check for non-empty GUID
                self.log(
                    f"[C4D REG] ## Warning ##: Got potentially invalid GUID '{obj_id}' for object '{actual_name}'. Cannot register."
                )
                return

            if not requested_name:
                self.log(
                    f"[C4D REG] ## Warning ## Empty requested name provided for '{actual_name}', using actual name."
                )
                requested_name = actual_name

            # Prepare names for registry check (lower case)
            req_name_lower = requested_name.lower()
            act_name_lower = actual_name.lower()

            # Clean up potentially stale entries for these names in _name_to_guid_registry
            for name_lower in {req_name_lower, act_name_lower}:
                old_guid = self._name_to_guid_registry.pop(name_lower, None)
                if old_guid and old_guid != obj_id:
                    self.log(
                        f"[C4D REG] Cleaning old name->guid mapping: '{name_lower}' pointed to {old_guid}, now points to {obj_id}"
                    )
                    # Also remove the reverse mapping for the old GUID if it exists
                    old_reg_entry = self._guid_to_name_registry.get(old_guid)
                    if old_reg_entry:
                        old_req_lower_check = old_reg_entry.get(
                            "requested_name", ""
                        ).lower()
                        old_act_lower_check = old_reg_entry.get(
                            "actual_name", ""
                        ).lower()
                        if (
                            old_req_lower_check == name_lower
                            or old_act_lower_check == name_lower
                        ):
                            self._guid_to_name_registry.pop(old_guid, None)
                            self.log(
                                f"[C4D REG] Removed stale guid->name entry for {old_guid}"
                            )

            # Clean up potentially stale entry for this GUID in _guid_to_name_registry
            old_name_entry = self._guid_to_name_registry.pop(obj_id, None)
            if old_name_entry:
                # Remove old name mappings associated with this GUID from _name_to_guid_registry
                self._name_to_guid_registry.pop(
                    old_name_entry.get("requested_name", "").lower(), None
                )
                self._name_to_guid_registry.pop(
                    old_name_entry.get("actual_name", "").lower(), None
                )
                self.log(
                    f"[C4D REG] Cleaning old guid->name mapping for {obj_id} (was pointing to '{old_name_entry.get('actual_name')}')."
                )

            # Add the new mappings to the *new* registries
            self._name_to_guid_registry[req_name_lower] = obj_id
            if (
                act_name_lower != req_name_lower
            ):  # Avoid duplicate key if names are same
                self._name_to_guid_registry[act_name_lower] = obj_id

            self._guid_to_name_registry[obj_id] = {
                "requested_name": requested_name,
                "actual_name": actual_name,
            }

            # --- Keep Original Registry Logic (for backward compatibility) ---
            # If old registry structure is needed for some reason
            self._object_name_registry[obj_id] = requested_name
            self._object_name_registry[requested_name] = obj_id

            self.log(
                f"[C4D REG] Registered: Req='{requested_name}', Act='{actual_name}', GUID={obj_id}"
            )

            # User Data
            try:
                has_tag = False
                userdata = obj.GetUserDataContainer()
                if userdata:
                    for data_index in range(len(userdata)):
                        desc_entry = userdata[data_index]
                        # Check if it's a valid description element before accessing DESC_NAME
                        if (
                            isinstance(desc_entry, tuple)
                            and len(desc_entry) > c4d.DESC_NAME
                        ):  # Handle tuple structure
                            if desc_entry[c4d.DESC_NAME] == "mcp_original_name":
                                has_tag = True
                                break
                        elif hasattr(desc_entry, "__getitem__") and c4d.DESC_NAME < len(
                            desc_entry
                        ):  # Handle potential sequence access
                            if desc_entry[c4d.DESC_NAME] == "mcp_original_name":
                                has_tag = True
                                break

                if not has_tag:
                    bc = c4d.GetCustomDataTypeDefault(c4d.DTYPE_STRING)
                    if bc:
                        bc[c4d.DESC_NAME] = "mcp_original_name"
                        bc[c4d.DESC_SHORT_NAME] = "MCP Name"
                        element = obj.AddUserData(bc)
                        if element:
                            # Make sure element is a DescID before using it as an index
                            if isinstance(element, c4d.DescID):
                                obj[element] = requested_name
                                self.log(
                                    f"[C4D] Stored original name '{requested_name}' in object user data"
                                )
                            else:
                                # Handle case where AddUserData returns index directly (older C4D)
                                try:
                                    descid_from_index = obj.GetUserDataContainer()[
                                        element
                                    ][c4d.DESC_ID]
                                    obj[descid_from_index] = requested_name
                                    self.log(
                                        f"[C4D] Stored original name '{requested_name}' in object user data (via index)"
                                    )
                                except Exception as e_ud_index:
                                    self.log(
                                        f"[C4D] ## Warning ##: AddUserData returned unexpected value '{element}', cannot set user data: {e_ud_index}"
                                    )

            except Exception as e:
                # Catch potential errors during GetUserDataContainer or AddUserData
                self.log(
                    f"[C4D] ## Warning ##: Could not add/check user data for original name: {str(e)}\n{traceback.format_exc()}"
                )

        except Exception as e:
            # Catch potential errors during GetGUID, GetName etc.
            failed_name = requested_name or (obj.GetName() if obj else "UnknownObject")
            self.log(
                f"[**ERROR**] Failed to register object '{failed_name}': {e}\n{traceback.format_exc()}"
            )

    def handle_render_preview_base64(self, frame=0, width=640, height=360):
        """SDK 2025-compliant base64 renderer with error resolution"""

        def _execute_render():
            try:
                doc = c4d.documents.GetActiveDocument()
                if not doc:
                    return {"error": "No active document"}

                # 1. Camera Validation (Critical Fix)
                if not doc.GetActiveBaseDraw().GetSceneCamera(doc):
                    return {"error": "No active camera (create camera first)"}

                # 2. RenderData Protocol Fix (SDK §9.1.3)
                original_rd = doc.GetActiveRenderData()
                if not original_rd:
                    return {"error": "No render settings configured"}

                rd_clone = original_rd.GetClone(c4d.COPYFLAGS_NONE)
                if not rd_clone:
                    return {"error": "RenderData clone failed"}

                try:
                    doc.InsertRenderData(rd_clone)
                    doc.SetActiveRenderData(rd_clone)  # Required activation

                    # 3. 2025-Specific Configuration
                    settings = rd_clone.GetData()
                    settings[c4d.RDATA_XRES] = width
                    settings[c4d.RDATA_YRES] = height
                    settings[c4d.RDATA_FRAMESEQUENCE] = (
                        c4d.RDATA_FRAMESEQUENCE_CURRENTFRAME
                    )

                    # 4. Mandatory Flags (SDK §9.4.5)
                    render_flags = (
                        c4d.RENDERFLAGS_EXTERNAL
                        | c4d.RENDERFLAGS_SHOWERRORS
                        | 0x00040000  # EMBREE_STREAMING
                        | c4d.RENDERFLAGS_NODOCUMENTCLONE
                    )

                    # 5. Bitmap Initialization (SDK §11.2.3)
                    bmp = c4d.bitmaps.MultipassBitmap(width, height, c4d.COLORMODE_RGB)
                    bmp.AddChannel(True, True)  # Required alpha

                    # 6. Frame Synchronization
                    doc.SetTime(c4d.BaseTime(frame, doc.GetFps()))
                    doc.ExecutePasses(None, True, True, True, c4d.BUILDFLAGS_NONE)

                    # 7. Core Render Execution
                    result = c4d.documents.RenderDocument(
                        doc, settings, bmp, render_flags
                    )
                    if result != c4d.RENDERRESULT_OK:
                        return {
                            "error": f"Render failed: {self._render_code_to_str(result)}"
                        }

                    # 8. MemoryFile Handling Fix
                    mem_file = c4d.storage.MemoryFileStruct()
                    mem_file.SetMemoryWriteMode()
                    if bmp.Save(mem_file, c4d.FILTER_PNG) != c4d.IMAGERESULT_OK:
                        return {"error": "PNG encoding failed"}

                    data, _ = mem_file.GetData()
                    return {
                        "success": True,
                        "image_base64": f"data:image/png;base64,{base64.b64encode(data).decode()}",
                    }

                finally:
                    # 9. Correct Resource Cleanup (SDK §9.1.4)
                    if rd_clone:
                        rd_clone.Remove()  # Fixed removal method
                    if "bmp" in locals():
                        bmp.FlushAll()
                    c4d.EventAdd()

            except Exception as e:
                return {"error": f"Render failure: {str(e)}"}

        return self.execute_on_main_thread(_execute_render, _timeout=120)

    def _render_code_to_str(self, code):
        """Convert Cinema4D render result codes to human-readable strings"""
        codes = {
            0: "Success",
            1: "Out of memory",
            2: "Command canceled",
            3: "Missing assets",
            4: "Rendering in progress",
            5: "Invalid document",
            6: "Version mismatch",
            7: "Network error",
            8: "Invalid parameters",
            9: "IO error",
        }
        return codes.get(code, f"Unknown error ({code})")

    def handle_modify_object(self, command):
        """Handle modify_object command with full property support and GUID option."""
        doc = c4d.documents.GetActiveDocument()
        if not doc:
            return {"error": "No active document"}

        properties = command.get("properties", {})
        if not properties:
            return {"error": "No properties provided to modify."}

        # --- MODIFIED: Identify target object ---
        identifier = None
        use_guid = False
        if command.get("guid"):
            identifier = command.get("guid")
            use_guid = True
            self.log(f"[MODIFY] Using GUID identifier: '{identifier}'")
        elif command.get("object_name"):
            identifier = command.get("object_name")
            use_guid = False
            self.log(f"[MODIFY] Using Name identifier: '{identifier}'")
        elif command.get(
            "name"
        ):  # Fallback check in case 'name' is used for identification
            identifier = command.get("name")
            use_guid = False
            self.log(f"[MODIFY] Using 'name' key as Name identifier: '{identifier}'")
        else:
            return {
                "error": "No object identifier ('guid', 'object_name', or 'name') provided."
            }

        # Find the object using the determined identifier and flag
        obj = self.find_object_by_name(doc, identifier, use_guid=use_guid)
        if obj is None:
            search_type = "GUID" if use_guid else "Name"
            return {
                "error": f"Object '{identifier}' (searched by {search_type}) not found."
            }

        # Apply modifications (using original logic)
        modified = {}
        name_before = obj.GetName()  # Store original name
        something_changed = False

        try:  # Wrap property modifications in try-except
            # Position
            pos = properties.get("position")
            if isinstance(pos, list) and len(pos) >= 3:
                try:
                    new_pos = c4d.Vector(float(pos[0]), float(pos[1]), float(pos[2]))
                    if obj.GetAbsPos() != new_pos:
                        obj.SetAbsPos(new_pos)
                        modified["position"] = [new_pos.x, new_pos.y, new_pos.z]
                        something_changed = True
                except (ValueError, TypeError) as e:
                    self.log(f"Warning: Invalid position value '{pos}': {e}")

            # Rotation (in degrees)
            rot = properties.get("rotation")
            if isinstance(rot, list) and len(rot) >= 3:
                try:
                    new_rot_deg = [float(r) for r in rot[:3]]
                    new_rot_rad = c4d.Vector(
                        *[c4d.utils.DegToRad(r) for r in new_rot_deg]
                    )
                    obj.SetAbsRot(new_rot_rad)  # Use Absolute rotation
                    modified["rotation"] = new_rot_deg
                    something_changed = True
                except (ValueError, TypeError) as e:
                    self.log(f"Warning: Invalid rotation value '{rot}': {e}")

            # Scale
            scale = properties.get("scale")
            if isinstance(scale, list) and len(scale) >= 3:
                try:
                    new_scale = c4d.Vector(
                        float(scale[0]), float(scale[1]), float(scale[2])
                    )
                    if obj.GetAbsScale() != new_scale:
                        obj.SetAbsScale(new_scale)
                        modified["scale"] = [new_scale.x, new_scale.y, new_scale.z]
                        something_changed = True
                except (ValueError, TypeError) as e:
                    self.log(f"Warning: Invalid scale value '{scale}': {e}")

            # Color (if object has a base color channel)
            color = properties.get("color")
            if isinstance(color, list) and len(color) >= 3:
                try:
                    new_color = c4d.Vector(
                        max(0.0, min(1.0, float(color[0]))),
                        max(0.0, min(1.0, float(color[1]))),
                        max(0.0, min(1.0, float(color[2]))),
                    )
                    if (
                        obj.IsCorrectType(c4d.Opoint)
                        or obj.IsCorrectType(c4d.Opolygon)
                        or obj.IsCorrectType(c4d.Ospline)
                        or obj.IsCorrectType(c4d.Onull)
                    ):
                        if obj[c4d.ID_BASEOBJECT_COLOR] != new_color:
                            obj[c4d.ID_BASEOBJECT_USECOLOR] = (
                                c4d.ID_BASEOBJECT_USECOLOR_ON
                            )
                            obj[c4d.ID_BASEOBJECT_COLOR] = new_color
                            modified["color"] = [new_color.x, new_color.y, new_color.z]
                            something_changed = True
                    else:
                        self.log(
                            f"Warning: Cannot set display color for object type {obj.GetType()} ('{name_before}')"
                        )
                except (ValueError, TypeError, AttributeError) as e:
                    self.log(f"Warning: Error setting color for '{name_before}': {e}")

            # Size primitives
            size = properties.get("size")
            if isinstance(size, list) and len(size) > 0:
                obj_type = obj.GetType()
                size_applied = False
                new_size_applied = []
                try:
                    safe_size = [float(s) for s in size if s is not None]
                    if not safe_size:
                        raise ValueError("No valid numeric sizes")
                    sx = safe_size[0]
                    sy = safe_size[1] if len(safe_size) > 1 else sx
                    sz = safe_size[2] if len(safe_size) > 2 else sx

                    if obj_type == c4d.Ocube:
                        new_val = c4d.Vector(sx, sy, sz)
                        if obj[c4d.PRIM_CUBE_LEN] != new_val:
                            obj[c4d.PRIM_CUBE_LEN] = new_val
                            size_applied = True
                            new_size_applied = [sx, sy, sz]
                    elif obj_type == c4d.Osphere:
                        new_val = sx / 2.0
                        if obj[c4d.PRIM_SPHERE_RAD] != new_val:
                            obj[c4d.PRIM_SPHERE_RAD] = new_val
                            size_applied = True
                            new_size_applied = [sx]
                    elif obj_type == c4d.Ocone:
                        new_brad, new_h = sx / 2.0, sy
                        if (
                            obj[c4d.PRIM_CONE_BRAD] != new_brad
                            or obj[c4d.PRIM_CONE_HEIGHT] != new_h
                        ):
                            obj[c4d.PRIM_CONE_BRAD] = new_brad
                            obj[c4d.PRIM_CONE_HEIGHT] = new_h
                            size_applied = True
                            new_size_applied = [sx, sy]
                    elif obj_type == c4d.Ocylinder:
                        new_r, new_h = sx / 2.0, sy
                        if (
                            obj[c4d.PRIM_CYLINDER_RADIUS] != new_r
                            or obj[c4d.PRIM_CYLINDER_HEIGHT] != new_h
                        ):
                            obj[c4d.PRIM_CYLINDER_RADIUS] = new_r
                            obj[c4d.PRIM_CYLINDER_HEIGHT] = new_h
                            size_applied = True
                            new_size_applied = [sx, sy]
                    elif obj_type == c4d.Oplane:
                        new_w, new_h = sx, sy
                        if (
                            obj[c4d.PRIM_PLANE_WIDTH] != new_w
                            or obj[c4d.PRIM_PLANE_HEIGHT] != new_h
                        ):
                            obj[c4d.PRIM_PLANE_WIDTH] = new_w
                            obj[c4d.PRIM_PLANE_HEIGHT] = new_h
                            size_applied = True
                            new_size_applied = [sx, sy]
                    # Add other primitives here if needed...

                    if size_applied:
                        modified["size"] = new_size_applied
                        something_changed = True
                    elif size:
                        self.log(
                            f"Info: 'size' property not applicable to object type {self.get_object_type_name(obj)} ('{name_before}')"
                        )
                except (TypeError, ValueError, IndexError, AttributeError) as e:
                    self.log(
                        f"Warning: Error modifying size for {name_before} (Type: {obj_type}): {e}"
                    )

            # Rename - process *after* other properties in case identifier was 'name'
            requested_new_name = properties.get("name")
            if isinstance(requested_new_name, str):
                new_name_stripped = requested_new_name.strip()
                if new_name_stripped and new_name_stripped != name_before:
                    self.log(
                        f"[MODIFY] Renaming '{name_before}' to '{new_name_stripped}'"
                    )
                    obj.SetName(new_name_stripped)
                    name_after_rename = (
                        obj.GetName()
                    )  # Get actual name after C4D potentially changed it
                    modified["name"] = {
                        "from": name_before,
                        "requested": new_name_stripped,
                        "to": name_after_rename,
                    }
                    something_changed = True
                    # Re-register with the *requested* new name for future lookups
                    self.register_object_name(obj, new_name_stripped)

            # --- Finalize ---
            if something_changed:
                doc.AddUndo(c4d.UNDOTYPE_CHANGE, obj)  # Add undo step for changes
                c4d.EventAdd()
            else:
                self.log(f"No modifications applied to '{name_before}'")

            # --- MODIFIED: Return Context ---
            final_name = obj.GetName()
            guid = str(obj.GetGUID())
            pos_vec = obj.GetAbsPos()
            rot_vec_rad = obj.GetAbsRot()
            scale_vec = obj.GetAbsScale()

            # Ensure registration is up-to-date if C4D renamed unexpectedly without request
            if "name" not in modified and final_name != name_before:
                self.log(
                    f"Warning: Object name changed unexpectedly from '{name_before}' to '{final_name}'. Updating registry."
                )
                self.register_object_name(
                    obj, name_before
                )  # Re-register with original intent

            return {
                "object": {
                    "requested_identifier": identifier,  # What was used to find the object
                    "was_guid": use_guid,  # Was the identifier treated as a GUID?
                    "actual_name": final_name,  # Current name after modifications
                    "guid": guid,
                    "name_before": name_before,  # Name before this operation started
                    "modified_properties": modified,  # Dictionary of changes applied by this command
                    # Include current state for confirmation
                    "current_position": [pos_vec.x, pos_vec.y, pos_vec.z],
                    "current_rotation": [
                        c4d.utils.RadToDeg(r)
                        for r in [rot_vec_rad.x, rot_vec_rad.y, rot_vec_rad.z]
                    ],
                    "current_scale": [scale_vec.x, scale_vec.y, scale_vec.z],
                }
            }

        except Exception as e:
            # Catch unexpected errors during property access/setting
            error_msg = f"Unexpected error modifying object '{name_before}': {str(e)}"
            self.log(f"[**ERROR**] {error_msg}\n{traceback.format_exc()}")
            return {"error": error_msg, "traceback": traceback.format_exc()}

    def handle_apply_material(self, command):
        """Handle apply_material command with GUID support."""
        doc = c4d.documents.GetActiveDocument()
        if not doc:
            return {"error": "No active document"}

        material_name = command.get("material_name", "")
        identifier = None
        use_guid = False

        # --- GUID Detection Improved ---
        if command.get("guid"):
            identifier = command.get("guid")
            use_guid = True
            self.log(f"[APPLY MAT] Using GUID identifier: '{identifier}'")
        elif command.get("object_name"):
            identifier = command.get("object_name")
            if "-" in str(identifier) and len(str(identifier)) > 30:
                self.log(
                    f"[APPLY MAT] Identifier '{identifier}' looks like GUID, treating as GUID."
                )
                use_guid = True
            else:
                use_guid = False
                self.log(f"[APPLY MAT] Using Name identifier: '{identifier}'")
        else:
            return {"error": "No object identifier ('guid' or 'object_name') provided."}

        # Find object
        obj = self.find_object_by_name(doc, identifier, use_guid=use_guid)
        if obj is None:
            search_type = "GUID" if use_guid else "Name"
            return {
                "error": f"Object '{identifier}' (searched by {search_type}) not found."
            }

        # Find material
        mat = self._find_material_by_name(doc, material_name)
        if mat is None:
            return {"error": f"Material not found: {material_name}"}

        material_type = command.get("material_type", "standard").lower()
        projection_type = command.get("projection_type", "cubic").lower()
        auto_uv = command.get("auto_uv", False)
        procedural = command.get("procedural", False)

        try:
            doc.StartUndo()

            # Create and configure texture tag
            tag = c4d.TextureTag()
            if not tag:
                raise RuntimeError("Failed to create TextureTag")
            tag.SetMaterial(mat)

            proj_map = {
                "cubic": c4d.TEXTURETAG_PROJECTION_CUBIC,
                "spherical": c4d.TEXTURETAG_PROJECTION_SPHERICAL,
                "flat": c4d.TEXTURETAG_PROJECTION_FLAT,
                "cylindrical": c4d.TEXTURETAG_PROJECTION_CYLINDRICAL,
                "frontal": c4d.TEXTURETAG_PROJECTION_FRONTAL,
                "uvw": c4d.TEXTURETAG_PROJECTION_UVW,
            }
            tag[c4d.TEXTURETAG_PROJECTION] = proj_map.get(
                projection_type, c4d.TEXTURETAG_PROJECTION_UVW
            )

            obj.InsertTag(tag)
            doc.AddUndo(c4d.UNDOTYPE_NEW, tag)

            # Auto UV generation
            if auto_uv:
                self.log(
                    f"[APPLY MAT] Attempting auto UV generation for '{obj.GetName()}'"
                )
                try:
                    if obj.IsInstanceOf(c4d.Opolygon):
                        uvw_tag = obj.GetTag(c4d.Tuvw)
                        if not uvw_tag:
                            uvw_tag = obj.MakeTag(c4d.Tuvw)
                            if uvw_tag:
                                doc.AddUndo(c4d.UNDOTYPE_NEW, uvw_tag)
                            else:
                                self.log("Warning: Failed to create UVW tag.")

                        if uvw_tag:
                            c4d.plugins.CallCommand(12205)  # Optimal Cubic Mapping
                            self.log("Executed Optimal (Cubic) UV mapping command.")
                        else:
                            self.log(
                                "Warning: Could not get or create UVW tag for auto UV."
                            )
                    else:
                        self.log(
                            f"Warning: Auto UV skipped, object '{obj.GetName()}' not a polygon."
                        )
                except Exception as e_uv:
                    self.log(
                        f"[**ERROR**] Error during auto UV generation: {str(e_uv)}"
                    )

            # Handle Redshift
            if (
                material_type == "redshift"
                and hasattr(c4d, "modules")
                and hasattr(c4d.modules, "redshift")
            ):
                self.log(
                    f"[APPLY MAT] Checking Redshift setup for material '{mat.GetName()}'"
                )
                try:
                    redshift = c4d.modules.redshift
                    rs_id = getattr(c4d, "ID_REDSHIFT_MATERIAL", 1036224)

                    if mat.GetType() != rs_id:
                        self.log(
                            f"Converting material '{mat.GetName()}' to Redshift (ID: {rs_id})"
                        )
                        rs_mat = c4d.BaseMaterial(rs_id)
                        if not rs_mat:
                            raise RuntimeError("Failed to create Redshift material")

                        rs_mat.SetName(f"RS_{mat.GetName()}")
                        doc.InsertMaterial(rs_mat)
                        doc.AddUndo(c4d.UNDOTYPE_NEW, rs_mat)

                        try:
                            if hasattr(c4d, "REDSHIFT_MATERIAL_DIFFUSE_COLOR"):
                                rs_mat[c4d.REDSHIFT_MATERIAL_DIFFUSE_COLOR] = mat[
                                    c4d.MATERIAL_COLOR_COLOR
                                ]
                        except Exception as e_color_copy:
                            self.log(
                                f"Warning: Could not copy color during RS conversion: {e_color_copy}"
                            )

                        try:
                            import maxon

                            ns_id = maxon.Id(
                                "com.redshift3d.redshift4c4d.class.nodespace"
                            )
                            node_rs_mat = c4d.NodeMaterial(rs_mat)
                            if node_rs_mat and not node_rs_mat.HasSpace(ns_id):
                                node_rs_mat.CreateDefaultGraph(ns_id)
                                self.log("Created default Redshift node graph.")
                        except Exception as e_graph:
                            self.log(
                                f"Warning: Failed to create Redshift graph: {e_graph}"
                            )

                        if procedural:
                            try:
                                node_space = redshift.GetRSMaterialNodeSpace(rs_mat)
                                root = redshift.GetRSMaterialRootShader(rs_mat)
                                if node_space and root:
                                    tex_node = (
                                        redshift.RSMaterialNodeCreator.CreateNode(
                                            node_space,
                                            redshift.RSMaterialNodeType.TEXTURE,
                                            "RS::TextureNode",
                                        )
                                    )
                                    if tex_node:
                                        tex_node[redshift.TEXTURE_TYPE] = (
                                            redshift.TEXTURE_NOISE
                                        )
                                        redshift.CreateConnectionBetweenNodes(
                                            node_space,
                                            tex_node,
                                            "outcolor",
                                            root,
                                            "diffuse_color",
                                        )
                                        self.log(
                                            "Connected procedural Noise node to diffuse color."
                                        )
                                    else:
                                        self.log(
                                            "Warning: Failed to create procedural texture node."
                                        )
                            except Exception as e_proc:
                                self.log(
                                    f"Warning: Error setting up procedural RS nodes: {e_proc}"
                                )

                        tag.SetMaterial(rs_mat)
                        mat = rs_mat
                        doc.AddUndo(c4d.UNDOTYPE_CHANGE, tag)
                        self.log(
                            f"Swapped tag to use new Redshift material '{rs_mat.GetName()}'"
                        )

                except Exception as e_rs_setup:
                    self.log(
                        f"[**ERROR**] Error during Redshift setup: {str(e_rs_setup)}"
                    )

            doc.EndUndo()
            c4d.EventAdd()

            return {
                "success": True,
                "message": f"Applied material '{mat.GetName()}' to object '{obj.GetName()}'.",
                "object_name": obj.GetName(),
                "object_guid": str(obj.GetGUID()),
                "material_name": mat.GetName(),
                "material_type_id": mat.GetType(),
                "projection": projection_type,
                "auto_uv_attempted": auto_uv,
            }

        except Exception as e:
            doc.EndUndo()
            err = f"Error applying material '{material_name}' to '{obj.GetName()}': {str(e)}"
            self.log(f"[**ERROR**] {err}\n{traceback.format_exc()}")
            return {"error": err, "traceback": traceback.format_exc()}

    def handle_snapshot_scene(self, command=None):
        """
        Generates a snapshot: object list + base64 preview render.
        Uses the corrected core render logic via handle_render_preview_base64.
        """
        doc = c4d.documents.GetActiveDocument()
        if not doc:
            return {"error": "No active document for snapshot."}

        frame = doc.GetTime().GetFrame(doc.GetFps())
        width, height = 640, 360

        self.log(f"[C4D SNAPSHOT] Generating snapshot for frame {frame}...")

        # List objects
        object_data = self.handle_list_objects()  # Runs via execute_on_main_thread
        objects = object_data.get("objects", [])

        # Render preview - uses handle_render_preview_base64 which now uses corrected core logic
        render_command = {"width": width, "height": height, "frame": frame}
        render_result = self.handle_render_preview_base64(
            **render_command
        )  # Runs via execute_on_main_thread

        render_info = {}
        if render_result and render_result.get("success"):
            render_info = {
                "frame": render_result.get("frame", frame),
                "resolution": f"{render_result.get('width', width)}x{render_result.get('height', height)}",
                "image_base64": render_result.get("image_base64"),
                "render_time": render_result.get("render_time", 0.0),
                "format": render_result.get("format", "png"),
                "success": True,
            }
            self.log(f"[C4D SNAPSHOT] Render successful.")
        else:
            error_msg = render_result.get("error", "Unknown rendering error")
            render_info = {"error": error_msg, "success": False}
            self.log(f"[C4D SNAPSHOT] Render failed: {error_msg}")
            # Include traceback from render result if available
            if isinstance(render_result, dict) and "traceback" in render_result:
                render_info["traceback"] = render_result["traceback"]

        # Return combined result
        return {
            "objects": objects,
            "render": render_info,
        }

    def handle_set_keyframe(self, command):
        """Set a keyframe on an object, supporting both GUID and name lookup."""
        doc = c4d.documents.GetActiveDocument()
        if not doc:
            return {"error": "No active document"}

        # --- Identifier Detection ---
        identifier = None
        use_guid = False
        if command.get("guid"):
            identifier = command.get("guid")
            use_guid = True
            self.log(f"[KEYFRAME] Using GUID identifier: '{identifier}'")
        elif command.get("object_name"):
            identifier = command.get("object_name")
            if "-" in str(identifier) and len(str(identifier)) > 30:
                use_guid = True
                self.log(
                    f"[KEYFRAME] Identifier '{identifier}' looks like GUID, treating as GUID."
                )
            else:
                use_guid = False
                self.log(f"[KEYFRAME] Using Name identifier: '{identifier}'")
        else:
            identifier = command.get("name")
            if identifier:
                use_guid = False
                self.log(
                    f"[KEYFRAME] Using 'name' key as Name identifier: '{identifier}'"
                )
            else:
                return {
                    "error": "No object identifier ('guid', 'object_name', or 'name') provided."
                }

        # Find object
        obj = self.find_object_by_name(doc, identifier, use_guid=use_guid)
        if obj is None:
            search_type = "GUID" if use_guid else "Name"
            return {
                "error": f"Object '{identifier}' (searched by {search_type}) not found for keyframing."
            }

        # --- Property, Frame, and Value ---
        property_type = (
            command.get("property_type") or command.get("property") or "position"
        ).lower()
        frame = command.get("frame", doc.GetTime().GetFrame(doc.GetFps()))
        value = command.get("value")
        if value is None:
            return {"error": "No 'value' provided for keyframe."}

        try:
            frame = int(frame)
        except (ValueError, TypeError):
            return {"error": f"Invalid frame: {frame}"}

        try:
            # --- Handle Different Property Types ---
            if "." in property_type:
                # Vector component property (e.g., position.x)
                parts = property_type.split(".")
                if len(parts) != 2:
                    return {
                        "error": f"Invalid property format: '{property_type}'. Use 'position.x' etc."
                    }

                base_property, component = parts
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
                    "r": c4d.VECTOR_X,
                    "g": c4d.VECTOR_Y,
                    "b": c4d.VECTOR_Z,
                }

                if (
                    base_property not in property_map
                    or property_map[base_property] is None
                ):
                    return {
                        "error": f"Unsupported/invalid base property '{base_property}' for object type."
                    }
                if component not in component_map:
                    return {
                        "error": f"Unsupported component '{component}'. Use x, y, z, r, g, or b."
                    }

                if isinstance(value, list):
                    value = value[0] if value else 0.0

                result = self._set_vector_component_keyframe(
                    obj,
                    frame,
                    property_map[base_property],
                    component_map[component],
                    float(value),
                    base_property,
                    component,
                )
                if not result:
                    return {"error": f"Failed to set {property_type} keyframe"}

            elif property_type in ["position", "rotation", "scale"]:
                # Full vector properties
                property_ids = {
                    "position": c4d.ID_BASEOBJECT_POSITION,
                    "rotation": c4d.ID_BASEOBJECT_ROTATION,
                    "scale": c4d.ID_BASEOBJECT_SCALE,
                }

                if isinstance(value, (int, float)):
                    value = [float(value)] * 3
                elif isinstance(value, list):
                    if len(value) == 1:
                        value = [float(value[0])] * 3
                    elif len(value) == 2:
                        value = [float(value[0]), float(value[1]), 0.0]
                    elif len(value) > 3:
                        value = [float(v) for v in value[:3]]
                    else:
                        value = [float(v) for v in value]
                else:
                    return {
                        "error": f"{property_type.capitalize()} value must be a number or a list [x,y,z]."
                    }

                if len(value) != 3:
                    return {
                        "error": f"{property_type.capitalize()} value must have 3 components."
                    }

                result = self._set_vector_keyframe(
                    obj, frame, property_ids[property_type], value, property_type
                )
                if not result:
                    return {"error": f"Failed to set {property_type} keyframe"}

            elif obj.GetType() == c4d.Olight and property_type in [
                "intensity",
                "color",
            ]:
                if property_type == "intensity":
                    if isinstance(value, list):
                        value = value[0] if value else 0.0
                    result = self._set_scalar_keyframe(
                        obj,
                        frame,
                        c4d.LIGHT_BRIGHTNESS,
                        c4d.DTYPE_REAL,
                        float(value) / 100.0,
                        "intensity",
                    )
                    if not result:
                        return {"error": "Failed to set intensity keyframe"}

                elif property_type == "color":
                    if not isinstance(value, list) or len(value) < 3:
                        return {"error": "Color must be a list [r,g,b]."}
                    result = self._set_vector_keyframe(
                        obj, frame, c4d.LIGHT_COLOR, value[:3], "color"
                    )
                    if not result:
                        return {"error": "Failed to set color keyframe"}

            else:
                return {
                    "error": f"Unsupported property type '{property_type}' for object '{obj.GetName()}'."
                }

            # --- Success ---
            return {
                "keyframe_set": {
                    "object_name": obj.GetName(),
                    "object_guid": str(obj.GetGUID()),
                    "property": property_type,
                    "value_set": value,
                    "frame": frame,
                    "success": True,
                }
            }

        except Exception as e:
            self.log(
                f"[**ERROR**] Error setting keyframe: {str(e)}\n{traceback.format_exc()}"
            )
            return {
                "error": f"Error setting keyframe: {str(e)}",
                "traceback": traceback.format_exc(),
            }

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
        allowed_imports = [
            "c4d",
            "math",
            "random",
            "time",
            "json",
            "os.path",
            "sys",
        ]

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
        """Handle create_mograph_cloner command with context and fixed parameter names."""
        doc = c4d.documents.GetActiveDocument()
        if not doc:
            return {"error": "No active document"}

        requested_name = command.get("cloner_name", "MoGraph Cloner")
        mode = command.get("mode", "grid").lower()
        object_identifier = command.get("object_name", None)

        use_child_guid = False
        clone_child_provided = object_identifier is not None
        if clone_child_provided:
            identifier_str = str(object_identifier)
            if "-" in identifier_str and len(identifier_str) > 30:
                use_child_guid = True
            elif identifier_str.isdigit() or (
                identifier_str.startswith("-") and identifier_str[1:].isdigit()
            ):
                if len(identifier_str) > 10:
                    use_child_guid = True

        # Count Parsing (robust version from previous step)
        default_count = [3, 1, 3] if mode == "grid" else 10
        raw_count = command.get("count", default_count)
        count_vec = None
        count_scalar = None
        reported_count = raw_count
        try:
            if mode == "grid":
                if isinstance(raw_count, list) and len(raw_count) >= 3:
                    count_vec = c4d.Vector(
                        int(raw_count[0]), int(raw_count[1]), int(raw_count[2])
                    )
                    reported_count = [int(c) for c in count_vec]
                elif isinstance(raw_count, (int, float)):
                    count_vec = c4d.Vector(int(raw_count), 1, 1)
                    reported_count = [int(raw_count), 1, 1]
                else:
                    self.log(
                        f"[CLONER] ## Warning ## Invalid count '{raw_count}' for grid. Using defaults."
                    )
                    count_vec = c4d.Vector(3, 1, 3)
                    reported_count = [3, 1, 3]
            elif mode in ["linear", "radial", "object", "spline", "honeycomb"]:
                if isinstance(raw_count, list) and len(raw_count) >= 1:
                    count_scalar = int(raw_count[0])
                    reported_count = count_scalar
                elif isinstance(raw_count, (int, float)):
                    count_scalar = int(raw_count)
                    reported_count = count_scalar
                else:
                    self.log(
                        f"[CLONER] ## Warning ## Invalid count '{raw_count}' for {mode}. Using default 10."
                    )
                    count_scalar = 10
                    reported_count = 10
            else:
                self.log(
                    f"[CLONER] ## Warning ## Unsupported mode '{mode}'. Using default grid."
                )
                mode = "grid"
                count_vec = c4d.Vector(3, 1, 3)
                reported_count = [3, 1, 3]
        except (ValueError, TypeError) as e:
            self.log(
                f"[CLONER] ## Warning ## Error parsing count '{raw_count}': {e}. Using defaults."
            )
            if mode == "grid":
                count_vec = c4d.Vector(3, 1, 3)
                reported_count = [3, 1, 3]
            else:
                count_scalar = 10
                reported_count = 10

        self.log(
            f"[C4D CLONER] Creating: Name='{requested_name}', Mode='{mode}', Count='{reported_count}', Source='{object_identifier}' (GUID: {use_child_guid})"
        )

        clone_obj_target = None
        child_obj_details = {"source": "Default Cube"}
        child_guid = None
        child_actual_name = "Default Cube"
        if clone_child_provided:
            clone_obj_target = self.find_object_by_name(
                doc, object_identifier, use_guid=use_child_guid
            )
            if not clone_obj_target:
                search_type = "GUID" if use_child_guid else "Name"
                return {
                    "error": f"Object '{object_identifier}' (searched by {search_type}) not found to clone."
                }
            else:
                target_name = clone_obj_target.GetName()
                target_guid = str(clone_obj_target.GetGUID())
                child_obj_details["source"] = (
                    f"Existing Object: '{target_name}' (GUID: {target_guid})"
                )
                self.log(
                    f"[CLONER] Found clone object: '{target_name}' (GUID: {target_guid})"
                )

        def create_mograph_cloner_safe(
            doc, name, mode, count, count_vec, found_clone_object
        ):
            nonlocal child_guid, child_actual_name
            try:
                cloner = c4d.BaseObject(c4d.Omgcloner)
                if not cloner:
                    raise RuntimeError("Failed to create Cloner object")
                cloner.SetName(name)

                mode_ids = {
                    "linear": 0,
                    "radial": 2,
                    "grid": 1,
                    "object": 3,
                    "spline": 4,
                    "honeycomb": 5,
                }
                mode_id = mode_ids.get(mode, 1)

                doc.StartUndo()
                doc.InsertObject(cloner)
                doc.AddUndo(c4d.UNDOTYPE_NEW, cloner)
                cloner[c4d.ID_MG_MOTIONGENERATOR_MODE] = mode_id

                if found_clone_object:
                    child_obj = found_clone_object.GetClone()
                else:
                    child_obj = c4d.BaseObject(c4d.Ocube)
                    child_obj.SetName("Default Cube")
                    child_obj.SetAbsScale(c4d.Vector(0.5, 0.5, 0.5))
                if not child_obj:
                    raise RuntimeError("Failed to create/clone child object")

                doc.InsertObject(child_obj)
                doc.AddUndo(c4d.UNDOTYPE_NEW, child_obj)
                child_actual_name = child_obj.GetName()
                child_guid = str(child_obj.GetGUID())
                child_obj.InsertUnderLast(cloner)
                self.register_object_name(
                    child_obj,
                    (
                        found_clone_object.GetName()
                        if found_clone_object
                        else "Default Cube"
                    ),
                )

                mg_bc = cloner.GetDataInstance()
                if not mg_bc:
                    raise RuntimeError("Failed to get MoGraph BaseContainer")

                # ---Use getattr for potentially missing constants ---
                if mode == "linear":
                    mg_bc[c4d.MG_LINEAR_COUNT] = count
                    # Use getattr for MG_LINEAR_PERSTEP, provide default vector if missing
                    perstep_id = getattr(c4d, "MG_LINEAR_PERSTEP", None)
                    mode_id_param = getattr(c4d, "MG_LINEAR_MODE", None)
                    perstep_mode_val = getattr(
                        c4d, "MG_LINEAR_MODE_PERSTEP", 0
                    )  # Default to 0 if missing

                    if perstep_id:
                        mg_bc[perstep_id] = c4d.Vector(0, 50, 0)
                    else:
                        self.log("[CLONER] ## Warning ## MG_LINEAR_PERSTEP not found.")
                    if mode_id_param:
                        mg_bc[mode_id_param] = perstep_mode_val
                    else:
                        self.log("[CLONER] ## Warning ## MG_LINEAR_MODE not found.")
                    self.log(f"[C4D CLONER] Set linear count: {count}")

                elif mode == "grid":
                    version = c4d.GetC4DVersion()
                    try:
                        if version >= 2025000 and hasattr(c4d, "MGGRIDARRAY_MODE"):
                            mg_bc[c4d.MGGRIDARRAY_MODE] = c4d.MGGRIDARRAY_MODE_ENDPOINT
                            mg_bc[c4d.MGGRIDARRAY_RESOLUTION] = count_vec
                            mg_bc[c4d.MGGRIDARRAY_SIZE] = c4d.Vector(200, 200, 200)
                            self.log(
                                f"[C4D CLONER] Using 2025+ MGGRIDARRAY_*; resolution: {count_vec}"
                            )
                        else:
                            if (
                                hasattr(c4d, "MG_GRID_COUNT")
                                and hasattr(c4d, "MG_GRID_MODE")
                                and hasattr(c4d, "MG_GRID_SIZE")
                            ):
                                mg_bc[c4d.MG_GRID_COUNT] = count_vec
                                mg_bc[c4d.MG_GRID_MODE] = c4d.MG_GRID_MODE_PERSTEP
                                mg_bc[c4d.MG_GRID_SIZE] = c4d.Vector(100, 100, 100)
                                self.log(
                                    f"[C4D CLONER] Using legacy MG_GRID_COUNT: {count_vec}, Mode: Per Step"
                                )
                            else:
                                if all(
                                    hasattr(c4d, attr)
                                    for attr in [
                                        "MG_GRID_COUNT_X",
                                        "MG_GRID_COUNT_Y",
                                        "MG_GRID_COUNT_Z",
                                        "MG_CLONER_SIZE",
                                    ]
                                ):
                                    mg_bc[c4d.MG_GRID_COUNT_X] = int(count_vec.x)
                                    mg_bc[c4d.MG_GRID_COUNT_Y] = int(count_vec.y)
                                    mg_bc[c4d.MG_GRID_COUNT_Z] = int(count_vec.z)
                                    mg_bc[c4d.MG_CLONER_SIZE] = c4d.Vector(
                                        200, 200, 200
                                    )
                                    self.log(
                                        f"[C4D CLONER] Using legacy MG_GRID_COUNT_X/Y/Z: {count_vec}, Size: 200"
                                    )
                                else:
                                    self.log(
                                        "[C4D CLONER] ## Warning ##: Could not find suitable grid parameters."
                                    )
                    except Exception as e_grid:
                        self.log(
                            f"[C4D CLONER] ## Warning ## Grid mode config failed: {e_grid}"
                        )
                elif mode == "radial":
                    if hasattr(c4d, "MG_POLY_COUNT") and hasattr(c4d, "MG_POLY_RADIUS"):
                        mg_bc[c4d.MG_POLY_COUNT] = count
                        mg_bc[c4d.MG_POLY_RADIUS] = 200
                        self.log(f"[C4D CLONER] Set radial count: {count}, Radius: 200")
                    else:
                        self.log(
                            "[C4D CLONER] ## Warning ##: Radial parameters not found."
                        )
                elif mode == "object":
                    self.log("[C4D CLONER] Object mode selected, requires linking.")
                    if not hasattr(c4d, "MG_OBJECT_LINK"):
                        self.log(
                            "[C4D CLONER] ## Warning ##: Object link parameter not found."
                        )

                if hasattr(c4d, "MGCLONER_MODE"):
                    cloner[c4d.MGCLONER_MODE] = c4d.MGCLONER_MODE_ITERATE

                doc.EndUndo()
                c4d.EventAdd()

                actual_cloner_name = cloner.GetName()
                cloner_guid = str(cloner.GetGUID())
                pos_vec = cloner.GetAbsPos()
                self.register_object_name(cloner, name)  # Use 'name' (requested name)

                return {
                    "cloner": {
                        "requested_name": name,
                        "actual_name": actual_cloner_name,
                        "guid": cloner_guid,
                        "type": mode,
                        "count_set": reported_count,
                        "position": [pos_vec.x, pos_vec.y, pos_vec.z],
                        "child_object": {
                            "source": child_obj_details["source"],
                            "actual_name": child_actual_name,
                            "guid": child_guid,
                        },
                    }
                }
            except Exception as e:
                doc.EndUndo()
                self.log(
                    f"[**ERROR**] Exception during cloner creation safe wrapper: {str(e)}\n{traceback.format_exc()}"
                )
                return {
                    "error": f"Exception during cloner creation: {str(e)}",
                    "traceback": traceback.format_exc(),
                }

        try:
            self.log("[C4D CLONER] Dispatching cloner creation to main thread")
            result = self.execute_on_main_thread(
                create_mograph_cloner_safe,
                args=(
                    doc,
                    requested_name,
                    mode,
                    count_scalar,
                    count_vec,
                    clone_obj_target,
                ),
                _timeout=30,
            )
            if isinstance(result, dict) and "error" in result:
                self.log(f"[C4D CLONER] Error: {result['error']}")
                return result
            return result
        except Exception as e:
            self.log(
                f"[**ERROR**] Exception in cloner handler dispatch: {str(e)}\n{traceback.format_exc()}"
            )
            return {
                "error": f"Exception dispatching cloner handler: {str(e)}",
                "traceback": traceback.format_exc(),
            }

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
                            modes = {
                                0: "Linear",
                                1: "Grid",
                                2: "Radial",
                                3: "Object",
                            }
                            mode_name = modes.get(mode_id, f"Mode {mode_id}")
                            additional_props["cloner_mode"] = mode_name

                            # Add counts based on mode - using R2025.1 constant paths
                            try:
                                # Try R2025.1 module path first
                                if mode_id == 0:  # Linear
                                    if hasattr(c4d, "MG_LINEAR_COUNT"):
                                        additional_props["count"] = current_obj[
                                            c4d.MG_LINEAR_COUNT
                                        ]
                                elif mode_id == 1:  # Grid
                                    if hasattr(c4d, "MGGRIDARRAY_RESOLUTION"):
                                        resolution = current_obj[
                                            c4d.MGGRIDARRAY_RESOLUTION
                                        ]
                                        additional_props["count_x"] = int(resolution.x)
                                        additional_props["count_y"] = int(resolution.y)
                                        additional_props["count_z"] = int(resolution.z)
                                        # Fallback to legacy MG_GRID_COUNT_* if available
                                    elif all(
                                        hasattr(c4d, attr)
                                        for attr in [
                                            "MG_GRID_COUNT_X",
                                            "MG_GRID_COUNT_Y",
                                            "MG_GRID_COUNT_Z",
                                        ]
                                    ):
                                        additional_props["count_x"] = int(
                                            current_obj[c4d.MG_GRID_COUNT_X]
                                        )
                                        additional_props["count_y"] = int(
                                            current_obj[c4d.MG_GRID_COUNT_Y]
                                        )
                                        additional_props["count_z"] = int(
                                            current_obj[c4d.MG_GRID_COUNT_Z]
                                        )
                                    else:
                                        self.log(
                                            "[C4D CLONER WARNING] No valid grid count parameters found"
                                        )
                                elif mode_id == 2:  # Radial
                                    if hasattr(c4d, "MG_POLY_COUNT"):
                                        additional_props["count"] = current_obj[
                                            c4d.MG_POLY_COUNT
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
        """Adds a MoGraph effector and optionally links it to a cloner, returns context."""
        doc = c4d.documents.GetActiveDocument()
        if not doc:
            return {"error": "No active document"}

        type_name = command.get("effector_type", "random").lower()
        # --- Use 'target' preferentially, fallback to 'cloner_name' ---
        cloner_identifier = command.get("target") or command.get("cloner_name") or ""
        properties = command.get("properties", {})
        requested_name = (
            command.get("name")
            or command.get("effector_name")
            or f"{type_name.capitalize()} Effector"
        )

        # --- Detect if the cloner_identifier looks like a GUID ---
        use_cloner_guid = False
        if cloner_identifier:  # Check only if identifier exists
            identifier_str = str(cloner_identifier)
            if "-" in identifier_str and len(identifier_str) > 30:
                use_cloner_guid = True
            elif identifier_str.isdigit() or (
                identifier_str.startswith("-") and identifier_str[1:].isdigit()
            ):
                if len(identifier_str) > 10:
                    use_cloner_guid = True

        effector = None
        try:
            self.log(
                f"[C4D EFFECTOR] Creating {type_name} effector named '{requested_name}'"
            )
            if cloner_identifier:
                self.log(
                    f"[C4D EFFECTOR] Will attempt to apply to cloner '{cloner_identifier}' (Treat as GUID: {use_cloner_guid})"
                )

            # (Effector type mapping and creation remains the same)
            effector_types = {
                "random": c4d.Omgrandom,
                "formula": c4d.Omgformula,
                "step": c4d.Omgstep,
                "target": getattr(
                    c4d, "Omgtarget", getattr(c4d, "Omgeffectortarget", None)
                ),
                "time": c4d.Omgtime,
                "sound": c4d.Omgsound,
                "plain": c4d.Omgplain,
                "delay": c4d.Omgdelay,
                "spline": c4d.Omgspline,
                "python": c4d.Omgpython,
                "shader": c4d.Omgshader,
                "volume": c4d.Omgvolume,
            }
            if hasattr(c4d, "Omgfalloff"):
                effector_types["falloff"] = c4d.Omgfalloff
            effector_id = effector_types.get(type_name)
            if effector_id is None:
                return {"error": f"Unsupported effector type: {type_name}"}

            doc.StartUndo()
            effector = c4d.BaseObject(effector_id)
            if effector is None:
                raise RuntimeError(f"Failed to create {type_name} effector BaseObject")
            effector.SetName(requested_name)

            # (Property setting remains the same)
            bc = effector.GetDataInstance()
            if bc:
                if "strength" in properties and isinstance(
                    properties["strength"], (int, float)
                ):
                    try:
                        bc[c4d.ID_MG_BASEEFFECTOR_STRENGTH] = (
                            float(properties["strength"]) / 100.0
                        )
                    except Exception as e_prop:
                        self.log(f"Warning: Could not set strength: {e_prop}")
                if "position_mode" in properties and isinstance(
                    properties["position_mode"], bool
                ):
                    try:
                        bc[c4d.ID_MG_BASEEFFECTOR_POSITION_ACTIVE] = properties[
                            "position_mode"
                        ]
                    except Exception as e_prop:
                        self.log(f"Warning: Could not set position_mode: {e_prop}")
                if "rotation_mode" in properties and isinstance(
                    properties["rotation_mode"], bool
                ):
                    try:
                        bc[c4d.ID_MG_BASEEFFECTOR_ROTATION_ACTIVE] = properties[
                            "rotation_mode"
                        ]
                    except Exception as e_prop:
                        self.log(f"Warning: Could not set rotation_mode: {e_prop}")
                if "scale_mode" in properties and isinstance(
                    properties["scale_mode"], bool
                ):
                    try:
                        bc[c4d.ID_MG_BASEEFFECTOR_SCALE_ACTIVE] = properties[
                            "scale_mode"
                        ]
                    except Exception as e_prop:
                        self.log(f"Warning: Could not set scale_mode: {e_prop}")
            else:
                self.log(
                    f"Warning: Could not get BaseContainer for effector '{requested_name}'"
                )

            doc.InsertObject(effector)
            doc.AddUndo(c4d.UNDOTYPE_NEW, effector)

            # --- Linking logic (remains the same, but find_object_by_name call uses correct flag now) ---
            cloner_applied_to_name = "None"
            cloner_applied_to_guid = None
            cloner_found = None

            if cloner_identifier:
                # Pass the use_cloner_guid flag correctly
                cloner_found = self.find_object_by_name(
                    doc, cloner_identifier, use_guid=use_cloner_guid
                )

                if cloner_found is None:
                    search_type = "GUID" if use_cloner_guid else "Name"
                    self.log(
                        f"[C4D EFFECTOR] ## Warning ##: Cloner '{cloner_identifier}' (searched by {search_type}) not found, effector created but not linked."
                    )
                else:
                    if cloner_found.GetType() != c4d.Omgcloner:
                        self.log(
                            f"[C4D EFFECTOR] ## Warning ##: Target '{cloner_found.GetName()}' is not a MoGraph Cloner (Type: {cloner_found.GetType()})"
                        )
                    else:
                        try:
                            effector_list = None
                            try:
                                effector_list = cloner_found[
                                    c4d.ID_MG_MOTIONGENERATOR_EFFECTORLIST
                                ]
                            except:
                                self.log(
                                    f"[C4D EFFECTOR] Creating new effector list for cloner '{cloner_found.GetName()}'"
                                )
                            if not isinstance(effector_list, c4d.InExcludeData):
                                effector_list = c4d.InExcludeData()

                            effector_list.InsertObject(effector, 1)
                            cloner_found[c4d.ID_MG_MOTIONGENERATOR_EFFECTORLIST] = (
                                effector_list
                            )
                            doc.AddUndo(c4d.UNDOTYPE_CHANGE, cloner_found)
                            cloner_applied_to_name = cloner_found.GetName()
                            cloner_applied_to_guid = str(cloner_found.GetGUID())
                            self.log(
                                f"[C4D EFFECTOR] Successfully applied effector to cloner '{cloner_applied_to_name}'"
                            )
                        except Exception as e_apply:
                            self.log(
                                f"[**ERROR**] Error applying effector to cloner '{cloner_found.GetName()}': {str(e_apply)}"
                            )

            doc.EndUndo()
            c4d.EventAdd()

            # --- Contextual Return (remains the same) ---
            actual_effector_name = effector.GetName()
            effector_guid = str(effector.GetGUID())
            pos_vec = effector.GetAbsPos()

            self.register_object_name(effector, requested_name)

            return {
                "effector": {
                    "requested_name": requested_name,
                    "actual_name": actual_effector_name,
                    "guid": effector_guid,
                    "type": type_name,
                    "position": [pos_vec.x, pos_vec.y, pos_vec.z],
                    "applied_to_cloner_name": cloner_applied_to_name,
                    "applied_to_cloner_guid": cloner_applied_to_guid,
                }
            }

        except Exception as e:
            doc.EndUndo()
            self.log(
                f"[**ERROR**] Error creating effector: {str(e)}\n{traceback.format_exc()}"
            )
            if effector and not effector.GetDocument():
                try:
                    effector.Remove()
                except:
                    pass
            return {
                "error": f"Failed to create effector: {str(e)}",
                "traceback": traceback.format_exc(),
            }

    def handle_apply_mograph_fields(self, command):
        """Applies a MoGraph field (as a child) to a MoGraph effector, returns context."""
        doc = c4d.documents.GetActiveDocument()
        if not doc:
            return {"error": "No active document"}

        field_type = command.get("field_type", "spherical").lower()
        requested_name = command.get("field_name", f"{field_type.capitalize()} Field")
        target_identifier = command.get("target_name", "")
        parameters = command.get("parameters", {})

        # Detect if target_identifier is likely a GUID
        use_target_guid = False
        if target_identifier:
            identifier_str = str(target_identifier)
            if "-" in identifier_str and len(identifier_str) > 30:
                use_target_guid = True
            elif identifier_str.isdigit() or (
                identifier_str.startswith("-") and identifier_str[1:].isdigit()
            ):
                if len(identifier_str) > 10:
                    use_target_guid = True

        field = None
        try:
            self.log(
                f"[C4D FIELDS] Request: Field='{requested_name}' Type='{field_type}' Target='{target_identifier}' (Treat as GUID: {use_target_guid})"
            )

            target = self.find_object_by_name(
                doc, target_identifier, use_guid=use_target_guid
            )
            if not target:
                search_type = "GUID" if use_target_guid else "Name"
                return {
                    "error": f"Target effector '{target_identifier}' (searched by {search_type}) not found"
                }

            valid_effector_types = {
                c4d.Omgplain,
                c4d.Omgrandom,
                c4d.Omgstep,
                c4d.Omgdelay,
                c4d.Omgformula,
                c4d.Omgtime,
                c4d.Omgsound,
                c4d.Omgpython,
                c4d.Omgshader,
                c4d.Omgvolume,
                getattr(c4d, "Omgtarget", getattr(c4d, "Omgeffectortarget", None)),
            }
            if target.GetType() not in valid_effector_types:
                return {
                    "error": f"Target '{target.GetName()}' is not a supported effector type (Type: {target.GetType()})"
                }

            target_name = target.GetName()
            target_guid = str(target.GetGUID())

            field_type_map = {
                "spherical": getattr(c4d, "Fspherical", 440000243),
                "box": getattr(c4d, "Fbox", 440000244),
                "radial": getattr(c4d, "Fradial", 440000245),
                "linear": getattr(c4d, "Flinear", 440000246),
                "noise": 440000248,
                "cylinder": getattr(c4d, "Fcylinder", 1039386),
                "cone": getattr(c4d, "Fcone", 1039388),
                "torus": getattr(c4d, "Ftorus", 1039387),
                "formula": getattr(c4d, "Fformula", 1040830),
                "random": getattr(c4d, "Frandom", 1040831),
                "step": getattr(c4d, "Fstep", 1040832),
            }
            field_type_id = field_type_map.get(field_type)
            if not field_type_id:
                return {"error": f"Unsupported field type: '{field_type}'"}

            doc.StartUndo()
            field = c4d.BaseObject(field_type_id)
            if not field:
                raise RuntimeError("Failed to create field BaseObject")
            field.SetName(requested_name)

            bc = field.GetDataInstance()
            if bc:
                if (
                    "position" in parameters
                    and isinstance(parameters["position"], list)
                    and len(parameters["position"]) >= 3
                ):
                    try:
                        field.SetAbsPos(
                            c4d.Vector(*[float(p) for p in parameters["position"][:3]])
                        )
                    except (ValueError, TypeError):
                        self.log(
                            f"Warning: Invalid field position {parameters['position']}"
                        )
                if (
                    "scale" in parameters
                    and isinstance(parameters["scale"], list)
                    and len(parameters["scale"]) >= 3
                ):
                    try:
                        field.SetAbsScale(
                            c4d.Vector(*[float(p) for p in parameters["scale"][:3]])
                        )
                    except (ValueError, TypeError):
                        self.log(f"Warning: Invalid field scale {parameters['scale']}")
                if (
                    "rotation" in parameters
                    and isinstance(parameters["rotation"], list)
                    and len(parameters["rotation"]) >= 3
                ):
                    try:
                        hpb_rad = [
                            c4d.utils.DegToRad(float(angle))
                            for angle in parameters["rotation"][:3]
                        ]
                        field.SetAbsRot(c4d.Vector(*hpb_rad))
                    except (ValueError, TypeError):
                        self.log(
                            f"Warning: Invalid field rotation {parameters['rotation']}"
                        )
                if field_type == "spherical" and "radius" in parameters:
                    radius_id = getattr(
                        c4d, "FIELD_SIZE", getattr(c4d, "FIELDSPHERICAL_RADIUS", None)
                    )
                    if radius_id:
                        try:
                            bc[radius_id] = float(parameters["radius"])
                        except (ValueError, TypeError):
                            self.log(
                                f"Warning: Invalid radius value {parameters['radius']}"
                            )
                    else:
                        self.log(
                            "Warning: Could not find radius parameter ID for spherical field."
                        )
            else:
                self.log(
                    f"Warning: Could not get BaseContainer for field '{requested_name}'"
                )

            doc.InsertObject(field)
            doc.AddUndo(c4d.UNDOTYPE_NEW, field)
            field.InsertUnder(target)
            doc.AddUndo(c4d.UNDOTYPE_CHANGE, target)
            doc.EndUndo()
            c4d.EventAdd()
            self.log(
                f"[C4D FIELDS] Linked field '{field.GetName()}' to effector '{target_name}'"
            )

            actual_field_name = field.GetName()
            field_guid = str(field.GetGUID())
            pos_vec = field.GetAbsPos()
            self.register_object_name(field, requested_name)

            return {
                "field": {
                    "requested_name": requested_name,
                    "actual_name": actual_field_name,
                    "guid": field_guid,
                    "type": field_type,
                    "target_name": target_name,
                    "target_guid": target_guid,
                    "position": [pos_vec.x, pos_vec.y, pos_vec.z],
                }
            }

        except Exception as e:
            doc.EndUndo()
            self.log(f"[**ERROR**] Error applying field: {e}\n{traceback.format_exc()}")
            if field and not field.GetDocument():
                try:
                    field.Remove()
                except:
                    pass
            return {
                "error": f"Exception occurred applying field: {str(e)}",
                "traceback": traceback.format_exc(),
            }

    def handle_create_soft_body(self, command):
        """Handle create_soft_body command with GUID support."""
        doc = c4d.documents.GetActiveDocument()
        if not doc:
            return {"error": "No active document"}

        # --- MODIFIED: Identify target object ---
        identifier = None
        use_guid = False
        if command.get("guid"):
            identifier = command.get("guid")
            use_guid = True
            self.log(f"[SOFT BODY] Using GUID identifier: '{identifier}'")
        elif command.get("object_name"):
            identifier = command.get("object_name")
            use_guid = False
            self.log(f"[SOFT BODY] Using Name identifier: '{identifier}'")
        else:
            return {"error": "No object identifier ('guid' or 'object_name') provided."}

        # Find target object using the determined method
        obj = self.find_object_by_name(doc, identifier, use_guid=use_guid)
        if obj is None:
            search_type = "GUID" if use_guid else "Name"
            return {
                "error": f"Object '{identifier}' (searched by {search_type}) not found for soft body."
            }

        # Get parameters (using original logic)
        name = command.get(
            "name", f"{obj.GetName()} Soft Body"
        )  # Default name based on object
        stiffness = command.get("stiffness", 50)
        mass = command.get("mass", 1.0)

        # Define safe wrapper (using original logic, but noting potential RIGID_BODY_SOFTBODY issue)
        def create_soft_body_safe(
            target_obj, tag_name, stiff_val, mass_val, obj_actual_name
        ):
            self.log(
                f"[C4D SBODY] Creating soft body dynamics tag '{tag_name}' for object '{obj_actual_name}'"
            )
            dynamics_tag_id = 180000102  # Standard Dynamics Body Tag ID

            tag = c4d.BaseTag(dynamics_tag_id)
            if tag is None:
                self.log(
                    f"[C4D SBODY] Error: Failed to create Dynamics Body tag with ID {dynamics_tag_id}"
                )
                raise RuntimeError("Failed to create Dynamics Body tag")

            tag.SetName(tag_name)
            self.log(f"[C4D SBODY] Successfully created dynamics tag: {tag_name}")

            # --- Potential Issue Area ---
            # RIGID_BODY_SOFTBODY might be deprecated in newer C4D versions.
            # This might need adjustment based on testing with the target C4D version.
            # A more modern approach uses RIGID_BODY_TYPE = 2
            try:
                # Try modern approach first
                if hasattr(c4d, "RIGID_BODY_TYPE"):
                    tag[c4d.RIGID_BODY_TYPE] = getattr(
                        c4d, "RIGID_BODY_TYPE_SOFTBODY", 2
                    )  # Use constant or fallback value 2
                    self.log(
                        f"[C4D SBODY] Set RIGID_BODY_TYPE to Soft Body ({tag[c4d.RIGID_BODY_TYPE]})"
                    )
                elif hasattr(c4d, "RIGID_BODY_SOFTBODY"):
                    # Fallback to older attribute if modern one doesn't exist
                    tag[c4d.RIGID_BODY_SOFTBODY] = True
                    self.log("[C4D SBODY] Set RIGID_BODY_SOFTBODY to True (legacy)")
                else:
                    self.log(
                        "[C4D SBODY] ## Warning ##: Cannot find suitable parameter to enable Soft Body mode."
                    )

                # Common properties (assuming these IDs are stable)
                tag[c4d.RIGID_BODY_DYNAMIC] = 1  # Enable dynamics
                tag[c4d.RIGID_BODY_MASS] = float(mass_val)

                # Stiffness might also have changed ID, add check
                softbody_stiffness_id = getattr(
                    c4d, "RIGID_BODY_SOFTBODY_STIFFNESS", 1110
                )  # Example ID 1110
                if tag.HasParameter(softbody_stiffness_id):
                    tag[softbody_stiffness_id] = (
                        float(stiff_val) / 100.0
                    )  # Assume 0-100 input
                    self.log(
                        f"[C4D SBODY] Set stiffness parameter ID {softbody_stiffness_id}"
                    )
                else:
                    self.log(
                        f"[C4D SBODY] ## Warning ##: Stiffness parameter ID {softbody_stiffness_id} not found."
                    )

            except AttributeError as ae:
                self.log(
                    f"[**ERROR**] Missing Dynamics attribute: {ae}. Dynamics setup might be incomplete."
                )
                # Don't raise, just log, tag might still be useful partially
            except Exception as e_tag:
                self.log(f"[**ERROR**] Error setting dynamics parameters: {e_tag}")
                # Don't raise, try inserting tag anyway

            target_obj.InsertTag(tag)
            doc.AddUndo(c4d.UNDOTYPE_NEW, tag)
            c4d.EventAdd()

            # Return context
            return {
                "object_name": obj_actual_name,
                "object_guid": str(target_obj.GetGUID()),  # Added GUID
                "tag_name": tag.GetName(),
                "stiffness_set": float(stiff_val),  # Report value requested
                "mass_set": float(mass_val),  # Report value requested
            }

        # Execute on main thread
        try:
            result = self.execute_on_main_thread(
                create_soft_body_safe,
                args=(obj, name, stiffness, mass, obj.GetName()),
            )
            # Check result structure from execute_on_main_thread
            if isinstance(result, dict) and "error" in result:
                return result  # Propagate error
            elif isinstance(result, dict) and result.get("status") == "completed_none":
                return {
                    "error": "Soft body creation function returned None unexpectedly."
                }
            else:
                return {"soft_body": result}  # Wrap successful result

        except Exception as e:
            # Catch errors related to execute_on_main_thread itself
            self.log(
                f"[**ERROR**] Failed to execute soft body creation via main thread: {e}\n{traceback.format_exc()}"
            )
            return {"error": f"Failed to queue/execute Soft Body creation: {str(e)}"}

    def handle_apply_dynamics(self, command):
        """Handle apply_dynamics command with GUID support."""
        doc = c4d.documents.GetActiveDocument()
        if not doc:
            return {"error": "No active document"}

        # --- MODIFIED: Identify target object ---
        identifier = None
        use_guid = False
        if command.get("guid"):
            identifier = command.get("guid")
            use_guid = True
            self.log(f"[DYNAMICS] Using GUID identifier: '{identifier}'")
        elif command.get("object_name"):
            identifier = command.get("object_name")
            use_guid = False
            self.log(f"[DYNAMICS] Using Name identifier: '{identifier}'")
        else:
            return {"error": "No object identifier ('guid' or 'object_name') provided."}

        # Find target object using the determined method
        obj = self.find_object_by_name(doc, identifier, use_guid=use_guid)
        if obj is None:
            search_type = "GUID" if use_guid else "Name"
            return {
                "error": f"Object '{identifier}' (searched by {search_type}) not found for dynamics."
            }

        tag_type = command.get("tag_type", "rigid_body").lower()
        params = command.get("parameters", {})
        tag_name = command.get(
            "tag_name", f"{obj.GetName()} {tag_type.replace('_',' ').title()}"
        )  # Default name

        try:
            # Use Tdynamicsbody if available, fallback to old ID
            dynamics_tag_id = getattr(c4d, "Tdynamicsbody", 180000102)
            self.log(
                f"[DYNAMICS] Using Dynamics Tag ID: {dynamics_tag_id} for type '{tag_type}'"
            )

            doc.StartUndo()  # Start undo block
            tag = obj.MakeTag(dynamics_tag_id)  # Use MakeTag for safer insertion
            if tag is None:
                raise RuntimeError(
                    f"Failed to create Dynamics tag (ID: {dynamics_tag_id}) on '{obj.GetName()}'"
                )

            tag.SetName(tag_name)
            bc = tag.GetDataInstance()
            if not bc:
                raise RuntimeError("Failed to get BaseContainer for dynamics tag")

            # Map tag_type string to RIGID_BODY_TYPE enum value
            type_map = {
                "rigid_body": getattr(c4d, "RIGID_BODY_TYPE_RIGIDBODY", 1),  # Usually 1
                "collider": getattr(c4d, "RIGID_BODY_TYPE_COLLIDER", 0),  # Usually 0
                "ghost": getattr(c4d, "RIGID_BODY_TYPE_GHOST", 3),  # Usually 3
                "soft_body": getattr(c4d, "RIGID_BODY_TYPE_SOFTBODY", 2),  # Usually 2
            }
            dynamics_type = type_map.get(tag_type)
            if dynamics_type is None:
                self.log(
                    f"Warning: Unknown dynamics tag_type '{tag_type}'. Defaulting to Collider."
                )
                dynamics_type = type_map["collider"]

            # Set dynamics type and enable
            bc[c4d.RIGID_BODY_TYPE] = dynamics_type
            bc[c4d.RIGID_BODY_ENABLED] = True

            # Apply common parameters from the 'params' dictionary safely
            if "mass" in params:
                try:
                    bc[c4d.RIGID_BODY_MASS_TYPE] = getattr(
                        c4d, "RIGID_BODY_MASS_TYPE_CUSTOM", 1
                    )
                    bc[c4d.RIGID_BODY_MASS_CUSTOM] = float(params["mass"])
                except (ValueError, TypeError, AttributeError) as e:
                    self.log(
                        f"Warning: Invalid/unsupported mass value '{params['mass']}': {e}"
                    )
            if "friction" in params:
                try:
                    bc[c4d.RIGID_BODY_FRICTION] = float(params["friction"])
                except (ValueError, TypeError, AttributeError) as e:
                    self.log(
                        f"Warning: Invalid/unsupported friction value '{params['friction']}': {e}"
                    )
            # Use BOUNCE as it's the more common ID name than ELASTICITY
            if "bounce" in params or "elasticity" in params:
                bounce_val = params.get(
                    "bounce", params.get("elasticity")
                )  # Accept either key
                try:
                    bc[c4d.RIGID_BODY_BOUNCE] = float(bounce_val)
                except (ValueError, TypeError, AttributeError) as e:
                    self.log(
                        f"Warning: Invalid/unsupported bounce/elasticity value '{bounce_val}': {e}"
                    )
            if "collision_shape" in params:
                shape_map = {
                    "auto": getattr(c4d, "RIGID_BODY_SHAPE_AUTO", 0),
                    "box": getattr(c4d, "RIGID_BODY_SHAPE_BOX", 1),
                    "sphere": getattr(c4d, "RIGID_BODY_SHAPE_SPHERE", 2),
                    "capsule": getattr(c4d, "RIGID_BODY_SHAPE_CAPSULE", 3),
                    "cylinder": getattr(c4d, "RIGID_BODY_SHAPE_CYLINDER", 4),
                    "cone": getattr(c4d, "RIGID_BODY_SHAPE_CONE", 5),
                    "static_mesh": getattr(c4d, "RIGID_BODY_SHAPE_STATICMESH", 7),
                    "moving_mesh": getattr(c4d, "RIGID_BODY_SHAPE_MOVINGMESH", 8),
                }
                shape_val = shape_map.get(str(params["collision_shape"]).lower())
                if shape_val is not None:
                    try:
                        bc[c4d.RIGID_BODY_SHAPE] = shape_val
                    except AttributeError as e:
                        self.log(f"Warning: Collision shape parameter not found: {e}")
                else:
                    self.log(
                        f"Warning: Invalid collision_shape value '{params['collision_shape']}'"
                    )
            else:  # Default collision shape if not specified
                try:
                    bc[c4d.RIGID_BODY_SHAPE] = getattr(c4d, "RIGID_BODY_SHAPE_AUTO", 0)
                except AttributeError as e:
                    self.log(
                        f"Warning: Default collision shape parameter not found: {e}"
                    )

            # No need for obj.InsertTag(tag) because MakeTag already inserts it
            doc.AddUndo(c4d.UNDOTYPE_NEW, tag)  # Add undo for the new tag
            doc.EndUndo()  # End undo block
            c4d.EventAdd()

            # Contextual Return
            return {
                "dynamics": {
                    "object_name": obj.GetName(),
                    "object_guid": str(obj.GetGUID()),
                    "tag_name": tag.GetName(),
                    "tag_type_applied": tag_type,
                    "parameters_received": params,  # Echo back received params for verification
                }
            }

        except Exception as e:
            doc.EndUndo()  # Ensure undo is ended on error
            self.log(
                f"[**ERROR**] Error applying dynamics: {e}\n{traceback.format_exc()}"
            )
            return {
                "error": f"Failed to apply Dynamics tag: {str(e)}",
                "traceback": traceback.format_exc(),
            }

    def handle_create_abstract_shape(self, command):
        """Handle create_abstract_shape command with context and C4D 2025 compatibility."""
        doc = c4d.documents.GetActiveDocument()
        if not doc:
            return {"error": "No active document"}

        shape_type = command.get("shape_type", "metaball").lower()
        # Accept both "name" and "object_name"
        requested_name = command.get("name") or command.get(
            "object_name", f"{shape_type.capitalize()}"
        )
        position_list = command.get("position", [0, 0, 0])

        # Safely parse position
        position = [0.0, 0.0, 0.0]
        if isinstance(position_list, list) and len(position_list) >= 3:
            try:
                position = [float(p) for p in position_list[:3]]
            except (ValueError, TypeError):
                self.log(f"Warning: Invalid position data {position_list}")

        self.log(
            f"[C4D ABSTRCTSHAPE] Creating abstract shape '{shape_type}' with requested name: '{requested_name}'"
        )

        shape = None  # Initialize shape variable
        try:
            shape_types = {
                "metaball": 5125,
                "blob": 5119,
                "loft": 5107,
                "sweep": 5118,
                "atom": 5168,
                "platonic": 5170,
                "cloth": 5186,
                "landscape": 5119,
                "extrude": 5116,
            }
            shape_type_id = shape_types.get(shape_type, shape_types["metaball"])
            self.log(
                f"[C4D ABSTRCTSHAPE] Creating abstract shape of type: {shape_type} (ID: {shape_type_id})"
            )

            doc.StartUndo()  # Start undo block
            shape = c4d.BaseObject(shape_type_id)
            if shape is None:
                raise RuntimeError(f"Failed to create {shape_type} object")

            shape.SetName(requested_name)
            shape.SetAbsPos(c4d.Vector(*position))

            child_objects_context = {}  # Store context for children

            # Add children based on type (using original logic)
            if shape_type in ["metaball", "blob"]:
                self.log(f"[C4D ABSTRCTSHAPE] Creating child sphere for {shape_type}")
                sphere = c4d.BaseObject(c4d.Osphere)
                if sphere:
                    child_req_name = (
                        f"{requested_name}_Sphere"  # Use requested name of parent
                    )
                    sphere.SetName(child_req_name)
                    sphere.SetAbsScale(c4d.Vector(2.0, 2.0, 2.0))  # Use floats
                    bc = sphere.GetDataInstance()
                    if bc:
                        bc.SetFloat(c4d.PRIM_SPHERE_RAD, 50.0)  # Use floats
                    sphere.InsertUnder(shape)
                    doc.AddUndo(c4d.UNDOTYPE_NEW, sphere)
                    # Add child context
                    child_actual_name = sphere.GetName()
                    child_guid = str(sphere.GetGUID())
                    child_objects_context["sphere"] = {
                        "requested_name": child_req_name,
                        "actual_name": child_actual_name,
                        "guid": child_guid,
                    }
                    self.register_object_name(sphere, child_req_name)  # Register child
                else:
                    self.log(f"Warning: Failed to create child sphere for {shape_type}")

            elif shape_type in ("loft", "sweep"):
                self.log(
                    f"[C4D ABSTRCTSHAPE] Creating profile and path splines for {shape_type}"
                )
                spline = c4d.BaseObject(c4d.Osplinecircle)
                path = c4d.BaseObject(c4d.Osplinenside)

                if spline:
                    child_req_name = f"{requested_name}_Profile"
                    spline.SetName(child_req_name)
                    spline.InsertUnder(shape)
                    doc.AddUndo(c4d.UNDOTYPE_NEW, spline)
                    child_actual_name = spline.GetName()
                    child_guid = str(spline.GetGUID())
                    child_objects_context["profile"] = {
                        "requested_name": child_req_name,
                        "actual_name": child_actual_name,
                        "guid": child_guid,
                    }
                    self.register_object_name(spline, child_req_name)
                else:
                    self.log("Warning: Failed to create profile spline")

                if path:
                    child_req_name = f"{requested_name}_Path"
                    path.SetName(child_req_name)
                    path.SetAbsPos(c4d.Vector(0, 50, 0))
                    path.InsertUnder(shape)
                    doc.AddUndo(c4d.UNDOTYPE_NEW, path)
                    child_actual_name = path.GetName()
                    child_guid = str(path.GetGUID())
                    child_objects_context["path"] = {
                        "requested_name": child_req_name,
                        "actual_name": child_actual_name,
                        "guid": child_guid,
                    }
                    self.register_object_name(path, child_req_name)
                else:
                    self.log("Warning: Failed to create path spline")

            # Insert the main shape object
            doc.InsertObject(shape)
            doc.AddUndo(c4d.UNDOTYPE_NEW, shape)
            doc.EndUndo()  # End undo block
            c4d.EventAdd()

            # --- MODIFIED: Contextual Return ---
            actual_name = shape.GetName()
            guid = str(shape.GetGUID())
            pos_vec = shape.GetAbsPos()
            shape_type_name = self.get_object_type_name(shape)  # Get user friendly name

            # Register the main shape object
            self.register_object_name(shape, requested_name)

            return {
                "shape": {
                    "requested_name": requested_name,
                    "actual_name": actual_name,
                    "guid": guid,
                    "type": shape_type_name,  # User friendly type
                    "type_id": shape.GetType(),  # C4D ID
                    "position": [pos_vec.x, pos_vec.y, pos_vec.z],
                    "child_objects": child_objects_context,  # Include context of children
                }
            }

        except Exception as e:
            doc.EndUndo()  # Ensure undo is ended on error
            self.log(
                f"[**ERROR**] Error creating abstract shape '{requested_name}': {str(e)}\n{traceback.format_exc()}"
            )
            # Clean up shape if created but not inserted
            if shape and not shape.GetDocument():
                try:
                    shape.Remove()
                except:
                    pass
            return {
                "error": f"Failed to create abstract shape: {str(e)}",
                "traceback": traceback.format_exc(),
            }

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
        """Light creation with context and EXACT 2025.0 SDK parameters"""
        doc = c4d.documents.GetActiveDocument()
        if not doc:
            return {"error": "No active document"}

        light_type = command.get("type", "spot").lower()
        # Use requested name or generate one
        requested_name = (
            command.get("name")
            or command.get("object_name")
            or f"MCP_{light_type.capitalize()}Light_{int(time.time()) % 1000}"
        )
        # Handle test harness name if provided
        if not requested_name and command.get("from_test_harness"):
            requested_name = "Test_Light"

        position_list = command.get("position", [0, 100, 0])
        color_list = command.get("color", [1, 1, 1])
        intensity = command.get("intensity", 100)
        temperature = command.get("temperature", 6500)
        width = command.get("width", 200)
        height = command.get("height", 200)

        LIGHT_TYPE_MAP = {"point": 0, "spot": 1, "area": 8, "infinite": 3}
        if light_type not in LIGHT_TYPE_MAP:
            valid_types = ", ".join(LIGHT_TYPE_MAP.keys())
            return {
                "error": f"Invalid light type: '{light_type}'. Valid: {valid_types}"
            }

        light = None  # Initialize light variable
        try:
            doc.StartUndo()  # Start undo block
            light = c4d.BaseObject(c4d.Olight)
            if not light:
                raise RuntimeError("Light creation failed")

            light_code = LIGHT_TYPE_MAP[light_type]
            light[c4d.LIGHT_TYPE] = light_code
            light.SetName(requested_name)
            self.log(
                f"[C4D LIGHT] Set requested name '{requested_name}' before insertion."
            )  # Log name set

            # Safely set position, color, brightness
            try:
                light.SetAbsPos(c4d.Vector(*[float(x) for x in position_list[:3]]))
            except (ValueError, TypeError):
                self.log(f"Warning: Invalid light position {position_list}")
            try:
                light[c4d.LIGHT_COLOR] = c4d.Vector(
                    *[max(0.0, min(1.0, float(c))) for c in color_list[:3]]
                )  # Clamp color 0-1
            except (ValueError, TypeError):
                self.log(f"Warning: Invalid light color {color_list}")
            try:
                light[c4d.LIGHT_BRIGHTNESS] = max(
                    0.0, float(intensity) / 100.0
                )  # Clamp brightness >= 0
            except (ValueError, TypeError):
                self.log(f"Warning: Invalid light intensity {intensity}")

            # Temperature handling
            if hasattr(c4d, "LIGHT_TEMPERATURE"):
                try:
                    light[c4d.LIGHT_TEMPERATURE] = int(float(temperature))
                except (TypeError, ValueError):
                    self.log(f"Warning: Invalid temperature '{temperature}'")

            # Area light parameters
            if light_code == 8:  # Area light
                try:
                    light[c4d.LIGHT_AREADETAILS_SIZEX] = max(
                        0.0, float(width)
                    )  # Ensure non-negative
                except (ValueError, TypeError):
                    self.log(f"Warning: Invalid area light width {width}")
                try:
                    light[c4d.LIGHT_AREADETAILS_SIZEY] = max(
                        0.0, float(height)
                    )  # Ensure non-negative
                except (ValueError, TypeError):
                    self.log(f"Warning: Invalid area light height {height}")
                try:
                    light[c4d.LIGHT_AREADETAILS_SHAPE] = 0  # Rectangle
                except AttributeError:
                    pass  # Ignore if param doesn't exist

            # Shadow parameters
            if hasattr(c4d, "LIGHT_SHADOWTYPE"):
                try:
                    light[c4d.LIGHT_SHADOWTYPE] = 1  # Soft shadows
                except AttributeError:
                    pass  # Ignore if param doesn't exist

            doc.InsertObject(light)
            doc.AddUndo(c4d.UNDOTYPE_NEW, light)  # Add undo for new light
            doc.EndUndo()  # End undo block
            c4d.EventAdd()

            # --- MODIFIED: Contextual Return ---
            actual_name = light.GetName()
            guid = str(light.GetGUID())
            pos_vec = light.GetAbsPos()
            light_type_name = self.get_object_type_name(light)  # Get user friendly name

            # Register the light object
            self.register_object_name(light, requested_name)

            return {
                "light": {  # Changed key from 'object' to 'light' for clarity
                    "requested_name": requested_name,
                    "actual_name": actual_name,
                    "guid": guid,
                    "type": light_type_name,  # User friendly type name
                    "type_id": light.GetType(),  # C4D ID
                    "position": [pos_vec.x, pos_vec.y, pos_vec.z],
                    # Optionally return other set properties for context
                    "color_set": [
                        light[c4d.LIGHT_COLOR].x,
                        light[c4d.LIGHT_COLOR].y,
                        light[c4d.LIGHT_COLOR].z,
                    ],
                    "intensity_set": light[c4d.LIGHT_BRIGHTNESS] * 100.0,
                }
            }

        except Exception as e:
            doc.EndUndo()  # Ensure undo is ended on error
            self.log(
                f"[**ERROR**] Error creating light '{requested_name}': {str(e)}\n{traceback.format_exc()}"
            )
            # Clean up light if created but not inserted
            if light and not light.GetDocument():
                try:
                    light.Remove()
                except:
                    pass
            return {
                "error": f"Light creation failed: {str(e)}",
                "traceback": traceback.format_exc(),
            }

    def handle_create_camera(self, command):
        """Create a new camera in the scene with context and optional properties."""
        doc = c4d.documents.GetActiveDocument()
        if not doc:
            return {"error": "No active document"}

        requested_name = command.get("name", "Camera")
        position_list = command.get("position", [0, 0, 0])
        properties = command.get("properties", {})

        # Safely parse position
        position = [0.0, 0.0, 0.0]
        if isinstance(position_list, list) and len(position_list) >= 3:
            try:
                position = [float(p) for p in position_list[:3]]
            except (ValueError, TypeError):
                self.log(f"Warning: Invalid camera position data {position_list}")

        camera = None  # Initialize camera variable
        try:
            doc.StartUndo()  # Start undo block
            camera = c4d.BaseObject(c4d.Ocamera)
            if not camera:
                raise RuntimeError("Failed to create camera object")

            camera.SetName(requested_name)
            camera.SetAbsPos(c4d.Vector(*position))

            # Apply camera-specific properties safely
            applied_properties = {}
            bc = camera.GetDataInstance()
            if bc:
                if "focal_length" in properties:
                    try:
                        val = float(properties["focal_length"])
                        # Use CAMERAOBJECT_FOCUS which is often the correct one, fallback to CAMERA_FOCUS
                        focus_id = getattr(c4d, "CAMERAOBJECT_FOCUS", c4d.CAMERA_FOCUS)
                        bc[focus_id] = val
                        applied_properties["focal_length"] = val
                    except (ValueError, TypeError, AttributeError) as e:
                        self.log(
                            f"Warning: Failed to set focal_length '{properties['focal_length']}': {e}"
                        )
                if "aperture" in properties:
                    try:
                        val = float(properties["aperture"])
                        bc[c4d.CAMERAOBJECT_APERTURE] = val  # Use CAMERAOBJECT_APERTURE
                        applied_properties["aperture"] = val
                    except (ValueError, TypeError, AttributeError) as e:
                        self.log(
                            f"Warning: Failed to set aperture '{properties['aperture']}': {e}"
                        )
                if "film_offset_x" in properties:
                    try:
                        val = float(properties["film_offset_x"])
                        bc[c4d.CAMERAOBJECT_FILM_OFFSET_X] = (
                            val  # Use CAMERAOBJECT_FILM_OFFSET_X
                        )
                        applied_properties["film_offset_x"] = val
                    except (ValueError, TypeError, AttributeError) as e:
                        self.log(
                            f"Warning: Failed to set film_offset_x '{properties['film_offset_x']}': {e}"
                        )
                if "film_offset_y" in properties:
                    try:
                        val = float(properties["film_offset_y"])
                        bc[c4d.CAMERAOBJECT_FILM_OFFSET_Y] = (
                            val  # Use CAMERAOBJECT_FILM_OFFSET_Y
                        )
                        applied_properties["film_offset_y"] = val
                    except (ValueError, TypeError, AttributeError) as e:
                        self.log(
                            f"Warning: Failed to set film_offset_y '{properties['film_offset_y']}': {e}"
                        )
            else:
                self.log(
                    f"Warning: Could not get BaseContainer for camera '{requested_name}'"
                )

            # Insert into the scene
            doc.InsertObject(camera)
            doc.AddUndo(c4d.UNDOTYPE_NEW, camera)
            doc.SetActiveObject(camera)  # Make active by default
            doc.EndUndo()  # End undo block
            c4d.EventAdd()

            self.log(f"[C4D] Created camera '{camera.GetName()}' at {position}")

            # --- MODIFIED: Contextual Return ---
            actual_name = camera.GetName()
            guid = str(camera.GetGUID())
            pos_vec = camera.GetAbsPos()
            camera_type_name = self.get_object_type_name(camera)

            # Register the camera object
            self.register_object_name(camera, requested_name)

            return {
                "camera": {
                    "requested_name": requested_name,
                    "actual_name": actual_name,
                    "guid": guid,
                    "type": camera_type_name,
                    "type_id": camera.GetType(),
                    "position": [pos_vec.x, pos_vec.y, pos_vec.z],
                    "properties_applied": applied_properties,  # Show which optional props were set
                }
            }

        except Exception as e:
            doc.EndUndo()  # Ensure undo ended
            self.log(
                f"[**ERROR**] Error creating camera '{requested_name}': {str(e)}\n{traceback.format_exc()}"
            )
            # Clean up camera if created but not inserted
            if camera and not camera.GetDocument():
                try:
                    camera.Remove()
                except:
                    pass
            return {
                "error": f"Failed to create camera: {str(e)}",
                "traceback": traceback.format_exc(),
            }

    def handle_animate_camera(self, command):
        """Handle animate_camera command with context."""
        doc = c4d.documents.GetActiveDocument()
        if not doc:
            return {"error": "No active document"}

        # Identify target camera ---
        identifier = None
        use_guid = False
        if command.get("guid"):  # Check for GUID first
            identifier = command.get("guid")
            use_guid = True
            self.log(f"[ANIM CAM] Using GUID identifier: '{identifier}'")
        elif command.get("camera_name"):
            identifier = command.get("camera_name")
            use_guid = False
            self.log(f"[ANIM CAM] Using Name identifier: '{identifier}'")

        path_type = command.get("path_type", "linear").lower()
        positions = command.get("positions", [])
        frames = command.get("frames", [])
        create_camera = command.get("create_camera", False)
        camera_properties = command.get("camera_properties", {})  # e.g., focal_length

        camera = None
        camera_created = False
        requested_name = (
            identifier if identifier else "Animated Camera"
        )  # Use identifier as requested name if provided

        # Find existing camera if identifier provided and not creating new
        if identifier and not create_camera:
            camera = self.find_object_by_name(doc, identifier, use_guid=use_guid)
            if camera and camera.GetType() != c4d.Ocamera:
                self.log(
                    f"Warning: Object '{identifier}' found but is not a camera. Will create new."
                )
                camera = None  # Force creation
            elif camera is None:
                search_type = "GUID" if use_guid else "Name"
                self.log(
                    f"Info: Camera '{identifier}' (searched by {search_type}) not found, will create a new one."
                )

        # Create camera if needed
        if camera is None:
            doc.StartUndo()  # Start undo if creating
            camera = c4d.BaseObject(c4d.Ocamera)
            if not camera:
                return {"error": "Failed to create camera object"}

            camera.SetName(requested_name)  # Use requested name
            self.log(f"[ANIM CAM] Created new camera: {camera.GetName()}")
            camera_created = True

            # Apply properties if provided (using original logic, ensure safety)
            applied_properties = {}
            bc = camera.GetDataInstance()
            if bc:
                if "focal_length" in camera_properties:
                    try:
                        val = float(camera_properties["focal_length"])
                        focus_id = getattr(c4d, "CAMERAOBJECT_FOCUS", c4d.CAMERA_FOCUS)
                        bc[focus_id] = val
                        applied_properties["focal_length"] = val
                    except (ValueError, TypeError, AttributeError) as e:
                        self.log(f"Warning: Failed to set focal_length: {e}")
                if "aperture" in camera_properties:
                    try:
                        val = float(camera_properties["aperture"])
                        bc[c4d.CAMERAOBJECT_APERTURE] = val
                        applied_properties["aperture"] = val
                    except (ValueError, TypeError, AttributeError) as e:
                        self.log(f"Warning: Failed to set aperture: {e}")
                # Add other properties as needed...

            doc.InsertObject(camera)
            doc.AddUndo(c4d.UNDOTYPE_NEW, camera)
            doc.SetActiveObject(camera)  # Make active
            # Register the newly created camera
            self.register_object_name(camera, requested_name)
            doc.EndUndo()  # End undo block for creation
        else:
            self.log(f"[ANIM CAM] Using existing camera: '{camera.GetName()}'")

        # --- Animation Logic ---
        try:
            doc.StartUndo()  # Start undo for animation changes

            # Add default frames if only positions are provided
            if positions and not frames:
                frames = list(range(len(positions)))  # Simple frame sequence

            if not positions or not frames or len(positions) != len(frames):
                # Allow animation types without positions/frames? e.g. wiggle?
                if path_type not in ["wiggle"]:  # Add other position-less types here
                    doc.EndUndo()  # End undo as nothing happened yet
                    return {
                        "error": f"Invalid positions/frames data for animation type '{path_type}'. They must be arrays of equal length."
                    }
                else:
                    self.log(
                        f"Info: No position/frame data provided for '{path_type}', proceeding if type supports it."
                    )

            keyframe_count = 0
            frame_range_set = []

            # Set keyframes for camera positions if provided
            if positions and frames:
                for pos, frame in zip(positions, frames):
                    if isinstance(pos, list) and len(pos) >= 3:
                        # Use internal helper which already includes AddUndo
                        if self._set_position_keyframe(camera, frame, pos):
                            keyframe_count += 1
                    else:
                        self.log(
                            f"Warning: Skipping invalid position data {pos} for frame {frame}"
                        )
                if frames:
                    frame_range_set = [min(frames), max(frames)]

            # Handle spline path if requested and positions available
            path_guid = None  # Store GUID of created path
            if path_type in ["spline", "spline_oriented"] and len(positions) > 1:
                self.log("[ANIM CAM] Creating spline path and alignment tag.")
                path = c4d.BaseObject(c4d.Ospline)
                path.SetName(f"{camera.GetName()} Path")
                points = [
                    c4d.Vector(p[0], p[1], p[2])
                    for p in positions
                    if isinstance(p, list) and len(p) >= 3
                ]
                if not points:
                    self.log("Warning: No valid points for spline path creation.")
                else:
                    path.ResizeObject(len(points))
                    for i, pt in enumerate(points):
                        path.SetPoint(i, pt)

                    doc.InsertObject(path)  # Insert path into scene
                    doc.AddUndo(c4d.UNDOTYPE_NEW, path)
                    path_guid = str(path.GetGUID())  # Get GUID after insertion
                    self.register_object_name(
                        path, path.GetName()
                    )  # Register the path spline

                    # Create and apply Align to Spline tag
                    align_tag = camera.MakeTag(c4d.Talignspline)  # Use Talignspline
                    if align_tag:
                        align_tag[c4d.ALIGNTOSPLINETAG_LINK] = (
                            path  # Link the path object
                        )
                        # Set Tangential if spline_oriented? Check specific tag params if needed.
                        # align_tag[c4d.ALIGNTOSPLINETAG_TANGENTIAL] = (path_type == "spline_oriented")
                        doc.AddUndo(c4d.UNDOTYPE_NEW, align_tag)  # Add undo for new tag
                        self.log("Applied Align to Spline tag.")
                    else:
                        self.log("Warning: Failed to create Align to Spline tag.")

            # Handle other animation types (like wiggle) if needed here...
            # elif path_type == "wiggle":
            #    # Apply wiggle expression or tag... (Requires specific implementation)
            #    self.log("Info: Wiggle animation type not fully implemented in this version.")

            doc.EndUndo()  # End undo block for animation changes
            c4d.EventAdd()

            # --- MODIFIED: Contextual Return ---
            actual_camera_name = camera.GetName()
            camera_guid = str(camera.GetGUID())

            response_data = {
                "requested_name": requested_name,  # Name used to find/create
                "actual_name": actual_camera_name,  # Final name
                "guid": camera_guid,
                "camera_created": camera_created,  # Was it created by this call?
                "path_type": path_type,
                "keyframe_count": keyframe_count,
                "frame_range_set": frame_range_set,  # Frames actually keyframed
                "spline_path_guid": path_guid,  # GUID of path spline if created
                # "properties_applied": applied_properties if camera_created else {}, # Properties set during creation
            }

            return {"camera_animation": response_data}  # Keep original top-level key

        except Exception as e:
            doc.EndUndo()  # Ensure undo ended
            self.log(
                f"[**ERROR**] Error animating camera '{requested_name}': {str(e)}\n{traceback.format_exc()}"
            )
            # Clean up camera if created but not inserted
            if camera and not camera.GetDocument():
                try:
                    camera.Remove()
                except:
                    pass
            return {
                "error": f"Failed to animate camera: {str(e)}",
                "traceback": traceback.format_exc(),
            }

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
                for attr in [
                    "Mmaterial",
                    "MATERIAL_TYPE",
                    "GetRSMaterialNodeSpace",
                ]:
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
        """Handle create_material command with context and proper NodeMaterial support for Redshift."""
        doc = c4d.documents.GetActiveDocument()
        if not doc:
            return {"error": "No active document"}

        requested_name = (
            command.get("name") or command.get("material_name") or "New Material"
        )
        color_list = command.get("color", [1.0, 1.0, 1.0])  # Default to white
        properties = command.get("properties", {})
        material_type = command.get(
            "material_type", "standard"
        ).lower()  # standard, redshift
        procedural = command.get(
            "procedural", False
        )  # Currently only affects Redshift in this example
        shader_type = command.get(
            "shader_type", "noise"
        )  # Used if procedural=True for Redshift

        # Safely parse color
        color = [1.0, 1.0, 1.0]
        if isinstance(color_list, list) and len(color_list) >= 3:
            try:
                color = [
                    max(0.0, min(1.0, float(c))) for c in color_list[:3]
                ]  # Clamp 0-1
            except (ValueError, TypeError):
                self.log(f"Warning: Invalid color data {color_list}")

        self.log(
            f"[C4D] Starting material creation: Name='{requested_name}', Type='{material_type}'"
        )

        mat = None
        has_redshift = False
        redshift_plugin_id = None
        rs_mat_id_used = None  # Store the ID actually used for RS material

        try:
            # Check for Redshift plugin
            plugins = c4d.plugins.FilterPluginList(c4d.PLUGINTYPE_MATERIAL, True)
            for plugin in plugins:
                if "redshift" in plugin.GetName().lower():
                    has_redshift = True
                    redshift_plugin_id = plugin.GetID()
                    self.log(
                        f"[C4D] Found Redshift plugin: {plugin.GetName()} (ID: {plugin_id})"
                    )
                    break  # Found it

            if material_type == "redshift" and not has_redshift:
                self.log(
                    "[C4D] ## Warning ##: Redshift requested but not found. Using standard material."
                )
                material_type = "standard"

            doc.StartUndo()  # Start undo block

            # Create material based on type
            if material_type == "redshift":
                self.log("[C4D] Attempting Redshift material creation...")
                try:
                    # Determine the Redshift material ID to use
                    rs_id = getattr(
                        c4d, "ID_REDSHIFT_MATERIAL", redshift_plugin_id or 1036224
                    )  # Prefer constant, then plugin, then default
                    rs_mat_id_used = rs_id  # Store the ID we are trying
                    self.log(f"[C4D] Using Redshift Material ID: {rs_id}")

                    mat = c4d.BaseMaterial(rs_id)
                    if not mat or mat.GetType() != rs_id:
                        raise RuntimeError(f"Failed to create material with ID {rs_id}")

                    mat.SetName(requested_name)
                    self.log(f"[C4D] Base Redshift material created: '{mat.GetName()}'")

                    # Setup node graph using maxon API (R20+)
                    try:
                        import maxon

                        redshift_ns = maxon.Id(
                            "com.redshift3d.redshift4c4d.class.nodespace"
                        )
                        node_mat = c4d.NodeMaterial(mat)  # Wrap in NodeMaterial
                        if not node_mat:
                            raise RuntimeError("Failed to create NodeMaterial wrapper")

                        # Create default graph if it doesn't exist
                        if not node_mat.HasSpace(redshift_ns):
                            graph = node_mat.CreateDefaultGraph(redshift_ns)
                            self.log("[C4D] Created default Redshift node graph")
                        else:
                            graph = node_mat.GetGraph(redshift_ns)
                            self.log("[C4D] Using existing Redshift node graph")

                        if not graph:
                            raise RuntimeError(
                                "Failed to get or create Redshift node graph"
                            )

                        # Find StandardMaterial node and set base color
                        standard_mat_node = None
                        for node in graph.GetNodes():
                            if "StandardMaterial" in node.GetId():
                                standard_mat_node = node
                                break

                        if standard_mat_node:
                            try:
                                standard_mat_node.SetParameter(
                                    maxon.nodes.ParameterID("base_color"),
                                    maxon.Color(*color),
                                    maxon.PROPERTYFLAGS_NONE,
                                )
                                self.log(f"[C4D] Set Redshift base_color to {color}")
                            except Exception as e_node:
                                self.log(
                                    f"Warning: Failed to set Redshift base_color: {e_node}"
                                )
                        else:
                            self.log(
                                "Warning: Could not find StandardMaterial node in Redshift graph to set color."
                            )

                    except ImportError:
                        self.log(
                            "Warning: 'maxon' module not found, cannot configure Redshift nodes."
                        )
                    except Exception as e_node_setup:
                        self.log(
                            f"Warning: Error setting up Redshift node graph: {e_node_setup}"
                        )

                except Exception as e_rs:
                    self.log(
                        f"[**ERROR**] Redshift material creation failed: {e_rs}\n{traceback.format_exc()}. Falling back to standard."
                    )
                    material_type = "standard"  # Fallback flag
                    mat = None  # Reset mat so standard creation runs

            # Create a standard material if needed (or if RS failed)
            if material_type == "standard":
                self.log("[C4D] Creating standard material")
                mat = c4d.BaseMaterial(c4d.Mmaterial)
                if not mat:
                    raise RuntimeError("Failed to create standard material")
                mat.SetName(requested_name)

                # Set standard material properties
                mat[c4d.MATERIAL_COLOR_COLOR] = c4d.Vector(*color)  # Set color

                # Apply additional standard properties if provided
                if (
                    "specular" in properties
                    and isinstance(properties["specular"], list)
                    and len(properties["specular"]) >= 3
                ):
                    try:
                        mat[c4d.MATERIAL_SPECULAR_COLOR] = c4d.Vector(
                            *[float(s) for s in properties["specular"][:3]]
                        )
                    except (ValueError, TypeError):
                        self.log(
                            f"Warning: Invalid specular color value {properties['specular']}"
                        )
                if "reflection" in properties:
                    try:
                        mat[c4d.MATERIAL_REFLECTION_BRIGHTNESS] = max(
                            0.0, float(properties["reflection"])
                        )  # Clamp >= 0
                    except (ValueError, TypeError):
                        self.log(
                            f"Warning: Invalid reflection value {properties['reflection']}"
                        )

            if not mat:  # Final check if creation failed completely
                raise RuntimeError("Material creation failed for unknown reason.")

            # Insert material into document
            doc.InsertMaterial(mat)
            doc.AddUndo(c4d.UNDOTYPE_NEW, mat)  # Add undo step
            doc.EndUndo()  # End undo block
            c4d.EventAdd()

            # --- MODIFIED: Contextual Return ---
            actual_name = mat.GetName()
            mat_type_id = mat.GetType()
            final_material_type = (
                "redshift" if mat_type_id == rs_mat_id_used else "standard"
            )  # Determine final type based on actual ID

            # Get final color (might differ if RS nodes failed)
            final_color = color  # Default to requested
            if final_material_type == "standard":
                try:
                    final_color = [
                        mat[c4d.MATERIAL_COLOR_COLOR].x,
                        mat[c4d.MATERIAL_COLOR_COLOR].y,
                        mat[c4d.MATERIAL_COLOR_COLOR].z,
                    ]
                except:
                    pass  # Keep requested color if read fails

            self.log(
                f"[C4D] Material created successfully: Name='{actual_name}', Type='{final_material_type}', ID={mat_type_id}"
            )

            # Note: Materials don't have GUIDs in the same way as objects, so we don't register them.
            # We return info based on the final state.
            return {
                "material": {
                    "requested_name": requested_name,
                    "actual_name": actual_name,
                    "type": final_material_type,  # Report actual type created
                    "color_set": final_color,  # Report the final color state if possible
                    "type_id": mat_type_id,
                    "redshift_available": has_redshift,
                    # Add any other relevant context about properties set
                }
            }

        except Exception as e:
            doc.EndUndo()  # Ensure undo ended
            error_msg = f"Failed to create material '{requested_name}': {str(e)}"
            self.log(f"[**ERROR**] {error_msg}\n{traceback.format_exc()}")
            # Clean up material if created but not inserted
            if mat and not mat.GetDocument():
                try:
                    mat.Remove()
                except:
                    pass
            return {"error": error_msg, "traceback": traceback.format_exc()}

    def handle_render_frame(
        self, command
    ):  # Renamed from handle_render_to_file to match command key
        """Render the current frame to a file, using adapted core logic."""
        doc = c4d.documents.GetActiveDocument()
        if not doc:
            return {"error": "No active document"}

        output_path = command.get("output_path")
        width = int(command.get("width", 640))
        height = int(command.get("height", 360))
        # Frame handling - default to current frame if not specified
        frame = command.get("frame")
        if frame is None:
            frame = doc.GetTime().GetFrame(doc.GetFps())
        else:
            try:
                frame = int(frame)
            except (ValueError, TypeError):
                self.log(f"Warning: Invalid frame value '{frame}', using current.")
                frame = doc.GetTime().GetFrame(doc.GetFps())

        self.log(
            f"[RENDER FRAME] Request: frame={frame}, size={width}x{height}, path={output_path}"
        )

        # Ensure output path is valid and directory exists
        if not output_path:
            doc_name = os.path.splitext(doc.GetDocumentName() or "Untitled")[0]
            fallback_dir = doc.GetDocumentPath() or os.path.join(
                os.path.expanduser("~"), "Desktop"
            )  # Fallback to desktop
            output_path = os.path.join(
                fallback_dir, f"{doc_name}_render_{frame:04d}.png"
            )
            self.log(
                f"[RENDER FRAME] No output path provided, using fallback: {output_path}"
            )
        else:
            output_path = os.path.normpath(os.path.expanduser(output_path))

        output_dir = os.path.dirname(output_path)
        try:
            os.makedirs(output_dir, exist_ok=True)
        except OSError as e:
            return {"error": f"Cannot create output directory '{output_dir}': {e}"}

        # Determine format from extension (Default to PNG)
        ext = os.path.splitext(output_path)[1].lower()
        format_map = {
            ".png": c4d.FILTER_PNG,
            ".jpg": c4d.FILTER_JPG,
            ".jpeg": c4d.FILTER_JPG,
            ".tif": c4d.FILTER_TIF,
            ".tiff": c4d.FILTER_TIF,
        }
        format_id = format_map.get(ext, c4d.FILTER_PNG)
        if format_id == c4d.FILTER_PNG and ext not in format_map:
            output_path = os.path.splitext(output_path)[0] + ".png"
            format_id = c4d.FILTER_PNG
            self.log(
                f"Warning: Unsupported output extension '{ext}', defaulting to PNG: {output_path}"
            )

        # --- Execute render task on main thread ---
        def render_task():
            bmp = None
            render_duration = 0.0
            original_rd = None  # Keep track of original RD
            rd_clone = None  # Keep track of clone RD
            temp_rd_inserted = False
            try:
                # --- Start Core Logic Adaptation ---
                if not doc:
                    return {"error": "No active document (in render_task)"}
                active_draw = doc.GetActiveBaseDraw()
                if not active_draw:
                    return {"error": "No active BaseDraw (in render_task)"}
                active_camera = (
                    active_draw.GetSceneCamera(doc) or active_draw.GetEditorCamera()
                )
                if not active_camera:
                    return {"error": "No active camera (in render_task)"}

                original_rd = doc.GetActiveRenderData()
                if not original_rd:
                    return {"error": "No active RenderData (in render_task)"}
                rd_clone = original_rd.GetClone(c4d.COPYFLAGS_NONE)
                if not rd_clone:
                    return {"error": "RenderData clone failed (in render_task)"}

                settings = rd_clone.GetDataInstance()
                if not settings:
                    raise RuntimeError("Failed to get settings instance")

                settings[c4d.RDATA_XRES] = float(width)
                settings[c4d.RDATA_YRES] = float(height)
                settings[c4d.RDATA_FRAMESEQUENCE] = c4d.RDATA_FRAMESEQUENCE_CURRENTFRAME
                settings[c4d.RDATA_SAVEIMAGE] = False  # Render to bitmap, not auto-save

                doc.InsertRenderData(rd_clone)
                temp_rd_inserted = True
                doc.SetActiveRenderData(rd_clone)

                target_time = c4d.BaseTime(frame, doc.GetFps())
                doc.SetTime(target_time)
                # --- ExecutePasses Call ---
                doc.ExecutePasses(
                    None, True, True, True, c4d.BUILDFLAGS_NONE
                )  # Use None instead of active_draw

                bmp = c4d.bitmaps.BaseBitmap()  # Use BaseBitmap
                if (
                    not bmp
                    or bmp.Init(int(width), int(height), 24) != c4d.IMAGERESULT_OK
                ):  # Use int() for dimensions
                    raise MemoryError(f"Bitmap Init failed ({width}x{height})")

                render_flags = (
                    c4d.RENDERFLAGS_EXTERNAL
                    | c4d.RENDERFLAGS_SHOWERRORS
                    | c4d.RENDERFLAGS_NODOCUMENTCLONE
                    | 0x00040000
                )
                start_time = time.time()
                result_code = c4d.documents.RenderDocument(
                    doc, settings, bmp, render_flags, None
                )
                render_duration = time.time() - start_time

                if result_code != c4d.RENDERRESULT_OK:
                    err_str = self._render_code_to_str(result_code)
                    last_c4d_err = c4d.GetLastError()
                    if last_c4d_err:
                        err_str += f" (GetLastError: {last_c4d_err})"
                    raise RuntimeError(f"RenderDocument failed: {err_str}")

                # Save the resulting bitmap to file
                self.log(
                    f"[RENDER FRAME] Saving bitmap to: {output_path} (Format ID: {format_id})"
                )
                save_result = bmp.Save(output_path, format_id)
                if save_result == c4d.IMAGERESULT_OK:
                    self.log(f"[RENDER FRAME] Bitmap saved successfully.")
                    return {
                        "success": True,
                        "output_path": output_path,
                        "width": width,
                        "height": height,
                        "frame": frame,
                        "render_time": render_duration,
                        "file_exists": os.path.exists(output_path),
                    }
                else:
                    return {
                        "error": f"Failed to save bitmap (Error code: {save_result})"
                    }

            except Exception as e_render:
                tb = traceback.format_exc()
                self.log(
                    f"[**ERROR**][RENDER FRAME] Error during render task: {e_render}\n{tb}"
                )
                return {
                    "error": f"Exception during render/save: {str(e_render)}",
                    "traceback": tb,
                }
            finally:
                # Cleanup render data clone
                if temp_rd_inserted and original_rd:
                    try:
                        doc.SetActiveRenderData(original_rd)
                        if rd_clone:
                            rd_clone.Remove()
                    except Exception as e_cleanup:
                        self.log(f"Warning: Error during RD cleanup: {e_cleanup}")
                # Cleanup bitmap
                if bmp:
                    try:
                        bmp.FlushAll()
                    except:
                        pass
                c4d.EventAdd()

        # Execute the task on the main thread
        response = self.execute_on_main_thread(render_task, _timeout=180)

        # Structure the final response for the tool
        if response and response.get("success"):
            return {
                "render_info": response
            }  # Return nested structure expected by server tool
        else:
            # Ensure error structure is consistent if render_task itself returns an error dict
            if isinstance(response, dict) and "error" in response:
                return response
            # Handle cases where execute_on_main_thread returned an error (like timeout)
            elif isinstance(response, dict) and "error" in response:
                return response
            else:  # Fallback for unexpected scenarios
                return {"error": "Unknown error during render frame execution."}

    def handle_apply_shader(self, command):
        """Handle apply_shader command with improved Redshift/Fresnel support and context."""
        doc = c4d.documents.GetActiveDocument()
        if not doc:
            return {"error": "No active document"}

        material_name = command.get("material_name", "")
        # --- MODIFIED: Identify target object ---
        identifier = None
        use_guid = False
        object_specified = False
        if command.get("guid"):  # Check for GUID first
            identifier = command.get("guid")
            use_guid = True
            object_specified = True
            self.log(f"[APPLY SHADER] Using GUID identifier for object: '{identifier}'")
        elif command.get("object_name"):
            identifier = command.get("object_name")
            use_guid = False
            object_specified = True
            self.log(f"[APPLY SHADER] Using Name identifier for object: '{identifier}'")

        shader_type = command.get("shader_type", "noise").lower()
        channel = command.get("channel", "color").lower()
        parameters = command.get("parameters", {})

        self.log(
            f"[APPLY SHADER] Request: Shader='{shader_type}', Channel='{channel}', Material='{material_name}', Object='{identifier}'"
        )

        mat = None
        created_new_material = False
        obj_to_apply = None

        try:
            doc.StartUndo()  # Start undo block

            # Find or create material
            if material_name:
                mat = self._find_material_by_name(doc, material_name)
            if mat is None:
                default_mat_name = (
                    material_name
                    if material_name
                    else f"{shader_type.capitalize()} Material"
                )
                mat = c4d.BaseMaterial(c4d.Mmaterial)  # Create standard by default
                if not mat:
                    raise RuntimeError("Failed to create new material")
                mat.SetName(default_mat_name)
                doc.InsertMaterial(mat)
                doc.AddUndo(c4d.UNDOTYPE_NEW, mat)
                created_new_material = True
                material_name = mat.GetName()  # Use actual name
                self.log(f"[APPLY SHADER] Created new material: '{material_name}'")

            # Find object if specified
            if object_specified:
                obj_to_apply = self.find_object_by_name(
                    doc, identifier, use_guid=use_guid
                )
                if obj_to_apply is None:
                    search_type = "GUID" if use_guid else "Name"
                    self.log(
                        f"Warning: Object '{identifier}' (searched by {search_type}) not found for shader application."
                    )
                    # Don't error out, just won't apply tag later

            # Determine if material is Redshift
            is_redshift_material = False
            rs_mat_id = getattr(
                c4d, "ID_REDSHIFT_MATERIAL", 1036224
            )  # Get RS ID safely
            if mat.GetType() == rs_mat_id:
                is_redshift_material = True
            elif mat.GetType() >= 1000000:  # General check for other RS types
                is_redshift_material = True
                self.log(
                    f"Info: Material '{material_name}' has high ID ({mat.GetType()}), treating as Redshift."
                )

            if is_redshift_material:
                self.log(
                    f"[APPLY SHADER] Applying shader to Redshift material '{material_name}'..."
                )
                # --- Redshift Node Graph Logic ---
                try:
                    import maxon

                    redshift_ns = maxon.Id(
                        "com.redshift3d.redshift4c4d.class.nodespace"
                    )
                    node_mat = c4d.NodeMaterial(mat)
                    if node_mat and node_mat.HasSpace(redshift_ns):
                        graph = node_mat.GetGraph(redshift_ns)
                        if graph:
                            with graph.BeginTransaction() as transaction:
                                # Find output node... (Simplified for brevity - assumes StandardMaterial exists)
                                material_output = None
                                for node in graph.GetNodes():
                                    if "StandardMaterial" in node.GetId():
                                        material_output = node
                                        break

                                if material_output:
                                    # Create shader node...
                                    shader_node = None
                                    shader_node_id_str = ""
                                    if shader_type == "noise":
                                        shader_node_id_str = "com.redshift3d.redshift4c4d.nodes.core.texturesampler"
                                    elif shader_type == "fresnel":
                                        shader_node_id_str = "com.redshift3d.redshift4c4d.nodes.core.fresnel"
                                    # Add more shader types here...

                                    if shader_node_id_str:
                                        shader_node = graph.AddChild(
                                            maxon.Id(), maxon.Id(shader_node_id_str)
                                        )
                                        if (
                                            shader_node and shader_type == "noise"
                                        ):  # Configure noise specific
                                            shader_node.SetParameter(
                                                maxon.nodes.ParameterID("tex0_tex"),
                                                4,
                                                maxon.PROPERTYFLAGS_NONE,
                                            )  # 4=Noise
                                            if "scale" in parameters:
                                                shader_node.SetParameter(
                                                    maxon.nodes.ParameterID(
                                                        "noise_scale"
                                                    ),
                                                    float(parameters["scale"]),
                                                    maxon.PROPERTYFLAGS_NONE,
                                                )

                                    # Connect shader node...
                                    if shader_node:
                                        # Find target port... (Simplified)
                                        target_port_id_str = (
                                            "base_color"
                                            if channel == "color"
                                            else "refl_color"
                                        )  # Example mapping
                                        target_port = material_output.GetInputs().Find(
                                            maxon.Id(target_port_id_str)
                                        )

                                        # Find source port... (Simplified)
                                        source_port_id_str = (
                                            "outcolor"
                                            if shader_type != "fresnel"
                                            else "out"
                                        )
                                        source_port = shader_node.GetOutputs().Find(
                                            maxon.Id(source_port_id_str)
                                        )

                                        if target_port and source_port:
                                            graph.CreateConnection(
                                                source_port, target_port
                                            )
                                            self.log(
                                                f"Connected RS {shader_type} node to {channel}"
                                            )
                                        else:
                                            self.log(
                                                "Warning: Could not find source/target ports for RS shader connection."
                                            )
                                    else:
                                        self.log(
                                            f"Warning: Failed to create RS {shader_type} node."
                                        )
                                else:
                                    self.log(
                                        "Warning: Could not find RS StandardMaterial output node."
                                    )
                                transaction.Commit()
                        else:
                            self.log("Warning: Could not get RS node graph.")
                    else:
                        self.log(
                            "Warning: Material is not a Redshift Node Material or lacks RS space."
                        )
                except ImportError:
                    self.log("Warning: 'maxon' module not found, cannot edit RS nodes.")
                except Exception as e_rs:
                    self.log(f"Error applying shader to RS material: {e_rs}")
                # Fallthrough to standard shader application is NOT intended here. If it's RS, we try nodes.

            else:
                # --- Standard Shader Logic (from original) ---
                self.log(
                    f"[APPLY SHADER] Applying shader to Standard material '{material_name}'..."
                )
                shader_types = {
                    "noise": 5832,
                    "gradient": 5825,
                    "fresnel": 5837,
                    "layer": 5685,
                    "checkerboard": 5831,
                }
                channel_map = {
                    "color": c4d.MATERIAL_COLOR_SHADER,
                    "luminance": c4d.MATERIAL_LUMINANCE_SHADER,
                    "transparency": c4d.MATERIAL_TRANSPARENCY_SHADER,
                    "reflection": c4d.MATERIAL_REFLECTION_SHADER,
                    "bump": c4d.MATERIAL_BUMP_SHADER,
                }  # Added bump
                shader_type_id = shader_types.get(shader_type, 5832)
                channel_id = channel_map.get(channel)

                if channel_id is None:
                    raise ValueError(f"Unsupported standard channel: {channel}")

                shader = c4d.BaseShader(shader_type_id)
                if shader is None:
                    raise RuntimeError(
                        f"Failed to create standard {shader_type} shader"
                    )

                # Apply parameters (example for noise)
                if shader_type == "noise" and hasattr(c4d, "SLA_NOISE_SCALE"):
                    if "scale" in parameters:
                        shader[c4d.SLA_NOISE_SCALE] = float(
                            parameters.get("scale", 1.0)
                        )
                    if "octaves" in parameters:
                        shader[c4d.SLA_NOISE_OCTAVES] = int(
                            parameters.get("octaves", 3)
                        )
                # Add more parameter settings for other standard shader types here...

                mat[channel_id] = shader

                # Enable the channel
                enable_map = {
                    "color": c4d.MATERIAL_USE_COLOR,
                    "luminance": c4d.MATERIAL_USE_LUMINANCE,
                    "transparency": c4d.MATERIAL_USE_TRANSPARENCY,
                    "reflection": c4d.MATERIAL_USE_REFLECTION,
                    "bump": c4d.MATERIAL_USE_BUMP,
                }
                if channel in enable_map:
                    try:
                        mat[enable_map[channel]] = True
                    except AttributeError:
                        self.log(
                            f"Warning: Could not find enable parameter for channel '{channel}'"
                        )

            mat.Update(True, True)
            doc.AddUndo(c4d.UNDOTYPE_CHANGE, mat)  # Add undo for material change

            # Apply material to object if found
            applied_to_name = "None"
            applied_to_guid = None
            if obj_to_apply:
                try:
                    # Check if object already has a texture tag for this material
                    existing_tag = None
                    for tag in obj_to_apply.GetTags():
                        if tag.GetType() == c4d.Ttexture and tag.GetMaterial() == mat:
                            existing_tag = tag
                            self.log(
                                f"Found existing texture tag for material '{material_name}' on '{obj_to_apply.GetName()}'"
                            )
                            break

                    if not existing_tag:
                        tag = obj_to_apply.MakeTag(
                            c4d.Ttexture
                        )  # Use MakeTag for safer insertion
                        if tag:
                            tag.SetMaterial(mat)
                            doc.AddUndo(c4d.UNDOTYPE_NEW, tag)
                            applied_to_name = obj_to_apply.GetName()
                            applied_to_guid = str(obj_to_apply.GetGUID())
                            self.log(
                                f"[APPLY SHADER] Applied material '{material_name}' to object '{applied_to_name}'"
                            )
                        else:
                            self.log(
                                f"Warning: Failed to create texture tag on '{obj_to_apply.GetName()}'"
                            )
                    else:
                        # Material already applied via existing tag
                        applied_to_name = obj_to_apply.GetName()
                        applied_to_guid = str(obj_to_apply.GetGUID())
                        self.log(
                            f"Material '{material_name}' was already applied to object '{applied_to_name}'"
                        )

                except Exception as e_tag:
                    self.log(
                        f"[**ERROR**] Error applying material tag to '{obj_to_apply.GetName()}': {str(e_tag)}"
                    )

            doc.EndUndo()  # End undo block
            c4d.EventAdd()

            # --- MODIFIED: Contextual Return ---
            return {
                "shader_application": {  # Changed key for clarity
                    "material_name": material_name,
                    "material_type_id": mat.GetType(),
                    "shader_type": shader_type,
                    "channel": channel,
                    "applied_to_object_name": applied_to_name,  # Name or "None"
                    "applied_to_object_guid": applied_to_guid,  # GUID or None
                    "created_new_material": created_new_material,
                    "is_redshift_material": is_redshift_material,
                }
            }

        except Exception as e:
            doc.EndUndo()  # Ensure undo ended
            self.log(
                f"[**ERROR**] Error applying shader: {str(e)}\n{traceback.format_exc()}"
            )
            return {
                "error": f"Failed to apply shader: {str(e)}",
                "traceback": traceback.format_exc(),
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
