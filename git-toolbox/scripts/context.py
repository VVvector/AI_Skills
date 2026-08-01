import re
import threading
from pathlib import Path
from typing import List, Optional, Dict, Any


class GitToolContext:
    def __init__(
        self,
        worktree_path: str,
        prompts_path: Optional[str] = None,
    ):
        self.worktree_path: Path = Path(worktree_path)
        self.prompts_path: Optional[Path] = Path(prompts_path) if prompts_path else None
        self._active_patch_files: List[str] = []
        self._active_patch_files_lock = threading.Lock()
        self._virtual_head: Optional[str] = None
        self._virtual_head_lock = threading.Lock()
        self.cache: Dict[str, Any] = {}
        self._cache_lock = threading.Lock()

    @property
    def active_patch_files(self) -> List[str]:
        with self._active_patch_files_lock:
            return list(self._active_patch_files)

    @active_patch_files.setter
    def active_patch_files(self, files: List[str]) -> None:
        with self._active_patch_files_lock:
            self._active_patch_files = list(files)

    @property
    def virtual_head(self) -> Optional[str]:
        with self._virtual_head_lock:
            return self._virtual_head

    @virtual_head.setter
    def virtual_head(self, sha: Optional[str]) -> None:
        with self._virtual_head_lock:
            self._virtual_head = sha

    def virtualize_ref(self, r: str) -> str:
        vhead = self.virtual_head
        if vhead is None:
            return r

        def _replace(m):
            return f"{m.group(1)}{vhead}{m.group(2)}"

        return re.sub(r"(^|[^/])\bHEAD($|[~^:.@])", _replace, r)

    def get_cache(self, key: str) -> Optional[Any]:
        with self._cache_lock:
            return self.cache.get(key)

    def set_cache(self, key: str, value: Any) -> None:
        with self._cache_lock:
            self.cache[key] = value