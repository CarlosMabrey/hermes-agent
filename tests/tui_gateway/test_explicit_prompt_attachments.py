import threading
from types import SimpleNamespace
import pytest
from tui_gateway import server

PERSIST_SUBMIT = server._persist_session_row_for_submit

@pytest.fixture
def admission(monkeypatch, tmp_path):
    session = dict(agent=SimpleNamespace(), agent_ready=threading.Event(), history=[],
        history_lock=threading.RLock(), history_version=0, running=False,
        attached_images=['client-A.png'], profile_home=str(tmp_path),
        session_key='explicit-test', transport=None)
    sid = 'explicit-test'
    monkeypatch.setitem(server._sessions, sid, session)
    for name in ['_start_agent_build', '_persist_session_row_for_submit']:
        monkeypatch.setattr(server, name, lambda *a, **kw: None)
    monkeypatch.setattr(server, '_restart_completed_failed_agent_build', lambda *a: False)
    monkeypatch.setattr(server, '_ensure_active_session_slot', lambda *a: None)
    monkeypatch.setattr(server, '_session_uses_compute_host', lambda *a: False)
    targets = []
    class DeferredThread:
        def __init__(self, target, **kw): self.target = target
        def start(self): targets.append(self.target)
    monkeypatch.setattr(server, 'threading', SimpleNamespace(Thread=DeferredThread))
    monkeypatch.setattr(server, '_wait_agent_for_prompt', lambda *a: None)
    seen = []
    monkeypatch.setattr(server, '_run_prompt_submit', lambda *a, **kw: seen.append(kw.get('image_paths')))
    yield sid, session, targets, seen
    server._sessions.pop(sid, None)

@pytest.mark.parametrize('text', ['hello B', '@file:notes.txt'])
def test_explicit_empty_never_claims_A_even_during_deferred_build(admission, text):
    sid, session, targets, seen = admission
    response = server.handle_request(dict(id='b', method='prompt.submit', params=dict(
        session_id=sid, text=text, attachment_mode='explicit', attachments=[])))
    assert 'error' not in response, response
    assert session['attached_images'] == ['client-A.png']
    session['attached_images'].append('client-C.png')
    targets.pop()()
    assert seen == [[]]
    assert session['attached_images'] == ['client-A.png', 'client-C.png']

def image_payload(color='blue'):
    import base64, io
    from PIL import Image
    b = io.BytesIO()
    Image.new('RGB', (2, 2), color).save(b, format='PNG')
    return dict(kind='image', mime_type='image/png', content_base64=base64.b64encode(b.getvalue()).decode(), filename='B.png')

def test_explicit_image_is_private_and_bound_before_build(admission):
    from pathlib import Path
    sid, session, targets, seen = admission
    response = server.handle_request(dict(id='b', method='prompt.submit', params=dict(
        session_id=sid, text='B', attachment_mode='explicit', attachments=[image_payload()])))
    assert 'error' not in response, response
    assert session['attached_images'] == ['client-A.png']
    targets.pop()()
    assert len(seen[0]) == 1
    path = Path(seen[0][0])
    assert path.is_file()
    assert path.is_relative_to(Path(session['profile_home']))
    assert path.name != 'B.png'

