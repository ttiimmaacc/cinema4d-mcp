"""Configuration handling for Cinema 4D MCP Server."""

import os

# Default configuration
C4D_HOST = os.environ.get('C4D_HOST', '127.0.0.1')
C4D_PORT = int(os.environ.get('C4D_PORT', 5555))