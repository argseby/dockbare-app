"""Dockbare — Docker volume backup, mirroring, encryption and recovery."""

__version__ = "3.0.0"
APP_NAME = "Dockbare"


def build_id() -> str:
    """Short fingerprint of the shipped source files, to tell deployments apart."""
    import hashlib
    import os

    h = hashlib.sha256()
    root = os.path.dirname(__file__)
    for dirpath, _dirs, files in sorted(os.walk(root)):
        if "__pycache__" in dirpath:
            continue
        for name in sorted(files):
            if name.endswith((".py", ".js", ".css", ".html")):
                path = os.path.join(dirpath, name)
                h.update(path[len(root):].encode())
                with open(path, "rb") as f:
                    h.update(f.read())
    return h.hexdigest()[:10]
