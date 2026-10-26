"""Real TCP/HTTP flow with two synthetic pictures and fake model inference."""
import copy

from test_app import wait_for
from test_http_regression import serve
from test_study_workflow import png, VisualBridge


def test_study_loopback_upload_confirm_answer_export_restart(tmp_path):
    data = tmp_path / 'study-http'
    bridge = VisualBridge()
    with serve(data, bridge) as (client, room):
        sid = client.get('/api/state').json()['session']['id']
        assert client.put('/api/settings', json={'auto_analyze': False}).status_code == 200
        for text in ('The supply curve shifts right. 供给增加。', 'Demand remains unchanged. 需求不变。'):
            assert client.post('/api/text', json={'session_id': sid, 'text': text}).status_code == 200
        state = client.get('/api/state').json()['session']
        end = max(s['end'] for s in state['segments'])
        for i in range(2):
            response = client.post('/api/slides', params={'session_id': sid, 'start': i * end / 2, 'title': f'图 {i+1}'},
                                   content=png(80+i), headers={'Content-Type': 'image/png'})
            assert response.status_code == 200, response.text
        request = {'session_id': sid, 'mode': 'recent120', 'anchor': end, 'explain': True}
        q = client.post('/api/study/prepare', json=request).json()
        assert len(q['evidence']['slides']) == 2
        assert q['evidence']['incomplete']  # Manual timing is honestly labelled.
        assert not bridge.calls
        frozen = copy.deepcopy(q['evidence'])
        assert client.post('/api/text', json={'text': 'Later irrelevant lecture.'}).status_code == 200
        route = f"/api/study/{q['id']}/send"
        assert client.post(route, json={'session_id': sid, 'destination_token': q['destination']['token']}).status_code == 409
        sent = client.post(route, json={'session_id': sid, 'destination_token': q['destination']['token'], 'confirm_images': True})
        assert sent.status_code == 200, sent.text
        wait_for(lambda: room.get(sid)['questions'][0]['status'] == 'done')
        assert len(bridge.calls) == 1 and len(bridge.calls[0]['images']) == 2
        assert 'Later irrelevant' not in bridge.calls[0]['context']
        saved = client.get('/api/export', params={'format': 'json'}).json()
        assert saved['questions'][0]['evidence'] == frozen
        markdown = client.get('/api/export').text
        assert '图片本体' in markdown and 'P0001' in markdown and 'P0002' in markdown
        bad = client.post('/api/study/prepare', json={**request, 'question': '😀'*4001})
        assert bad.status_code == 422 and '4001' in bad.json()['detail']
        assert client.post('/api/study/prepare', json={**request, 'slide_ids': ['P9999']}).status_code == 422
    with serve(data, VisualBridge()) as (client, _):
        state = client.get('/api/state').json()['session']
        assert state['questions'][0]['evidence'] == frozen
        assert state['questions'][0]['answer'] == saved['questions'][0]['answer']
        for p in frozen['slides']:
            assert client.get('/api/slides/'+p['id'], params={'session_id': sid}).status_code == 200
