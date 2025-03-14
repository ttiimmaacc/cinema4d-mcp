#!/usr/bin/env python3
"""
Cinema 4D MCP Server - Main entry point script

This script starts the Cinema 4D MCP server either directly or through
package imports, allowing it to be run both as a script and as a module.
"""

import sys
import os
import socket
import logging
import traceback

# Configure logging to stderr
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler(sys.stderr)
    ]
)

logger = logging.getLogger("cinema4d-mcp")

# Add the src directory to the Python path
src_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'src')
if os.path.exists(src_path):
    sys.path.insert(0, src_path)

# Add the project root to Python path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

def log_to_stderr(message):
    """Log a message to stderr for Claude Desktop to capture."""
    print(message, file=sys.stderr, flush=True)

def main():
    """Main entry point function."""
    log_to_stderr("========== CINEMA 4D MCP SERVER STARTING ==========")
    log_to_stderr(f"Python version: {sys.version}")
    log_to_stderr(f"Current directory: {os.getcwd()}")
    log_to_stderr(f"Python path: {sys.path}")

    # Check if Cinema 4D socket is available
    c4d_host = os.environ.get('C4D_HOST', '127.0.0.1')
    c4d_port = int(os.environ.get('C4D_PORT', 5555))

    log_to_stderr(f"Checking connection to Cinema 4D on {c4d_host}:{c4d_port}")
    try:
        test_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        test_socket.settimeout(5)  # Set 5 second timeout
        test_socket.connect((c4d_host, c4d_port))
        test_socket.close()
        log_to_stderr("✅ Successfully connected to Cinema 4D socket!")
    except Exception as e:
        log_to_stderr(f"❌ Could not connect to Cinema 4D socket: {e}")
        log_to_stderr("   The server will still start, but Cinema 4D integration won't work!")

    try:
        log_to_stderr("Importing cinema4d_mcp...")
        from cinema4d_mcp import main as package_main
        
        log_to_stderr("🚀 Starting Cinema 4D MCP Server...")
        package_main()
    except Exception as e:
        log_to_stderr(f"❌ Error starting server: {e}")
        log_to_stderr(traceback.format_exc())
        sys.exit(1)

if __name__ == "__main__":
    main()