@pytest.mark.parametrize('mode', ['queue', 'steer', 'interrupt'])
@pytest.mark.parametrize('media', [False, True])
def test_busy_explicit_preserves_A_and_media_is_distinct_fifo(admission, monkeypatch, mode, media):
    sid, session, targets, seen = admission
    corrections = []
    session.update(running=True, agent=SimpleNamespace(
        steer=lambda text: corrections.append(text) or True,
        redirect=lambda text: corrections.append(text) or True,
        _supports_active_turn_redirect=True))
    monkeypatch.setattr(server, '_load_busy_input_mode', lambda: mode)
    monkeypatch.setattr(server, '_persist_queued_user_row', lambda *a: None)
    monkeypatch.setattr(server, '_interrupt_busy_session', lambda *a: pytest.fail('must not interrupt'))
    for text in ['B', 'C']:
        response = server.handle_request(dict(id=text, method='prompt.submit', params=dict(
            session_id=sid, text=text, attachment_mode='explicit',
            attachments=[image_payload()] if media else [])))
        assert 'error' not in response, response
        assert session['attached_images'] == ['client-A.png']
        assert response['result']['status'] == ('queued' if media or mode == 'queue' else
                                               'steered' if mode == 'steer' else 'redirected')
    if media:
        entries = [session['queued_prompt'], *session['queued_prompts']]
        assert [e['text'] for e in entries] == ['B', 'C']
        assert all(len(e['image_paths']) == 1 for e in entries)
        assert entries[0]['image_paths'] != entries[1]['image_paths']
        assert corrections == []
    elif mode == 'queue':
        assert session['queued_prompt']['image_paths'] == []

@pytest.mark.parametrize('media', [False, True])
@pytest.mark.parametrize('fallback', [False, True])
def test_compute_and_fallback_use_same_explicit_bundle(admission, monkeypatch, media, fallback):
    sid, session, targets, seen = admission
    monkeypatch.setattr(server, '_session_uses_compute_host', lambda *a: True)
    dispatched = []
    def compute(*a, **kw):
        dispatched.append(kw.get('image_paths'))
        return {'error': {'message': 'send failed'}} if fallback else {'result': {'status': 'streaming'}}
    monkeypatch.setattr(server, '_submit_prompt_to_compute_host', compute)
    response = server.handle_request(dict(id='b', method='prompt.submit', params=dict(
        session_id=sid, text='B', attachment_mode='explicit', attachments=[image_payload()] if media else [])))
    assert 'error' not in response, response
    assert dispatched[0] is not None
    assert len(dispatched[0]) == int(media)
    if fallback:
        targets.pop()()
        assert seen == dispatched
    assert session['attached_images'] == ['client-A.png']

def test_legacy_claim_is_at_admission_not_after_build(admission):
    sid, session, targets, seen = admission
    response = server.handle_request(dict(id='a', method='prompt.submit', params=dict(session_id=sid, text='A')))
    assert 'error' not in response, response
    assert session['attached_images'] == []
    session['attached_images'].append('client-C.png')
    targets.pop()()
    assert seen == [['client-A.png']]
    assert session['attached_images'] == ['client-C.png']

@pytest.mark.parametrize('bad', [
    {'attachment_mode': 'typo'},
    {'attachment_mode': 'legacy', 'attachments': [image_payload()]},
    {'attachment_mode': 'explicit', 'attachments': [{'kind': 'file'}]},
    {'attachment_mode': 'explicit', 'attachments': [{**image_payload(), 'content_base64': '%%%'}]},
    {'attachment_mode': 'explicit', 'attachments': [{**image_payload(), 'mime_type': 'image/jpeg'}]},
    {'attachment_mode': 'explicit', 'attachments': [{**image_payload(), 'content_base64': 'aGVsbG8='}]},
    {'attachment_mode': 'explicit', 'attachments': [{**image_payload(), 'path': 'other-profile/secret'}]},
    {'attachment_mode': 'explicit', 'attachments': [{**image_payload(), 'filename': '../evil.png'}]},
    {'attachment_mode': 'explicit', 'attachments': [image_payload()] * 5},
])
def test_malformed_explicit_refused_before_admission(admission, bad):
    from pathlib import Path
    sid, session, targets, seen = admission
    response = server.handle_request(dict(id='b', method='prompt.submit', params=dict(session_id=sid, text='B', **bad)))
    assert response.get('error', {}).get('code') == 4000, response
    assert session['running'] is False
    assert session['attached_images'] == ['client-A.png']
    assert targets == []
    assert not list(Path(session['profile_home']).rglob('*.png'))

