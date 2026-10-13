"""Browser output is distinct from physical speaker, but can continue audibly."""
import asyncio
import time

from annie_core import RobotConfig
from annie_runtime import HarborRuntime

from tests.test_short_continuation import build, identity, settle
from tests.test_speech_expression_integration import FakeSocket, ready


def arm_browser(service, client):
    turn='typed'
    client._last_user_turn_id=turn
    client._turn_timelines[turn]={'input_mode':'text'}
    client.robot.turn_cache[turn]={
        'turn_id':turn,'turn_source':'user','status':'completed',
        'user_text':'今天天气和我的好心情一样，我们继续聊聊。',
        'assistant_generated_text':'那我们去喝杯柠檬水吧。',
        'updated_ms':time.time_ns()//1_000_000,
    }
    assert service.viewed_text('a',turn,identity('a'))
    service.on_event('a',{'kind':'tts.playback.drained','turn_id':turn,
                          'source':'browser_test',**identity('a')})
    return turn


def test_visible_browser_drained_reply_continues_with_audio_not_text_only():
    async def run():
        service,clients,_,planner=build(robots=('a',))
        arm_browser(service,clients['a'])
        assert service.snapshot('a')['channel']=='browser_test'
        await settle(service)
        assert len(planner.calls)==1
        assert clients['a'].messages[0][-1] is False
        await settle(service)
        assert len(clients['a'].messages)==1
    asyncio.run(run())


def test_hidden_browser_or_stale_receipt_does_not_arm_audible_continuation():
    async def run():
        service,clients,_,planner=build(robots=('a',))
        turn=arm_browser(service,clients['a'])
        service.rows['a']['visible_at']=time.monotonic()-9
        await settle(service)
        assert planner.calls==[]
        assert service.snapshot('a')['reason']=='web_not_visible'
        service.viewed_text('a',turn,identity('a'))
        assert service.rows['a']['visible_at']>time.monotonic()-1
        service.on_event('a',{'kind':'tts.playback.drained','turn_id':turn,
                             'source':'browser_test',**identity('a'),'generation':0})
        assert service.snapshot('a')['channel']=='browser_test'
    asyncio.run(run())


def test_typed_english_instruction_is_sent_before_response_create_without_waiting_ack(tmp_path):
    async def run():
        runtime=HarborRuntime([RobotConfig('a','r')],tmp_path/'db',realtime_key='fixture',
                             realtime_socket_factory=FakeSocket)
        await runtime.start()
        try:
            await ready(runtime)
            c=runtime.realtime['a'];c.socket.outbound.clear()
            c._visual_last_send_at=time.monotonic()  # old coalescer would postpone hint
            result=await c.submit_text('Have an iced drink at a small shop.')
            sent=c.socket.outbound
            update=next(i for i,e in enumerate(sent) if e.get('type')=='session.update'
                        and '目标语种=en' in e.get('session',{}).get('instructions',''))
            create=next(i for i,e in enumerate(sent) if e.get('type')=='response.create')
            assert update<create
            c.language.first_text(result['turn_id'])
            assert c.turn_speech_style(result['turn_id'],c.robot.session_state()).language=='en'
        finally:await runtime.close()
    asyncio.run(run())


def test_current_panel_latest_tts_preserves_actual_parameters_after_owner_is_gone(tmp_path):
    async def run():
        runtime=HarborRuntime([RobotConfig('a','r')],tmp_path/'db',realtime_key='fixture',
                             realtime_socket_factory=FakeSocket)
        await runtime.start()
        try:
            await ready(runtime);c=runtime.realtime['a']
            c.publish('turn.started',turn_id='old',user_text='你好')
            c.publish('tts.run_task',turn_id='old',task_id='task',parameters={'instruction':'具体指令','rate':1.04})
            proof=c.speech_expression_snapshot()['latest_tts']
            assert proof['turn_id']=='old' and proof['tts']['parameters']['instruction']=='具体指令'
            c.publish('turn.started',turn_id='no-speech',user_text='只显示文案')
            assert c.speech_expression_snapshot()['latest_tts']['turn_id']=='old'
            proof['tts']['parameters']['instruction']='mutated'
            assert c.speech_expression_snapshot()['latest_tts']['tts']['parameters']['instruction']=='具体指令'
            c.robot.generation+=1
            assert c.speech_expression_snapshot()['latest_tts'] is None
        finally:await runtime.close()
    asyncio.run(run())


