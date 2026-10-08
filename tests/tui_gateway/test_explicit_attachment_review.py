import importlib.util
from pathlib import Path
import pytest
spec = importlib.util.spec_from_file_location("attachment_tests", Path.cwd() / "tests/tui_gateway/test_explicit_prompt_attachments.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
admission = m.admission
server = m.server

def queue_image(admission, monkeypatch):
    sid, session, targets, seen = admission
    session["running"] = True
    monkeypatch.setattr(server, "_load_busy_input_mode", lambda: "queue")
    monkeypatch.setattr(server, "_persist_queued_user_row", lambda *a: None)
    response = server.handle_request(dict(id="b", method="prompt.submit", params=dict(session_id=sid, text="B", attachment_mode="explicit", attachments=[m.image_payload()])))
    assert response["result"]["status"] == "queued"
    session["running"] = False
    return sid, session, Path(session["queued_prompt"]["image_paths"][0])

@pytest.mark.platforms("windows")
def test_queue_worker_refusal_cleans_bundle(admission, monkeypatch):
    sid, session, path = queue_image(admission, monkeypatch)
    monkeypatch.setattr(server, "_run_prompt_submit", lambda *a, **kw: False)
    assert server._drain_queued_prompt("drain", sid, session)
    assert session["attached_images"] == ["client-A.png"]
    assert not path.exists(), "unrun queued bundle leaked after worker refusal"

@pytest.mark.platforms("windows")
def test_queue_ambiguous_compute_error_retains_bundle(admission, monkeypatch):
    sid, session, path = queue_image(admission, monkeypatch)
    monkeypatch.setattr(server, "_session_uses_compute_host", lambda *a: True)
    monkeypatch.setattr(server, "_submit_prompt_to_compute_host", lambda *a, **kw: {"error": {"message": "send failed"}})
    monkeypatch.setattr(server, "_emit", lambda *a, **kw: None)
    assert server._drain_queued_prompt("drain", sid, session)
    assert path.exists(), "generic compute error does not prove nonacceptance"

def test_cleanup_preserves_concurrent_replacement(tmp_path, monkeypatch):
    from tui_gateway import explicit_attachments as ea
    bundle = ea.stage({"profile_home": str(tmp_path)}, [m.image_payload()])
    path = Path(bundle.paths[0])
    original = ea._file_identity
    def identity_then_replace(p):
        result = original(p)
        other = p.with_suffix(".replacement")
        other.write_bytes(b"foreign replacement")
        p.rename(p.with_suffix(".original"))
        other.replace(p)
        return result
    monkeypatch.setattr(ea, "_file_identity", identity_then_replace)
    bundle.cleanup()
    assert path.exists(), "cleanup unlinked concurrent foreign replacement"
    assert path.read_bytes() == b"foreign replacement"


@pytest.mark.parametrize("boundary", ["stop", "close", "prepare_error"])
@pytest.mark.platforms("windows")
def test_claimed_bundle_is_disposed_before_dispatch(admission, monkeypatch, boundary):
    sid, session, path = queue_image(admission, monkeypatch)
    monkeypatch.setattr(server, "_emit", lambda *a, **kw: None)
    monkeypatch.setattr(server, "_session_profile_runtime_scope", lambda *a, **kw: __import__("contextlib").nullcontext())
    def prepare(*a):
        if boundary == "stop":
            # Avoid recursively invoking this seam inside interrupt.
            monkeypatch.setattr(server, "_session_uses_compute_host", lambda *a: False)
            server._interrupt_session_turn(sid, session)
        elif boundary == "close":
            server._pop_session_by_id(sid)
        else:
            raise OSError("pre-dispatch preparation failed")
        return False
    monkeypatch.setattr(server, "_session_uses_compute_host", prepare)
    try:
        assert server._drain_queued_prompt("drain", sid, session)
    except OSError:
        pass
    assert not path.exists(), "claimed but unrun envelope lost its cleanup authority"
    assert not session.get("queued_prompt"), "cancelled explicit envelope resurrected"
    assert not admission[3], "cancelled explicit envelope dispatched"
    assert not session.get("_dispatching_queued_prompt")


@pytest.mark.parametrize("outcome", ["accepted", "unknown"])
def test_dispatch_acceptance_or_uncertainty_retains_media(admission, monkeypatch, outcome):
    sid, session, path = queue_image(admission, monkeypatch)
    def run(*a, **kw):
        if outcome == "unknown":
            raise RuntimeError("dispatch outcome unknown")
        return True
    monkeypatch.setattr(server, "_run_prompt_submit", run)
    assert server._drain_queued_prompt("drain", sid, session)
    assert path.exists()
    assert not session.get("_dispatching_queued_prompt")


@pytest.mark.platforms("windows")
def test_staging_pins_parent_against_directory_replacement(tmp_path, monkeypatch):
    from tui_gateway import explicit_attachments as ea
    original = Path.open
    blocked = []
    def opening(path, mode="r", *a, **kw):
        if mode == "xb" and "explicit-turns" in str(path):
            try:
                path.parent.rename(path.parent.with_name("foreign-renamed"))
            except OSError:
                blocked.append(True)
            else:
                path.parent.mkdir()
        return original(path, mode, *a, **kw)
    monkeypatch.setattr(Path, "open", opening)
    bundle = ea.stage({"profile_home": str(tmp_path)}, [m.image_payload()])
    assert blocked == [True]
    bundle.cleanup()


def test_staging_retention_budget_refuses_further_writes(tmp_path, monkeypatch):
    from tui_gateway import explicit_attachments as ea
    first = ea.stage({"profile_home": str(tmp_path)}, [m.image_payload()])
    monkeypatch.setattr(ea, "MAX_STAGED_BYTES", Path(first.paths[0]).stat().st_size, raising=False)
    with pytest.raises(ValueError):
        ea.stage({"profile_home": str(tmp_path)}, [m.image_payload()])
    assert len(list((tmp_path / "images/explicit-turns").iterdir())) == 1
    first.cleanup()


@pytest.mark.parametrize("boundary", ["stop", "close"])
@pytest.mark.platforms("windows")
def test_claimed_head_cleanup_is_visible_during_blocked_preparation(admission, monkeypatch, boundary):
    import threading
    sid, session, path = queue_image(admission, monkeypatch)
    entered, release = threading.Event(), threading.Event()
    monkeypatch.setattr(server, "_emit", lambda *a, **kw: None)
    monkeypatch.setattr(server, "_session_profile_runtime_scope", lambda *a, **kw: __import__("contextlib").nullcontext())
    def prepare(*a):
        entered.set()
        assert release.wait(5)
        return False
    monkeypatch.setattr(server, "_session_uses_compute_host", prepare)
    worker = threading.Thread(target=lambda: server._drain_queued_prompt("drain", sid, session))
    worker.start()
    try:
        assert entered.wait(5)
        monkeypatch.setattr(server, "_session_uses_compute_host", lambda *a: False)
        if boundary == "stop":
            server._interrupt_session_turn(sid, session)
        else:
            server._pop_session_by_id(sid)
        assert not path.exists(), "claimed envelope was invisible to cancellation"
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive()
    assert not admission[3]


@pytest.mark.platforms("windows")
def test_queue_row_preparation_failure_cleans_bundle(admission, monkeypatch):
    sid, session, path = queue_image(admission, monkeypatch)
    def broken(*a, **kw):
        raise OSError("queued row storage failed")
    monkeypatch.setattr(server, "_replace_queued_user_row_for_turn", broken)
    try:
        server._drain_queued_prompt("drain", sid, session)
    except OSError:
        pass
    assert not path.exists()
    assert not session.get("_dispatching_queued_prompt")
    assert not session["running"]


@pytest.mark.platforms("windows")
def test_successful_write_cannot_admit_replaced_staging_path(tmp_path, monkeypatch):
    from tui_gateway import explicit_attachments as ea
    original = Path.open
    replaced = []
    class ReplacingWrite:
        def __init__(self, stream, path):
            self.stream, self.path = stream, path
        def __enter__(self):
            return self
        def __exit__(self, *a):
            self.stream.close()
            self.path.rename(self.path.with_suffix(".original"))
            self.path.write_bytes(b"foreign replacement")
            replaced.append(self.path)
        def fileno(self):
            return self.stream.fileno()
        def write(self, raw):
            return self.stream.write(raw)
    def opening(path, mode="r", *a, **kw):
        stream = original(path, mode, *a, **kw)
        return ReplacingWrite(stream, path) if mode == "xb" else stream
    monkeypatch.setattr(Path, "open", opening)
    with pytest.raises(OSError):
        ea.stage({"profile_home": str(tmp_path)}, [m.image_payload()])
    if replaced:
        assert replaced[0].read_bytes() == b"foreign replacement"
    else:
        assert not list((tmp_path / "images/explicit-turns").glob("*.png"))


def test_retirement_reservation_lasts_through_claimed_dispatch(admission, monkeypatch):
    from hermes_cli import backend_retirement as br
    sid, session, path = queue_image(admission, monkeypatch)
    fence = br.RetirementFence()
    monkeypatch.setattr(br, "retirement", fence)
    observed = []
    monkeypatch.setattr(server, "_session_uses_compute_host", lambda *a: observed.append(fence.active_count()) or False)
    server._drain_queued_prompt("drain", sid, session)
    assert observed == [1], "retirement lost the queue claim before dispatch settled"
    assert fence.active_count() == 0
    assert path.exists()


@pytest.mark.platforms("windows")
def test_staging_guard_excludes_directory_reparse_writers(tmp_path):
    from tui_gateway import explicit_attachment_storage as storage
    root = tmp_path / "images/explicit-turns"
    with storage.protected_directory(root):
        handle = storage._k.CreateFileW(str(root), 0x40000000, 7, None, 3, 0x02000000, None)
        if handle != __import__("ctypes").c_void_p(-1).value:
            storage._k.CloseHandle(handle)
            pytest.fail("directory write handle admitted while staging guard held")


@pytest.mark.platforms("posix")
def test_posix_cleanup_retains_instead_of_racing_pathname_unlink(tmp_path):
    from tui_gateway import explicit_attachments as ea
    bundle = ea.stage({"profile_home": str(tmp_path)}, [m.image_payload()])
    path = Path(bundle.paths[0])
    original = path.read_bytes()
    bundle.cleanup()
    assert path.read_bytes() == original


@pytest.mark.platforms("windows")
def test_concurrent_cleanup_cannot_close_a_handle_in_use(tmp_path, monkeypatch):
    import threading
    from tui_gateway import explicit_attachments as ea, explicit_attachment_storage as storage
    bundle = ea.stage({"profile_home": str(tmp_path)}, [m.image_payload()])
    handle = bundle.owners[0].handle
    entered, release, closed = threading.Event(), threading.Event(), threading.Event()
    reopen, close = storage._k.ReOpenFile, storage._k.CloseHandle
    first_thread = []
    def opening(h, *a):
        if threading.current_thread() is first_thread[0]:
            entered.set()
            assert release.wait(5)
        return reopen(h, *a)
    def closing(h):
        if h == handle:
            closed.set()
        return close(h)
    monkeypatch.setattr(storage._k, "ReOpenFile", opening)
    monkeypatch.setattr(storage._k, "CloseHandle", closing)
    first = threading.Thread(target=bundle.cleanup)
    first_thread.append(first)
    second = threading.Thread(target=bundle.cleanup)
    first.start()
    try:
        assert entered.wait(5)
        second.start()
        assert not closed.wait(2), "parallel cleanup closed the first cleanup authority"
    finally:
        release.set()
        first.join(5)
        if second.ident is not None:
            second.join(5)
    assert not first.is_alive() and not second.is_alive()
    assert bundle.owners[0].handle is None


@pytest.mark.platforms("windows")
def test_ambiguous_compute_error_retains_media_when_error_event_fails(admission, monkeypatch):
    sid, session, path = queue_image(admission, monkeypatch)
    monkeypatch.setattr(server, "_session_uses_compute_host", lambda *a: True)
    monkeypatch.setattr(server, "_submit_prompt_to_compute_host", lambda *a, **kw: {"error": {"message": "unknown acceptance"}})
    def emit(*a, **kw):
        raise OSError("client disconnected")
    monkeypatch.setattr(server, "_emit", emit)
    assert server._drain_queued_prompt("drain", sid, session)
    assert path.exists()


@pytest.mark.platforms("windows")
def test_transport_preparation_failure_cleans_removed_queue_head(admission, monkeypatch):
    sid, session, path = queue_image(admission, monkeypatch)
    session["queued_prompt"]["transport"] = object()
    monkeypatch.setattr(server, "_transport_is_dead", lambda *a: False)
    def attach(*a):
        raise OSError("transport preparation failed")
    monkeypatch.setattr(server, "_attach_session_transport", attach)
    try:
        server._drain_queued_prompt("drain", sid, session)
    except OSError:
        pass
    assert not path.exists()
    assert not session.get("_dispatching_queued_prompt")
    assert not session["running"]


def test_generation_reanchor_restores_live_explicit_bundle(admission, monkeypatch):
    sid, session, path = queue_image(admission, monkeypatch)
    envelope = session["queued_prompt"]
    def reanchor(*a):
        session["_queued_prompt_generation"] = session.get("_queued_prompt_generation", 0) + 1
        return False
    monkeypatch.setattr(server, "_session_uses_compute_host", reanchor)
    assert server._drain_queued_prompt("drain", sid, session)
    assert session["queued_prompt"] is envelope
    assert path.exists()
    assert not admission[3]
    assert not session.get("_dispatching_queued_prompt")



def test_generation_reanchor_during_rows_restores_explicit_bundle(admission, monkeypatch):
    sid, session, path = queue_image(admission, monkeypatch)
    envelope = session["queued_prompt"]
    def reanchor(*a, **kw):
        session["_queued_prompt_generation"] = session.get("_queued_prompt_generation", 0) + 1
        return None
    monkeypatch.setattr(server, "_replace_queued_user_row_for_turn", reanchor)
    assert server._drain_queued_prompt("drain", sid, session)
    assert session["queued_prompt"] is envelope
    assert path.exists()
    assert not admission[3]


def test_cleanup_large_replacement_read_is_bounded(tmp_path, monkeypatch):
    from tui_gateway import explicit_attachments as ea
    bundle = ea.stage({"profile_home": str(tmp_path)}, [m.image_payload()])
    path = Path(bundle.paths[0])
    path.rename(path.with_suffix(".original"))
    with path.open("wb") as stream:
        stream.truncate(ea.MAX_IMAGE_BYTES + 1)
    original = Path.open
    reads = []
    def recording(p, mode="r", *a, **kw):
        if p == path and mode == "rb":
            reads.append(p.stat().st_size)
        return original(p, mode, *a, **kw)
    monkeypatch.setattr(Path, "open", recording)
    bundle.cleanup()
    assert path.stat().st_size == ea.MAX_IMAGE_BYTES + 1
    assert bundle.owners[0].handle is None
    assert not reads, "cleanup read an oversized foreign replacement into memory: " + repr(reads)




def test_cleanup_growth_during_digest_read_is_hard_bounded(tmp_path, monkeypatch):
    from tui_gateway import explicit_attachments as ea
    bundle = ea.stage({"profile_home": str(tmp_path)}, [m.image_payload()])
    path = Path(bundle.paths[0])
    opening = Path.open
    requests = []
    class GrowingReader:
        def __init__(self, stream):
            self.stream = stream
        def __enter__(self):
            return self
        def __exit__(self, *a):
            self.stream.close()
        def fileno(self):
            return self.stream.fileno()
        def read(self, size=-1):
            requests.append(size)
            with opening(path, "r+b") as writer:
                writer.truncate(ea.MAX_IMAGE_BYTES + 1)
            return self.stream.read(size)
    def open_reader(p, mode="r", *a, **kw):
        stream = opening(p, mode, *a, **kw)
        return GrowingReader(stream) if p == path and mode == "rb" else stream
    monkeypatch.setattr(Path, "open", open_reader)
    bundle.cleanup()
    assert requests and all(0 < size <= ea.MAX_IMAGE_BYTES + 1 for size in requests), requests
    assert path.stat().st_size == ea.MAX_IMAGE_BYTES + 1
    assert bundle.owners[0].handle is None


@pytest.mark.platforms("windows")
def test_cleanup_identity_exception_releases_ownership(tmp_path, monkeypatch):
    from tui_gateway import explicit_attachments as ea
    bundle = ea.stage({"profile_home": str(tmp_path)}, [m.image_payload(), m.image_payload("red")])
    def failed(path):
        raise RuntimeError("unexpected digest failure")
    monkeypatch.setattr(ea, "_file_identity", failed)
    with pytest.raises(RuntimeError, match="unexpected digest failure"):
        bundle.cleanup()
    assert all(owner.handle is None for owner in bundle.owners)


def test_compute_send_delivered_then_flush_error_retains_media(admission, monkeypatch):
    import json, os
    from types import SimpleNamespace
    from tui_gateway.host_supervisor import HostSupervisor
    sid, session, path = queue_image(admission, monkeypatch)
    monkeypatch.setattr(server, "_session_uses_compute_host", lambda *a: True)
    monkeypatch.setattr(server, "_emit", lambda *a, **kw: None)
    rfd, wfd = os.pipe()
    received = []
    class DeliveredPipe:
        def write(self, text):
            raw = text.encode("utf-8")
            assert os.write(wfd, raw) == len(raw)
            received.append(json.loads(os.read(rfd, len(raw))))
            return len(text)
        def flush(self):
            raise OSError("injected flush failure AFTER complete frame delivery")
    supervisor = HostSupervisor(autostart=False, expected_build_sha="test", expected_hermes_home=session["profile_home"])
    supervisor._proc = SimpleNamespace(poll=lambda: None, stdin=DeliveredPipe())
    monkeypatch.setattr(server, "_get_compute_host_supervisor", lambda *a: supervisor)
    try:
        assert server._drain_queued_prompt("drain", sid, session)
        assert len(received) == 1
        assert received[0]["attached_images"] == [str(path)]
        assert received[0]["type"] == "turn.start"
        assert path.exists(), "compute frame delivered but ambiguous send failure deleted its media"
    finally:
        os.close(rfd)
        os.close(wfd)



@pytest.mark.platforms("windows")
def test_native_handle_count_no_leak_and_reader_access(tmp_path):
    import ctypes, gc
    from ctypes import wintypes
    from tui_gateway import explicit_attachments as ea
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.GetCurrentProcess.restype = wintypes.HANDLE
    k.GetProcessHandleCount.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    def count():
        gc.collect()
        n = wintypes.DWORD()
        assert k.GetProcessHandleCount(k.GetCurrentProcess(), ctypes.byref(n))
        return n.value
    baseline = count()
    for i in range(50):
        bundle = ea.stage({"profile_home": str(tmp_path)}, [m.image_payload()])
        path = Path(bundle.paths[0])
        assert path.read_bytes().startswith(bytes.fromhex("89504e47"))
        # A live CRT reader may deny DELETE; cleanup must retain and release authority.
        with path.open("rb") as reader:
            bundle.cleanup()
            assert reader.read().startswith(bytes.fromhex("89504e47"))
        assert bundle.owners[0].handle is None
        if path.exists():
            path.unlink()
        # Ancestor pins must not persist after staging or cleanup.
        root = path.parent
        renamed = root.with_name("renamed")
        root.rename(renamed)
        renamed.rename(root)
    assert count() <= baseline + 2


@pytest.mark.platforms("windows")
def test_native_directory_guard_failure_releases_prior_pins(tmp_path):
    import subprocess
    from tui_gateway import explicit_attachments as ea
    root, target = tmp_path / "profile", tmp_path / "foreign"
    root.mkdir()
    target.mkdir()
    link = root / "images"
    subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], check=True, capture_output=True)
    try:
        with pytest.raises((ValueError, OSError)):
            ea.stage({"profile_home": str(root)}, [m.image_payload()])
        assert list(target.iterdir()) == []
    finally:
        link.rmdir()
    renamed = root.with_name("profile-renamed")
    root.rename(renamed)
    renamed.rename(root)
