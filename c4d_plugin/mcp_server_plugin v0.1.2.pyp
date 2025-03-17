import c4d
from c4d import gui
import socket
import threading
import json
import queue
import os
import time

PLUGIN_ID = 1057843  # Ensure this is a unique ID


class C4DSocketServer(threading.Thread):
    """Socket Server running in a background thread, sending logs & status via queue."""

    # Class-level command queue for safe execution on main thread
    command_queue = queue.Queue()
    command_results = {}
    command_counter = 0
    command_lock = threading.Lock()

    def __init__(self, msg_queue, host="127.0.0.1", port=5555):
        super().__init__()
        self.host = host
        self.port = port
        self.socket = None
        self.running = False
        self.msg_queue = msg_queue
        self.daemon = True

    def log(self, message):
        """Send log messages to UI via queue and trigger an event."""
        self.msg_queue.put(("LOG", message))
        c4d.SpecialEventAdd(PLUGIN_ID)  # Notify UI thread

    def update_status(self, status):
        """Update status via queue and trigger an event."""
        self.msg_queue.put(("STATUS", status))
        c4d.SpecialEventAdd(PLUGIN_ID)

    def execute_on_main_thread(self, func, *args, **kwargs):
        """Safely execute a function on the main thread and wait for the result.

        Args:
            func: The function to execute
            *args, **kwargs: Arguments to pass to the function

        Returns:
            The result of the function execution
        """
        # Generate a unique command ID
        with C4DSocketServer.command_lock:
            command_id = C4DSocketServer.command_counter
            C4DSocketServer.command_counter += 1

        # Create event that will be set when the command completes
        event = threading.Event()

        # Queue the command
        C4DSocketServer.command_queue.put((command_id, func, args, kwargs, event))

        # Trigger special event to process commands in main thread
        c4d.SpecialEventAdd(PLUGIN_ID + 1)  # Use a different ID for command execution

        # Wait for the command to complete
        event.wait()

        # Retrieve and return the result
        with C4DSocketServer.command_lock:
            result = C4DSocketServer.command_results.pop(command_id, None)

        return result

    @classmethod
    def process_command_queue(cls):
        """Process all pending commands in the queue.
        This method should be called from the main thread."""
        while not cls.command_queue.empty():
            try:
                # Get the next command
                command_id, func, args, kwargs, event = cls.command_queue.get_nowait()

                # Execute the function
                try:
                    result = func(*args, **kwargs)
                    # Store the result
                    with cls.command_lock:
                        cls.command_results[command_id] = result
                except Exception as e:
                    # Store the exception
                    with cls.command_lock:
                        cls.command_results[command_id] = e

                # Signal that the command is complete
                event.set()

            except queue.Empty:
                break

    def run(self):
        """Main server loop"""
        try:
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.socket.bind((self.host, self.port))
            self.socket.listen(5)
            self.running = True
            self.update_status("Online")
            self.log(f"[C4D] Server running at {self.host}:{self.port}")

            while self.running:
                try:
                    client, addr = self.socket.accept()
                except OSError as e:
                    if not self.running:
                        break
                    self.log(f"[C4D] Error accepting connection: {e}")
                    break

                self.log(f"[C4D] Client connected: {addr}")
                threading.Thread(target=self.handle_client, args=(client,)).start()

        except Exception as e:
            self.log(f"[C4D] Server Error: {str(e)}")
            self.update_status("Offline")
            self.running = False

    def handle_client(self, client):
        """Handle incoming client connections."""
        buffer = ""
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
                    handler_name = f"handle_{command.get('command')}"

                    handler = getattr(self, handler_name, None)
                    if handler:
                        # Execute the handler
                        response = handler(command)

                        # Trigger command processing on main thread in case any commands
                        # were queued but not processed
                        c4d.SpecialEventAdd(PLUGIN_ID + 1)
                    else:
                        response = {"error": "Unknown command"}

                except json.JSONDecodeError:
                    response = {"error": "Invalid JSON"}
                except Exception as e:
                    self.log(f"[C4D] Client error: {str(e)}")
                    response = {"error": str(e)}

                # Send response back to client
                client.sendall((json.dumps(response) + "\n").encode("utf-8"))
                self.log(f"[C4D] Sent response for {handler_name}")

        # Clean up
        client.close()
        self.log("[C4D] Client disconnected")

    def stop(self):
        """Stop the server."""
        self.running = False
        if self.socket:
            try:
                self.socket.close()  # Ensure the socket is properly closed
            except Exception as e:
                self.log(f"[C4D] Error closing socket: {e}")

        # Notify UI about shutdown
        self.update_status("Offline")
        self.log("[C4D] Server stopped")

    def handle_get_scene_info(self, command=None):
        """Handle get_scene_info command."""
        doc = c4d.documents.GetActiveDocument()

        # Define function to run on main thread
        def get_scene_info_safe():
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
            return scene_info

        try:
            # Execute on main thread and get result
            scene_info = self.execute_on_main_thread(get_scene_info_safe)
            return {"scene_info": scene_info}
        except Exception as e:
            return {"error": f"Failed to get scene info: {str(e)}"}

    def handle_list_objects(self, command):
        """Handle list_objects command."""
        doc = c4d.documents.GetActiveDocument()

        # Define function to run on main thread
        def list_objects_safe():
            objects = []
            obj = doc.GetFirstObject()
            while obj:
                # Extract needed info from objects
                object_info = {
                    "name": obj.GetName(),
                    "type": self.get_object_type_name(obj),
                    "id": str(obj.GetGUID()),
                    "position": [
                        obj.GetAbsPos().x,
                        obj.GetAbsPos().y,
                        obj.GetAbsPos().z,
                    ],
                    "children_count": sum(1 for _ in obj.GetChildren()),
                }
                objects.append(object_info)
                obj = obj.GetNext()
            return objects

        try:
            # Execute on main thread and get result
            objects = self.execute_on_main_thread(list_objects_safe)
            return {"objects": objects}
        except Exception as e:
            return {"error": f"Failed to list objects: {str(e)}"}

    def handle_add_primitive(self, command):
        """Handle add_primitive command."""
        doc = c4d.documents.GetActiveDocument()
        primitive_type = command.get("type", "cube").lower()
        name = command.get("object_name", primitive_type.capitalize())
        position = command.get("position", [0, 0, 0])
        size = command.get("size", [100, 100, 100])

        # Define function to run on main thread
        def create_primitive_safe():
            # Create the appropriate primitive object
            if primitive_type == "cube":
                obj = c4d.BaseObject(c4d.Ocube)
                if len(size) >= 3:
                    obj[c4d.PRIM_CUBE_LEN_X] = size[0]
                    obj[c4d.PRIM_CUBE_LEN_Y] = size[1]
                    obj[c4d.PRIM_CUBE_LEN_Z] = size[2]
            elif primitive_type == "sphere":
                obj = c4d.BaseObject(c4d.Osphere)
                if len(size) >= 1:
                    obj[c4d.PRIM_SPHERE_RAD] = size[0] / 2
            elif primitive_type == "cone":
                obj = c4d.BaseObject(c4d.Ocone)
                if len(size) >= 2:
                    obj[c4d.PRIM_CONE_TRAD] = 0
                    obj[c4d.PRIM_CONE_BRAD] = size[0] / 2
                    obj[c4d.PRIM_CONE_HEIGHT] = size[1]
            elif primitive_type == "cylinder":
                obj = c4d.BaseObject(c4d.Ocylinder)
                if len(size) >= 2:
                    obj[c4d.PRIM_CYLINDER_RADIUS] = size[0] / 2
                    obj[c4d.PRIM_CYLINDER_HEIGHT] = size[1]
            elif primitive_type == "plane":
                obj = c4d.BaseObject(c4d.Oplane)
                if len(size) >= 2:
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
            doc.AddUndo(c4d.UNDOTYPE_NEW, obj)
            doc.SetActiveObject(obj)

            # Update the document
            c4d.EventAdd()

            # Return information about the created object
            return {
                "name": obj.GetName(),
                "id": str(obj.GetGUID()),
                "position": [obj.GetAbsPos().x, obj.GetAbsPos().y, obj.GetAbsPos().z],
            }

        try:
            # Execute on main thread and get result
            object_info = self.execute_on_main_thread(create_primitive_safe)
            return {"object": object_info}
        except Exception as e:
            return {"error": f"Failed to create primitive: {str(e)}"}

    def handle_modify_object(self, command):
        """Handle modify_object command."""
        doc = c4d.documents.GetActiveDocument()
        object_name = command.get("object_name", "")
        properties = command.get("properties", {})

        # Find the object by name first (this is safe to do in the worker thread)
        obj = self.find_object_by_name(doc, object_name)
        if obj is None:
            return {"error": f"Object not found: {object_name}"}

        # Define function to run on main thread
        def modify_object_safe(obj, properties):
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
                obj.SetRotation(c4d.Vector(rot_rad[0], rot_rad[1], rot_rad[2]))
                modified["rotation"] = rot

            # Scale
            if (
                "scale" in properties
                and isinstance(properties["scale"], list)
                and len(properties["scale"]) >= 3
            ):
                scale = properties["scale"]
                obj.SetScale(c4d.Vector(scale[0], scale[1], scale[2]))
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
                    obj[c4d.ID_BASEOBJECT_COLOR] = c4d.Vector(
                        color[0], color[1], color[2]
                    )
                    modified["color"] = color
                except:
                    pass  # Silently fail if property doesn't exist

            # Register undo for the modification
            doc.AddUndo(c4d.UNDOTYPE_CHANGE, obj)

            # Update the document
            c4d.EventAdd()

            return {
                "name": obj.GetName(),
                "id": str(obj.GetGUID()),
                "modified": modified,
            }

        try:
            # Execute on main thread and get result
            object_info = self.execute_on_main_thread(
                modify_object_safe, obj, properties
            )
            return {"object": object_info}
        except Exception as e:
            return {"error": f"Failed to modify object: {str(e)}"}

    def handle_create_material(self, command):
        """Handle create_material command."""
        doc = c4d.documents.GetActiveDocument()
        name = command.get("material_name", "New Material")
        color = command.get("color", [1, 1, 1])
        properties = command.get("properties", {})

        # Define function to run on main thread
        def create_material_safe(name, color, properties):
            # Create a new material
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

            return {"name": mat.GetName(), "id": material_id, "color": color}

        try:
            # Execute on main thread and get result
            material_info = self.execute_on_main_thread(
                create_material_safe, name, color, properties
            )
            return {"material": material_info}
        except Exception as e:
            return {"error": f"Failed to create material: {str(e)}"}

    def handle_apply_material(self, command):
        """Handle apply_material command."""
        doc = c4d.documents.GetActiveDocument()
        material_name = command.get("material_name", "")
        object_name = command.get("object_name", "")

        # Find the object and material (this is safe to do in the worker thread)
        obj = self.find_object_by_name(doc, object_name)
        if obj is None:
            return {"error": f"Object not found: {object_name}"}

        # Find the material
        mat = self.find_material_by_name(doc, material_name)
        if mat is None:
            return {"error": f"Material not found: {material_name}"}

        # Define function to run on main thread
        def apply_material_safe(obj, mat, material_name, object_name):
            # Create a texture tag
            tag = c4d.TextureTag()
            tag.SetMaterial(mat)

            # Add the tag to the object
            obj.InsertTag(tag)
            doc.AddUndo(c4d.UNDOTYPE_NEW, tag)

            # Update the document
            c4d.EventAdd()

            return {
                "success": True,
                "message": f"Applied material '{material_name}' to object '{object_name}'",
            }

        try:
            # Execute on main thread and get result
            result = self.execute_on_main_thread(
                apply_material_safe, obj, mat, material_name, object_name
            )
            return result
        except Exception as e:
            return {"error": f"Failed to apply material: {str(e)}"}

    def handle_render_frame(self, command):
        """Handle render_frame command."""
        doc = c4d.documents.GetActiveDocument()
        output_path = command.get("output_path", None)
        width = command.get("width", None)
        height = command.get("height", None)

        # Create directory if needed (safe to do in worker thread)
        if output_path is not None:
            output_dir = os.path.dirname(output_path)
            if output_dir and not os.path.exists(output_dir):
                try:
                    os.makedirs(output_dir)
                except Exception as e:
                    return {"error": f"Failed to create output directory: {str(e)}"}

        # Define function to run on main thread
        def render_frame_safe(output_path, width, height):
            # Set render settings if provided
            rd = doc.GetActiveRenderData().GetClone()

            if width is not None and height is not None:
                rd.SetParameter(c4d.RDATA_XRES, width)
                rd.SetParameter(c4d.RDATA_YRES, height)

            # Set output path if provided
            if output_path is not None:
                rd.SetParameter(c4d.RDATA_PATH, output_path)

            # Get start time for timing
            start_time = time.time()

            # Render the frame
            bitmap = c4d.documents.RenderDocument(doc, rd.GetData(), None, None, None)

            # Calculate render time
            render_time = time.time() - start_time

            if bitmap is None:
                raise RuntimeError("Failed to render frame")

            # Save to file if path is specified
            path = "Memory only"
            if output_path:
                if bitmap.Save(output_path, c4d.SAVEBIT_0):
                    path = output_path
                else:
                    raise RuntimeError(f"Failed to save render to {output_path}")

            # Return render info
            return {
                "path": path,
                "width": bitmap.GetWidth(),
                "height": bitmap.GetHeight(),
                "render_time": render_time,
            }

        try:
            # Execute on main thread and get result
            render_info = self.execute_on_main_thread(
                render_frame_safe, output_path, width, height
            )
            return {"render_info": render_info}
        except Exception as e:
            return {"error": f"Failed to render: {str(e)}"}

    def handle_set_keyframe(self, command):
        """Handle set_keyframe command."""
        doc = c4d.documents.GetActiveDocument()
        object_name = command.get("object_name", "")
        property_name = command.get("property_name", "")
        value = command.get("value", 0)
        frame = command.get("frame", doc.GetTime().GetFrame(doc.GetFps()))

        # Find the object by name (safe to do in worker thread)
        obj = self.find_object_by_name(doc, object_name)
        if obj is None:
            return {"error": f"Object not found: {object_name}"}

        # Map property names to C4D constants (this can be done in worker thread)
        property_map = {
            "position.x": c4d.ID_BASEOBJECT_POSITION_X,
            "position.y": c4d.ID_BASEOBJECT_POSITION_Y,
            "position.z": c4d.ID_BASEOBJECT_POSITION_Z,
            "rotation.h": c4d.ID_BASEOBJECT_ROTATION_H,
            "rotation.p": c4d.ID_BASEOBJECT_ROTATION_P,
            "rotation.b": c4d.ID_BASEOBJECT_ROTATION_B,
            "scale.x": c4d.ID_BASEOBJECT_SCALE_X,
            "scale.y": c4d.ID_BASEOBJECT_SCALE_Y,
            "scale.z": c4d.ID_BASEOBJECT_SCALE_Z,
        }

        # Get the C4D property ID
        if property_name not in property_map:
            return {"error": f"Unsupported property: {property_name}"}

        prop_id = property_map[property_name]

        # Define function to run on main thread
        def set_keyframe_safe(obj, prop_id, property_name, value, frame):
            # Get or create track
            track = obj.FindCTrack(prop_id)
            if track is None:
                track = c4d.CTrack(obj, prop_id)
                obj.InsertTrackSorted(track)
                doc.AddUndo(c4d.UNDOTYPE_NEW, track)

            # Get the curve
            curve = track.GetCurve()
            if curve is None:
                raise RuntimeError("Failed to get animation curve")

            # Set the keyframe
            time_point = c4d.BaseTime(frame, doc.GetFps())

            # For rotation, convert degrees to radians
            local_value = value
            if "rotation" in property_name:
                local_value = c4d.utils.DegToRad(value)

            # Add or modify the key
            key = curve.AddKey(time_point)
            if key is None or key["key"] is None:
                raise RuntimeError("Failed to create keyframe")

            key["key"].SetValue(curve, local_value)
            doc.AddUndo(c4d.UNDOTYPE_CHANGE, track)

            # Update the document
            c4d.EventAdd()

            return {
                "success": True,
                "message": f"Keyframe set for {object_name}.{property_name} = {value} at frame {frame}",
            }

        try:
            # Execute on main thread and get result
            result = self.execute_on_main_thread(
                set_keyframe_safe, obj, prop_id, property_name, value, frame
            )
            return result
        except Exception as e:
            return {"error": f"Failed to set keyframe: {str(e)}"}

    def handle_save_scene(self, command):
        """Handle save_scene command."""
        doc = c4d.documents.GetActiveDocument()
        file_path = command.get("file_path", None)

        # Process file path (this is safe to do in worker thread)
        try:
            # If no path is provided, use the current one
            if file_path is None:
                # These getters are safe to call from any thread
                doc_path = doc.GetDocumentPath()
                doc_name = doc.GetDocumentName()

                if not doc_path or not doc_name:
                    return {
                        "error": "No save path specified and document has no current path"
                    }

                file_path = os.path.join(doc_path, doc_name)

            # Make sure path has proper extension
            if not file_path.lower().endswith(".c4d"):
                file_path += ".c4d"

            # Make sure directory exists - this is safe to do outside main thread
            directory = os.path.dirname(file_path)
            if directory and not os.path.exists(directory):
                try:
                    os.makedirs(directory)
                except Exception as e:
                    return {
                        "error": f"Failed to create directory {directory}: {str(e)}"
                    }

            # Define function to run on main thread
            def save_scene_safe(doc, file_path):
                # Save document (must be done on main thread)
                if c4d.documents.SaveDocument(
                    doc,
                    file_path,
                    c4d.SAVEDOCUMENTFLAGS_DONTADDTORECENTLIST,
                    c4d.FORMAT_C4DEXPORT,
                ):
                    return {"success": True, "path": file_path}
                else:
                    raise RuntimeError(f"Failed to save document to {file_path}")

            # Execute on main thread and get result
            save_info = self.execute_on_main_thread(save_scene_safe, doc, file_path)
            return {"save_info": save_info}

        except Exception as e:
            return {"error": f"Failed to save scene: {str(e)}"}

    def handle_load_scene(self, command):
        """Handle load_scene command."""
        file_path = command.get("file_path", "")

        if not file_path:
            return {"error": "No file path provided"}

        # Check file existence (safe to do in worker thread)
        if not os.path.exists(file_path):
            return {"error": f"File not found: {file_path}"}

        try:
            # Define function to run on main thread
            def load_scene_safe(file_path):
                # Load the document (must be done on main thread)
                loaded_doc = c4d.documents.LoadDocument(
                    file_path, c4d.SCENEFILTER_OBJECTS | c4d.SCENEFILTER_MATERIALS
                )

                if loaded_doc is None:
                    raise RuntimeError(f"Failed to load document from {file_path}")

                # Make it the active document
                c4d.documents.SetActiveDocument(loaded_doc)

                # Update the C4D UI
                c4d.EventAdd()

                return {"success": True, "message": f"Loaded scene from {file_path}"}

            # Execute on main thread and get result
            return self.execute_on_main_thread(load_scene_safe, file_path)
        except Exception as e:
            return {"error": f"Failed to load scene: {str(e)}"}

    def handle_execute_python(self, command):
        """Handle execute_python command."""
        script = command.get("script", "")

        if not script:
            return {"error": "No script provided"}

        doc = c4d.documents.GetActiveDocument()

        # Define function to run on main thread
        def execute_python_safe(script, doc):
            import sys
            from io import StringIO

            # Save original stdout and print function
            old_stdout = sys.stdout
            original_print = __builtins__["print"]
            output_buffer = StringIO()

            # Also track print output in case some code bypasses sys.stdout
            print_output = {"output": ""}

            try:
                # Redirect stdout
                sys.stdout = output_buffer

                # Define a function to capture print output
                def capture_print(*args, **kwargs):
                    # Convert args to strings and join with spaces
                    output = " ".join(str(arg) for arg in args)
                    if "end" in kwargs:
                        output += kwargs["end"]
                    else:
                        output += "\n"
                    print_output["output"] += output
                    # Also write to our StringIO buffer
                    output_buffer.write(output)

                # Replace print with our capture function
                __builtins__["print"] = capture_print

                # Create a local environment with document and other useful objects
                local_env = {
                    "doc": doc,  # Current document
                    "c4d": c4d,  # C4D module
                    "op": doc.GetActiveObject(),  # Active object
                    "mat": doc.GetActiveMaterial(),  # Active material
                }

                # Execute the script
                exec(script, globals(), local_env)

                # Ensure UI is updated
                c4d.EventAdd()

                # Combine both outputs (stdout redirect and print capture)
                stdout_output = output_buffer.getvalue()
                print_captured = print_output["output"]

                # Use whichever has more content
                final_output = (
                    stdout_output
                    if len(stdout_output) > len(print_captured)
                    else print_captured
                )

                return (
                    final_output.strip()
                    or "Script executed successfully with no output."
                )

            except Exception as e:
                import traceback

                tb = traceback.format_exc()
                return f"ERROR: {str(e)}\n\n{tb}"

            finally:
                # Always restore original stdout and print
                sys.stdout = old_stdout
                __builtins__["print"] = original_print

        try:
            # Execute on main thread and get result
            output = self.execute_on_main_thread(execute_python_safe, script, doc)
            return {"result": output}
        except Exception as e:
            return {"error": f"Script execution failed: {str(e)}"}

    def handle_create_mograph_cloner(self, command):
        """Handle create_mograph_cloner command."""
        doc = c4d.documents.GetActiveDocument()
        name = command.get("cloner_name", "MoGraph Cloner")
        mode = command.get("mode", "grid").lower()
        count = command.get("count", 10)
        object_name = command.get("object_name", None)

        # Find object to clone if specified (this is safe to do in the worker thread)
        clone_obj = None
        if object_name:
            clone_obj = self.find_object_by_name(doc, object_name)
            if not clone_obj:
                return {"error": f"Object '{object_name}' not found."}

        # Define function to run on main thread
        def create_mograph_cloner_safe(name, mode, count, clone_obj):
            # Create MoGraph Cloner object correctly
            cloner = c4d.BaseObject(c4d.Omgcloner)
            cloner.SetName(name)

            # Configure the Cloner mode
            mode_ids = {
                "linear": 0,  # Linear mode
                "radial": 2,  # Radial mode
                "grid": 1,  # Grid mode
                "object": 3,  # Object mode
            }
            cloner[c4d.ID_MG_MOTIONGENERATOR_MODE] = mode_ids.get(
                mode, 0
            )  # Default to Linear

            # Set clone counts based on mode
            if mode == "linear":
                cloner[c4d.MG_LINEAR_COUNT] = count
            elif mode == "grid":
                grid_dim = int(round(count ** (1 / 3))) or 1
                cloner[c4d.MG_GRID_COUNT_X] = grid_dim
                cloner[c4d.MG_GRID_COUNT_Y] = grid_dim
                cloner[c4d.MG_GRID_COUNT_Z] = grid_dim
            elif mode == "radial":
                cloner[c4d.MG_POLY_COUNT] = count

            # Create the clone object
            if clone_obj:
                clone = clone_obj.GetClone()
            else:
                clone = c4d.BaseObject(c4d.Ocube)
                clone.SetName("Default Cube")
                # Scale down for better visualization
                clone.SetAbsScale(c4d.Vector(0.5, 0.5, 0.5))

            # Insert cloned object under Cloner
            clone.InsertUnder(cloner)

            # Insert Cloner into document and add undo
            doc.InsertObject(cloner)
            doc.AddUndo(c4d.UNDOTYPE_NEW, cloner)
            doc.AddUndo(c4d.UNDOTYPE_NEW, clone)

            c4d.EventAdd()

            return {
                "name": cloner.GetName(),
                "id": str(cloner.GetGUID()),
                "type": mode,
                "count": count,
            }

        try:
            # Execute on main thread and get result
            cloner_info = self.execute_on_main_thread(
                create_mograph_cloner_safe, name, mode, count, clone_obj
            )
            return {"success": True, "cloner": cloner_info}
        except Exception as e:
            return {"error": f"Failed to create MoGraph Cloner: {str(e)}"}

    def handle_apply_mograph_fields(self, command):
        """Handle apply_mograph_fields command."""
        doc = c4d.documents.GetActiveDocument()
        target_name = command.get("target_name", "")
        field_type = command.get("field_type", "spherical").lower()
        field_name = command.get("field_name", f"{field_type.capitalize()} Field")
        parameters = command.get("parameters", {})

        # Find target object if specified (this is safe to do in worker thread)
        target = None
        if target_name:
            target = self.find_object_by_name(doc, target_name)
            if target is None:
                return {"error": f"Target object not found: {target_name}"}

        # Define function to run on main thread
        def apply_mograph_fields_safe(
            field_type, field_name, parameters, target, target_name
        ):
            # Map field type names to C4D constants
            field_types = {
                "spherical": 1039384,  # Spherical Field
                "box": 1039385,  # Box Field
                "cylindrical": 1039386,  # Cylindrical Field
                "torus": 1039387,  # Torus Field
                "cone": 1039388,  # Cone Field
                "linear": 1039389,  # Linear Field
                "radial": 1039390,  # Radial Field
                "object": 1039469,  # Object Field
                "noise": 1039394,  # Noise Field
                "formula": 1039470,  # Formula Field
                "sound": 1039471,  # Sound Field
                "random": 1039472,  # Random Field
            }

            # Get field type ID
            field_type_id = field_types.get(
                field_type, 1039384
            )  # Default to Spherical Field

            # Create the field
            field = c4d.BaseObject(field_type_id)
            if field is None:
                raise RuntimeError(f"Failed to create {field_type} field")

            field.SetName(field_name)

            # Set field parameters
            if "strength" in parameters and isinstance(
                parameters["strength"], (int, float)
            ):
                field[c4d.FIELD_STRENGTH] = float(parameters["strength"])

            if "falloff" in parameters and isinstance(
                parameters["falloff"], (int, float)
            ):
                field[c4d.FIELD_FALLOFF] = float(parameters["falloff"])

            # Insert field into document
            doc.InsertObject(field)
            doc.AddUndo(c4d.UNDOTYPE_NEW, field)

            # Apply to target object if specified
            if target:
                # Add field to target object
                tag = c4d.BaseTag(c4d.Tfields)
                if tag is None:
                    raise RuntimeError("Failed to create Fields tag")

                target.InsertTag(tag)
                doc.AddUndo(c4d.UNDOTYPE_NEW, tag)

                # Add field to the fields list
                field_list = tag[c4d.FIELDS_LIST]
                if field_list is None:
                    field_list = c4d.FieldList()

                # Link field to the tag
                field_layer = c4d.FieldLayer()
                field_layer.SetLinkedObject(field)
                field_list.InsertLayer(field_layer)

                tag[c4d.FIELDS_LIST] = field_list
                doc.AddUndo(c4d.UNDOTYPE_CHANGE, tag)

            # Update the document
            c4d.EventAdd()

            return {
                "name": field.GetName(),
                "id": str(field.GetGUID()),
                "type": field_type,
                "applied_to": target_name if target_name else "None",
            }

        try:
            # Execute on main thread and get result
            field_info = self.execute_on_main_thread(
                apply_mograph_fields_safe,
                field_type,
                field_name,
                parameters,
                target,
                target_name,
            )
            return {"field": field_info}
        except Exception as e:
            return {"error": f"Failed to apply MoGraph field: {str(e)}"}

    def handle_create_soft_body(self, command):
        """Handle create_soft_body command."""
        doc = c4d.documents.GetActiveDocument()
        object_name = command.get("object_name", "")
        name = command.get("name", "Soft Body")
        stiffness = command.get("stiffness", 50)
        mass = command.get("mass", 1.0)

        # Find the target object (safe to do in worker thread)
        obj = self.find_object_by_name(doc, object_name)
        if obj is None:
            return {"error": f"Object not found: {object_name}"}

        # Define function to run on main thread
        def create_soft_body_safe(obj, name, stiffness, mass, object_name):
            # Create a Dynamics/Bullet tag - correct ID is 180000102
            tag = c4d.BaseTag(180000102)  # Proper Dynamics Body tag ID
            if tag is None:
                raise RuntimeError("Failed to create Dynamics Body tag")

            tag.SetName(name)

            # Set tag parameters for Soft Body
            tag[c4d.RIGID_BODY_DYNAMIC] = 1  # Enable dynamics
            tag[c4d.RIGID_BODY_MASS] = mass

            # Set to Soft Body type
            tag[c4d.RIGID_BODY_SOFTBODY] = True

            # Apply tag to object
            obj.InsertTag(tag)
            doc.AddUndo(c4d.UNDOTYPE_NEW, tag)

            # Update the document
            c4d.EventAdd()

            return {
                "object": object_name,
                "tag_name": tag.GetName(),
                "stiffness": stiffness,
                "mass": mass,
            }

        try:
            # Execute on main thread and get result
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
            # Find the target object
            obj = self.find_object_by_name(doc, object_name)
            if obj is None:
                return {"error": f"Object not found: {object_name}"}

            # Map tag types to C4D constants - using correct dynamic IDs
            tag_types = {
                "rigid_body": 180000102,  # Rigid Body tag
                "collider": 180000102,  # Also uses Dynamics tag, just different mode
                "connector": 180000103,  # Connector tag
                "ghost": 180000102,  # Also uses Dynamics tag, special mode
            }

            # Get the tag type ID
            tag_type_id = tag_types.get(tag_type, 180000102)  # Default to Rigid Body

            # Create the dynamics tag
            tag = c4d.BaseTag(tag_type_id)
            if tag is None:
                return {"error": f"Failed to create {tag_type} tag"}

            # Set tag parameters based on type
            if tag_type == "rigid_body":
                tag[c4d.RIGID_BODY_DYNAMIC] = 2  # Dynamic
            elif tag_type == "collider":
                tag[c4d.RIGID_BODY_DYNAMIC] = 0  # Static
            elif tag_type == "ghost":
                tag[c4d.RIGID_BODY_DYNAMIC] = 3  # Ghost

            # Set common parameters
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

            # Apply tag to object
            obj.InsertTag(tag)
            doc.AddUndo(c4d.UNDOTYPE_NEW, tag)

            # Update the document
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
            # Map shape types to C4D constants
            shape_types = {
                "metaball": 5159,  # Metaball
                "metaball_spline": 5161,  # Metaball Spline
                "loft": 5107,  # Loft NURBS
                "sweep": 5118,  # Sweep NURBS
                "atom": 5168,  # Atom Array
                "platonic": 5170,  # Platonic solid
                "cloth": 5186,  # Cloth NURBS
                "landscape": 5119,  # Landscape
                "extrude": 5116,  # Extrude NURBS
            }

            # Get the shape type ID
            shape_type_id = shape_types.get(shape_type, 5159)  # Default to Metaball

            # Create the shape
            shape = c4d.BaseObject(shape_type_id)
            if shape is None:
                return {"error": f"Failed to create {shape_type} object"}

            shape.SetName(name)

            # Set position
            if len(position) >= 3:
                shape.SetAbsPos(c4d.Vector(position[0], position[1], position[2]))

            # Create additional objects depending on the shape type
            if shape_type == "metaball":
                # Add a sphere as child
                sphere = c4d.BaseObject(c4d.Osphere)
                sphere.SetName("Metaball Sphere")
                sphere.SetAbsScale(c4d.Vector(0.5, 0.5, 0.5))
                sphere.InsertUnder(shape)
                doc.AddUndo(c4d.UNDOTYPE_NEW, sphere)

            elif shape_type == "loft" or shape_type == "sweep":
                # Add a spline to work with
                spline = c4d.BaseObject(c4d.Osplinecircle)
                spline.SetName("Profile Spline")
                spline.InsertUnder(shape)
                doc.AddUndo(c4d.UNDOTYPE_NEW, spline)

                # Add a second spline for loft/sweep
                path = c4d.BaseObject(c4d.Osplinenside)
                path.SetName("Path Spline")
                path.SetAbsPos(c4d.Vector(0, 50, 0))
                path.InsertUnder(shape)
                doc.AddUndo(c4d.UNDOTYPE_NEW, path)

            # Insert shape into document
            doc.InsertObject(shape)
            doc.AddUndo(c4d.UNDOTYPE_NEW, shape)

            # Update the document
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
            # Create light object
            light = c4d.BaseObject(c4d.Olight)
            if light is None:
                return {"error": "Failed to create light object"}

            light.SetName(name)

            # Set light type
            light_type_map = {
                "spot": 0,
                "point": 1,
                "distant": 2,
                "area": 3,
                "paraxial": 4,
                "parallel": 5,
                "omni": 1,  # alias for point
            }

            light_type_id = light_type_map.get(light_type, 1)  # Default to point
            light[c4d.LIGHT_TYPE] = light_type_id

            # Set position
            if len(position) >= 3:
                light.SetAbsPos(c4d.Vector(position[0], position[1], position[2]))

            # Set color
            if len(color) >= 3:
                light[c4d.LIGHT_COLOR] = c4d.Vector(color[0], color[1], color[2])

            # Set intensity
            light[c4d.LIGHT_BRIGHTNESS] = intensity

            # Set shadows on
            light[c4d.LIGHT_SHADOWTYPE] = 1  # Shadow maps

            # Insert light into document
            doc.InsertObject(light)
            doc.AddUndo(c4d.UNDOTYPE_NEW, light)

            # Update the document
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
        shader_type = command.get("shader_type", "noise").lower()
        channel = command.get("channel", "color").lower()
        parameters = command.get("parameters", {})

        try:
            # Find the material
            mat = self.find_material_by_name(doc, material_name)
            if mat is None:
                return {"error": f"Material not found: {material_name}"}

            # Map shader types to C4D constants
            shader_types = {
                "noise": 5832,  # Noise shader
                "gradient": 5825,  # Gradient shader
                "fresnel": 5837,  # Fresnel shader
                "layer": 5685,  # Layer shader
                "posterizer": 5847,  # Posterizer shader
                "colorizer": 5693,  # Colorizer shader
                "distorter": 5694,  # Distorter shader
                "spline": 5688,  # Spline shader
                "brick": 5834,  # Brick shader
                "marble": 5835,  # Marble shader
                "wood": 5836,  # Wood shader
                "checkerboard": 5831,  # Checkerboard shader
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

            # Get shader type ID
            shader_type_id = shader_types.get(shader_type, 5832)  # Default to Noise

            # Get channel ID
            channel_id = channel_map.get(channel, c4d.MATERIAL_COLOR_SHADER)

            # Create shader
            shader = c4d.BaseShader(shader_type_id)
            if shader is None:
                return {"error": f"Failed to create {shader_type} shader"}

            # Set shader parameters
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

            # Apply shader to material channel
            mat[channel_id] = shader

            # Enable the channel if not already enabled
            channel_enable_map = {
                "color": c4d.MATERIAL_USE_COLOR,
                "luminance": c4d.MATERIAL_USE_LUMINANCE,
                "transparency": c4d.MATERIAL_USE_TRANSPARENCY,
                "reflection": c4d.MATERIAL_USE_REFLECTION,
            }

            if channel in channel_enable_map:
                # Enable the channel
                enable_id = channel_enable_map.get(channel)
                if enable_id is not None:
                    mat[enable_id] = True

            # Update the material and document
            mat.Update(True, True)
            doc.AddUndo(c4d.UNDOTYPE_CHANGE, mat)
            c4d.EventAdd()

            return {
                "shader": {
                    "material": material_name,
                    "type": shader_type,
                    "channel": channel,
                    "applied": True,
                }
            }
        except Exception as e:
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
            # Find or create the camera
            camera = None

            if camera_name:
                camera = self.find_object_by_name(doc, camera_name)

            if camera is None or create_camera:
                # Create a new camera
                camera = c4d.BaseObject(c4d.Ocamera)
                camera.SetName(camera_name or "Animated Camera")

                # Set camera properties
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

                # Insert camera into document
                doc.InsertObject(camera)
                doc.AddUndo(c4d.UNDOTYPE_NEW, camera)

                # Set as active camera
                doc.SetActiveObject(camera)

            # Validate positions and frames
            if not positions or not frames or len(positions) != len(frames):
                return {
                    "error": "Invalid positions or frames data. Make sure they are of the same length."
                }

            # Set keyframes for camera positions
            for i in range(len(positions)):
                position = positions[i]
                frame = frames[i]

                if len(position) >= 3:
                    # Use helper method to set position keyframes
                    self.set_position_keyframe(camera, frame, position)

            # Add a spline path if requested
            if path_type == "spline" and len(positions) > 1:
                # Create a spline object to hold the camera path
                path = c4d.BaseObject(c4d.Ospline)
                path.SetName(f"{camera.GetName()} Path")

                # Create a spline with the camera positions
                points = [
                    c4d.Vector(p[0], p[1], p[2]) for p in positions if len(p) >= 3
                ]
                path.ResizeObject(len(points))

                for i, point in enumerate(points):
                    path.SetPoint(i, point)

                # Insert path into document
                doc.InsertObject(path)
                doc.AddUndo(c4d.UNDOTYPE_NEW, path)

                # Determine if camera should align to path
                align_to_path = path_type == "spline_oriented"

                # Create Path constraint tag
                path_tag = c4d.BaseTag(c4d.Talignment)
                path_tag[c4d.ALIGNMENTOBJECT_LINK] = path
                path_tag[c4d.ALIGNMENTOBJECT_ALIGN] = align_to_path

                # Add tag to camera
                camera.InsertTag(path_tag)
                doc.AddUndo(c4d.UNDOTYPE_NEW, path_tag)

            # Update the document
            c4d.EventAdd()

            return {
                "camera_animation": {
                    "camera": camera.GetName(),
                    "path_type": path_type,
                    "keyframe_count": len(positions),
                    "frame_range": [min(frames), max(frames)] if frames else [0, 0],
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
            # Map of effector type names to C4D constants
            effector_types = {
                "random": c4d.Omgrandom,  # Random Effector
                "formula": c4d.Omgformula,  # Formula Effector
                "step": c4d.Omgstep,  # Step Effector
                "target": c4d.Omgtarget,  # Target Effector
                "time": c4d.Omgtime,  # Time Effector
                "sound": c4d.Omgsound,  # Sound Effector
                "plain": c4d.Omgplain,  # Plain Effector
                "delay": c4d.Omgdelay,  # Delay Effector
                "spline": c4d.Omgspline,  # Spline Effector
                "python": c4d.Omgpython,  # Python Effector
                "falloff": c4d.Omgfalloff,  # Falloff Effector
            }

            # Get effector type ID
            effector_type = effector_types.get(
                type_name, c4d.Omgrandom
            )  # Default to Random Effector

            # Create the effector
            effector = c4d.BaseObject(effector_type)
            if effector is None:
                return {"error": f"Failed to create {type_name} effector"}

            effector.SetName(name)

            # Apply properties if specified
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

            # Insert effector into document
            doc.InsertObject(effector)
            doc.AddUndo(c4d.UNDOTYPE_NEW, effector)

            # Find the cloner
            if cloner_name:
                cloner = self.find_object_by_name(doc, cloner_name)
                if cloner is None:
                    return {"error": f"Cloner not found: {cloner_name}"}

                # Make sure it's a cloner
                if cloner.GetType() != c4d.Omgcloner:
                    return {"error": f"Object '{cloner_name}' is not a MoGraph Cloner"}

                # Add effector to cloner using InExcludeData
                effector_list = cloner[c4d.ID_MG_MOTIONGENERATOR_EFFECTORLIST]
                if not isinstance(effector_list, c4d.InExcludeData):
                    effector_list = c4d.InExcludeData()

                # Add with "enabled" flag (1)
                effector_list.InsertObject(effector, 1)
                cloner[c4d.ID_MG_MOTIONGENERATOR_EFFECTORLIST] = effector_list
                doc.AddUndo(c4d.UNDOTYPE_CHANGE, cloner)

            # Update the document
            c4d.EventAdd()

            return {
                "effector": {
                    "name": effector.GetName(),
                    "id": str(effector.GetGUID()),
                    "type": type_name,
                    "applied_to_cloner": bool(cloner_name),
                }
            }
        except Exception as e:
            return {"error": f"Failed to create effector: {str(e)}"}

    # All helper methods
    def find_material_by_name(self, doc, name):
        """Find a material by name."""
        for mat in doc.GetMaterials():
            if mat.GetName() == name:
                return mat
        return None

    def find_object_by_name(self, doc, name):
        """Find an object by name in the document."""
        obj = doc.GetFirstObject()
        while obj:
            if obj.GetName() == name:
                return obj
            obj = obj.GetNext()
        return None

    def count_objects(self, doc):
        # Define a function to safely count objects on the main thread
        def count_objects_safe(doc):
            count = 0
            obj = doc.GetFirstObject()
            while obj:
                count += 1
                obj = obj.GetNext()
            return count

        return self.execute_on_main_thread(count_objects_safe, doc)

    def count_polygons(self, doc):
        # Define a function to safely count polygons on the main thread
        def count_polygons_safe(doc):
            count = 0
            obj = doc.GetFirstObject()
            while obj:
                if obj.GetType() == c4d.Opolygon:
                    count += obj.GetPolygonCount()
                obj = obj.GetNext()
            return count

        return self.execute_on_main_thread(count_polygons_safe, doc)

    def get_object_type_name(self, obj):
        """Get a human-readable object type name."""
        type_id = obj.GetType()

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
            c4d.Omgcloner: "MoGraph Cloner",
            c4d.Ospline: "Spline",
            c4d.Osplinenside: "N-Side Spline",
            c4d.Osplinecircle: "Circle Spline",
            c4d.Osplinetext: "Text Spline",
            c4d.Otorus: "Torus",
            c4d.Opyrmaind: "Pyramid",
            c4d.Oextrude: "Extrude NURBS",
            c4d.Oloft: "Loft NURBS",
            c4d.Osweep: "Sweep NURBS",
            c4d.Oplatonic: "Platonic",
            5159: "Metaball",  # Metaball object
            1057829: "Field",  # Field object
            180000102: "Dynamics",  # Dynamics object
        }

        # Try to get object name from type map
        if type_id in type_map:
            return type_map[type_id]
        else:
            # For unknown types, get the object's base type
            base_type = obj.GetTypeName()
            if base_type:
                return base_type
            else:
                # Fallback to object plugin ID as string
                return f"Object ({type_id})"


class SocketServerDialog(gui.GeDialog):
    """GUI Dialog to control the server and display logs."""

    def __init__(self):
        super(SocketServerDialog, self).__init__()
        self.server = None
        self.msg_queue = queue.Queue()  # Thread-safe queue for communication
        self.SetTimer(100)  # Updates UI every 100ms

    def CreateLayout(self):
        """Set up UI elements."""
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
        """Handles UI updates triggered by SpecialEventAdd()."""
        if id == PLUGIN_ID:
            while not self.msg_queue.empty():
                msg_type, msg_value = self.msg_queue.get()
                if msg_type == "STATUS":
                    self.UpdateStatusText(msg_value)
                elif msg_type == "LOG":
                    self.AppendLog(msg_value)
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
        """Executed when the plugin is run from C4D"""
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

    def RestoreLayout(self, sec_ref):
        """Restores the dialog layout when C4D reopens"""
        if self.dialog is None:
            self.dialog = SocketServerDialog()
        return self.dialog.Restore(pluginid=self.PLUGIN_ID, secret=sec_ref)


def PluginMessage(id, data):
    """Handles plugin messages for initialization and cleanup."""
    if id == c4d.C4DPL_ENDPROGRAM:
        if hasattr(c4d, "SpecialEventAdd"):
            c4d.SpecialEventAdd(PLUGIN_ID)  # Notify the UI about shutdown
        print("[C4D] Plugin is shutting down.")
    return True  # Ensure C4D continues normal processing


if __name__ == "__main__":
    c4d.plugins.RegisterCommandPlugin(
        SocketServerPlugin.PLUGIN_ID,
        SocketServerPlugin.PLUGIN_NAME,
        0,
        None,
        None,
        SocketServerPlugin(),
    )
