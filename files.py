import os
from pathlib import Path

# Where the /files route serves from when R2 is not configured. Overridable
# because the default only exists inside the container: the Dockerfile creates
# and chowns it, and /app is not writable when the server runs from a checkout.
# Deliberately not created at import — src/server.py imports src/http_app.py,
# so an mkdir here would run on every stdio start too.
FILES_DIR = Path(os.getenv("MCP_FILES_DIR", "/app/files"))
