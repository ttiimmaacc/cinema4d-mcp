# __init__.py
from . import server
import asyncio

def main():
    """Main entry point for the package."""
    # Run the MCP application
    server.mcp_app.run()

# Optionally expose other important items at package level
__all__ = ['main', 'server']