import asyncio
import json
import time

import pytest
from annie_core import RobotConfig, RobotUnavailableError
from annie_runtime import HarborRuntime


class FakeSocket:
    def __init__(self, url, key):
        self.outbound = []
        self.inbound = asyncio.Queue()

    async def open(self):
        return self

    async def send(self, event):
        self.outbound.append(event)
        if event.get('type') == 'session.update':
            self.inbound.put_nowait({'type': 'session.updated',
                                    'session': {'id': 'fixture', **event['session']}})

    async def recv(self):
        return await self.inbound.get()

    async def close(self):
        pass


async def ready(runtime):
    for _ in range(200):
        if all(c.ready for c in runtime.realtime.values()):
            return
        await asyncio.sleep(.005)
    raise AssertionError('realtime handshake timeout')


def test_preference_api_identity_reload_and_isolation(tmp_path):
    async def run():
        runtime = HarborRuntime([RobotConfig('a', 'r1'), RobotConfig('b', 'r2')],
                                tmp_path/'runtime.db', realtime_key='fixture',
                                realtime_socket_factory=FakeSocket)
        await runtime.start()
        try:
            await ready(runtime)
            identity = runtime.session_state('a')
            result = await runtime.set_language_preference('a', scope='chat', language='zh',
                    dialect='sichuan', request_id='click-1', expected_identity=identity)
            assert result['status'] == 'set'
            assert runtime.realtime['a'].language.preferences['chat']['dialect'] == 'sichuan'
            assert runtime.realtime['b'].language.preferences['chat'] is None
            await runtime.switch_session('a', chat_id='another', persona_id='default')
            with pytest.raises(RobotUnavailableError):
                await runtime.set_language_preference('a', scope='robot', language='en',
                    dialect='unknown', request_id='stale', expected_identity=identity)
            await ready(runtime)
            assert runtime.realtime['a'].language.preferences['chat'] is None
        finally:
            await runtime.close()
    asyncio.run(run())


def test_tool_requires_matching_explicit_user_scope(tmp_path):
    async def run():
        runtime = HarborRuntime([RobotConfig('a', 'r')], tmp_path/'runtime.db',
                                realtime_key='fixture', realtime_socket_factory=FakeSocket)
        await runtime.start()
        try:
            await ready(runtime)
            c = runtime.realtime['a']
            await c.submit_text('我们去四川旅游吧')
            await c._tool_call({'call_id':'bad','name':'set_language_preference',
                               'arguments':json.dumps(dict(scope='robot',language='zh',dialect='sichuan'))})
            outputs=[e['item']['output'] for e in c.socket.outbound if e.get('item',{}).get('type')=='function_call_output']
            assert json.loads(outputs[-1])['status'] == 'rejected'
            assert runtime.store.load_language_preferences('a',c.robot.chat_id)['robot'] is None
            await c.cancel()
            await c.submit_text('以后你都用英语')
            await c._tool_call({'call_id':'good','name':'set_language_preference',
                               'arguments':json.dumps(dict(scope='robot',language='en',dialect='unknown'))})
            assert runtime.store.load_language_preferences('a',c.robot.chat_id)['robot']['language']=='en'
        finally:
            await runtime.close()
    asyncio.run(run())


def test_main_sidecar_association_uses_sample_window_not_text_or_item(tmp_path):
    async def run():
        runtime = HarborRuntime([RobotConfig('a','r')],tmp_path/'runtime.db',
                                realtime_key='fixture',realtime_socket_factory=FakeSocket)
        await runtime.start()
        try:
            await ready(runtime)
            c=runtime.realtime['a']
            gen=c.robot.generation
            await c.handle({'type':'input_audio_buffer.speech_started','item_id':'main-1',
                            'audio_start_ms':100},gen)
            await c.handle({'type':'input_audio_buffer.speech_stopped','item_id':'main-1',
                            'audio_end_ms':500},gen)
            base={**c.robot.session_state(),'source_id':None,
                  'audio_stream_epoch':c.robot.audio.stream_epoch,
                  'provider_item_id':'entirely-different', 'language':'en','emotion':'happy',
                  'text':'hello friend','observed_monotonic_ms':time.monotonic()*1000,
                  'main_sample_start':1600,'main_sample_end':8000}
            assert c.accept_acoustic_observation(base)
            assert c.language.observation['language']=='en'
            assert not c.accept_acoustic_observation({**base,'main_sample_start':16000,'main_sample_end':20000})
        finally:
            await runtime.close()
    asyncio.run(run())