@pytest.mark.parametrize('limit,value', [('MAX_IMAGE_BYTES', 10), ('MAX_TOTAL_BYTES', 10), ('MAX_PIXELS', 3)])
def test_attachment_limits_are_enforced_before_writes(admission, monkeypatch, limit, value):
    from tui_gateway import explicit_attachments
    monkeypatch.setattr(explicit_attachments, limit, value)
    sid, session, targets, seen = admission
    response = server.handle_request(dict(id='b', method='prompt.submit', params=dict(session_id=sid,
        text='B', attachment_mode='explicit', attachments=[image_payload()])))
    assert response.get('error', {}).get('code') == 4000, response
    assert targets == []
    assert session['attached_images'] == ['client-A.png']

@pytest.mark.parametrize('failure', ['refused', 'storage', 'build', 'cancel', 'voice_stop'])
@pytest.mark.platforms("windows")
def test_unrun_bundle_cleanup_does_not_delete_other_drafts(admission, monkeypatch, failure):
    from pathlib import Path
    sid, session, targets, seen = admission
    a = Path(session['profile_home']) / 'A.png'
    a.write_bytes(b'client A owned draft')
    session['attached_images'] = [str(a)]
    if failure == 'refused':
        monkeypatch.setattr(server, '_ensure_active_session_slot', lambda *a: 'full')
    if failure == 'storage':
        monkeypatch.setattr(server, '_persist_session_row_for_submit', lambda *a: {'error': {'message': 'storage'}})
    if failure == 'build':
        monkeypatch.setattr(server, '_wait_agent_for_prompt', lambda *a: {'error': {'message': 'build'}})
        monkeypatch.setattr(server, '_emit_terminal_turn_error', lambda *a, **kw: None)
        monkeypatch.setattr(server, '_session_info', lambda *a: {})
    monkeypatch.setattr(server, '_emit', lambda *a, **kw: None)
    if failure == 'voice_stop':
        monkeypatch.setattr(server, '_typed_stop_phrase_response', lambda *a: {'result': {'voice_stopped': True}})
    response = server.handle_request(dict(id='b', method='prompt.submit', params=dict(session_id=sid,
        text='B', attachment_mode='explicit', attachments=[image_payload()])))
    if failure == 'cancel':
        session['_turn_cancel_requested'] = True
    if targets:
        targets.pop()()
    assert list(Path(session['profile_home']).rglob('*.png')) == [a]
    assert session['attached_images'] == [str(a)]
    assert a.read_bytes() == b'client A owned draft'

@pytest.mark.platforms("windows")
def test_stop_cleans_only_queued_owned_bundles(admission, monkeypatch):
    from pathlib import Path
    sid, session, targets, seen = admission
    session.update(running=True)
    monkeypatch.setattr(server, '_load_busy_input_mode', lambda: 'queue')
    monkeypatch.setattr(server, '_persist_queued_user_row', lambda *a: None)
    monkeypatch.setattr(server, '_session_profile_runtime_scope', lambda *a, **kw: __import__('contextlib').nullcontext())
    for text in ['B', 'C']:
        response = server.handle_request(dict(id=text, method='prompt.submit', params=dict(session_id=sid,
            text=text, attachment_mode='explicit', attachments=[image_payload()])))
        assert 'error' not in response
    assert len(list(Path(session['profile_home']).rglob('*.png'))) == 2
    server._interrupt_session_turn(sid, session)
    assert list(Path(session['profile_home']).rglob('*.png')) == []
    assert session['attached_images'] == ['client-A.png']

def test_stage_refuses_directory_links_and_cleanup_preserves_replaced_file(tmp_path):
    from tui_gateway.explicit_attachments import stage
    from pathlib import Path
    root = tmp_path / 'profile'
    foreign = tmp_path / 'foreign'
    foreign.mkdir()
    root.mkdir()
    # Windows junctions need no administrator symlink permission.
    import os, subprocess
    if os.name == 'nt':
        subprocess.run(['cmd', '/c', 'mklink', '/J', str(root / 'images'), str(foreign)], check=True, capture_output=True)
    else:
        (root / 'images').symlink_to(foreign, target_is_directory=True)
    try:
        with pytest.raises((ValueError, OSError)):
            stage({'profile_home': str(root)}, [image_payload()])
        assert list(foreign.iterdir()) == []
    finally:
        if os.name == 'nt': (root / 'images').rmdir()
        else: (root / 'images').unlink()
    bundle = stage({'profile_home': str(root)}, [image_payload()])
    owned = Path(bundle.paths[0])
    owned.unlink()
    owned.write_bytes(b'replacement not owned')
    bundle.cleanup()
    assert owned.read_bytes() == b'replacement not owned'

