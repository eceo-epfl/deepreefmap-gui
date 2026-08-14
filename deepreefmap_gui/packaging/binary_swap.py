"""Everything the app does to its own binary: pick the asset, download it, swap it, provision it.

Only meaningful under PyApp, where the app is one executable plus a per-version environment it
provisions on first launch. In a dev venv `pyapp_binary()` returns None and the callers stand
down.

The asset is chosen from the running build, not from the user: a ROCm install must not be handed
a CUDA binary by a dropdown, so `resolve_asset_name` reads the accelerator off the current
environment. Downloads land on a `.part` and are size-checked against both the server and the
release metadata before the rename, because a truncated binary that swaps in cleanly is
indistinguishable from a working one until the next launch.

Three constraints shape the rest:

- **Windows cannot overwrite a running executable.** The old binary is renamed aside first, and if
  the second rename then fails (antivirus, a full volume) the backup is put back, since the
  alternative is an install with no binary at all. The stale `.old` is swept on next startup.
- **PyApp checks that a version's environment exists, not that it is intact.** An OS update or an
  antivirus sweep that deletes files inside it leaves a broken env in place, so `env_is_healthy`
  looks for the heavy native packages and `self_restore` rebuilds from the shared uv cache.
- **Rolling back must not need the network.** A field laptop that has to undo an update is the
  least likely to have one, so the outgoing binary is copied into `previous/` before it is
  overwritten and `perform_rollback` swaps it back from there. `prune_previous_binaries` caps that
  ring; the binaries are small, and the multi-GB environments beside them are never pruned
  automatically -- re-provisioning needs a package index, so what to delete is the user's call,
  made on Setup's Updates view.

Qt-free, so the whole swap is testable headless; the dialog that drives it is `update/dialog.py`,
and `tests/e2e/update_e2e.sh` runs the real thing on two real binaries.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import sysconfig
import urllib.request
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)


def pyapp_binary() -> str | None:
    """Path of the running PyApp binary, or None in a dev venv.

    PyApp exports ``PYAPP=<binary path>`` (older releases exported ``"1"``).
    """
    value = os.environ.get("PYAPP")
    if value and value != "1" and Path(value).exists():
        return value
    return None


class BinarySwapError(RuntimeError):
    pass


def torch_local_version() -> str | None:
    """The build tag on the installed torch (``rocm6.4``, ``cu130``), or None if absent.

    ``""`` and ``None`` mean different things: a plain PyPI wheel carries no
    local version and is the default build, while None is no torch at all and no
    answer. Only the second is a reason to go looking elsewhere.

    Read from the distribution's metadata rather than by importing torch. The
    import costs half a second of GIL and pulls the GPU runtime in behind it,
    and the update check runs on a worker thread a few hundred ms into startup:
    paying it there froze the window for four seconds before its first frame,
    and left torch resident so the GPU probe took its expensive in-process path
    as well.
    """
    import importlib.metadata

    try:
        version = importlib.metadata.version("torch")
    except Exception:
        return None
    _, _, local = version.partition("+")
    return local


# The GPU variants a release publishes an asset for, longest first so "cu130"
# is not matched by a shorter tag that is also a prefix of it. Anything not
# listed -- the cu126 line, a plain PyPI wheel -- takes the unsuffixed asset,
# which is what release.yml calls the default build.
_ASSET_VARIANTS = ("rocm", "cu130")


def build_variant() -> str:
    """Which GPU build this is: ``rocm``, ``cu130``, or ``""`` for the default.

    An update must stay on the variant it replaces -- handing a ROCm laptop the
    CUDA build would leave it with no working card -- so this decides which
    release asset the updater asks for.

    The env's own torch wheel is the answer. PyApp provisions that env from this
    binary's requirements, so the tag is baked in at build time and survives the
    user renaming the download, which the asset filename does not. The binary's
    name is consulted only when there is no torch to ask: a half-provisioned or
    repaired env, and the tests, which set PYAPP to names with no env behind them.
    """
    local = torch_local_version()
    if local is not None:
        for variant in _ASSET_VARIANTS:
            # rocm6.4 -> rocm. cu126 matches nothing and takes the default
            # asset, as does a plain PyPI wheel's empty tag.
            if local.startswith(variant):
                return variant
        return ""

    pyapp = os.environ.get("PYAPP")
    if pyapp:
        # Raw env read, and the NAME only: tests set PYAPP to names that need
        # not exist on disk, so this cannot go through pyapp_binary().
        name = Path(pyapp).name
        for variant in _ASSET_VARIANTS:
            if variant in name:
                return variant
    return ""


def _is_rocm_build() -> bool:
    return build_variant() == "rocm"


def _cuda_variant_suffix() -> str:
    """``-cu130`` for a CUDA 13 build, else ``""``. Keeps an in-app update on its variant."""
    variant = build_variant()
    return f"-{variant}" if variant == "cu130" else ""


def resolve_asset_name(platform: str | None = None) -> str:
    p = (platform or sys.platform).lower()
    if p.startswith("linux"):
        if _is_rocm_build():
            return "deepreefmap-gui-linux-x64-rocm"
        return f"deepreefmap-gui-linux-x64{_cuda_variant_suffix()}"
    if p.startswith("win"):
        return f"deepreefmap-gui-windows-x64{_cuda_variant_suffix()}.exe"
    if p.startswith("darwin"):
        return "deepreefmap-gui-macos-arm64"
    raise BinarySwapError(f"No binary asset is built for platform {p!r}")


def match_asset(release: dict, asset_name: str) -> dict | None:
    """This platform's downloadable asset in a release, or None."""
    # Release assets carry a version label (deepreefmap-gui-linux-x64-1.2.0[.exe])
    # while resolve_asset_name yields the bare platform name, so accept both.
    candidates = {asset_name}
    tag = str(release.get("tag_name", "")).lstrip("v")
    if tag:
        stem, dot, ext = asset_name.rpartition(".")
        if dot:
            candidates.add(f"{stem}-{tag}.{ext}")
        else:
            candidates.add(f"{asset_name}-{tag}")
    for asset in release.get("assets", []):
        if asset.get("name") in candidates and asset.get("browser_download_url"):
            return dict(asset)
    return None


