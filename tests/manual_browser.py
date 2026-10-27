"""Optional manual browser fixture. Synthetic data only; never a real model.

python tests/manual_browser.py --data-dir <NEW-ISOLATED-DIRECTORY> --port 8768
Visit / for the app, /acceptance/cache for native IndexedDB checks.
POST /acceptance/flags/{fail_upload|lose_send|model_timeout|review_invalid}/{true|false} to
inject failures. Existing non-fixture directories are refused.
"""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi.responses import FileResponse, JSONResponse
from app import create_app
from codex_bridge import CodexBridgeError
from test_app import FakeASR, FakeBridge
from review_fixture import synthetic_review_result


def build(data):
    data = Path(data).resolve()
    marker = data / '.synthetic-browser-fixture'
    if data.exists() and any(data.iterdir()) and not marker.exists():
        raise ValueError('Refusing an existing non-fixture directory; use a new isolated folder.')
    data.mkdir(parents=True, exist_ok=True)
    marker.write_text('Synthetic browser acceptance only. No real classroom data.', encoding='utf-8')
    flags = dict(fail_upload=False, lose_send=False, model_timeout=False, review_invalid=False)

    class Bridge(FakeBridge):
        context_chars = 48000

        def generate_review(self, evidence, options, images, **kwargs):
            self.calls.append({'kind': 'review', 'sources': evidence.get('source_ids', []),
                               'images': [p['id'] for p in images]})
            if flags['model_timeout']:
                raise CodexBridgeError('synthetic timeout', kind='timeout')
            time.sleep(.3)
            return '{"interrupted":' if flags['review_invalid'] else synthetic_review_result(evidence, options)

        def ask_visual(self, context, question, images, **kwargs):
            self.calls.append({'context': context, 'question': question, 'images': [p['id'] for p in images]})
            if flags['model_timeout']:
                raise CodexBridgeError('synthetic timeout', kind='timeout')
            time.sleep(.3)
            return ('【合成服务，未调用真实 AI】供给增加、需求不变时，价格下降、数量增加。[S0001] '
                    + ' '.join('[' + image['id'] + ']' for image in images))

    bridge = Bridge()
    app = create_app({}, data, engine=FakeASR(), bridge=bridge)
    room = app.state.room
    if len(room.sessions) == 1 and not room.get()['segments']:
        room.get()['title'] = 'Synthetic browser classroom A'
        room.get()['settings']['auto_analyze'] = False
        room.append_text(room.current, 'The supply curve shifts right. 供给增加。', 0, 12, 'synthetic')
        room.append_text(room.current, 'Demand is unchanged. 需求不变。', 12, 24, 'synthetic')
        room.save(room.get())

    @app.middleware('http')
    async def synthetic_only(request, call_next):
        if request.method != 'GET' and request.url.path in ('/api/ai/settings', '/api/ai/test'):
            return JSONResponse({'detail': 'Synthetic fixture forbids real provider setup or connection tests.'}, status_code=403)
        if flags['fail_upload'] and request.method == 'POST' and request.url.path == '/api/slides':
            return JSONResponse({'detail': 'Synthetic upload failure; nothing saved.'}, status_code=503)
        response = await call_next(request)
        if flags['lose_send'] and request.method == 'POST' and request.url.path.endswith('/send'):
            flags['lose_send'] = False
            return JSONResponse({'detail': 'Synthetic lost response after enqueue; check original task.'}, status_code=503)
        return response

    @app.get('/acceptance/cache')
    def cache_page():
        return FileResponse(Path(__file__).with_name('study-cache-browser.html'))

    @app.get('/acceptance/cache.js')
    def cache_script():
        return FileResponse(Path(__file__).with_name('study-cache-browser.js'), media_type='application/javascript')

    @app.get('/acceptance/state')
    def state():
        return {'flags': flags, 'fake_calls': bridge.calls}

    @app.post('/acceptance/flags/{name}/{enabled}')
    def toggle(name: str, enabled: bool):
        if name not in flags:
            return JSONResponse({'detail': 'Unknown synthetic failure.'}, status_code=404)
        flags[name] = enabled
        return flags

    return app


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, required=True)
    parser.add_argument('--port', type=int, default=8768)
    args = parser.parse_args()
    import uvicorn
    uvicorn.run(build(args.data_dir), host='127.0.0.1', port=args.port, log_level='warning')
