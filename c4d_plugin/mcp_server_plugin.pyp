import c4d
from c4d import gui
import socket
import threading
import json
import time
import queue
import os

PLUGIN_ID = 1057843  # Unique plugin ID for SpecialEventAdd

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
                while '\n' in buffer:
                    message, buffer = buffer.split('\n', 1)
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
            "polygon_count": self.count_polygons(doc),
            "material_count": len(doc.GetMaterials()),
            "current_frame": doc.GetTime().GetFrame(doc.GetFps()),
            "fps": doc.GetFps(),
            "frame_start": doc.GetMinTime().GetFrame(doc.GetFps()),
            "frame_end": doc.GetMaxTime().GetFrame(doc.GetFps())
        }
        
        return {"scene_info": scene_info}

    def handle_list_objects(self):
        """Handle list_objects command."""
        doc = c4d.documents.GetActiveDocument()
        objects = []
        
        obj = doc.GetFirstObject()
        while obj:
            objects.append({
                "name": obj.GetName(),
                "type": self.get_object_type_name(obj),
                "id": str(obj.GetGUID())
            })
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
        doc.SetActiveObject(obj)
        
        # Update the document
        c4d.EventAdd()
        
        # Return information about the created object
        return {
            "object": {
                "name": obj.GetName(),
                "id": str(obj.GetGUID()),
                "position": [obj.GetAbsPos().x, obj.GetAbsPos().y, obj.GetAbsPos().z]
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
        if "position" in properties and isinstance(properties["position"], list) and len(properties["position"]) >= 3:
            pos = properties["position"]
            obj.SetAbsPos(c4d.Vector(pos[0], pos[1], pos[2]))
            modified["position"] = pos
        
        # Rotation (in degrees)
        if "rotation" in properties and isinstance(properties["rotation"], list) and len(properties["rotation"]) >= 3:
            rot = properties["rotation"]
            # Convert degrees to radians
            rot_rad = [c4d.utils.DegToRad(r) for r in rot]
            obj.SetRotation(c4d.Vector(rot_rad[0], rot_rad[1], rot_rad[2]))
            modified["rotation"] = rot
        
        # Scale
        if "scale" in properties and isinstance(properties["scale"], list) and len(properties["scale"]) >= 3:
            scale = properties["scale"]
            obj.SetScale(c4d.Vector(scale[0], scale[1], scale[2]))
            modified["scale"] = scale
        
        # Color (if object has a base color channel)
        if "color" in properties and isinstance(properties["color"], list) and len(properties["color"]) >= 3:
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
                "modified": modified
            }
        }

    def handle_create_material(self, command):
        """Handle create_material command."""
        doc = c4d.documents.GetActiveDocument()
        name = command.get("name", "New Material")
        color = command.get("color", [1, 1, 1])
        properties = command.get("properties", {})
        
        try:
            # Create a new material
            mat = c4d.BaseMaterial(c4d.Mmaterial)
            mat.SetName(name)
            
            # Set base color
            if len(color) >= 3:
                color_vector = c4d.Vector(color[0], color[1], color[2])
                mat[c4d.MATERIAL_COLOR_COLOR] = color_vector
            
            # Apply additional properties (if needed)
            if "specular" in properties and isinstance(properties["specular"], list) and len(properties["specular"]) >= 3:
                spec = properties["specular"]
                mat[c4d.MATERIAL_SPECULAR_COLOR] = c4d.Vector(spec[0], spec[1], spec[2])
            
            if "reflection" in properties and isinstance(properties["reflection"], (int, float)):
                mat[c4d.MATERIAL_REFLECTION_BRIGHTNESS] = float(properties["reflection"])
            
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
                    "name": mat.GetName(),
                    "id": material_id,
                    "color": color
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
                "message": f"Applied material '{material_name}' to object '{object_name}'"
            }
        except Exception as e:
            return {"error": f"Failed to apply material: {str(e)}"}

    def handle_render_frame(self, command):
        """Handle render_frame command."""
        doc = c4d.documents.GetActiveDocument()
        output_path = command.get("output_path", None)
        width = command.get("width", None)
        height = command.get("height", None)
        
        # Set render settings if provided
        rd = doc.GetActiveRenderData().GetClone()
        
        if width is not None and height is not None:
            rd.SetParameter(c4d.RDATA_XRES, width)
            rd.SetParameter(c4d.RDATA_YRES, height)
        
        # Set output path if provided
        if output_path is not None:
            rd.SetParameter(c4d.RDATA_PATH, output_path)
            # Make sure the directory exists
            output_dir = os.path.dirname(output_path)
            if not os.path.exists(output_dir):
                try:
                    os.makedirs(output_dir)
                except:
                    pass
        
        try:
            # Get start time for timing
            start_time = time.time()
            
            # Render the frame
            bitmap = c4d.documents.RenderDocument(doc, rd.GetData(), None, None, None)
            
            # Calculate render time
            render_time = time.time() - start_time
            
            if bitmap is None:
                return {"error": "Failed to render frame"}
            
            # Save to file if path is specified
            path = "Memory only"
            if output_path:
                if bitmap.Save(output_path, c4d.SAVEBIT_0):
                    path = output_path
                else:
                    return {"error": f"Failed to save render to {output_path}"}
            
            # Return render info
            return {
                "render_info": {
                    "path": path,
                    "width": bitmap.GetWidth(),
                    "height": bitmap.GetHeight(),
                    "render_time": render_time
                }
            }
        except Exception as e:
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
                "position.x": c4d.ID_BASEOBJECT_POSITION_X,
                "position.y": c4d.ID_BASEOBJECT_POSITION_Y,
                "position.z": c4d.ID_BASEOBJECT_POSITION_Z,
                "rotation.h": c4d.ID_BASEOBJECT_ROTATION_H,
                "rotation.p": c4d.ID_BASEOBJECT_ROTATION_P,
                "rotation.b": c4d.ID_BASEOBJECT_ROTATION_B,
                "scale.x": c4d.ID_BASEOBJECT_SCALE_X,
                "scale.y": c4d.ID_BASEOBJECT_SCALE_Y,
                "scale.z": c4d.ID_BASEOBJECT_SCALE_Z
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
                "message": f"Keyframe set for {object_name}.{property_name} = {value} at frame {frame}"
            }
        except Exception as e:
            return {"error": f"Failed to set keyframe: {str(e)}"}

    def handle_save_scene(self, command):
        """Handle save_scene command."""
        doc = c4d.documents.GetActiveDocument()
        file_path = command.get("file_path", None)
        
        try:
            # If no path is provided, use the current one
            if file_path is None:
                file_path = doc.GetDocumentPath() + "/" + doc.GetDocumentName()
                
                # If document has no path yet, return error
                if not doc.GetDocumentPath() or not doc.GetDocumentName():
                    return {"error": "No save path specified and document has no current path"}
            
            # Make sure path has proper extension
            if not file_path.lower().endswith(".c4d"):
                file_path += ".c4d"
            
            # Make sure directory exists
            directory = os.path.dirname(file_path)
            if directory and not os.path.exists(directory):
                try:
                    os.makedirs(directory)
                except:
                    return {"error": f"Failed to create directory {directory}"}
            
            # Save document
            if c4d.documents.SaveDocument(doc, file_path, c4d.SAVEDOCUMENTFLAGS_DONTADDTORECENTLIST, c4d.Format("C4D")):
                return {
                    "save_info": {
                        "success": True,
                        "path": file_path
                    }
                }
            else:
                return {"error": f"Failed to save document to {file_path}"}
                
        except Exception as e:
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
            loaded_doc = c4d.documents.LoadDocument(file_path, c4d.SCENEFILTER_OBJECTS | c4d.SCENEFILTER_MATERIALS)
            
            if loaded_doc is None:
                return {"error": f"Failed to load document from {file_path}"}
            
            # Make it the active document
            c4d.documents.SetActiveDocument(loaded_doc)
            
            # Update the C4D UI
            c4d.EventAdd()
            
            return {
                "success": True,
                "message": f"Loaded scene from {file_path}"
            }
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
            local_env = {
                "doc": c4d.documents.GetActiveDocument(),
                "c4d": c4d
            }
            
            # Execute the script
            exec(script, globals(), local_env)
            
            # Restore original print
            __builtins__["print"] = original_print
            
            # Ensure UI is updated
            c4d.EventAdd()
            
            return {
                "result": output_dict["output"] or "Script executed successfully with no output."
            }
        except Exception as e:
            return {"error": f"Script execution failed: {str(e)}"}

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
        
        type_map = {
            c4d.Ocube: "Cube",
            c4d.Osphere: "Sphere",
            c4d.Ocone: "Cone", 
            c4d.Ocylinder: "Cylinder",
            c4d.Oplane: "Plane",
            c4d.Olight: "Light",
            c4d.Ocamera: "Camera",
            c4d.Onull: "Null",
            c4d.Opolygon: "Polygon Object"
        }
        
        return type_map.get(type_id, f"Object (Type: {type_id})")

    def find_object_by_name(self, doc, name):
        """Find an object by name in the document."""
        obj = doc.GetFirstObject()
        while obj:
            if obj.GetName() == name:
                return obj
            obj = obj.GetNext()
        return None

    def find_material_by_name(self, doc, name):
        """Find a material by name in the document."""
        materials = doc.GetMaterials()
        for mat in materials:
            if mat.GetName() == name:
                return mat
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

        self.status_text = self.AddStaticText(1002, c4d.BFH_SCALEFIT, name="Server: Offline")

        self.GroupBegin(1010, c4d.BFH_SCALEFIT, 2, 1)
        self.AddButton(1011, c4d.BFH_SCALE, name="Start Server")
        self.AddButton(1012, c4d.BFH_SCALE, name="Stop Server")
        self.GroupEnd()

        self.log_box = self.AddMultiLineEditText(1004, c4d.BFH_SCALEFIT, initw=400, inith=250, style=c4d.DR_MULTILINE_READONLY)

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
        if self.dialog is None:
            self.dialog = SocketServerDialog()
        return self.dialog.Open(dlgtype=c4d.DLG_TYPE_ASYNC, pluginid=self.PLUGIN_ID, defaultw=400, defaulth=300)

    def GetState(self, doc):
        return c4d.CMD_ENABLED

if __name__ == "__main__":
    c4d.plugins.RegisterCommandPlugin(SocketServerPlugin.PLUGIN_ID, SocketServerPlugin.PLUGIN_NAME, 0, None, None, SocketServerPlugin())