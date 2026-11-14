"""Regression coverage for native sessions, durable runs and in-flight additions."""
import json
import threading
import time
import unittest
from unittest.mock import patch

from guest_gateway import GuestGateway, GuestJob, ProgressReporter, ProgressSignal
from test_guest_gateway_context import gateway, invocation, guest_bot_reply


class SteeringTests(unittest.TestCase):
    def make_gateway(self):
        gw = gateway()
        self.addCleanup(gw._test_temp.cleanup)
        gw.cfg.hermes_native_sessions = True
        gw.cfg.hermes_poll_interval = 0.005
        gw.cfg.hermes_run_retry_backoff = 0
        gw.cfg.placeholder_enabled = False
        gw.cfg.progress_enabled = False
        gw.cfg.reactions_enabled = False
        gw.cfg.owner_media_enabled = False
        gw.cfg.tool_recovery_enabled = False
        gw.sent = []
        gw.tg = lambda method, payload, **kw: gw.sent.append((method, payload)) or {
            'ok': True, 'result': {'message_id': 900 + len(gw.sent)}}
        return gw

    def admit(self, gw, number, reply=None):
        message = invocation(number, reply)
        gw.handle_guest({'update_id': number, 'guest_message': message})
        return gw.pending_jobs[f'update:{number}']

    def wait_idle(self, gw):
        deadline = time.monotonic() + 3
        while gw.jobs.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertEqual(gw.jobs.unfinished_tasks, 0)

    def test_owner_reply_chain_new_root_and_cross_scope_isolation(self):
        gw = self.make_gateway()
        a = self.admit(gw, 10)
        b = self.admit(gw, 11, a.message)
        c = self.admit(gw, 12, b.message)
        d = self.admit(gw, 13)
        self.assertEqual(a.context_thread_id, b.context_thread_id)
        self.assertEqual(a.context_thread_id, c.context_thread_id)
        self.assertNotEqual(a.context_thread_id, d.context_thread_id)
        forged = invocation(14, a.message, chat_id=-100222)
        self.assertNotEqual(gw._context_info(forged, 14)['thread_id'], a.context_thread_id)
        restored = GuestGateway(gw.cfg, gw.state_path)
        self.assertEqual(restored._context_info(invocation(15, c.message),15)['thread_id'], a.context_thread_id)

    def test_native_runs_omit_mirror_history_and_keep_stable_session(self):
        gw = self.make_gateway()
        a = self.admit(gw,10)
        sid = gw._guest_session_id(a.message)
        gw.reply_sessions[sid]['messages'] = [{'role':'user','content':'STALE MIRROR'}]
        posts = []
        def http(url, payload=None, headers=None, **kw):
            if payload is not None:
                posts.append((payload,headers))
                return {'run_id':'r1'}
            return {'status':'completed','output':'native answer'}
        with patch('guest_gateway.http_json',side_effect=http):
            self.assertEqual(gw.call_hermes(a.message),'native answer')
        self.assertNotIn('conversation_history',posts[0][0])
        self.assertEqual(posts[0][0]['session_id'],sid)
        self.assertEqual(posts[0][1]['X-Hermes-Session-Key'],sid)
        self.assertTrue(posts[0][1]['Idempotency-Key'].startswith('guest-'))
        self.assertEqual(gw.reply_sessions[sid]['messages'],[])

    def test_running_reply_is_steered_once_and_has_one_final_answer(self):
        gw = self.make_gateway()
        gw.cfg.worker_count = 2
        a = self.admit(gw,10)
        running = threading.Event()
        steers = []
        starts = []
        def http(url, payload=None, **kw):
            if url.endswith('/steer'):
                steers.append(payload['input'])
                return {'accepted':True}
            if url.endswith('/runs'):
                starts.append(payload)
                return {'run_id':'active'}
            running.set()
            return {'status':'completed','output':'combined answer'} if steers else {'status':'running'}
        with patch('guest_gateway.http_json',side_effect=http):
            gw.start_worker()
            try:
                self.assertTrue(running.wait(2))
                b = self.admit(gw,11,a.message)
                self.wait_idle(gw)
            finally:
                gw.stop_worker()
        self.assertEqual(len(starts),1)
        self.assertEqual(len(steers),1)
        self.assertIn('question 11',steers[0])
        self.assertFalse(gw.pending_jobs)
        self.assertEqual(len([m for m,p in gw.sent if m in {'sendRichMessage','sendMessage'}]),1)
        self.assertEqual(b.steer_status,'accepted')

    def test_terminal_race_defers_addition_to_same_native_session(self):
        gw = self.make_gateway()
        a = self.admit(gw,10)
        b = self.admit(gw,11,a.message)
        starts=[]; polls={}; steer_calls=[]
        def http(url,payload=None,**kw):
            if url.endswith('/steer'):
                steer_calls.append(url)
                raise RuntimeError('HTTP 409 run_not_accepting_steer')
            if url.endswith('/runs'):
                starts.append(payload)
                return {'run_id':f'r{len(starts)}'}
            n=polls.get(url,0);polls[url]=n+1
            if url.endswith('/r1') and n==0:return {'status':'running'}
            return {'status':'completed','output':'done'}
        with patch('guest_gateway.http_json',side_effect=http):
            gw.start_worker()
            try:self.wait_idle(gw)
            finally:gw.stop_worker()
        self.assertEqual(len(starts),2)
        self.assertEqual(len(steer_calls),1)
        self.assertEqual(starts[0]['session_id'],starts[1]['session_id'])
        self.assertIn('question 11',starts[1]['input'])
        self.assertFalse(gw.pending_jobs)

    def test_restart_resumes_run_without_post_even_after_runtime_deadline(self):
        gw = self.make_gateway();a=self.admit(gw,10)
        a.run_id='existing';a.run_started_at=time.time()-99999
        a.run_payload={'input':'frozen original','session_id':gw._guest_session_id(a.message)}
        gw._write_state_locked()
        restored=GuestGateway(gw.cfg,gw.state_path)
        a=restored.pending_jobs[a.key]
        calls=[]
        def http(url,payload=None,**kw):
            calls.append((url,payload))
            return {'status':'completed','output':'saved output'}
        with patch('guest_gateway.http_json',side_effect=http), patch.object(restored,'_build_hermes_prompt',side_effect=AssertionError('no redownload')):
            self.assertEqual(restored.call_hermes(a.message),'saved output')
        self.assertEqual(len(calls),1)
        self.assertIsNone(calls[0][1])
        self.assertTrue(calls[0][0].endswith('/existing'))

    def test_lost_start_response_reuses_frozen_payload_and_key(self):
        gw=self.make_gateway();a=self.admit(gw,10);posts=[]
        def http(url,payload=None,headers=None,**kw):
            if url.endswith('/runs'):
                posts.append((json.dumps(payload,sort_keys=True),headers['Idempotency-Key']))
                self.assertIsNotNone(json.loads(gw.state_path.read_text())['pending_jobs'][a.key]['run_payload'])
                if len(posts)==1:raise RuntimeError('connection timed out')
                return {'run_id':'single'}
            return {'status':'completed','output':'done'}
        with patch('guest_gateway.http_json',side_effect=http):
            self.assertEqual(gw.call_hermes(a.message),'done')
        self.assertEqual(posts[0],posts[1])

    def test_accepted_steer_survives_restart_without_second_run(self):
        gw=self.make_gateway();a=self.admit(gw,10);b=self.admit(gw,11,a.message)
        a.run_id='existing';a.run_started_at=time.time();a.run_payload={'input':'original'}
        b.steer_parent_key=a.key;b.steer_status='accepted';gw._write_state_locked()
        restored=GuestGateway(gw.cfg,gw.state_path);restored.tg=gw.tg
        calls=[]
        def http(url,payload=None,**kw):
            calls.append((url,payload));return {'status':'completed','output':'combined'}
        with patch('guest_gateway.http_json',side_effect=http):
            restored.start_worker()
            try:self.wait_idle(restored)
            finally:restored.stop_worker()
        self.assertEqual(len(calls),1)
        self.assertFalse(restored.pending_jobs)

    def test_uncertain_steer_is_not_reposted_and_other_branch_not_injected(self):
        gw=self.make_gateway();a=self.admit(gw,10);b=self.admit(gw,11,a.message);c=self.admit(gw,12)
        a.run_id='active'
        with patch('guest_gateway.http_json',side_effect=RuntimeError('timed out')) as http:
            gw._drain_steering(a);gw._drain_steering(a)
        self.assertEqual(http.call_count,1)
        self.assertEqual(b.steer_status,'uncertain')
        self.assertEqual(c.steer_status,'')
        self.assertIn('may already',b.message['_guest_recovery_instruction'])

    def test_admission_ack_finishes_before_steering_can_consume_it(self):
        gw=self.make_gateway();a=self.admit(gw,10);b=self.admit(gw,11,a.message)
        a.run_id='active';gw.admitting_jobs.add(b.key)
        with patch('guest_gateway.http_json',return_value={'accepted':True}) as http:
            gw._drain_steering(a)
            self.assertEqual(http.call_count,0)
            gw.admitting_jobs.remove(b.key)
            gw._drain_steering(a)
            self.assertEqual(http.call_count,1)

    def test_restored_sending_steer_is_conservative_followup(self):
        gw=self.make_gateway();a=self.admit(gw,10);b=self.admit(gw,11,a.message)
        b.steer_parent_key=a.key;b.steer_status='sending';gw._write_state_locked()
        restored=GuestGateway(gw.cfg,gw.state_path)
        child=restored.pending_jobs[b.key]
        self.assertEqual(child.steer_status,'uncertain')
        self.assertIn('avoid repeating',child.message['_guest_recovery_instruction'])

    def test_tool_badges_stay_visible_and_duplicate_statuses_are_suppressed(self):
        gw=self.make_gateway();sent=[]
        r=ProgressReporter(sent.append,0,0.02,animation=gw._progress_animation)
        try:
            r.update(ProgressSignal('🧪 Запускаю тесты…','tool:tests:1',('🧪 Тесты выполняются…',)))
            r.report('🧪 Запускаю тесты…')
            self.assertEqual(sent,['🧪 Запускаю тесты…'])
            self.assertGreaterEqual(r.heartbeat_timer.interval,6)
            r.activity_started_at -= 15
            r.heartbeat_timer.cancel()
            r._heartbeat(r.activity_generation)
            self.assertIn('с',sent[-1])
            self.assertTrue(sent[-1].startswith('🧪'))
            self.assertNotIn('🤔',sent[-1])
            r.update(ProgressSignal('✅ Тесты завершены.'))
            r.heartbeat_timer.cancel()
            r._heartbeat(r.activity_generation)
            self.assertIn('Думаю…',sent[-1])
        finally:r.close()

    def test_specific_tool_categories_and_command_privacy(self):
        from guest_gateway import progress_status_for_event
        cases={
            'browser_type':'⌨️ Заполняю форму на странице…',
            'browser_screenshot':'📸 Смотрю, как выглядит страница…',
            'image_generate':'🎨 Создаю изображение…',
            'vision_analyze':'👁 Рассматриваю изображение…',
            'text_to_speech':'🗣 Создаю озвучку…',
            'spreadsheet_read':'📊 Работаю с таблицей…',
        }
        for tool,expected in cases.items():
            result=progress_status_for_event({'event':'tool.started','tool':tool,'preview':'PRIVATE TOKEN'})
            self.assertEqual(result,expected)
            self.assertNotIn('PRIVATE',result)
        self.assertEqual(progress_status_for_event({'event':'tool.started','tool':'exec_command',
            'preview':'pytest /private/client/project'}),'🧪 Запускаю тесты…')

    def test_lightweight_reasoning_is_limited_to_fresh_plain_greetings(self):
        gw=self.make_gateway()
        for text in ('@guest_bot Хэй!', '@guest_bot привет', '@guest_bot hello'):
            message=invocation(30,text=text)
            message['_guest_context']={'mode':'standalone','uses_prior_context':False}
            self.assertTrue(gw._is_standalone_greeting(message))
            for extra in (
                {'reply_to_message':{'text':'old task'}}, {'photo':[{'file_id':'x'}]},
                {'_guest_context':{'mode':'followup','uses_prior_context':True}},
                {'text':'@guest_bot Хэй, исправь ошибку'}, {'text':'@guest_bot убери'},
                {'_guest_recovery_instruction':'continue'},
            ):
                self.assertFalse(gw._is_standalone_greeting({**message,**extra}))

    def test_greeting_override_keeps_native_session_and_same_model(self):
        gw=self.make_gateway();message=invocation(10,text='@guest_bot Хэй')
        message['_guest_context']=gw._context_info(message,10)
        posts=[]
        def http(url,payload=None,**kw):
            if payload is not None:posts.append(payload);return {'run_id':'greeting'}
            return {'status':'completed','output':'Привет!'}
        with patch('guest_gateway.http_json',side_effect=http):
            gw.call_hermes(message)
        self.assertEqual(posts[0]['model_options'],{'reasoning_effort':'low'})
        self.assertEqual(posts[0]['model'],gw.cfg.model)
        self.assertEqual(posts[0]['session_id'],gw._guest_session_id(message))
        self.assertNotIn('conversation_history',posts[0])

    def test_compact_thinking_has_one_cat_and_static_text(self):
        gw=self.make_gateway();gw.cfg.placeholder_custom_emoji_id='5370844436005692355'
        gw.cfg.placeholder_custom_emoji_alt='😼';gw.cfg.progress_cats=True
        self.assertEqual(gw._placeholder_rich_message(),
                         gw._input_rich_message(gw._progress_animation("Думаю…", 0)))
        for frame in range(12):
            text=gw._progress_animation('Думаю…',frame)
            expected=1
            self.assertEqual(text.count('<tg-emoji'),expected)
            rich=gw._input_rich_message(text)
            self.assertEqual(json.dumps(rich).count('5370844436005692355'),expected)
            plain=gw._fit_reply(text,rich=False)
            self.assertNotIn('tg-emoji',plain)
            self.assertEqual(plain.count('😼'),expected)
            self.assertNotIn('\n',plain)
            self.assertNotIn('⏳',plain)
            self.assertEqual(plain, '😼 Думаю…')
        self.assertEqual(gw._progress_animation('🌐 Ищу источники…',3),'🌐 Ищу источники…')

    def test_initial_cat_survives_rich_delivery_fallback(self):
        gw=self.make_gateway();gw.cfg.progress_cats=True
        gw.cfg.placeholder_custom_emoji_id='5370844436005692355'
        gw.cfg.placeholder_custom_emoji_alt='😼'
        payloads=[]
        def send(method,payload,**kwargs):
            payloads.append(json.loads(json.dumps(payload)))
            if len(payloads)==1:
                raise RuntimeError('rich message rejected')
            return {'result':{}}
        gw.tg=send
        gw.answer_guest('q1','Думаю…',purpose='placeholder')
        rich=payloads[0]['result']['input_message_content']['rich_message']
        self.assertEqual(json.dumps(rich).count('5370844436005692355'),1)
        plain=payloads[1]['result']['input_message_content']['message_text']
        self.assertEqual(plain,'😼 Думаю…')

    def test_client_animated_cat_needs_no_heartbeat_edits(self):
        gw=self.make_gateway();sent=[]
        r=ProgressReporter(sent.append,0,0.02,animation=gw._progress_animation,
                           heartbeat_enabled=False)
        try:
            r.update(ProgressSignal('Думаю…'))
            self.assertEqual(len(sent),1)
            self.assertIsNone(r.heartbeat_timer)
            r.report('Думаю…')
            self.assertEqual(len(sent),1)
            r.update(ProgressSignal('🌐 Ищу источники…','tool:web:1',('🌐 Поиск…',)))
            self.assertEqual(sent[-1],'🌐 Ищу источники…')
            self.assertIsNone(r.heartbeat_timer)
        finally:
            r.close()

    def test_slow_status_edit_does_not_block_run_start(self):
        entered=threading.Event();release=threading.Event()
        def send(text):
            entered.set();release.wait(2)
        r=ProgressReporter(send,0,30,background=True)
        try:
            t=time.monotonic()
            r.update(ProgressSignal('Думаю…','phase:planning',('Думаю…',)))
            self.assertLess(time.monotonic()-t,0.2)
            self.assertTrue(entered.wait(1))
        finally:
            release.set();r.close()


    def test_compatibility_additions_run_as_turns_and_preserve_history(self):
        gw = self.make_gateway()
        gw.cfg.hermes_native_sessions = False
        parent = self.admit(gw, 40)
        self.admit(gw, 41, parent.message)
        posts, polls, steers = [], {}, []
        def http(url, payload=None, **kwargs):
            if url.endswith('/steer'):
                steers.append(payload)
                return {'accepted': True}
            if url.endswith('/runs'):
                posts.append(payload)
                return {'run_id': f'r{len(posts)}'}
            polls[url] = polls.get(url, 0) + 1
            if url.endswith('/r1') and polls[url] == 1:
                return {'status': 'running'}
            return {'status': 'completed', 'output': 'done'}
        with patch('guest_gateway.http_json', side_effect=http):
            gw.start_worker()
            try:
                self.wait_idle(gw)
            finally:
                gw.stop_worker()
        self.assertEqual(steers, [])
        self.assertEqual(len(posts), 2)
        self.assertEqual(posts[0]['session_id'], posts[1]['session_id'])
        sid = posts[0]['session_id']
        history = gw.reply_sessions[sid]['messages']
        self.assertIn('question 41', json.dumps(history))
        self.assertEqual([turn['role'] for turn in history], ['user', 'assistant', 'user', 'assistant'])
        self.assertIn('question 40', json.dumps(posts[1]['conversation_history']))


if __name__ == '__main__':
    unittest.main()
