"""Tools added on top of the upstream Grok-MCP server.

Upstream `src/` is left untouched so `git pull` keeps working. Each module here
exposes `register(mcp)`, called from http_server.py at startup. A module may
replace an upstream tool by popping its name first:

    mcp._tool_manager._tools.pop("generate_video", None)

FastMCP's add_tool keeps the FIRST registration for a name and only logs a
warning on a duplicate, so the pop is required for a replacement to take effect.
"""

def register_all(mcp):
    # Imported here, not at module level: src/http_app.py imports ext.cutout
    # for FILES_DIR before `mcp` exists in src/server.py, and importing any
    # submodule runs this file first, so a module-level import here would
    # drag every ext module's dependencies into that chain too.
    from ext import audio, brand, cutout, images, notify, render, storage, video

    for module in (video, audio, render, storage, notify, brand, images, cutout):
        module.register(mcp)