def test_cancelled_queued_language_tool_cannot_commit_after_epoch_retires(tmp_path):
    import threading

    async def run():
        runtime = HarborRuntime([RobotConfig('a', 'r')], tmp_path/'runtime.db',
                                realtime_key='fixture', realtime_socket_factory=FakeSocket)
        await runtime.start()
        entered, release = threading.Event(), threading.Event()
        original = runtime.store.set_language_preference
        def delayed(*args, **kwargs):
            entered.set()
            assert release.wait(2)
            return original(*args, **kwargs)
        runtime.store.set_language_preference = delayed
        try:
            await ready(runtime)
            c = runtime.realtime['a']
            await c.submit_text('以后你都用英语')
            c._queue_tool_call({'call_id': 'queued', 'name': 'set_language_preference',
                               'arguments': json.dumps(dict(scope='robot',language='en',dialect='unknown'))})
            for _ in range(100):
                if entered.is_set():
                    break
                await asyncio.sleep(.005)
            assert entered.is_set()
            epoch = c._tool_epoch
            cancelling = asyncio.create_task(c.cancel())
            for _ in range(100):
                if c._tool_epoch != epoch:
                    break
                await asyncio.sleep(.001)
            assert c._tool_epoch != epoch
            release.set()
            await asyncio.wait_for(cancelling, 2)
            assert runtime.store.load_language_preferences('a', c.robot.chat_id)['robot'] is None
        finally:
            release.set()
            await runtime.close()
    asyncio.run(run())


def test_normal_smart_turn_interrupt_retires_style_preserves_new_explicit_request(tmp_path):
    async def run():
        runtime = HarborRuntime([RobotConfig('a', 'r')], tmp_path/'runtime.db',
                                realtime_key='fixture', realtime_socket_factory=FakeSocket)
        await runtime.start()
        try:
            await ready(runtime)
            c=runtime.realtime['a']
            await c.submit_text('这句请用河南话')
            await c.handle({'type':'response.created','response':{'id':'old'}}, c.robot.generation)
            await c.handle({'type':'response.text.delta','delta':'旧的回复'}, c.robot.generation)
            old=c.turn_id
            c.language.user_request('这句请用四川话')
            await c._interrupt_active_turn('valid_smart_turn')
            assert c.language.response is None
            c._create_turn('这句请用四川话',audio=True)
            await c.handle({'type':'response.created','response':{'id':'new'}}, c.robot.generation)
            await c.handle({'type':'response.text.delta','delta':'新的回复'}, c.robot.generation)
            assert c.language.style_for(c.turn_id).dialect=='sichuan'
            assert c.turn_id != old
        finally:
            await runtime.close()
    asyncio.run(run())


def test_http_contract_offline_status_strict_identity_and_preference_commit(tmp_path):
    from annie_web import create_app
    from fastapi.testclient import TestClient

    runtime=HarborRuntime([RobotConfig('a','r')],tmp_path/'runtime.db')
    app=create_app(runtime,tmp_path)
    with TestClient(app) as http:
        status=http.get('/api/robots/a/speech-expression').json()
        assert status['sidecar']['state']=='not_configured'
        assert status['language']['target']['source']=='runtime_default'
        identity={k:status[k] for k in ('chat_id','persona_id','session_epoch','generation')}
        body={**identity,'scope':'chat','request_id':'click','language':'zh','dialect':'henan'}
        assert http.put('/api/robots/a/language-preference',json=body).json()['status']=='set'
        status=http.get('/api/robots/a/speech-expression').json()
        assert status['language']['preferences']['chat']['dialect']=='henan'
        assert http.put('/api/robots/a/speech-expression/sidecar',json={**identity,'enabled':True}).status_code==409
        assert http.put('/api/robots/a/language-preference',json={**body,'generation':True}).status_code==422
        assert http.put('/api/robots/a/language-preference',json={**body,'generation':999}).status_code==409
        cleared=http.request('DELETE','/api/robots/a/language-preference',json={**identity,'scope':'chat','request_id':'clear'})
        assert cleared.json()['status']=='cleared'


