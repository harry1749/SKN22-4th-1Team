import os
import sys
from pathlib import Path

import uvicorn


def _as_bool(value: str, default: bool = True) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


if __name__ == "__main__":
    base_dir = Path(__file__).resolve().parent
    project_root = base_dir.parent

    # Ensure imports are stable regardless of current working directory.
    os.chdir(base_dir)
    if str(base_dir) not in sys.path:
        sys.path.insert(0, str(base_dir))
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "skn22_4th_prj.settings")

    host = os.getenv("UVICORN_HOST", "127.0.0.1")
    port = int(os.getenv("UVICORN_PORT", "8000"))
    reload_enabled = _as_bool(os.getenv("UVICORN_RELOAD"), default=True)

    uvicorn.run(
        "skn22_4th_prj.asgi:application",
        host=host,
        port=port,
        reload=reload_enabled,
        reload_dirs=[str(base_dir)],
    )
