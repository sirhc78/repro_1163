mnemosyne#1163 reproduction harness
===================================

Contents:
- repro_1163.py       standalone harness; stock PyPI wheel, no gateway/profiles/user data;
                      disposable temp-dir stores; fd counting via /proc (Linux).
                      Exit 0 always: documents behavior, does not assert a fix.
- harness_output_4.0.0.txt    captured run: mnemosyne-memory 4.0.0, CPython 3.14, Linux (3 identical runs)
- harness_output_4.0.0b4.txt captured run: 4.0.0b4 (helper close_thread_connection absent - handled)
- fix_1163.diff       proposed core change: close() on Mnemosyne/BeamMemory with the
                      ownership contract (identity-scoped TLS clearing, idempotent, cross-thread safe)
                      plus tests (tests/test_core_close.py, integrations/hermes shutdown test)

Run it yourself:
    python3 -m venv v && v/bin/pip install mnemosyne-memory==4.0.0
    v/bin/python repro_1163.py

What it shows (4.0.0):
    [0] no close() anywhere in the object model
    [1] single-path sessions: flat fd count (TLS reuse - baseline, not a leak)
    [2] two-path alternating sessions: +30 fds for 12 sessions (db+wal displaced, never closed)
    [3] gc x2 reclaims 20/30; the rest stay pinned by live owners
    [4] #1228 close_thread_connection from another thread: no-op (False)
    [5] worker thread exit + gc: 0 fds (the only full-release path today)