def test_legacy_busy_empty_claim_does_not_steal_later_paste(admission, monkeypatch):
    sid, session, targets, seen = admission
    session.update(running=True, attached_images=[])
    monkeypatch.setattr(server, '_load_busy_input_mode', lambda: 'queue')
    monkeypatch.setattr(server, '_persist_queued_user_row', lambda *a: None)
    response = server.handle_request(dict(id='b', method='prompt.submit', params=dict(session_id=sid, text='B')))
    assert 'error' not in response
    session.update(running=False, attached_images=['late-A.png'])
    server._drain_queued_prompt('drain', sid, session)
    assert seen == [[]]
    assert session['attached_images'] == ['late-A.png']

def test_idle_to_busy_race_queues_explicit_media_instead_of_second_runner(admission, monkeypatch):
    sid, session, targets, seen = admission
    original = server._lock_in_submit_turn
    raced = False
    def claim(*a, **kw):
        nonlocal raced
        if not raced:
            raced = True
            session['running'] = True  # another client won admission after our idle read
        return original(*a, **kw)
    monkeypatch.setattr(server, '_lock_in_submit_turn', claim)
    monkeypatch.setattr(server, '_load_busy_input_mode', lambda: 'steer')
    monkeypatch.setattr(server, '_persist_queued_user_row', lambda *a: None)
    response = server.handle_request(dict(id='b', method='prompt.submit', params=dict(session_id=sid,
        text='B', attachment_mode='explicit', attachments=[image_payload()])))
    assert response['result']['status'] == 'queued'
    assert targets == []
    assert session['queued_prompt']['text'] == 'B'
    assert session['attached_images'] == ['client-A.png']

@pytest.mark.parametrize('name', ['CON.png', 'A.png:ads', 'a.JPG', 'evil.png ', 'LPT1.png', 'C:\\escape.png'])
def test_unsafe_or_mismatched_filename_refused(admission, name):
    sid, session, targets, seen = admission
    response = server.handle_request(dict(id='b', method='prompt.submit', params=dict(session_id=sid,
        text='B', attachment_mode='explicit', attachments=[{**image_payload(), 'filename': name}])))
    assert response.get('error', {}).get('code') == 4000, response

def test_staged_raster_strips_metadata_and_trailing_content(tmp_path):
    import base64, io
    from PIL import Image, PngImagePlugin
    from tui_gateway.explicit_attachments import stage
    b = io.BytesIO()
    info = PngImagePlugin.PngInfo()
    info.add_text('secret', 'test metadata')
    Image.new('RGB', (2, 2), 'blue').save(b, format='PNG', pnginfo=info)
    payload = {**image_payload(), 'content_base64': base64.b64encode(b.getvalue() + b'trailing-active-content').decode()}
    bundle = stage({'profile_home': str(tmp_path)}, [payload])
    from pathlib import Path
    raw = Path(bundle.paths[0]).read_bytes()
    assert b'trailing-active-content' not in raw
    with Image.open(io.BytesIO(raw)) as image:
        assert 'secret' not in image.info
        assert image.getpixel((0, 0)) == (0, 0, 255)

