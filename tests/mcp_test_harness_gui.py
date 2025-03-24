import socket
import json
import time
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext
import threading
import os


SERVER_HOST = "localhost"
SERVER_PORT = 5555
# python3 mcp_test_harness_gui.py


class MCPTestHarnessGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("MCP Test Harness")

        # File selection
        self.file_label = tk.Label(root, text="Test File:")
        self.file_label.grid(row=0, column=0, sticky="w")
        self.file_entry = tk.Entry(root, width=50)
        self.file_entry.grid(row=0, column=1)
        self.browse_button = tk.Button(root, text="Browse", command=self.browse_file)
        self.browse_button.grid(row=0, column=2)

        # Run button
        self.run_button = tk.Button(root, text="Run Test", command=self.run_test_thread)
        self.run_button.grid(row=1, column=1, pady=10)

        # Output log
        self.log_text = scrolledtext.ScrolledText(
            root, wrap=tk.WORD, width=80, height=30
        )
        self.log_text.grid(row=2, column=0, columnspan=3)

    def browse_file(self):
        filename = filedialog.askopenfilename(
            filetypes=[("JSON Lines", "*.jsonl"), ("All Files", "*.*")]
        )
        if filename:
            self.file_entry.delete(0, tk.END)
            self.file_entry.insert(0, filename)

    def log(self, message):
        self.log_text.insert(tk.END, message + "\n")
        self.log_text.see(tk.END)
        print(message)

    def run_test_thread(self):
        threading.Thread(target=self.run_test, daemon=True).start()

    def run_test(self):
        test_file = self.file_entry.get()
        if not os.path.exists(test_file):
            messagebox.showerror("Error", "Test file does not exist.")
            return

        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                self.log(f"Connecting to MCP server at {SERVER_HOST}:{SERVER_PORT}...")
                sock.connect((SERVER_HOST, SERVER_PORT))
                self.log("Connected ✅\n")

                with open(test_file, "r", encoding="utf-8") as f:
                    for line_num, line in enumerate(f, 1):
                        command = json.loads(line.strip())
                        self.log(f"▶️ Command {line_num}: {command['command']}")
                        sock.sendall((json.dumps(command) + "\n").encode("utf-8"))
                        response = sock.recv(65536)
                        decoded = json.loads(response.decode("utf-8"))
                        self.log(
                            f"✅ Response {line_num}: {json.dumps(decoded, indent=2)}\n"
                        )
                        time.sleep(0.5)
        except Exception as e:
            self.log(f"❌ Error: {str(e)}")


if __name__ == "__main__":
    root = tk.Tk()
    app = MCPTestHarnessGUI(root)
    root.mainloop()