def test_cancel_during_typed_instruction_send_does_not_resurrect_response(tmp_path):
    async def run():
        class BlockedSocket(FakeSocket):
            async def send(self,event):
                if (event.get('type')=='session.update' and
                        '目标语种=en' in event.get('session',{}).get('instructions','')):
                    entered.set();await release.wait()
                await super().send(event)
        entered,release=asyncio.Event(),asyncio.Event()
        runtime=HarborRuntime([RobotConfig('a','r')],tmp_path/'db',realtime_key='fixture',
                             realtime_socket_factory=BlockedSocket)
        await runtime.start()
        try:
            await ready(runtime);c=runtime.realtime['a'];c.socket.outbound.clear()
            task=asyncio.create_task(c.submit_text('Have an iced drink at a small shop.'))
            await asyncio.wait_for(entered.wait(),1)
            owned=c.turn_id
            await c.cancel();release.set()
            result=await asyncio.gather(task,return_exceptions=True)
            assert isinstance(result[0],Exception)
            assert c.robot.turn_cache[owned]['status']=='cancelled'
            assert not any(e['type']=='response.create' for e in c.socket.outbound)
            assert not any(e.get('item',{}).get('role')=='user' for e in c.socket.outbound)
        finally:release.set();await runtime.close()
    asyncio.run(run())


def test_cancel_during_user_item_send_does_not_create_response(tmp_path):
    async def run():
        class BlockedSocket(FakeSocket):
            async def send(self,event):
                if event.get('item',{}).get('role')=='user':
                    entered.set();await release.wait()
                await super().send(event)
        entered,release=asyncio.Event(),asyncio.Event()
        runtime=HarborRuntime([RobotConfig('a','r')],tmp_path/'db',realtime_key='fixture',
                             realtime_socket_factory=BlockedSocket)
        await runtime.start()
        try:
            await ready(runtime);c=runtime.realtime['a'];c.socket.outbound.clear()
            task=asyncio.create_task(c.submit_text('hello'))
            await asyncio.wait_for(entered.wait(),1)
            owned=c.turn_id
            await c.cancel();release.set()
            result=await asyncio.gather(task,return_exceptions=True)
            assert isinstance(result[0],Exception)
            assert c.robot.turn_cache[owned]['status']=='cancelled'
            assert not any(e['type']=='response.create' for e in c.socket.outbound)
        finally:release.set();await runtime.close()
    asyncio.run(run())


def test_speech_during_typed_instruction_send_preempts_text_response(tmp_path):
    async def run():
        class BlockedSocket(FakeSocket):
            async def send(self,event):
                if (event.get('type')=='session.update' and
                        '目标语种=en' in event.get('session',{}).get('instructions','')):
                    entered.set();await release.wait()
                await super().send(event)
        entered,release=asyncio.Event(),asyncio.Event()
        runtime=HarborRuntime([RobotConfig('a','r')],tmp_path/'db',realtime_key='fixture',
                             realtime_socket_factory=BlockedSocket)
        await runtime.start()
        try:
            await ready(runtime);c=runtime.realtime['a'];c.socket.outbound.clear()
            task=asyncio.create_task(c.submit_text('hello'))
            await asyncio.wait_for(entered.wait(),1)
            c.speech_active=True;c._last_provider_speech_started_t=time.monotonic()
            release.set()
            result=await asyncio.gather(task,return_exceptions=True)
            assert isinstance(result[0],Exception)
            assert not any(e['type']=='response.create' for e in c.socket.outbound)
        finally:release.set();await runtime.close()
    asyncio.run(run())


def test_fast_completed_response_remains_accepted(tmp_path):
    async def run():
        class FastSocket(FakeSocket):
            async def send(self,event):
                await super().send(event)
                if event['type']=='response.create':
                    c=runtime.realtime['a'];gen=c.robot.generation
                    await c.handle({'type':'response.created','response':{'id':'fast'}},gen)
                    await c.handle({'type':'response.text.delta','response_id':'fast','delta':'Hello!'},gen)
                    await c.handle({'type':'response.done','response':{'id':'fast','status':'completed','output':[]}},gen)
        runtime=HarborRuntime([RobotConfig('a','r')],tmp_path/'db',realtime_key='fixture',
                             realtime_socket_factory=FastSocket)
        await runtime.start()
        try:
            await ready(runtime)
            result=await runtime.realtime['a'].submit_text('hello')
            assert result['status']=='accepted'
            assert runtime.robots['a'].turn_cache[result['turn_id']]['status']=='completed'
        finally:await runtime.close()
    asyncio.run(run())
