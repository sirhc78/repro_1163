#!/usr/bin/env python3
"""Reproduction harness for mnemosyne#1163.

Standalone: runs against the stock `mnemosyne-memory` wheel (PyPI) with no
gateway, no profiles and no user data. Creates disposable SQLite stores in
a temp directory and counts open file descriptors via /proc.

Measures, on the RELEASED code:
  1. same-path reuse          -> flat fd count (baseline, not a leak)
  2. alternating paths        -> +1 db fd per displaced handle, none closed
  3. teardown (drop refs, gc) -> partial recovery only; live owners pin handles
  4. thread exit              -> full recovery (the only release path today)

Exit 0 always: this harness documents behavior, it does not assert a fix.

Usage:
    python3 repro_1163.py [--package mnemosyne-memory==4.0.0]

Environment note: descriptor counting requires /proc (Linux). On macOS the
script prints the same table with fd columns marked n/a and still exercises
the object-model paths (close() presence, retired-instance semantics).
"""

from __future__ import annotations

import argparse
import gc
import importlib
import shutil
import sqlite3
import sys
import tempfile
import threading
from pathlib import Path


# --------------------------------------------------------------------------- helpers

def db_fd_count(root: Path, name: str) -> int:
    """Descriptors open on <root>/<name> and its -wal/-shm sidecars."""
    if not Path("/proc/self/fd").exists():
        return -1
    import os
    prefix = str(root / name)
    n = 0
    for fd in Path("/proc/self/fd").iterdir():
        try:
            target = os.readlink(fd)
        except OSError:
            continue
        if target == prefix or target.startswith(prefix + "-"):
            n += 1
    return n


def dir_fd_count(root: Path) -> int:
    """Descriptors open on any SQLite file under root."""
    if not Path("/proc/self/fd").exists():
        return -1
    import os
    prefix = str(root) + "/"
    n = 0
    for fd in Path("/proc/self/fd").iterdir():
        try:
            target = os.readlink(fd)
        except OSError:
            continue
        if target.startswith(prefix):
            n += 1
    return n


def has_close(obj) -> bool:
    return hasattr(obj, "close") and callable(obj.close)


ROW = "{}{:<46} {:>14}"


def show(label: str, value, note: str = "") -> None:
    val = "n/a" if value < 0 else str(value)
    print(ROW.format("", label, val) + ("   " + note if note else ""))


# --------------------------------------------------------------------------- scenarios

def scenario_same_path(root, BeamMemory, sessions: int = 10):
    """Baseline: same path every time, beam dropped each session."""
    keep = []
    for i in range(sessions):
        beam = BeamMemory(session_id=f"s{i}", db_path=root / "bank_a.db")
        beam.remember("harness baseline note %d" % i, importance=0.3)
        keep.append(beam)          # hold refs, then drop them all at once
        del beam
    gc.collect()
    # drop refs then measure residual
    for b in keep:
        del b
    del keep
    gc.collect()
    return db_fd_count(root, "bank_a.db")


def scenario_alternating_paths(root, BeamMemory, sessions: int = 12):
    """The production pattern: session-scoped beams alternating between two
    paths on one long-lived worker thread. Displaced TLS handles stay open."""
    residual = []
    worker_err = []

    def worker():
        try:
            for i in range(sessions):
                path = root / ("bank_a.db" if i % 2 == 0 else "bank_b.db")
                beam = BeamMemory(session_id=f"s{i}", db_path=path)
                beam.remember("harness alternating note %d" % i, importance=0.3)
                residual.append(beam)   # retain beams (session scope in prod)
        except BaseException as exc:   # surfaced to main thread
            worker_err.append(exc)

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    if worker_err:
        raise worker_err[0]
    return db_fd_count(root, "bank_a.db") + db_fd_count(root, "bank_b.db"), residual


def scenario_teardown(residual, root):
    """Drop all refs + gc twice: how many handles does collection reclaim?"""
    n_before = sum(db_fd_count(root, b) for b in ("bank_a.db", "bank_b.db"))
    residual.clear()
    del residual[:]
    gc.collect()
    gc.collect()
    n_after = sum(db_fd_count(root, b) for b in ("bank_a.db", "bank_b.db"))
    return n_before, n_after


def scenario_release_helper(residual, root):
    """#1228's close_thread_connection from the MAIN thread (wrong thread):
    documented no-op for a live worker thread's TLS slot."""
    try:
        from mnemosyne.core.connection_release import close_thread_connection
    except ImportError:
        return "helper not present in this version"
    freed = close_thread_connection(str(root / "bank_a.db"))
    return "returned %r (cross-thread: no-op)" % freed


