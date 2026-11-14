"""全局配置：路径、端点、P2P 与运行参数，持久化到 <home>/settings.json。"""

from __future__ import annotations

import json
import os
import secrets
import socket
import uuid
from pathlib import Path

from pydantic import BaseModel, Field

APP_NAME = "AetherNode"
DEFAULT_PORT = 8737
DEFAULT_RELAY_PORT = 8738

# 公共 Nostr relay，作为零配置的发现通道。连不上不影响本地功能。
DEFAULT_PUBLIC_RELAYS = [
    "wss://relay.damus.io",
    "wss://nos.lol",
    "wss://relay.nostr.band",
]


def default_home() -> Path:
    env = os.environ.get("AETHER_HOME")
    if env:
        return Path(env).expanduser().resolve()
    return Path(__file__).resolve().parent.parent / "data"


class Settings(BaseModel):
    # ---- 模型来源 ----
    hf_endpoint: str = "https://huggingface.co"
    hf_token: str = ""

    # ---- 本地服务 ----
    # 默认只监听回环：把 GPU 暴露到网络是显式动作，不是默认行为。
    # 开启 share_enabled 后才绑 0.0.0.0，并强制生成 api_key。
    api_host: str = "127.0.0.1"
    api_port: int = DEFAULT_PORT
    api_key: str = ""
    share_enabled: bool = False

    # ---- 存储 ----
    home: Path = Field(default_factory=default_home)

    # ---- 推理运行参数 ----
    llama_bin: str = ""
    context_limit: int = 8192
    parallel_slots: int = 1
    gpu_layers: int = -1
    auto_start_on_boot: bool = False

    # ---- P2P ----
    node_name: str = Field(default_factory=lambda: socket.gethostname()[:32])
    region: str = ""
    public_relays: list[str] = Field(default_factory=lambda: list(DEFAULT_PUBLIC_RELAYS))
    extra_relays: list[str] = Field(default_factory=list)
    local_relay_enabled: bool = True
    local_relay_port: int = DEFAULT_RELAY_PORT
    advertise: bool = False
    share_policy: str = "free"  # free | metered
    price_in: float = 0.0
    price_out: float = 0.0
    # 手动指定对外可达地址（有公网域名/端口映射时填），留空则按 api_host 推断
    public_endpoint: str = ""

    # ---- 路由 ----
    auto_route: bool = True
    prefer_local: bool = True
    route_timeout: float = 120.0

    # ---- 审计（抽样复算）----
    audit_rate: float = 0.0          # 0 = 关闭；0.05 表示 5% 的跨节点非流式请求被复算
    # 中文短答对字符级相似度敏感（一个空格就能掉 0.03），阈值取 0.85 更合适
    audit_threshold: float = 0.85
    audit_seed: int = 42

    # ---- 防护 ----
    relay_rate_limit: int = 60        # 每个节点每分钟最多发起的隧道请求数，0 = 不限
    relay_replay_window: int = 300    # 请求 id 去重窗口（秒）
    tunnel_max_concurrency: int = 4   # 本节点同时处理的隧道请求上限
    api_max_concurrency: int = 4      # /v1 同时处理的请求上限

    # ---- 自有外部 API（纳入统一路由与计量）----
    # 每项形如：
    #   {"name": "my-openai", "endpoint": "https://api.openai.com", "api_key": "sk-...",
    #    "models": ["gpt-4o-mini"], "price": {"in": 0.15, "out": 0.6}}
    external_providers: list[dict] = Field(default_factory=list)
    external_score: float = 38.0      # 外部端点的基础分：高于网络节点（可用性更稳），低于本机

    # ---- 内部 ----
    install_id: str = Field(default_factory=lambda: uuid.uuid4().hex)

    # 派生路径 ------------------------------------------------------------
    @property
    def models_path(self) -> Path:
        return self.home / "models"

    @property
    def runtime_path(self) -> Path:
        return self.home / "runtime"

    @property
    def downloads_path(self) -> Path:
        return self.home / "downloads"

    @property
    def settings_file(self) -> Path:
        return self.home / "settings.json"

    @property
    def logs_path(self) -> Path:
        return self.home / "logs"

    # 便捷 ---------------------------------------------------------------
    def effective_host(self) -> str:
        """实际绑定的地址：只有显式开启共享才对外。"""
        if self.share_enabled:
            return "0.0.0.0"
        return self.api_host or "127.0.0.1"

    def ensure_api_key(self) -> str:
        """共享模式下必须有鉴权凭证；缺失就生成一个并落盘。"""
        if not self.share_enabled:
            return self.api_key
        if self.api_key:
            return self.api_key
        self.api_key = secrets.token_urlsafe(24)
        return self.api_key

    def all_relays(self) -> list[str]:
        out: list[str] = []
        for url in [*self.public_relays, *self.extra_relays]:
            url = url.strip()
            if url and url not in out:
                out.append(url)
        return out

    def local_relay_url(self) -> str:
        """内置 relay 复用 API 端口，挂在 /relay 路径。

        端口必须取进程实际监听的那个（命令行可能覆盖配置），否则会连错端口。
        """
        return f"ws://127.0.0.1:{get_runtime_port()}/relay"

    def api_base(self) -> str:
        return f"http://{self.api_host}:{self.api_port}"


_settings: Settings | None = None


def _ensure_dirs(s: Settings) -> None:
    for p in (s.home, s.models_path, s.runtime_path, s.downloads_path, s.logs_path):
        p.mkdir(parents=True, exist_ok=True)


def load_settings() -> Settings:
    """读取配置；文件不存在或损坏时回退默认值。"""
    global _settings
    home = default_home()
    home.mkdir(parents=True, exist_ok=True)
    path = home / "settings.json"
    data: dict = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    # home 以环境变量为准，避免配置文件把数据目录指到别处
    data.pop("home", None)
    # 老配置里显式写了 0.0.0.0 说明本来就想共享：保留行为，但要补上鉴权
    if data.get("api_host") == "0.0.0.0" and not data.get("share_enabled"):
        data["share_enabled"] = True
        data["api_host"] = "127.0.0.1"
    _settings = Settings(home=home, **{k: v for k, v in data.items() if k in Settings.model_fields})
    _ensure_dirs(_settings)
    if _settings.share_enabled and not _settings.api_key:
        _settings.ensure_api_key()
        save_settings(_settings)
    return _settings


def get_settings() -> Settings:
    if _settings is None:
        return load_settings()
    return _settings


def save_settings(updated: Settings | None = None) -> Settings:
    global _settings
    if updated is not None:
        _settings = updated
    s = _settings or load_settings()
    _ensure_dirs(s)
    payload = s.model_dump(mode="json")
    payload.pop("home", None)
    s.settings_file.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return s


def update_settings(**kwargs) -> Settings:
    s = get_settings().model_copy(update=kwargs)
    return save_settings(s)


_runtime_port: int = 0
_runtime_host: str = ""


def set_runtime_port(port: int) -> None:
    """记录进程实际监听的端口（可能被命令行覆盖）。"""
    global _runtime_port
    _runtime_port = int(port)


def get_runtime_port() -> int:
    return _runtime_port or get_settings().api_port


def set_runtime_host(host: str) -> None:
    """记录进程实际监听的地址，用于判定自身可达性。"""
    global _runtime_host
    _runtime_host = host or ""


def get_runtime_host() -> str:
    return _runtime_host or get_settings().api_host
