import os
import sys
from pathlib import Path
from dotenv import load_dotenv
from src import main

load_dotenv(".env" if Path(".env").exists() else "example.env")

if __name__ == "__main__":

    if not os.getenv("XAI_API_KEY"):
        print(" XAI_API_KEY not found in environment.", file=sys.stderr)
        print("Please set your API key in example.env file or export it: export XAI_API_KEY='your_api_key' ", file=sys.stderr)
    else:
        print("XAI_API_KEY found", file=sys.stderr)
        print("Started Grok MCP server", file=sys.stderr)

    main()
