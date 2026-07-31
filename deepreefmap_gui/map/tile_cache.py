"""Tile pixmap cache: memory LRU over a persistent disk cache, network last."""

from __future__ import annotations

import logging
import os
import time
from collections import OrderedDict
from functools import lru_cache
from pathlib import Path

from PySide6.QtCore import QObject, QUrl, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest

from deepreefmap_gui.map.layers import TileLayer
from deepreefmap_gui.packaging.releases import current_version
from deepreefmap_gui.paths import tile_cache_dir

logger = logging.getLogger(__name__)

_MEMORY_TILES = 256

# A reply carrying one of these means there is no route to the tile server, not
# that a particular tile is missing (a 404 is ContentNotFoundError, which is not
# here). Seeing one flips the cache to offline so the map can say so.
_OFFLINE_ERRORS = frozenset({
    QNetworkReply.NetworkError.ConnectionRefusedError,
    QNetworkReply.NetworkError.RemoteHostClosedError,
    QNetworkReply.NetworkError.HostNotFoundError,
    QNetworkReply.NetworkError.TimeoutError,
    QNetworkReply.NetworkError.TemporaryNetworkFailureError,
    QNetworkReply.NetworkError.NetworkSessionFailedError,
    QNetworkReply.NetworkError.ProxyConnectionRefusedError,
    QNetworkReply.NetworkError.ProxyNotFoundError,
    QNetworkReply.NetworkError.UnknownNetworkError,
})

# A tile request that never completes holds its key in _in_flight, which blocks
# every later attempt at that tile. Qt has no default transfer timeout.
_TRANSFER_TIMEOUT_MS = 15_000

# A failed tile is retried, with the wait doubling per consecutive failure. The
# common cause of failure is having no network at all, so every visible tile
# fails at once; retrying them freely would hammer the tile server the moment it
# looked reachable, and never retrying leaves the map blank until the app is
# restarted.
_RETRY_BASE_S = 5.0
_RETRY_MAX_S = 300.0

# Nothing removed tiles once written, so the cache grew for the life of the
# install. Pruning walks the tree, so it is amortised: only after enough new
# tiles have landed to be worth the walk.
_DISK_BUDGET_BYTES = 512 * 1024 * 1024
_PRUNE_AFTER_BYTES = 16 * 1024 * 1024
_TOUCH_AFTER_S = 24 * 60 * 60


