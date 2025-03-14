# main.py
import sys
import os

# Add the 'src' directory to the Python path
sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'src'))

from cinema4d_mcp import main

if __name__ == "__main__":
    print("🚀 Starting Cinema 4D MCP Server...")
    main()