def scenario_thread_exit(root, BeamMemory):
    """Fresh worker thread, alternating paths, then let the thread DIE.
    Thread exit + gc is today's only full-release path."""
    def worker():
        for i in range(6):
            path = root / ("bank_c.db" if i % 2 == 0 else "bank_d.db")
            beam = BeamMemory(session_id=f"x{i}", db_path=path)
            beam.remember("harness exit note %d" % i, importance=0.3)
    t = threading.Thread(target=worker)
    t.start()
    t.join()
    gc.collect()
    return db_fd_count(root, "bank_c.db") + db_fd_count(root, "bank_d.db")


def object_model_probe(root, BeamMemory, Mnemosyne):
    """What the wrapper's shutdown already expects from core (production
    finding 1): does close() exist, and what happens on the retired path."""
    out = []
    beam = BeamMemory(session_id="probe", db_path=root / "probe.db")
    out.append(("BeamMemory.close exists", has_close(beam)))
    if has_close(beam):
        try:
            beam.close()
            out.append(("BeamMemory.close() call", "ok (no raise)"))
        except Exception as exc:
            out.append(("BeamMemory.close() call", "raised %r" % exc))
    if Mnemosyne is not None:
        m = Mnemosyne(db_path=root / "probe.db")
        out.append(("Mnemosyne.close exists", has_close(m)))
        if has_close(m):
            try:
                m.close()
                out.append(("Mnemosyne.close() call", "ok (no raise)"))
            except Exception as exc:
                out.append(("Mnemosyne.close() call", "raised %r" % exc))
        del m
    del beam
    gc.collect()
    return out


# --------------------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--package", default=None,
                    help="e.g. mnemosyne-memory==4.0.0 (default: already-installed)")
    args = ap.parse_args()

    if args.package:
        print("note: install %s into this interpreter first (pip install %s)"
              % (args.package, args.package))

    import mnemosyne
    from mnemosyne.core.beam import BeamMemory
    try:
        from mnemosyne.core.memory import Mnemosyne
    except ImportError:
        Mnemosyne = None
    try:
        import mnemosyne_memory  # noqa: F401  (dist metadata probe)
        pkg_version = getattr(mnemosyne_memory, "__version__", "?")
    except ImportError:
        pkg_version = "?"

    root = Path(tempfile.mkdtemp(prefix="mnem1163-"))
    print("mnemosyne#1163 reproduction harness")
    print("package: mnemosyne-memory %s | mnemosyne %s | python %s"
          % (pkg_version, getattr(mnemosyne, "__version__", "?"),
             sys.version.split()[0]))
    print("scratch dir (disposable): %s" % root)
    print("platform: %s | /proc fd counting: %s"
          % (sys.platform, Path('/proc/self/fd').exists()))
    print()

    # --- object model probe -------------------------------------------------
    print("[0] object model (what provider shutdown expects from core)")
    for k, v in object_model_probe(root, BeamMemory, Mnemosyne):
        print("    %-34s %s" % (k, v))
    print()

    # --- 1. baseline: same path ---------------------------------------------
    n = scenario_same_path(root, BeamMemory)
    show("[1] 10 sessions, ONE path, beams dropped", n,
         "(baseline: TLS reuse, flat)" if n >= 0 else "")

    # --- 2. the production pattern -------------------------------------------
    n2, residual = scenario_alternating_paths(root, BeamMemory)
    show("[2] 12 sessions, TWO paths, beams kept", n2,
         "(one displaced handle per retained beam)" if n2 >= 0 else "")

    # --- 3. teardown ----------------------------------------------------------
    nb, na = scenario_teardown(residual, root)
    if nb >= 0:
        show("[3] drop all refs + gc x2", na,
             "(reclaimed %d of %d; live owners pin the rest)" % (nb - na, nb))
    else:
        show("[3] drop all refs + gc x2", -1)

    # --- 4. cross-thread release helper (#1228) -------------------------------
    print("[4] close_thread_connection from main thread:",
          scenario_release_helper(residual, root))

    # --- 5. thread exit --------------------------------------------------------
    n5 = scenario_thread_exit(root, BeamMemory)
    show("[5] worker thread exits + gc", n5,
         "(full release: the only path that reaches 0 today)" if n5 == 0 else "")

    print()
    print("done. scratch dir kept for inspection: %s" % root)
    return 0


if __name__ == "__main__":
    sys.exit(main())
