# C4D Socket Listener - This would be a Cinema 4D plugin
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
        # This method would be implemented to handle all the commands
        # that our MCP server sends.
        
        # Example implementation:
        command_type = command.get("command", "")
        
        if command_type == "get_scene_info":
            return self.handle_get_scene_info()
        elif command_type == "add_primitive":
            return self.handle_add_primitive(command)
        elif command_type == "modify_object":
            return self.handle_modify_object(command)
        # Add handlers for all other commands...
        
        return {"error": f"Unknown command: {command_type}"}
    
    def handle_get_scene_info(self):
        """Handle get_scene_info command."""
        doc = c4d.documents.GetActiveDocument()
        
        # Get scene information
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
        primitive_type = command.get("type", "cube")
        name = command.get("name", primitive_type)
        position = command.get("position", [0, 0, 0])
        size = command.get("size", [100, 100, 100])
        
        # Code to actually create the primitive in Cinema 4D
        # would go here
        
        # For this example, return a mock response
        return {
            "object": {
                "name": name,
                "id": "obj_12345",  # This would be generated by C4D
                "position": position
            }
        }
    
    # Add more handler methods for other commands...
    
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

# Global instance of the server
socket_server = None

def main():
    global socket_server
    if socket_server is None:
        socket_server = C4DSocketServer()
        socket_server.start()

if __name__ == '__main__':
    main()