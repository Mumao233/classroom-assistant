import json
import threading
from pathlib import Path

import httpx
import pytest

from ai_api import ApiBridge
from codex_bridge import CodexBridge, CodexBridgeError
from test_review_schema import evidence, options, result


class Capture(CodexBridge):
    def __init__(self):
        self.context_chars = 48000
        self.calls = []

    def _run(self, prompt, source, **kwargs):
        self.calls.append((prompt, source, kwargs))
        return json.dumps(result(), ensure_ascii=False)


def image(tmp_path):
    path = tmp_path / 'image.png'
    path.write_bytes(b'synthetic image bytes')
    return {'id': 'P0001', 'path': str(path.resolve()), 'mime_type': 'image/png', 'start': 0, 'end': 12}


def test_review_bridge_sends_exact_sources_and_returns_unmodified_raw_json(tmp_path):
    bridge = Capture()
    raw = bridge.generate_review(evidence(), options(), [image(tmp_path)])
    assert json.loads(raw) == result()
    assert len(bridge.calls) == 1
    prompt, source, args = bridge.calls[0]
    assert str(tmp_path) not in prompt
    assert args['allowed_refs'] == {'S0001', 'P0001'}
    assert '缺少' in args['instructions']


@pytest.mark.parametrize('case', ['missing', 'extra', 'relative', 'mime', 'five', 'oversize', 'cancelled'])
def test_invalid_or_cancelled_review_never_dispatches(tmp_path, case):
    bridge = Capture()
    source, images, event = evidence(), [image(tmp_path)], None
    if case == 'missing': images = []
    elif case == 'extra': images[0]['id'] = 'P9999'
    elif case == 'relative': images[0]['path'] = 'relative.png'
    elif case == 'mime': images[0]['mime_type'] = 'text/html'
    elif case == 'five':
        images = [{**images[0], 'id': f'P{n:04d}'} for n in range(1, 6)]
        source['slides'] = [{'id': x['id']} for x in images]
    elif case == 'oversize': source['segments'][0]['text'] = 'x' * 48000
    else:
        event = threading.Event()
        event.set()
    with pytest.raises(CodexBridgeError):
        bridge.generate_review(source, options(), images, cancel_event=event)
    assert bridge.calls == []


def test_api_inherits_review_prompt_with_one_mock_request_and_inline_image(tmp_path):
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps(result())}, 'finish_reason': 'stop'}]})
    bridge = ApiBridge({'base_url': 'https://synthetic.invalid/v1', 'model': 'fake'}, 'synthetic-key',
                       transport=httpx.MockTransport(handler))
    raw = bridge.generate_review(evidence(), options(), [image(tmp_path)])
    assert json.loads(raw) == result()
    assert len(requests) == 1
    payload = json.loads(requests[0].content)
    assert 'tools' not in payload
    assert 'data:image/png;base64,' in json.dumps(payload)
    assert str(tmp_path) not in json.dumps(payload)


def test_completed_invalid_review_output_returns_raw_for_local_validation_and_retention(tmp_path):
    broken = result()
    broken['cards'][0]['source_ids'] = ['S9999']
    raw = json.dumps(broken)
    bridge = ApiBridge({'base_url': 'https://synthetic.invalid/v1', 'model': 'fake'}, 'synthetic-key',
        transport=httpx.MockTransport(lambda request: httpx.Response(200,
            json={'choices': [{'message': {'content': raw}, 'finish_reason': 'stop'}]})))
    assert bridge.generate_review(evidence(), options(), [image(tmp_path)]) == raw
