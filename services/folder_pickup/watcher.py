"""Filesystem watcher and startup scan for folder pickup."""

from __future__ import annotations

import logging
import queue
import threading
from pathlib import Path
from typing import Dict, Optional, Set

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from config import settings
from services.folder_pickup.file_utils import (
    ensure_pickup_dirs,
    is_incoming_file,
    list_incoming_files,
    move_file,
    wait_for_file_stable,
)
from services.folder_pickup.processor import process_pickup_file

logger = logging.getLogger(__name__)


class _PickupEventHandler(FileSystemEventHandler):
    def __init__(self, service: "FolderPickupService") -> None:
        self._service = service

    def on_created(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        self._service.schedule_enqueue(Path(event.src_path), reason="created")

    def on_moved(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        self._service.schedule_enqueue(Path(event.dest_path), reason="moved")

    def on_modified(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        self._service.schedule_enqueue(Path(event.src_path), reason="modified")


class FolderPickupService:
    def __init__(self, inbox: Path) -> None:
        self.inbox = inbox.resolve()
        self.processed_dir, self.failed_dir = ensure_pickup_dirs(self.inbox)
        self._queue: queue.Queue[Path] = queue.Queue()
        self._stop_event = threading.Event()
        self._processing_lock = threading.Lock()
        self._debounce_lock = threading.Lock()
        self._inflight: Set[str] = set()
        self._debounce_timers: Dict[str, threading.Timer] = {}
        self._worker = threading.Thread(
            target=self._worker_loop,
            name="folder-pickup-worker",
            daemon=True,
        )
        self._poller: Optional[threading.Thread] = None
        self._observer: Optional[Observer] = None

    def start(self) -> None:
        logger.info("[folder-pickup] Starting watcher on %s", self.inbox)
        self._worker.start()
        self.scan_existing_files(reason="startup")

        handler = _PickupEventHandler(self)
        self._observer = Observer()
        self._observer.schedule(handler, str(self.inbox), recursive=False)
        self._observer.start()

        poll_interval = float(settings.document_pickup_poll_interval_seconds)
        if poll_interval > 0:
            self._poller = threading.Thread(
                target=self._poll_loop,
                name="folder-pickup-poller",
                daemon=True,
                kwargs={"interval_seconds": poll_interval},
            )
            self._poller.start()
            logger.info(
                "[folder-pickup] Polling fallback enabled every %.1fs",
                poll_interval,
            )

        logger.info(
            "[folder-pickup] Watching %s (processed=%s, failed=%s)",
            self.inbox,
            self.processed_dir,
            self.failed_dir,
        )

    def stop(self) -> None:
        logger.info("[folder-pickup] Stopping watcher")
        self._stop_event.set()

        with self._debounce_lock:
            for timer in self._debounce_timers.values():
                timer.cancel()
            self._debounce_timers.clear()

        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=5)
            self._observer = None

        if self._poller is not None:
            self._poller.join(timeout=5)
            self._poller = None

        self._queue.put_nowait(Path("__STOP__"))
        self._worker.join(timeout=10)

    def schedule_enqueue(self, path: Path, *, reason: str = "event") -> None:
        """
        Debounce filesystem events so partially-written files are picked up once
        the write/copy settles.
        """
        if not is_incoming_file(path, self.inbox):
            return

        key = str(path.resolve())
        delay = float(settings.document_pickup_event_debounce_seconds)

        with self._debounce_lock:
            existing = self._debounce_timers.pop(key, None)
            if existing is not None:
                existing.cancel()

            timer = threading.Timer(
                delay,
                self._debounced_enqueue,
                args=(path, reason),
            )
            timer.daemon = True
            self._debounce_timers[key] = timer
            timer.start()

    def _debounced_enqueue(self, path: Path, reason: str) -> None:
        with self._debounce_lock:
            self._debounce_timers.pop(str(path.resolve()), None)
        self.enqueue_path(path, reason=reason)

    def scan_existing_files(self, *, reason: str = "scan") -> None:
        for path in list_incoming_files(self.inbox):
            self.enqueue_path(path, reason=reason)

    def enqueue_path(self, path: Path, *, reason: str = "detected") -> None:
        if not is_incoming_file(path, self.inbox):
            return
        key = str(path.resolve())
        with self._processing_lock:
            if key in self._inflight:
                return
            self._inflight.add(key)
        logger.info("[folder-pickup] File detected (%s): %s", reason, path)
        self._queue.put(path)

    def _poll_loop(self, *, interval_seconds: float) -> None:
        while not self._stop_event.wait(interval_seconds):
            try:
                self.scan_existing_files(reason="poll")
            except Exception:
                logger.exception("[folder-pickup] Poll scan failed")

    def _worker_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                path = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue

            if path.name == "__STOP__":
                break

            try:
                self._handle_file(path)
            finally:
                key = str(path.resolve())
                with self._processing_lock:
                    self._inflight.discard(key)

    def _handle_file(self, path: Path) -> None:
        if not is_incoming_file(path, self.inbox):
            return

        if not wait_for_file_stable(
            path,
            stable_seconds=settings.document_pickup_stable_seconds,
        ):
            logger.warning(
                "[folder-pickup] File not stable yet, will retry on next event/poll: %s",
                path,
            )
            return

        if not path.exists():
            return

        outcome = process_pickup_file(path)
        try:
            if outcome.get("status") == "success" and path.exists():
                dest = move_file(path, self.processed_dir)
                logger.info("[folder-pickup] Moved to processed: %s", dest)
            elif path.exists():
                dest = move_file(path, self.failed_dir)
                logger.info("[folder-pickup] Moved to failed: %s", dest)
        except Exception:
            logger.exception("[folder-pickup] Could not move file: %s", path)


_service: Optional[FolderPickupService] = None
_service_lock = threading.Lock()


def start_folder_pickup(inbox: Path) -> None:
    global _service
    with _service_lock:
        if _service is not None:
            return
        _service = FolderPickupService(inbox)
        _service.start()


def stop_folder_pickup() -> None:
    global _service
    with _service_lock:
        if _service is None:
            return
        _service.stop()
        _service = None