class TileCache(QObject):
    """Serves tiles from memory, then disk; missing tiles are fetched once.

    Tiles reach the disk cache only by being displayed, which keeps previously
    visited areas available offline without violating the OSM tile policy.
    """

    tile_ready = Signal(int, int, int)
    offline_changed = Signal(bool)

    def __init__(self, layer: TileLayer, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._layer = layer
        self._memory: OrderedDict[tuple[int, int, int], QPixmap] = OrderedDict()
        self._in_flight: set[tuple[int, int, int]] = set()
        # key -> (consecutive failures, monotonic time it may be tried again)
        self._failed: dict[tuple[int, int, int], tuple[int, float]] = {}
        self._network: QNetworkAccessManager | None = None
        self._written_since_prune = 0
        self.network_enabled = True
        self._offline = False

    @property
    def layer(self) -> TileLayer:
        return self._layer

    @property
    def offline(self) -> bool:
        """True when tiles cannot be fetched, so only the saved area will draw."""
        return not self.network_enabled or self._offline

    def _disk_path(self, zoom: int, x: int, y: int) -> Path:
        return tile_cache_dir() / self._layer.id / str(zoom) / str(x) / f"{y}.png"

    def pixmap(self, zoom: int, x: int, y: int) -> QPixmap | None:
        """Cached tile pixmap, scheduling a network fetch when absent."""
        if not 0 <= y < 2**zoom:
            return None
        x %= 2**zoom
        key = (zoom, x, y)
        cached = self._memory.get(key)
        if cached is not None:
            self._memory.move_to_end(key)
            return cached
        disk_path = self._disk_path(zoom, x, y)
        if disk_path.is_file():
            pixmap = QPixmap(str(disk_path))
            if not pixmap.isNull():
                self._touch(disk_path)
                self._remember(key, pixmap)
                return pixmap
        self._fetch(key)
        return None

    def _touch(self, path: Path) -> None:
        """Restamp a tile served from disk, so pruning drops what is no longer
        looked at rather than what was fetched longest ago.

        Preparing for a trip means visiting the site on the map; without this,
        that site's tiles are the oldest on disk and the first to be pruned. The
        day's grace keeps a repaint from rewriting metadata on every frame.
        """
        try:
            if time.time() - path.stat().st_mtime > _TOUCH_AFTER_S:
                os.utime(path)
        except OSError:
            logger.debug("Could not restamp cached tile %s", path, exc_info=True)

    def _remember(self, key: tuple[int, int, int], pixmap: QPixmap) -> None:
        self._memory[key] = pixmap
        while len(self._memory) > _MEMORY_TILES:
            self._memory.popitem(last=False)

    def _may_retry(self, key: tuple[int, int, int]) -> bool:
        entry = self._failed.get(key)
        if entry is None:
            return True
        _, retry_at = entry
        return time.monotonic() >= retry_at

    def _note_failure(self, key: tuple[int, int, int]) -> None:
        attempts = self._failed.get(key, (0, 0.0))[0] + 1
        backoff = min(_RETRY_BASE_S * 2 ** (attempts - 1), _RETRY_MAX_S)
        self._failed[key] = (attempts, time.monotonic() + backoff)

    def _fetch(self, key: tuple[int, int, int]) -> None:
        if not self.network_enabled or key in self._in_flight:
            return
        if not self._may_retry(key):
            return
        if self._network is None:
            self._network = QNetworkAccessManager(self)
        zoom, x, y = key
        url = self._layer.url_template.format(z=zoom, x=x, y=y)
        request = QNetworkRequest(QUrl(url))
        request.setHeader(
            QNetworkRequest.KnownHeaders.UserAgentHeader,
            f"deepreefmap-gui/{current_version()} (+https://github.com/eceo-epfl/deepreefmap-gui)",
        )
        request.setTransferTimeout(_TRANSFER_TIMEOUT_MS)
        self._in_flight.add(key)
        reply = self._network.get(request)
        reply.finished.connect(lambda: self._on_reply(key, reply))

    def _on_reply(self, key: tuple[int, int, int], reply: QNetworkReply) -> None:
        self._in_flight.discard(key)
        reply.deleteLater()
        self._note_reply_error(reply.error())
        if reply.error() != QNetworkReply.NetworkError.NoError:
            logger.debug("Tile %s failed: %s", key, reply.errorString())
            self._note_failure(key)
            return
        data = bytes(reply.readAll().data())
        pixmap = QPixmap()
        if not pixmap.loadFromData(data):
            self._note_failure(key)
            return
        self._failed.pop(key, None)
        zoom, x, y = key
        disk_path = self._disk_path(zoom, x, y)
        try:
            disk_path.parent.mkdir(parents=True, exist_ok=True)
            disk_path.write_bytes(data)
        except OSError:
            logger.warning("Could not write tile cache at %s", disk_path, exc_info=True)
        else:
            self._written_since_prune += len(data)
            if self._written_since_prune >= _PRUNE_AFTER_BYTES:
                self._written_since_prune = 0
                self._prune_disk_cache()
        self._remember(key, pixmap)
        self.tile_ready.emit(zoom, x, y)

    def _note_reply_error(self, error: QNetworkReply.NetworkError) -> None:
        """Flip the offline flag from a reply's error, announcing real changes.

        A connectivity error means offline; any successful reply clears it. A
        content error (a 404 for a tile past the edge of coverage) leaves it be.
        """
        if error == QNetworkReply.NetworkError.NoError:
            offline = False
        elif error in _OFFLINE_ERRORS:
            offline = True
        else:
            return
        if offline != self._offline:
            self._offline = offline
            self.offline_changed.emit(self.offline)

    def _prune_disk_cache(self) -> None:
        """Drop the least recently used tiles until the cache fits its budget.

        Ordered by mtime rather than atime, which a cache mounted noatime would
        not maintain; _touch keeps the mtime of a tile served from disk current
        so this stays least-recently-*used*. A tile that falls out is re-fetched
        when next displayed.
        """
        root = tile_cache_dir() / self._layer.id
        try:
            tiles = [(p.stat().st_mtime, p.stat().st_size, p) for p in root.rglob("*.png")]
        except OSError:
            logger.warning("Could not read the tile cache at %s", root, exc_info=True)
            return
        total = sum(size for _, size, _ in tiles)
        if total <= _DISK_BUDGET_BYTES:
            return
        removed = 0
        for _, size, path in sorted(tiles):
            if total <= _DISK_BUDGET_BYTES:
                break
            try:
                path.unlink()
            except OSError:
                continue
            total -= size
            removed += 1
        logger.info("Pruned %s tiles from %s, now %s bytes", removed, root, total)


@lru_cache(maxsize=None)
def shared_tile_cache() -> TileCache:
    """One cache (and network manager) shared by every map widget."""
    from deepreefmap_gui.map.layers import OSM_LAYER

    return TileCache(OSM_LAYER)