def test_stop_flag_during_completed_receive_does_not_reenter_read_loop(tmp_path):
    async def run():
        reference={}
        class StoppingSocket(FakeSocket):
            async def recv(self):
                if self.inbound.empty() and reference['client'].ready:
                    reference['client']._stopping=True
                    return {'type':'session.updated','session':{}}
                return await super().recv()
        runtime=HarborRuntime([RobotConfig('a','r')],tmp_path/'runtime.db',
                            realtime_key='fixture',realtime_socket_factory=StoppingSocket)
        reference['client']=runtime.realtime['a']
        await runtime.start()
        c=reference['client']
        try:
            done,_=await asyncio.wait([c.task],timeout=.3)
            assert done, 'stopping flag must leave receive loop even if receive completed concurrently'
        finally:
            # Explicitly retire generation so pre-fix test also cleans up its blocked reader.
            c.robot.generation+=1
            await asyncio.wait_for(runtime.close(),1)
    asyncio.run(run())


def test_current_distress_context_survives_interrupt_style_reset(tmp_path):
    async def run():
        runtime=HarborRuntime([RobotConfig('a','r')],tmp_path/'runtime.db',
                            realtime_key='fixture',realtime_socket_factory=FakeSocket)
        await runtime.start()
        try:
            await ready(runtime)
            c=runtime.realtime['a']
            await c.submit_text('之前的话题')
            c.language.user_request('我很难过，想求助')
            await c._interrupt_active_turn('valid_smart_turn')
            c.language.response_started()
            c.language.first_text('new')
            c.language.reply_intent(language='zh',emotion='sarcastic')
            assert c.language.style_for('new').emotion in {'serious','calm'}
        finally:
            await runtime.close()
    asyncio.run(run())


def test_session_ack_received_before_send_returns_confirms_language_version(tmp_path):
    async def run():
        runtime=HarborRuntime([RobotConfig('a','r')],tmp_path/'runtime.db',
                            realtime_key='fixture',realtime_socket_factory=FakeSocket)
        await runtime.start()
        try:
            await ready(runtime)
            c=runtime.realtime['a']
            await c._stop_visual_task()
            c.language.user_request('这句请用河南话')
            c._visual_last_send_at=0
            original=c.socket.send
            async def fast_ack(event):
                await c.handle({'type':'session.updated','session':event['session']},c.robot.generation)
                await original(event)
            c.socket.send=fast_ack
            await c._visual_loop(c.socket,c.robot.generation)
            assert c.language.confirmed_revision == c.language.revision
        finally:
            await runtime.close()
    asyncio.run(run())


def test_admitted_pcm_fanout_reaches_policy_via_real_sidecar_callback(tmp_path):
    from annie_adapters import VERIFIED_MIC_FORMAT

    async def run():
        sockets=[]
        class AcousticSocket(FakeSocket):
            def __init__(self,*args):
                super().__init__(*args)
                self.audio=0
                sockets.append(self)
            async def send(self,event):
                await super().send(event)
                if event.get('type')=='input_audio_buffer.append':
                    self.audio+=1
                    if self.audio==1:
                        self.inbound.put_nowait({'type':'input_audio_buffer.speech_started',
                                                'item_id':'sidecar-item','audio_start_ms':0})
                    if self.audio==3:
                        self.inbound.put_nowait({'type':'conversation.item.input_audio_transcription.text',
                                                'item_id':'sidecar-item','language':'en','emotion':'happy',
                                                'text':'hello my friend','stash':'draft only'})
            def abort(self):
                pass
        runtime=HarborRuntime([RobotConfig('a','r',listen_enabled=True)],tmp_path/'runtime.db',
                            realtime_key='fixture',realtime_socket_factory=FakeSocket,
                            acoustic_enabled=True,acoustic_socket_factory=AcousticSocket)
        await runtime.start()
        try:
            await ready(runtime)
            c=runtime.realtime['a']
            source='room:r:reb_mic'
            runtime.bind_input_source(source,['a'])
            runtime.start_pcm_stream(source,VERIFIED_MIC_FORMAT)
            await c.handle({'type':'input_audio_buffer.speech_started','item_id':'main-item',
                            'audio_start_ms':0},c.robot.generation)
            for _ in range(10):
                runtime.ingest_pcm(source,b'\x00\x09MIC_AUDIO'+bytes(640))
                await asyncio.sleep(.005)
            for _ in range(100):
                if c.language.observation:
                    break
                await asyncio.sleep(.005)
            assert c.language.observation['language']=='en'
            assert c.language.observation['source']=='qwen3_asr_sidecar'
            assert c.language.observation['provider_item_id']=='sidecar-item'
            assert c.language.observation['utterance_id'] != 'main-item'
            assert c.frames_sent==10
            assert len(sockets)==1
            assert not any(e['type']=='conversation.item.create' for e in c.socket.outbound)
        finally:
            await runtime.close()
    asyncio.run(run())
