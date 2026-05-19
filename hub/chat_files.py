"""User workspace file management for chat training platform."""
from __future__ import annotations

import os
import shutil
from typing import Optional


def sanitize_path(path: str) -> str:
    """Reject path traversal and absolute paths. Return clean relative path."""
    if not path:
        raise ValueError("empty path")
    if os.path.isabs(path):
        raise ValueError("absolute paths not allowed")
    if ".." in path.split(os.sep):
        raise ValueError("path traversal not allowed")
    if ".." in path.split("/"):
        raise ValueError("path traversal not allowed")
    return path


class WorkspaceManager:
    def __init__(self, workspace_root: str, template_root: Optional[str] = None):
        self.workspace_root = workspace_root
        self.template_root = template_root or os.path.join(workspace_root, "..", "training-templates")
        os.makedirs(self.workspace_root, exist_ok=True)
        os.makedirs(self.template_root, exist_ok=True)

    def get_user_path(self, user_id: str) -> str:
        path = os.path.join(self.workspace_root, user_id)
        os.makedirs(path, exist_ok=True)
        return path

    def resolve_path(self, user_path: str, relative_path: str) -> str:
        """Resolve a relative path within the user's workspace. Raises on traversal."""
        clean = sanitize_path(relative_path)
        full = os.path.normpath(os.path.join(user_path, clean))
        if not full.startswith(os.path.normpath(user_path)):
            raise ValueError("path escapes user workspace")
        return full

    def copy_template_files(self, template_id: str, user_path: str) -> None:
        src = os.path.join(self.template_root, template_id)
        if not os.path.isdir(src):
            return  # No starter files for this template
        dest = os.path.join(user_path, template_id)
        os.makedirs(dest, exist_ok=True)
        for item in os.listdir(src):
            s = os.path.join(src, item)
            d = os.path.join(dest, item)
            if os.path.isdir(s):
                shutil.copytree(s, d, dirs_exist_ok=True)
            else:
                shutil.copy2(s, d)

    def list_directory(self, user_path: str, relative_path: str) -> list[dict]:
        full = self.resolve_path(user_path, relative_path)
        if not os.path.isdir(full):
            return []
        entries = []
        for name in sorted(os.listdir(full)):
            path = os.path.join(full, name)
            is_dir = os.path.isdir(path)
            size = 0 if is_dir else os.path.getsize(path)
            entries.append({"name": name, "is_directory": is_dir, "size": size})
        return entries

    def read_file(self, user_path: str, relative_path: str) -> bytes:
        full = self.resolve_path(user_path, relative_path)
        if not os.path.isfile(full):
            raise FileNotFoundError(f"file not found: {relative_path}")
        with open(full, "rb") as f:
            return f.read()

    def write_file(self, user_path: str, relative_path: str, content: bytes) -> None:
        full = self.resolve_path(user_path, relative_path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "wb") as f:
            f.write(content)

    def delete_file(self, user_path: str, relative_path: str) -> None:
        full = self.resolve_path(user_path, relative_path)
        if os.path.isdir(full):
            shutil.rmtree(full)
        elif os.path.isfile(full):
            os.remove(full)