@pytest.mark.platforms("windows")
def test_worker_admission_refusal_cleans_owned_bundle(admission, monkeypatch):
    from pathlib import Path
    sid, session, targets, seen = admission
    monkeypatch.setattr(server, '_run_prompt_submit', lambda *a, **kw: False)
    response = server.handle_request(dict(id='b', method='prompt.submit', params=dict(session_id=sid,
        text='B', attachment_mode='explicit', attachments=[image_payload()])))
    assert response['result']['status'] == 'streaming'
    targets.pop()()
    assert list(Path(session['profile_home']).rglob('*.png')) == []
    assert session['attached_images'] == ['client-A.png']

def test_launch_profile_image_uses_launch_home_not_client_profile(admission, monkeypatch, tmp_path):
    from pathlib import Path
    sid, session, targets, seen = admission
    session['profile_home'] = None
    monkeypatch.setattr(server, '_hermes_home', tmp_path / 'launch')
    response = server.handle_request(dict(id='b', method='prompt.submit', params=dict(session_id=sid,
        text='B', attachment_mode='explicit', attachments=[image_payload()])))
    assert 'error' not in response, response
    targets.pop()()
    assert Path(seen[0][0]).is_relative_to(tmp_path / 'launch')

def test_explicit_cross_profile_stamp_refused_before_staging(admission, monkeypatch, tmp_path):
    sid, session, targets, seen = admission
    monkeypatch.setattr(server, '_profile_home', lambda *a: tmp_path / 'foreign')
    response = server.handle_request(dict(id='b', method='prompt.submit', params=dict(session_id=sid,
        profile='foreign', text='B', attachment_mode='explicit', attachments=[image_payload()])))
    assert response.get('error', {}).get('code') == 4000, response
    assert session['attached_images'] == ['client-A.png']
    assert not (tmp_path / 'foreign').exists()

@pytest.mark.parametrize('text,media', [('B text', False), ('@file:notes.txt', False), ('B image', True)])
def test_two_client_actual_staging_with_build_barrier(admission, monkeypatch, text, media):
    from pathlib import Path
    from PIL import Image
    sid, session, targets, seen = admission
    session['attached_images'] = []
    a = server.handle_request(dict(id='a-stage', method='image.attach_bytes', params=dict(
        session_id=sid, **{k: v for k, v in image_payload('red').items() if k in ['content_base64', 'filename']})))
    assert 'error' not in a
    a_path = a['result']['path']
    entered, release = threading.Event(), threading.Event()
    def wait(*a):
        entered.set()
        assert release.wait(5), 'barrier did not release'
    monkeypatch.setattr(server, '_wait_agent_for_prompt', wait)
    admitted = []
    def real_turn(_rid, _sid, record, prompt, **kw):
        result = server._admit_prompt_turn(_sid, record, prompt, kw.get('image_paths'), None, None, None)
        admitted.append(result[0])
        return True
    monkeypatch.setattr(server, '_run_prompt_submit', real_turn)
    b = server.handle_request(dict(id='b-send', method='prompt.submit', params=dict(
        session_id=sid, text=text, attachment_mode='explicit', attachments=[image_payload('blue')] if media else [])))
    assert 'error' not in b
    worker = threading.Thread(target=targets.pop())
    worker.start()
    try:
        assert entered.wait(5)
        c = server.handle_request(dict(id='c-stage', method='image.attach_bytes', params=dict(
            session_id=sid, **{k: v for k, v in image_payload('green').items() if k in ['content_base64', 'filename']})))
        assert 'error' not in c
        assert session['attached_images'] == [a_path, c['result']['path']]
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive()
    assert len(admitted[0]) == int(media)
    if media:
        with Image.open(admitted[0][0]) as image: assert image.getpixel((0, 0)) == (0, 0, 255)
    session['running'] = False
    a_send = server.handle_request(dict(id='a-send', method='prompt.submit', params=dict(session_id=sid, text='A and C')))
    assert 'error' not in a_send
    targets.pop()()
    assert admitted[-1] == [a_path, c['result']['path']]
    assert session['attached_images'] == []
    assert Path(a_path).is_file()

