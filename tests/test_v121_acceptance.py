"""Real loopback TCP acceptance; generated images, fake model, isolated data."""
import copy

from test_app import wait_for
from test_http_regression import serve
from test_study_workflow import VisualBridge, png


def test_cross_class_revisions_history_duplicate_requests_and_restart(tmp_path):
    folder = tmp_path / 'acceptance-v121'
    bridge = VisualBridge()
    with serve(folder, bridge) as (client, room):
        sid = client.get('/api/state').json()['session']['id']
        client.put('/api/settings', json={'auto_analyze': False}).raise_for_status()
        client.post('/api/text', json={'session_id': sid, 'text': 'Supply increases. 供给增加😀'}).raise_for_status()
        segment = client.get('/api/state').json()['session']['segments'][0]
        params = {'session_id': sid, 'start': 0, 'title': 'Synthetic supply chart', 'request_id': 'http-upload-v121'}
        p = client.post('/api/slides', params=params, content=png(), headers={'Content-Type': 'image/png'}).json()
        old_bytes = client.get('/api/slides/' + p['id'], params={'session_id': sid, 'version': 1, 'sha256': p['sha256']}).content
        request = {'session_id': sid, 'mode': 'recent30', 'anchor': segment['end'], 'explain': True,
                   'request_id': 'http-prepare-v121'}
        response = client.post('/api/study/prepare', json=request)
        assert response.status_code == 200, response.text
        q = response.json()
        evidence = copy.deepcopy(q['evidence'])
        other = client.post('/api/sessions', json={'title': 'Synthetic class B'}).json()['id']
        # A's lost responses are looked up while B is active, without leaking A into B.
        assert client.get('/api/slides/requests/http-upload-v121', params={'session_id': sid}).json()['slide']['id'] == p['id']
        assert not client.get('/api/slides/requests/http-upload-v121', params={'session_id': other}).json()['found']
        assert client.post('/api/slides', params=params, content=png()).json()['id'] == p['id']
        assert client.post('/api/study/prepare', json=request).json()['id'] == q['id']
        state_b = client.get('/api/state').json()['session']
        assert state_b['id'] == other and not state_b['slides'] and not state_b['questions']
        rotation = {'session_id': sid, 'degrees': 90, 'request_id': 'http-rotate-v121'}
        rotated = client.post(f"/api/slides/{p['id']}/rotate", json=rotation)
        assert rotated.status_code == 200, rotated.text
        assert rotated.json()['version'] == 2
        assert client.post(f"/api/slides/{p['id']}/rotate", json=rotation).json()['version'] == 2
        assert client.get('/api/slides/' + p['id'], params={'session_id': sid, 'version': 1, 'sha256': p['sha256']}).content == old_bytes
        send = {'session_id': sid, 'destination_token': q['destination']['token'], 'confirm_images': True}
        route = f"/api/study/{q['id']}/send"
        assert client.post(route, json=send).status_code == 409
        changed = client.get(f"/api/study/{q['id']}", params={'session_id': sid}).json()
        assert changed['source_status']['changed'] and changed['evidence'] == evidence
        send.update(confirm_original=True, source_token=changed['source_status']['token'])
        client.post(route, json=send).raise_for_status()
        client.post(route, json=send).raise_for_status()
        wait_for(lambda: room.get(sid)['questions'][0]['status'] == 'done')
        assert len(bridge.calls) == 1
        client.delete('/api/slides/' + p['id'], params={'session_id': sid}).raise_for_status()
        client.post('/api/segments/S0001/edit', json={'session_id': sid, 'text': 'Corrected explanation.'}).raise_for_status()
        result = client.get(f"/api/study/{q['id']}", params={'session_id': sid}).json()
        assert result['evidence'] == evidence and result['source_status']['slides'][0]['removed']
        assert {'text', 'rotation', 'removal'} <= {item['kind'] for item in result['source_status']['changes']}
        export = client.get('/api/export', params={'format': 'json', 'session_id': sid}).json()
        assert export['questions'][0]['evidence'] == evidence
        assert export['segments'][0]['machine_text'] == segment['text']
    with serve(folder, VisualBridge()) as (client, room):
        result = client.get(f"/api/study/{q['id']}", params={'session_id': sid}).json()
        assert result['evidence'] == evidence and result['source_status']['slides'][0]['removed']
        assert client.get('/api/slides/' + p['id'], params={'session_id': sid, 'version': 1, 'sha256': p['sha256']}).content == old_bytes
        assert client.post(route, json=send).json()['status'] == 'done'
        assert not room.bridge.calls and room.ai_queue.empty()
        # Corruption is reported for this exact historical asset, never replaced.
        (room.data / sid / evidence['slides'][0]['path']).write_bytes(b'synthetic corruption')
        unavailable = client.get(f"/api/study/{q['id']}", params={'session_id': sid}).json()
        assert unavailable['source_status']['slides'][0]['available'] is False
        assert client.get('/api/slides/' + p['id'], params={'session_id': sid, 'version': 1, 'sha256': p['sha256']}).status_code >= 400
