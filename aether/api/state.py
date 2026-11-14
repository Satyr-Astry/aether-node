"""应用状态单例。

单独成模块是为了打断循环导入：routes_* 需要 get_state，
而 app.py 又要 import routes_*。
"""

from __future__ import annotations

from aether.config import load_settings
from aether.downloads import DownloadManager
from aether.identity import get_identity
from aether.ledger import get_ledger
from aether.library import ModelLibrary
from aether.llama_server import runtime
from aether.p2p.service import P2PService


class AppState:
    def __init__(self) -> None:
        self.library = ModelLibrary()
        self.downloads = DownloadManager(self.library)
        self.p2p = P2PService(self.library, runtime)
        self.ledger = get_ledger()

    @property
    def identity(self):
        return get_identity()


_state: AppState | None = None


def get_state() -> AppState:
    global _state
    if _state is None:
        load_settings()
        _state = AppState()
    return _state