@pytest.mark.parametrize('compute', [False, True])
def test_busy_explicit_empty_survives_queue_drain(admission, monkeypatch, compute):
    sid, session, targets, seen = admission
    session['running'] = True
    monkeypatch.setattr(server, '_load_busy_input_mode', lambda: 'queue')
    monkeypatch.setattr(server, '_persist_queued_user_row', lambda *a: None)
    response = server.handle_request(dict(id='b', method='prompt.submit', params=dict(session_id=sid,
        text='B', attachment_mode='explicit', attachments=[])))
    assert response['result']['status'] == 'queued'
    session['running'] = False
    monkeypatch.setattr(server, '_session_uses_compute_host', lambda *a: compute)
    if compute:
        monkeypatch.setattr(server, '_submit_prompt_to_compute_host', lambda *a, **kw:
            seen.append(kw.get('image_paths')) or {'result': {'status': 'streaming'}})
    assert server._drain_queued_prompt('drain', sid, session)
    assert seen == [[]]
    assert session['attached_images'] == ['client-A.png']

@pytest.mark.parametrize('media', [False, True])
@pytest.mark.parametrize('fail', [False, True])
def test_real_compute_frame_and_pipe_failure_fallback(admission, monkeypatch, media, fail):
    sid, session, targets, seen = admission
    frames = []
    def submit(frame, on_complete):
        frames.append(frame)
        if fail:
            on_complete({'reason': 'send_failed'})
            raise BrokenPipeError('test pipe')
    monkeypatch.setattr(server, '_session_uses_compute_host', lambda *a: True)
    monkeypatch.setattr(server, '_get_compute_host_supervisor', lambda *a: SimpleNamespace(submit_turn=submit))
    response = server.handle_request(dict(id='b', method='prompt.submit', params=dict(session_id=sid,
        text='B', attachment_mode='explicit', attachments=[image_payload()] if media else [])))
    assert 'error' not in response, response
    assert len(frames[0]['attached_images']) == int(media)
    if fail:
        targets.pop()()
        assert seen == [frames[0]['attached_images']]
    assert session['attached_images'] == ['client-A.png']

def test_explicit_acceptance_has_one_durable_user_row_without_foreign_draft(admission, monkeypatch, tmp_path):
    from hermes_state import SessionDB
    from contextlib import nullcontext
    sid, session, targets, seen = admission
    db = SessionDB(db_path=tmp_path / 'acceptance.db')
    db.create_session(session['session_key'], source='desktop')
    monkeypatch.setattr(server, '_ensure_session_db_row', lambda *a: True)
    monkeypatch.setattr(server, '_session_db', lambda *a: nullcontext(db))
    monkeypatch.setattr(server, '_persist_session_row_for_submit', PERSIST_SUBMIT)
    try:
        response = server.handle_request(dict(id='b', method='prompt.submit', params=dict(session_id=sid,
            text='B only', attachment_mode='explicit', attachments=[image_payload()])))
        assert 'error' not in response, response
        rows = db.get_messages_as_conversation(session['session_key'], include_row_ids=True)
        assert [(r['role'], r['content']) for r in rows] == [('user', 'B only')]
        assert response['result']['user_row_id'] == rows[0]['_row_id']
        assert session['attached_images'] == ['client-A.png']
    finally:
        db.close()

def test_gateway_advertises_explicit_attachments_contract():
    response = server.handle_request({'id': 'caps', 'method': 'gateway.capabilities', 'params': {}})
    assert response['result'].get('prompt_explicit_attachments_v1') is True

