"""Cinema 4D MCP Server - Connect Claude to Cinema 4D"""

__version__ = "0.1.0"

from . import server

def main():
    """Main entry point for the package."""
    server.mcp_app.run()

def main_wrapper():
    """Entry point for the wrapper script."""
    main()