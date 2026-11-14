"""模型下载任务管理：异步下载、进度事件、取消，完成后登记进模型库。"""

from __future__ import annotations

import asyncio
import shutil
import time
import uuid
from dataclasses import dataclass, field

from aether.config import get_settings
from aether.events import bus
from aether.hub import HubClient
from aether.library import ModelLibrary


@dataclass
class DownloadTask:
    id: str
    repo: str
    file: str
    status: str = "pending"  # pending | running | done | failed | cancelled
    done: int = 0
    total: int = 0
    speed: float = 0.0
    error: str = ""
    started_at: float = field(default_factory=time.time)
    finished_at: float = 0.0
    cancel: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def pct(self) -> float:
        return round(self.done / self.total * 100, 1) if self.total else 0.0

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "repo": self.repo,
            "file": self.file,
            "status": self.status,
            "done": self.done,
            "total": self.total,
            "pct": self.pct,
            "speed_mbps": round(self.speed / (1 << 20), 2),
            "error": self.error,
            "elapsed": round((self.finished_at or time.time()) - self.started_at, 1),
        }


class DownloadManager:
    def __init__(self, library: ModelLibrary) -> None:
        self.library = library
        self.tasks: dict[str, DownloadTask] = {}
        self._workers: dict[str, asyncio.Task] = {}

    def _hub(self) -> HubClient:
        s = get_settings()
        return HubClient(s.hf_endpoint, s.hf_token)

    async def create(self, repo: str, file: str) -> DownloadTask:
        for t in self.tasks.values():
            if t.repo == repo and t.file == file and t.status in ("pending", "running"):
                return t  # 同一文件不重复排队
        task = DownloadTask(id=uuid.uuid4().hex[:12], repo=repo, file=file)
        self.tasks[task.id] = task
        self._workers[task.id] = asyncio.create_task(self._run(task), name=f"dl:{task.id}")
        return task

    async def _run(self, task: DownloadTask) -> None:
        s = get_settings()
        hub = self._hub()
        task.status = "running"
        bus.publish("download.state", task.to_dict())

        def progress(done: int, total: int, speed: float) -> None:
            task.done, task.total, task.speed = done, total, speed
            bus.publish("download.progress", task.to_dict())

        try:
            src = await hub.download(
                task.repo, task.file, s.downloads_path, progress, task.cancel
            )
            dest_dir = s.models_path / task.repo.replace("/", "__")
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / task.file.replace("/", "__")
            if dest.exists():
                dest.unlink()
            await asyncio.to_thread(shutil.move, str(src), str(dest))
            model = self.library.register(task.repo, task.file.replace("/", "__"), dest)
            task.status = "done"
            bus.publish("download.state", task.to_dict())
            bus.publish("model.added", model.to_dict())
        except asyncio.CancelledError:
            task.status = "cancelled"
        except Exception as exc:
            task.status = "failed"
            task.error = str(exc)[:300]
            bus.publish("download.state", task.to_dict())
        finally:
            task.finished_at = time.time()
            await hub.aclose()
            self._workers.pop(task.id, None)

    def cancel(self, task_id: str) -> bool:
        task = self.tasks.get(task_id)
        if not task or task.status not in ("pending", "running"):
            return False
        task.cancel.set()
        worker = self._workers.get(task_id)
        if worker:
            worker.cancel()
        task.status = "cancelled"
        task.finished_at = time.time()
        bus.publish("download.state", task.to_dict())
        return True

    def list(self) -> list[dict]:
        return [t.to_dict() for t in sorted(self.tasks.values(), key=lambda x: x.started_at, reverse=True)]

    def clear_finished(self) -> int:
        done = [tid for tid, t in self.tasks.items() if t.status in ("done", "failed", "cancelled")]
        for tid in done:
            self.tasks.pop(tid, None)
        return len(done)