@pytest.mark.platforms("windows")
def test_partial_staging_failure_leaves_no_owned_or_foreign_leak(admission, monkeypatch):
    from pathlib import Path
    sid, session, targets, seen = admission
    original = Path.open
    writes = 0
    class FailingWrite:
        def __init__(self, stream): self.stream = stream
        def __enter__(self): return self
        def __exit__(self, *a): self.stream.close()
        def fileno(self): return self.stream.fileno()
        def write(self, raw):
            self.stream.write(raw[:8])
            raise OSError('fixture disk-full')
    def open_file(path, mode='r', *a, **kw):
        nonlocal writes
        stream = original(path, mode, *a, **kw)
        if mode == 'xb' and 'explicit-turns' in str(path):
            writes += 1
            if writes == 2: return FailingWrite(stream)
        return stream
    monkeypatch.setattr(Path, 'open', open_file)
    response = server.handle_request(dict(id='b', method='prompt.submit', params=dict(session_id=sid,
        text='B', attachment_mode='explicit', attachments=[image_payload(), image_payload()])))
    assert response.get('error', {}).get('code') == 5027, response
    assert list(Path(session['profile_home']).rglob('*.png')) == []
    assert session['attached_images'] == ['client-A.png']
    assert targets == []

def test_legacy_voice_stop_preserves_unknown_session_compatibility(monkeypatch):
    monkeypatch.setattr(server, '_typed_stop_phrase_response', lambda *a: {'result': {'voice_stopped': True}})
    response = server.handle_request({'id': 'stop', 'method': 'prompt.submit', 'params': {'session_id': 'stale', 'text': 'stop'}})
    assert response.get('result', {}).get('voice_stopped') is True, response

def test_legacy_storage_refusal_restores_only_claimed_drafts_in_order(admission, monkeypatch):
    sid, session, targets, seen = admission
    def refused(*a):
        session['attached_images'].append('late-C.png')
        return {'error': {'message': 'storage refusal'}}
    monkeypatch.setattr(server, '_persist_session_row_for_submit', refused)
    response = server.handle_request(dict(id='a', method='prompt.submit', params=dict(session_id=sid, text='A')))
    assert 'error' in response
    assert session['attached_images'] == ['client-A.png', 'late-C.png']
    assert targets == []

@pytest.mark.parametrize('busy', [False, True])
def test_image_only_explicit_turn_is_admitted_without_foreign_drafts(admission, monkeypatch, busy):
    sid, session, targets, seen = admission
    session['running'] = busy
    monkeypatch.setattr(server, '_load_busy_input_mode', lambda: 'steer')
    monkeypatch.setattr(server, '_persist_queued_user_row', lambda *a: None)
    response = server.handle_request(dict(id='b', method='prompt.submit', params=dict(session_id=sid,
        text='', attachment_mode='explicit', attachments=[image_payload()])))
    assert response['result']['status'] == ('queued' if busy else 'streaming')
    if not busy:
        targets.pop()()
        assert len(seen[0]) == 1
    else:
        assert len(session['queued_prompt']['image_paths']) == 1
    assert session['attached_images'] == ['client-A.png']

def test_staging_io_failure_does_not_delete_a_replacement(admission, monkeypatch):
    from pathlib import Path
    sid, session, targets, seen = admission
    original = Path.open
    replacement_path = []
    class ReplacedWrite:
        def __init__(self, stream, path): self.stream, self.path = stream, path
        def __enter__(self): return self
        def __exit__(self, *a): self.stream.close()
        def fileno(self): return self.stream.fileno()
        def write(self, raw):
            self.stream.write(raw[:8])
            self.stream.close()
            other = self.path.with_suffix('.replacement')
            other.write_bytes(b'foreign replacement')
            replacement_path.append(other)
            self.path.rename(self.path.with_suffix(".original"))
            other.replace(self.path)
            replacement_path[0] = self.path
            raise OSError('fixture interrupted write')
    def open_file(path, mode='r', *a, **kw):
        stream = original(path, mode, *a, **kw)
        return ReplacedWrite(stream, path) if mode == 'xb' and 'explicit-turns' in str(path) else stream
    monkeypatch.setattr(Path, 'open', open_file)
    response = server.handle_request(dict(id='b', method='prompt.submit', params=dict(session_id=sid,
        text='B', attachment_mode='explicit', attachments=[image_payload()])))
    assert response.get('error', {}).get('code') == 5027, response
    assert replacement_path[0].read_bytes() == b'foreign replacement'
    assert session['attached_images'] == ['client-A.png']
    assert targets == []