def match_asset_url(release: dict, asset_name: str) -> str | None:
    asset = match_asset(release, asset_name)
    return str(asset["browser_download_url"]) if asset is not None else None


def declared_asset_size(release: dict, asset_name: str) -> int | None:
    """Byte count GitHub records for the asset, or None if the release omits it.

    Worth checking against separately from Content-Length: this figure comes from
    api.github.com while the bytes come from the release CDN, so the two disagree
    when a transfer is truncated or a proxy substitutes a response.
    """
    asset = match_asset(release, asset_name)
    size = asset.get("size") if asset is not None else None
    return int(size) if size else None


def find_asset_url(release: dict, asset_name: str) -> str:
    url = match_asset_url(release, asset_name)
    if url is None:
        raise BinarySwapError(
            f"Release {release.get('tag_name', '?')} has no {asset_name!r} asset. "
            "This release may pre-date binary distribution. Pick a newer version."
        )
    return url


# Bounds each socket operation. Without one, a laptop that drops off the network
# mid-download (or lands behind a captive portal) blocks on the read forever, with
# a frozen progress bar and no way to cancel.
_DOWNLOAD_TIMEOUT_S = 60.0


def download_to(
    url: str,
    dest_path: Path,
    progress_cb: Callable[[int, int], None] | None = None,
    chunk_size: int = 64 * 1024,
    *,
    expected_size: int | None = None,
    timeout: float = _DOWNLOAD_TIMEOUT_S,
) -> None:
    """Download `url` to `dest_path`, or leave nothing at `dest_path` at all.

    The bytes accumulate in a sibling ``.part`` file and are moved into place only
    once the transfer is known to be complete, so a truncated download can never
    be handed to replace_binary and installed as the running program.
    """
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    part = dest_path.with_name(dest_path.name + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": "deepreefmap-gui-updater"})  # noqa: S310 (asset URL from our GH release metadata)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (URL is our GH release metadata)
            declared = int(resp.headers.get("Content-Length") or 0)
            total = declared or expected_size or 0
            done = 0
            with part.open("wb") as f:
                while True:
                    chunk = resp.read(chunk_size)
                    if not chunk:
                        break
                    f.write(chunk)
                    done += len(chunk)
                    if progress_cb is not None:
                        progress_cb(done, total)
        for source, size in (("the server", declared), ("the release metadata", expected_size)):
            if size and done != size:
                raise BinarySwapError(
                    f"Downloaded {done} bytes but {source} said {size}. "
                    "The transfer was interrupted or altered; try again."
                )
        os.replace(part, dest_path)
    except BaseException:
        part.unlink(missing_ok=True)
        raise


def replace_binary(target_path: Path, src_path: Path) -> None:
    if not src_path.exists():
        raise BinarySwapError(f"Source binary missing: {src_path}")
    if sys.platform.startswith("win"):
        backup = target_path.with_suffix(target_path.suffix + ".old")
        if backup.exists():
            try:
                backup.unlink()
            except OSError:
                logger.debug("Could not remove stale backup %s", backup, exc_info=True)
        moved_aside = False
        if target_path.exists():
            os.rename(target_path, backup)
            moved_aside = True
        try:
            os.rename(src_path, target_path)
        except OSError as exc:
            # Windows cannot overwrite a running .exe, which is why the old one is
            # moved aside first. If this second rename then fails -- antivirus
            # holding the new file open, a full volume -- the install is left with
            # no binary at all unless the backup goes back.
            if moved_aside:
                try:
                    os.rename(backup, target_path)
                except OSError:
                    raise BinarySwapError(
                        f"Update failed and {target_path} could not be restored. "
                        f"The previous binary is at {backup}; rename it back to recover."
                    ) from exc
            raise BinarySwapError(
                f"Could not move the new binary into place: {exc}"
            ) from exc
    else:
        os.chmod(src_path, 0o755)
        os.rename(src_path, target_path)


def cleanup_stale_backups(binary_path: Path) -> None:
    if not sys.platform.startswith("win"):
        return
    backup = binary_path.with_suffix(binary_path.suffix + ".old")
    if backup.exists():
        try:
            backup.unlink()
        except OSError:
            logger.debug("Failed to remove %s during startup cleanup", backup, exc_info=True)


# --- Environment health / self-heal -----------------------------------------
# PyApp only checks that a version's env exists, not that it is intact, so an OS
# update or antivirus that deletes files inside it leaves a broken env in place.


def env_is_healthy(purelib: str | os.PathLike[str] | None = None) -> bool:
    """True if the heavy native deps look intact in the active environment.

    Returns True when the layout is unknown, so a false alarm never triggers a
    needless re-provision.
    """
    try:
        base = Path(purelib) if purelib is not None else Path(sysconfig.get_path("purelib"))
    except Exception:
        return True
    for pkg in ("torch", "PySide6"):
        if not (base / pkg).is_dir():
            return False
    # A present-but-empty torch/lib is the signature of a half-finished install.
    torch_lib = base / "torch" / "lib"
    torch_lib_is_empty = torch_lib.is_dir() and not any(torch_lib.iterdir())
    return not torch_lib_is_empty


def self_restore(binary_path: str | os.PathLike[str]) -> bool:
    """Repair a broken env via PyApp's ``self restore`` (wipe + reinstall from the
    warm uv cache). Used only by the self-heal path, which fires solely when
    ``env_is_healthy`` is False, so it never touches a good environment. Returns
    True on success."""
    try:
        subprocess.run([str(binary_path), "self", "restore"], check=True)
        return True
    except Exception:
        logger.exception("`self restore` failed for %s", binary_path)
        return False


# --- Retained binaries for rollback -------------------------------------------
# Every version change keeps the outgoing binary next to the installed one, so a
# later rollback needs no download. Each version keeps its own environment, so if
# that version's env is still on disk the rollback is instant; if the user has
# deleted it on Setup's Updates view, the next launch re-provisions it from the cache.


def _versioned_name(binary_path: Path, version: str) -> str:
    """`deepreefmap-gui-linux-x64` + `1.1.0` -> `deepreefmap-gui-linux-x64-1.1.0`,
    preserving a `.exe` suffix so a kept Windows binary stays runnable."""
    if binary_path.suffix:
        return f"{binary_path.stem}-{version}{binary_path.suffix}"
    return f"{binary_path.name}-{version}"


def previous_dir(binary_path: str | os.PathLike[str]) -> Path:
    return Path(binary_path).parent / "previous"


def retain_previous_binary(
    binary_path: str | os.PathLike[str], version: str | None
) -> Path | None:
    """Copy the current binary into ``previous/`` before it is overwritten.

    Returns the kept path, or None when there is nothing to keep (no version
    known, or the binary is missing).
    """
    binary_path = Path(binary_path)
    if not version or not binary_path.exists():
        return None
    dest_dir = previous_dir(binary_path)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / _versioned_name(binary_path, version)
    if dest.resolve() == binary_path.resolve():
        return None
    shutil.copy2(binary_path, dest)
    return dest


def available_previous_versions(
    binary_path: str | os.PathLike[str],
) -> dict[str, Path]:
    """Versions whose binary is kept locally, mapped to their file.

    These are the versions a rollback can reach with no network at all.
    """
    binary_path = Path(binary_path)
    result: dict[str, Path] = {}
    directory = previous_dir(binary_path)
    if not directory.is_dir():
        return result
    stem = binary_path.stem if binary_path.suffix else binary_path.name
    suffix = binary_path.suffix
    prefix = f"{stem}-"
    for kept in directory.iterdir():
        if not kept.is_file():
            continue
        name = kept.name
        if suffix:
            if not name.endswith(suffix):
                continue
            name = name[: -len(suffix)]
        if not name.startswith(prefix):
            continue
        version = name[len(prefix):]
        if version:
            result[version] = kept
    return result


def prune_previous_binaries(
    binary_path: str | os.PathLike[str], keep: int = 3
) -> list[Path]:
    """Cap the retained-binary store, newest kept. Each is tens of MB, so a small
    ring is cheap insurance against an update that provisions but runs badly."""
    kept = available_previous_versions(binary_path)
    if len(kept) <= keep:
        return []
    by_mtime = sorted(kept.values(), key=lambda p: p.stat().st_mtime, reverse=True)
    removed: list[Path] = []
    for stale in by_mtime[keep:]:
        try:
            stale.unlink()
            removed.append(stale)
        except OSError:
            logger.debug("Could not prune retained binary %s", stale, exc_info=True)
    return removed


def perform_update(
    release: dict,
    binary_path: Path,
    target_version: str,
    current_version: str | None = None,
    progress_cb: Callable[[int, int], None] | None = None,
    line_cb: Callable[[str], None] | None = None,
) -> None:
    """Download the release's asset and swap it in.

    Provisioning is left to PyApp: the new version's environment is built from the
    warm uv cache on the next launch (its wheels are already cached, so nothing
    bulky is re-downloaded). This never touches the running env, which on Windows
    holds file locks anyway.

    The outgoing binary is copied into ``previous/`` first, so a later rollback
    to ``current_version`` needs no download.
    """
    binary_path = Path(binary_path)

    def log(message: str) -> None:
        if line_cb is not None:
            line_cb(message)

    asset_name = resolve_asset_name()
    log(f"Looking up {asset_name} in release {release.get('tag_name')}…")
    url = find_asset_url(release, asset_name)
    expected_size = declared_asset_size(release, asset_name)
    log(f"Downloading {url}")
    staged = binary_path.with_name(binary_path.name + ".new")
    if staged.exists():
        staged.unlink()
    download_to(url, staged, progress_cb=progress_cb, expected_size=expected_size)
    size = staged.stat().st_size
    if expected_size:
        log(f"Downloaded {size} bytes, matching the size recorded for the release.")
    else:
        log(f"Downloaded {size} bytes; the release records no size to check it against.")
    kept = retain_previous_binary(binary_path, current_version)
    if kept is not None:
        log(f"Kept the current binary for offline rollback: {kept.name}")
    log(f"Replacing binary at {binary_path}")
    replace_binary(binary_path, staged)
    log(f"Installed {target_version}. Restart to apply; its environment is prepared on the first launch.")


def perform_rollback(
    binary_path: Path,
    target_version: str,
    current_version: str | None = None,
    line_cb: Callable[[str], None] | None = None,
) -> None:
    """Swap in a locally kept binary with no download.

    That version keeps its own environment, so if it still exists the next launch
    is instant; if it was deleted on Setup's Updates view, PyApp re-provisions it from
    the warm cache. Raises BinarySwapError when ``target_version`` was not retained.
    """
    binary_path = Path(binary_path)

    def log(message: str) -> None:
        if line_cb is not None:
            line_cb(message)

    kept = available_previous_versions(binary_path).get(target_version)
    if kept is None:
        raise BinarySwapError(
            f"No locally kept binary for {target_version}. "
            "Roll back over the network instead, or pick a version marked offline."
        )
    log(f"Restoring the kept binary for {target_version}: {kept.name}")
    staged = binary_path.with_name(binary_path.name + ".new")
    if staged.exists():
        staged.unlink()
    # Copy rather than move: the kept binary must survive so this version can be
    # rolled back to again later.
    shutil.copy2(kept, staged)
    retained = retain_previous_binary(binary_path, current_version)
    if retained is not None:
        log(f"Kept the current binary for offline rollback: {retained.name}")
    log(f"Replacing binary at {binary_path}")
    replace_binary(binary_path, staged)
    log(f"Rolled back to {target_version}. Restart to apply.")
