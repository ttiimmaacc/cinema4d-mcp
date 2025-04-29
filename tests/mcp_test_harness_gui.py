import socket
import json
import time
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext
import threading
import os
import copy  # Needed for deep copying commands

SERVER_HOST = "localhost"
SERVER_PORT = 5555


class MCPTestHarnessGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("MCP Test Harness")

        # --- GUI Elements ---
        self.file_label = tk.Label(root, text="Test File:")
        self.file_label.grid(row=0, column=0, sticky="w")
        self.file_entry = tk.Entry(root, width=50)
        self.file_entry.grid(row=0, column=1)
        self.browse_button = tk.Button(root, text="Browse", command=self.browse_file)
        self.browse_button.grid(row=0, column=2)
        self.run_button = tk.Button(root, text="Run Test", command=self.run_test_thread)
        self.run_button.grid(row=1, column=1, pady=10)
        self.log_text = scrolledtext.ScrolledText(
            root, wrap=tk.WORD, width=80, height=30
        )
        self.log_text.grid(row=2, column=0, columnspan=3)

        # --- ADDED: Storage for GUIDs ---
        self.guid_map = {}  # Maps requested_name -> actual_guid

    def browse_file(self):
        filename = filedialog.askopenfilename(
            filetypes=[("JSON Lines", "*.jsonl"), ("All Files", "*.*")]
        )
        if filename:
            self.file_entry.delete(0, tk.END)
            self.file_entry.insert(0, filename)

    def log(self, message):
        # --- Modified to handle updates from thread ---
        def update_log():
            self.log_text.insert(tk.END, message + "\n")
            self.log_text.see(tk.END)

        # Schedule the update in the main Tkinter thread
        self.root.after(0, update_log)
        print(message)  # Also print to console

    def run_test_thread(self):
        # Disable run button during test
        self.run_button.config(state=tk.DISABLED)
        # Clear log and GUID map for new run
        self.log_text.delete(1.0, tk.END)
        self.guid_map = {}
        # Start the test in a separate thread
        threading.Thread(target=self.run_test, daemon=True).start()

    # --- ADDED: Recursive substitution function ---
    def substitute_placeholders(self, data_structure):
        """Recursively substitutes known names with GUIDs in dicts and lists."""
        if isinstance(data_structure, dict):
            new_dict = {}
            for key, value in data_structure.items():
                # Substitute the value if it's a string matching a known name
                if isinstance(value, str) and value in self.guid_map:
                    new_dict[key] = self.guid_map[value]
                    self.log(
                        f"    Substituted '{key}': '{value}' -> '{self.guid_map[value]}'"
                    )
                # Recursively process nested structures
                elif isinstance(value, (dict, list)):
                    new_dict[key] = self.substitute_placeholders(value)
                else:
                    new_dict[key] = value
            return new_dict
        elif isinstance(data_structure, list):
            new_list = []
            for item in data_structure:
                # Substitute the item if it's a string matching a known name
                if isinstance(item, str) and item in self.guid_map:
                    new_list.append(self.guid_map[item])
                    self.log(
                        f"    Substituted item in list: '{item}' -> '{self.guid_map[item]}'"
                    )
                # Recursively process nested structures
                elif isinstance(item, (dict, list)):
                    new_list.append(self.substitute_placeholders(item))
                else:
                    new_list.append(item)
            return new_list
        else:
            # If it's not a dict or list, check if the value itself needs substitution
            if isinstance(data_structure, str) and data_structure in self.guid_map:
                substituted_value = self.guid_map[data_structure]
                self.log(
                    f"    Substituted value: '{data_structure}' -> '{substituted_value}'"
                )
                return substituted_value
            return data_structure  # Return other types unchanged

    def run_test(self):
        test_file = self.file_entry.get()
        if not os.path.exists(test_file):
            messagebox.showerror("Error", "Test file does not exist.")
            self.run_button.config(state=tk.NORMAL)  # Re-enable button
            return

        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                self.log(f"Connecting to MCP server at {SERVER_HOST}:{SERVER_PORT}...")
                sock.connect((SERVER_HOST, SERVER_PORT))
                self.log("Connected ✅\n--- Test Start ---")

                with open(test_file, "r", encoding="utf-8") as f:
                    for line_num, line in enumerate(f, 1):
                        if not line.strip():
                            continue  # Skip empty lines

                        try:
                            # Load original command from file
                            original_command = json.loads(line.strip())
                            self.log(
                                f"\n▶️ Command {line_num} (Original): {json.dumps(original_command)}"
                            )

                            # --- MODIFIED: Substitute placeholders before sending ---
                            # Deep copy to avoid modifying the original dict before logging
                            command_to_send = copy.deepcopy(original_command)
                            command_to_send = self.substitute_placeholders(
                                command_to_send
                            )

                            if command_to_send != original_command:
                                self.log(
                                    f"   Command {line_num} (Substituted): {json.dumps(command_to_send)}"
                                )

                            # Send the potentially modified command
                            sock.sendall(
                                (json.dumps(command_to_send) + "\n").encode("utf-8")
                            )

                            # Receive response (increase buffer size significantly for base64 previews)
                            response_data = b""
                            sock.settimeout(120.0)  # Set generous timeout for receiving
                            while True:
                                try:
                                    chunk = sock.recv(32768)  # Read larger chunks
                                    if not chunk:
                                        # Connection closed prematurely?
                                        if not response_data:
                                            raise ConnectionAbortedError(
                                                "Server closed connection unexpectedly before sending response."
                                            )
                                        break  # No more data
                                    response_data += chunk
                                    # Basic check for newline delimiter, might need refinement for large data
                                    if b"\n" in chunk:
                                        break
                                except socket.timeout:
                                    # Check if we received *any* data before timeout
                                    if not response_data:
                                        raise TimeoutError(
                                            f"Timeout waiting for response to Command {line_num}"
                                        )
                                    else:
                                        self.log(
                                            f"    Warning: Socket timeout, but received partial data ({len(response_data)} bytes). Assuming complete."
                                        )
                                        break  # Process what we got
                            sock.settimeout(None)  # Reset timeout

                            # Decode and parse response
                            response_text = response_data.decode("utf-8").strip()
                            if not response_text:
                                raise ValueError("Received empty response from server.")

                            decoded_response = json.loads(response_text)

                            # Log the response (truncate potentially huge base64 data)
                            loggable_response = copy.deepcopy(decoded_response)
                            if isinstance(loggable_response, dict):
                                # Check common keys for base64 data and truncate
                                for key in ["image_base64", "image_data"]:
                                    if (
                                        key in loggable_response
                                        and isinstance(loggable_response[key], str)
                                        and len(loggable_response[key]) > 100
                                    ):
                                        loggable_response[key] = (
                                            loggable_response[key][:50]
                                            + "... [truncated]"
                                        )
                                # Also check within nested 'render' dict for snapshot
                                if "render" in loggable_response and isinstance(
                                    loggable_response["render"], dict
                                ):
                                    for key in ["image_base64", "image_data"]:
                                        render_dict = loggable_response["render"]
                                        if (
                                            key in render_dict
                                            and isinstance(render_dict[key], str)
                                            and len(render_dict[key]) > 100
                                        ):
                                            render_dict[key] = (
                                                render_dict[key][:50]
                                                + "... [truncated]"
                                            )

                            self.log(
                                f"✅ Response {line_num}: {json.dumps(loggable_response, indent=2)}"
                            )

                            # --- ADDED: Capture GUID from response ---
                            if isinstance(decoded_response, dict):
                                # Check common patterns for created objects
                                context_keys = [
                                    "object",
                                    "light",
                                    "camera",
                                    "material",
                                    "cloner",
                                    "effector",
                                    "field",
                                    "shape",
                                    "group",
                                ]
                                for key in context_keys:
                                    if key in decoded_response and isinstance(
                                        decoded_response[key], dict
                                    ):
                                        obj_info = decoded_response[key]
                                        req_name = obj_info.get("requested_name")
                                        guid = obj_info.get("guid")
                                        act_name = obj_info.get("actual_name")
                                        if req_name and guid:
                                            self.guid_map[req_name] = guid
                                            self.log(
                                                f"    Captured GUID: '{req_name}' -> {guid} (Actual name: '{act_name}')"
                                            )
                                            # Also map actual name if different, preferring requested name if collision
                                            if (
                                                act_name
                                                and act_name != req_name
                                                and act_name not in self.guid_map
                                            ):
                                                self.guid_map[act_name] = guid
                                                self.log(
                                                    f"    Mapped actual name: '{act_name}' -> {guid}"
                                                )
                                        break  # Assume only one primary object context per response

                            # Brief pause between commands
                            time.sleep(0.1)

                        except json.JSONDecodeError as e:
                            self.log(f"❌ Error decoding JSON for line {line_num}: {e}")
                            self.log(f"   Raw line: {line.strip()}")
                            break  # Stop test on error
                        except Exception as cmd_e:
                            self.log(f"❌ Error processing command {line_num}: {cmd_e}")
                            import traceback

                            self.log(traceback.format_exc())
                            break  # Stop test on error

                self.log("--- Test End ---")

        except ConnectionRefusedError:
            self.log(
                f"❌ Connection Refused: Ensure C4D plugin server is running on {SERVER_HOST}:{SERVER_PORT}."
            )
            messagebox.showerror(
                "Connection Error",
                "Connection Refused. Is the Cinema 4D plugin server running?",
            )
        except socket.timeout:
            self.log(
                f"❌ Connection Timeout: Could not connect to {SERVER_HOST}:{SERVER_PORT}."
            )
            messagebox.showerror("Connection Error", "Connection Timeout.")
        except Exception as e:
            self.log(f"❌ Unexpected Error: {str(e)}")
            import traceback

            self.log(traceback.format_exc())
            messagebox.showerror("Error", f"An unexpected error occurred:\n{str(e)}")
        finally:
            # Re-enable run button after test finishes or errors out
            self.root.after(0, lambda: self.run_button.config(state=tk.NORMAL))


if __name__ == "__main__":
    root = tk.Tk()
    app = MCPTestHarnessGUI(root)
    root.mainloop()
