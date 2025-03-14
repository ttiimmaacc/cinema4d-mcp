"""
Cinema 4D Socket Server Plugin

This plugin creates a socket server inside Cinema 4D that listens for
commands from the MCP server and executes them in the C4D environment.

Installation:
1. Place this file in Cinema 4D's scripts folder
2. Start Cinema 4D and run this script
3. The socket server will start and listen on port 5555
"""

import c4d
import socket
import threading
import json
import time

class C4DSocketServer:
    def __init__(self, host='127.0.0.1', port=5555):
        self.host = host
        self.port = port
        self.socket = None
        self.running = False
        self.thread = None
    
    def start(self):
        """Start the socket server."""
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        
        try:
            self.socket.bind((self.host, self.port))
            self.socket.listen(1)  # Allow only one connection
            
            print(f"[C4D] Socket server started on {self.host}:{self.port}")
            self.running = True
            
            # Start listener thread
            self.thread = threading.Thread(target=self.accept_connections)
            self.thread.daemon = True
            self.thread.start()
            
            return True
        except Exception as e:
            print(f"[C4D] Error starting socket server: {str(e)}")
            return False
    
    def stop(self):
        """Stop the socket server."""
        self.running = False
        if self.socket:
            self.socket.close()
        print("[C4D] Socket server stopped")
    
    def accept_connections(self):
        """Accept and handle client connections."""
        while self.running:
            try:
                client, addr = self.socket.accept()
                print(f"[C4D] Client connected from {addr}")
                
                # Handle client in a new thread
                client_thread = threading.Thread(target=self.handle_client, args=(client,))
                client_thread.daemon = True
                client_thread.start()
                
            except Exception as e:
                if self.running:  # Only log if not intentionally shut down
                    print(f"[C4D] Error accepting connection: {str(e)}")
                break
    
    def handle_client(self, client):
        """Handle commands from a connected client."""
        buffer = ""
        
        while self.running:
            try:
                # Receive data
                data = client.recv(4096)
                if not data:
                    break
                
                # Add to buffer and process complete messages
                buffer += data.decode('utf-8')
                
                # Process complete messages (separated by newlines)
                while '\n' in buffer:
                    message, buffer = buffer.split('\n', 1)
                    
                    # Parse and process the command
                    command = json.loads(message)
                    response = self.process_command(command)
                    
                    # Send response
                    client.sendall((json.dumps(response) + '\n').encode('utf-8'))
                
            except Exception as e:
                print(f"[C4D] Error handling client: {str(e)}")
                break
        
        client.close()
        print("[C4D] Client disconnected")
    
    def process_command(self, command):
        """Process a command and return a response."""
        command_type = command.get("command", "")
        
        try:
            if command_type == "get_scene_info":
                return self.handle_get_scene_info()
            elif command_type == "add_primitive":
                return self.handle_add_primitive(command)
            elif command_type == "modify_object":
                return self.handle_modify_object(command)
            elif command_type == "list_objects":
                return self.handle_list_objects()
            elif command_type == "create_material":
                return self.handle_create_material(command)
            elif command_type == "apply_material":
                return self.handle_apply_material(command)
            elif command_type == "render_frame":
                return self.handle_render_frame(command)
            elif command_type == "set_keyframe":
                return self.handle_set_keyframe(command)
            elif command_type == "save_scene":
                return self.handle_save_scene(command)
            elif command_type == "load_scene":
                return self.handle_load_scene(command)
            elif command_type == "execute_python":
                return self.handle_execute_python(command)
            else:
                return {"error": f"Unknown command: {command_type}"}
        except Exception as e:
            print(f"[C4D] Error processing command: {str(e)}")
            return {"error": f"Error processing command: {str(e)}"}
    
    def handle_get_scene_info(self):
        """Handle get_scene_info command."""
        doc = c4d.documents.GetActiveDocument()
        
        scene_info = {
            "filename": doc.GetDocumentName(),
            "object_count": self.count_objects(doc),
            "polygon_count": self.count_polygons(doc),
            "material_count": len(doc.GetMaterials()),
            "current_frame": doc.GetTime().GetFrame(doc.GetFps()),
            "fps": doc.GetFps(),
            "frame_start": doc.GetMinTime().GetFrame(doc.GetFps()),
            "frame_end": doc.GetMaxTime().GetFrame(doc.GetFps())
        }
        
        return {"scene_info": scene_info}
    
    def handle_add_primitive(self, command):
        """Handle add_primitive command."""
        doc = c4d.documents.GetActiveDocument()
        primitive_type = command.get("type", "cube").lower()
        name = command.get("name", primitive_type)
        position = command.get("position", [0, 0, 0])
        size = command.get("size", [100, 100, 100])
        
        obj = None
        
        # Create the appropriate primitive
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
            return {"error": f"Unknown primitive type: {primitive_type}"}
        
        # Set position and name
        obj.SetAbsPos(c4d.Vector(*position))
        obj.SetName(name)
        
        # Add to document
        doc.InsertObject(obj)
        
        # Update Cinema 4D
        c4d.EventAdd()
        
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
        if not obj:
            return {"error": f"Object not found: {object_name}"}
        
        # Apply properties
        for prop, value in properties.items():
            if prop == "position":
                if isinstance(value, list) and len(value) == 3:
                    obj.SetAbsPos(c4d.Vector(*value))
            elif prop == "rotation":
                if isinstance(value, list) and len(value) == 3:
                    rad_values = [v * 3.14159265359 / 180.0 for v in value]  # Convert to radians
                    obj.SetRotation(c4d.Vector(*rad_values))
            elif prop == "scale":
                if isinstance(value, list) and len(value) == 3:
                    obj.SetScale(c4d.Vector(*value))
            elif prop == "name":
                obj.SetName(str(value))
            else:
                # For other properties, try to set directly
                try:
                    obj[prop] = value
                except:
                    print(f"[C4D] Could not set property {prop}")
        
        # Update Cinema 4D
        c4d.EventAdd()
        
        return {
            "success": True,
            "object_name": obj.GetName()
        }
    
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
    
    def handle_create_material(self, command):
        """Handle create_material command."""
        doc = c4d.documents.GetActiveDocument()
        name = command.get("name", "Material")
        color = command.get("color", [1, 1, 1])
        properties = command.get("properties", {})
        
        # Create material
        mat = c4d.BaseMaterial(c4d.Mmaterial)
        mat.SetName(name)
        
        # Set color
        if len(color) >= 3:
            mat[c4d.MATERIAL_COLOR_COLOR] = c4d.Vector(color[0], color[1], color[2])
        
        # Apply additional properties
        for prop, value in properties.items():
            try:
                mat[prop] = value
            except:
                print(f"[C4D] Could not set material property {prop}")
        
        # Insert material in document
        doc.InsertMaterial(mat)
        
        # Update Cinema 4D
        c4d.EventAdd()
        
        return {
            "material": {
                "id": str(mat.GetGUID()),
                "name": mat.GetName(),
                "color": [mat[c4d.MATERIAL_COLOR_COLOR].x, mat[c4d.MATERIAL_COLOR_COLOR].y, mat[c4d.MATERIAL_COLOR_COLOR].z]
            }
        }
    
    def handle_apply_material(self, command):
        """Handle apply_material command."""
        doc = c4d.documents.GetActiveDocument()
        material_name = command.get("material_name", "")
        object_name = command.get("object_name", "")
        
        # Find material
        mat = self.find_material_by_name(doc, material_name)
        if not mat:
            return {"error": f"Material not found: {material_name}"}
        
        # Find object
        obj = self.find_object_by_name(doc, object_name)
        if not obj:
            return {"error": f"Object not found: {object_name}"}
        
        # Apply material
        tag = obj.MakeTag(c4d.Ttexture)
        tag[c4d.TEXTURETAG_MATERIAL] = mat
        
        # Update Cinema 4D
        c4d.EventAdd()
        
        return {
            "success": True,
            "material_name": material_name,
            "object_name": object_name
        }
    
    def handle_render_frame(self, command):
        """Handle render_frame command."""
        doc = c4d.documents.GetActiveDocument()
        output_path = command.get("output_path", None)
        width = command.get("width", None)
        height = command.get("height", None)
        
        # Save render settings
        old_width = doc.GetActiveRenderData()[c4d.RDATA_XRES]
        old_height = doc.GetActiveRenderData()[c4d.RDATA_YRES]
        old_path = doc.GetActiveRenderData()[c4d.RDATA_PATH]
        
        # Apply new settings
        if width is not None:
            doc.GetActiveRenderData()[c4d.RDATA_XRES] = width
        if height is not None:
            doc.GetActiveRenderData()[c4d.RDATA_YRES] = height
        if output_path is not None:
            doc.GetActiveRenderData()[c4d.RDATA_PATH] = output_path
        
        # Render
        start_time = time.time()
        bitmap = c4d.documents.RenderDocument(doc, doc.GetActiveRenderData(), c4d.RENDERFLAGS_EXTERNAL)
        end_time = time.time()
        
        if not bitmap:
            # Restore settings
            doc.GetActiveRenderData()[c4d.RDATA_XRES] = old_width
            doc.GetActiveRenderData()[c4d.RDATA_YRES] = old_height
            doc.GetActiveRenderData()[c4d.RDATA_PATH] = old_path
            c4d.EventAdd()
            
            return {"error": "Render failed"}
        
        # Save if path specified
        final_path = output_path if output_path else doc.GetActiveRenderData()[c4d.RDATA_PATH]
        if final_path:
            bitmap.Save(final_path, c4d.SAVEBIT_ALPHA)
        
        # Restore settings
        doc.GetActiveRenderData()[c4d.RDATA_XRES] = old_width
        doc.GetActiveRenderData()[c4d.RDATA_YRES] = old_height
        doc.GetActiveRenderData()[c4d.RDATA_PATH] = old_path
        c4d.EventAdd()
        
        return {
            "render_info": {
                "path": final_path,
                "width": bitmap.GetBw(),
                "height": bitmap.GetBh(),
                "render_time": end_time - start_time
            }
        }
    
    def handle_set_keyframe(self, command):
        """Handle set_keyframe command."""
        doc = c4d.documents.GetActiveDocument()
        object_name = command.get("object_name", "")
        property_name = command.get("property_name", "")
        value = command.get("value")
        frame = command.get("frame", 0)
        
        # Find object
        obj = self.find_object_by_name(doc, object_name)
        if not obj:
            return {"error": f"Object not found: {object_name}"}
        
        # Set document time
        doc.SetTime(c4d.BaseTime(frame, doc.GetFps()))
        
        # Set property value
        if "." in property_name:
            prop_parts = property_name.split(".")
            base_prop = prop_parts[0]
            sub_prop = prop_parts[1]
            
            if base_prop == "position":
                pos = obj.GetAbsPos()
                if sub_prop == "x":
                    pos.x = value
                elif sub_prop == "y":
                    pos.y = value
                elif sub_prop == "z":
                    pos.z = value
                obj.SetAbsPos(pos)
            elif base_prop == "rotation":
                rot = obj.GetRotation()
                value_rad = value * 3.14159265359 / 180.0  # Convert to radians
                if sub_prop == "x":
                    rot.x = value_rad
                elif sub_prop == "y":
                    rot.y = value_rad
                elif sub_prop == "z":
                    rot.z = value_rad
                obj.SetRotation(rot)
            elif base_prop == "scale":
                scale = obj.GetScale()
                if sub_prop == "x":
                    scale.x = value
                elif sub_prop == "y":
                    scale.y = value
                elif sub_prop == "z":
                    scale.z = value
                obj.SetScale(scale)
            else:
                return {"error": f"Unknown property: {property_name}"}
        else:
            try:
                obj[property_name] = value
            except:
                return {"error": f"Could not set property: {property_name}"}
        
        # Create keyframe
        track = obj.FindCTrack(property_name)
        if not track:
            track = c4d.CTrack(obj, c4d.DescID(c4d.DescLevel(c4d.ID_BASEOBJECT_REL_POSITION, c4d.DTYPE_VECTOR, 0),
                                         c4d.DescLevel(0, c4d.DTYPE_REAL, 0)))
            obj.InsertTrackSorted(track)
        
        curve = track.GetCurve()
        key = curve.AddKey(c4d.BaseTime(frame, doc.GetFps()))
        
        # Update Cinema 4D
        c4d.EventAdd()
        
        return {
            "success": True,
            "object_name": object_name,
            "property_name": property_name,
            "value": value,
            "frame": frame
        }
    
    def handle_save_scene(self, command):
        """Handle save_scene command."""
        doc = c4d.documents.GetActiveDocument()
        file_path = command.get("file_path", None)
        
        if not file_path:
            # Use existing path or create a default one
            if doc.GetDocumentPath() and doc.GetDocumentName():
                file_path = os.path.join(doc.GetDocumentPath(), doc.GetDocumentName())
            else:
                file_path = os.path.expanduser("~/Desktop/scene.c4d")
        
        # Ensure .c4d extension
        if not file_path.lower().endswith(".c4d"):
            file_path += ".c4d"
        
        # Save document
        if c4d.documents.SaveDocument(doc, file_path, c4d.SAVEDOCUMENTFLAGS_DONTADDTORECENTLIST, c4d.FORMAT_C4DEXPORT):
            return {
                "save_info": {
                    "path": file_path,
                    "success": True
                }
            }
        else:
            return {"error": f"Failed to save scene to {file_path}"}
    
    def handle_load_scene(self, command):
        """Handle load_scene command."""
        file_path = command.get("file_path", "")
        
        if not os.path.exists(file_path):
            return {"error": f"File not found: {file_path}"}
        
        # Load document
        loaded_doc = c4d.documents.LoadDocument(file_path, c4d.SCENEFILTER_NONE)
        if not loaded_doc:
            return {"error": f"Failed to load scene from {file_path}"}
        
        # Set as active document
        c4d.documents.SetActiveDocument(loaded_doc)
        c4d.EventAdd()
        
        return {
            "success": True,
            "file_path": file_path
        }
    
    def handle_execute_python(self, command):
        """Handle execute_python command."""
        script = command.get("script", "")
        
        # Create a string buffer for capturing output
        import io
        import sys
        
        old_stdout = sys.stdout
        redirected_output = io.StringIO()
        sys.stdout = redirected_output
        
        try:
            # Execute the script
            exec(script)
            output = redirected_output.getvalue()
        except Exception as e:
            output = f"Error: {str(e)}"
        finally:
            # Restore stdout
            sys.stdout = old_stdout
        
        return {
            "result": output
        }
    
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
        mat = doc.GetFirstMaterial()
        while mat:
            if mat.GetName() == name:
                return mat
            mat = mat.GetNext()
        return None
    
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

# Global instance of the server
socket_server = None

def main():
    global socket_server
    if socket_server is None:
        socket_server = C4DSocketServer()
        socket_server.start()

if __name__ == '__main__':
    main()