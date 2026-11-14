"""本地模型库：扫描 GGUF、维护索引、提供对外 model id。"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from aether.config import get_settings
from aether.gguf import read_gguf_meta


@dataclass
class LocalModel:
    key: str
    repo: str
    file: str
    path: str
    size: int
    quant: str
    added_at: float = field(default_factory=time.time)
    meta: dict = field(default_factory=dict)
    loaded: bool = False

    @property
    def display(self) -> str:
        base = self.repo.rsplit("/", 1)[-1]
        return f"{base}:{self.quant}"

    @property
    def short_name(self) -> str:
        return self.repo.rsplit("/", 1)[-1]

    def to_dict(self) -> dict:
        d = asdict(self)
        d.update({
            "display": self.display,
            "short_name": self.short_name,
            "size_gb": round(self.size / (1 << 30), 2),
            "loaded": self.loaded,
        })
        return d


class ModelLibrary:
    def __init__(self) -> None:
        self._settings = get_settings()
        self._index_file = self._settings.models_path / "index.json"
        self._models: dict[str, LocalModel] = {}
        self.load()

    # ---- 持久化 ----
    def load(self) -> None:
        raw: dict = {}
        if self._index_file.exists():
            try:
                raw = json.loads(self._index_file.read_text(encoding="utf-8"))
            except Exception:
                raw = {}
        self._models = {}
        for key, item in raw.items():
            try:
                self._models[key] = LocalModel(**item)
            except TypeError:
                continue
        self.rescan()

    def save(self) -> None:
        self._settings.models_path.mkdir(parents=True, exist_ok=True)
        data = {k: asdict(v) for k, v in self._models.items()}
        self._index_file.write_text(
            json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    def rescan(self) -> None:
        """补齐磁盘上存在但索引里没有的 GGUF，剔除已删除的。"""
        root = self._settings.models_path
        changed = False
        seen: set[str] = set()
        for p in root.rglob("*.gguf"):
            seen.add(str(p))
        for key, m in list(self._models.items()):
            if m.path not in seen or not Path(m.path).exists():
                del self._models[key]
                changed = True

        known_paths = {m.path for m in self._models.values()}
        for p in root.rglob("*.gguf"):
            if str(p) in known_paths:
                continue
            rel = p.relative_to(root)
            repo_dir = rel.parts[0] if len(rel.parts) > 1 else ""
            repo = repo_dir.replace("__", "/") if repo_dir else p.stem
            fname = rel.parts[-1]
            from aether.hub import QUANT_RE  # 延迟导入避免循环

            quant_match = QUANT_RE.search(fname)
            model = LocalModel(
                key=f"{repo}::{fname}",
                repo=repo,
                file=fname,
                path=str(p),
                size=p.stat().st_size,
                quant=quant_match.group(1).upper() if quant_match else "?",
                meta=read_gguf_meta(p).summary(),
            )
            self._models[model.key] = model
            changed = True
        if changed:
            self.save()

    # ---- 查询 ----
    def all(self) -> list[LocalModel]:
        return sorted(self._models.values(), key=lambda m: m.added_at, reverse=True)

    def get(self, key: str) -> LocalModel | None:
        return self._models.get(key)

    def by_model_id(self, model_id: str) -> LocalModel | None:
        for m in self._models.values():
            if m.display == model_id:
                return m
        return None

    def find_by_path(self, path: str) -> LocalModel | None:
        for m in self._models.values():
            if m.path == path:
                return m
        return None

    # ---- 变更 ----
    def register(self, repo: str, file: str, path: Path) -> LocalModel:
        from aether.hub import QUANT_RE

        quant_match = QUANT_RE.search(file)
        model = LocalModel(
            key=f"{repo}::{file}",
            repo=repo,
            file=file,
            path=str(path),
            size=path.stat().st_size,
            quant=quant_match.group(1).upper() if quant_match else "?",
            meta=read_gguf_meta(path).summary(),
        )
        self._models[model.key] = model
        self.save()
        return model

    def remove(self, key: str, delete_file: bool = True) -> bool:
        m = self._models.get(key)
        if not m:
            return False
        if delete_file:
            try:
                Path(m.path).unlink(missing_ok=True)
                parent = Path(m.path).parent
                if parent != self._settings.models_path and not any(parent.iterdir()):
                    parent.rmdir()
            except OSError:
                pass
        del self._models[key]
        self.save()
        return True

    def mark_loaded(self, key: str | None) -> None:
        for k, m in self._models.items():
            m.loaded = (k == key)
        self.save()
