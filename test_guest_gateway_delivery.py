"""Guest-only delivery, durable recovery, rate limits and event reconciliation."""
import io
import json
import threading
import time
import unittest
from unittest.mock import patch, Mock
from guest_gateway import GuestGateway, RunEventState, validate_hermes_url, UploadedMedia
from test_guest_gateway_queue import FakeGateway, update
from test_guest_gateway_rich import FakeGateway as RichGateway, message

FLOOD = RuntimeError('HTTP 429 {"error_code":429,"parameters":{"retry_after":21}}')

class DeliveryTests(unittest.TestCase):
    def gateway(self):
        g=FakeGateway();self.addCleanup(g._tmp_finalizer)
        g.cfg.placeholder_enabled=True;g.cfg.reactions_enabled=False
        g.cfg.progress_enabled=False;g.cfg.owner_media_enabled=False
        return g

    def job(self,g,number=120):
        g.handle_guest(update(number,'q-'+str(number),'task'))
        return g.pending_jobs['update:'+str(number)]

    def drain(self,g):
        g.running=False;g._worker_loop()

    def test_guest_reply_does_not_require_membership_or_send_message(self):
        g=self.gateway();self.job(g);self.drain(g)
        self.assertEqual([m for m,p in g.calls],['answerGuestQuery','editMessageText'])
        self.assertIn('inline_message_id',g.calls[-1][1])
        self.assertIn('rich_message',g.calls[-1][1])
        self.assertFalse(g.pending_jobs)

    def test_failed_delivery_retained_and_replayed_without_model_or_upload(self):
        g=self.gateway();j=self.job(g)
        g.edit_guest_answer=Mock(side_effect=RuntimeError('connection reset'))
        g._send_owner_message=Mock(side_effect=RuntimeError('connection reset'))
        self.drain(g)
        self.assertEqual(j.delivery['phase'],'retry')
        self.assertEqual(len(g.hermes_calls),1)
        restored=GuestGateway(g.cfg,g.state_path)
        self.assertEqual(restored.pending_jobs[j.key].delivery['text'],'final: task')
        restored.call_hermes=Mock(side_effect=AssertionError('must not infer again'))
        restored._send_owner_media_for_reply=Mock(side_effect=AssertionError('must not upload again'))
        restored.tg=Mock(return_value={'ok':True,'result':True})
        self.assertTrue(restored.jobs.empty())
        with patch('guest_gateway.time.time',return_value=j.delivery['next_at']+1):
            restored._schedule_delivery_retries()
        self.drain(restored)
        self.assertFalse(restored.pending_jobs)
        restored.call_hermes.assert_not_called()
        self.assertEqual(restored.tg.call_args.args[0],'editMessageText')
        self.assertNotIn('pending_jobs',json.loads(g.state_path.read_text()))

    def test_retry_enqueue_is_deduplicated(self):
        g=self.gateway();j=self.job(g);self.drain(g)
        g.pending_jobs[j.key]=j;j.delivery={'phase':'retry','next_at':0,'text':'saved'}
        g._schedule_delivery_retries();g._schedule_delivery_retries()
        self.assertEqual(g.jobs.qsize(),1)

    def test_media_file_ids_survive_restart(self):
        g=self.gateway();j=self.job(g);j.inline_message_id='inline-known'
        j.delivery={'phase':'ready','text':'# Picture','media':[{'kind':'photo','file_id':'cached-file'}]}
        g._write_state_locked()
        restored=GuestGateway(g.cfg,g.state_path);restored.tg=Mock(return_value={'ok':True,'result':True})
        restored.call_hermes=Mock(side_effect=AssertionError('no inference'))
        self.drain(restored)
        self.assertIn('cached-file',json.dumps(restored.tg.call_args.args[1]))

    def test_unknown_upload_and_new_send_are_held_after_restart(self):
        for phase,kind in [('staging',None),('sending','chat'),('sending','guest')]:
            with self.subTest(phase=phase,kind=kind):
                g=self.gateway();j=self.job(g);j.inline_message_id='inline-existing'
                j.delivery={'phase':phase,'send_kind':kind,'text':'saved'};g._write_state_locked()
                restored=GuestGateway(g.cfg,g.state_path)
                self.assertEqual(restored.pending_jobs[j.key].delivery['phase'],'review')
                self.assertTrue(restored.jobs.empty())

    def test_ambiguous_new_chat_send_does_not_duplicate_into_inline(self):
        g=self.gateway();g.cfg.final_delivery_mode='new_message_then_edit';j=self.job(g)
        g.send_chat_message=Mock(side_effect=RuntimeError('connection reset'))
        g.edit_guest_answer=Mock();g._send_owner_message=Mock()
        self.drain(g)
        self.assertEqual(j.delivery['phase'],'review')
        g.edit_guest_answer.assert_not_called()
        g._send_owner_message.assert_called_once()

    def test_explicit_not_member_uses_existing_guest_anchor(self):
        g=self.gateway();g.cfg.final_delivery_mode='new_message_then_edit';self.job(g)
        g.send_chat_message=Mock(side_effect=RuntimeError('HTTP 403 bot is not a member'))
        self.drain(g)
        self.assertFalse(g.pending_jobs)
        self.assertEqual(g.calls[-1][0],'editMessageText')

    def test_sent_receipt_cleanup_does_not_resend(self):
        g=self.gateway();j=self.job(g);j.delivery={'phase':'sent','text':'saved'};g._write_state_locked()
        restored=GuestGateway(g.cfg,g.state_path);restored.tg=Mock()
        restored.call_hermes=Mock(side_effect=AssertionError('no inference'))
        self.drain(restored);restored.tg.assert_not_called()
        self.assertFalse(restored.pending_jobs)

    def test_no_plain_downgrade_for_transient_new_message(self):
        for error in [FLOOD,RuntimeError('HTTP 502 Bad Gateway'),RuntimeError('timed out')]:
            g=RichGateway();g.tg=Mock(side_effect=error)
            with self.assertRaises(RuntimeError):g.send_chat_message(message(),'# Rich')
            self.assertEqual(g.tg.call_count,1)
            self.assertEqual(g.tg.call_args.args[0],'sendRichMessage')

    def test_no_plain_downgrade_for_guest_answer_or_owner_fallback(self):
        for route in ['guest','owner']:
            g=RichGateway();g.tg=Mock(side_effect=FLOOD)
            with self.assertRaises(RuntimeError):
                if route=='guest':g.answer_guest('q','# Rich',purpose='placeholder')
                else:g._send_owner_message('# Rich')
            self.assertEqual(g.tg.call_count,1)

    def test_shared_transport_retries_exact_rich_payload_on_429(self):
        g=self.gateway();now=[100.0];calls=[]
        def http(url,payload,**kwargs):
            calls.append((url,payload))
            if len(calls)==1:raise FLOOD
            return {'ok':True,'result':{}}
        with patch('guest_gateway.http_json',side_effect=http),patch('guest_gateway.time.monotonic',side_effect=lambda:now[0]),patch('guest_gateway.time.sleep',side_effect=lambda n:now.__setitem__(0,now[0]+n)):
            GuestGateway.tg(g,'sendRichMessage',{'chat_id':1,'rich_message':{'blocks':[]}})
        self.assertEqual(calls[0],calls[1]);self.assertEqual(now[0],121.5)

    def test_state_noop_does_not_fsync_again(self):
        g=self.gateway()
        with patch('guest_gateway.os.fsync') as sync:
            g._write_state_locked();g._write_state_locked()
        self.assertEqual(sync.call_count,1)
        self.assertEqual(json.loads(g.state_path.read_text())['session_policy_version'],2)

    def test_transport_rechecks_cooldown_extended_by_another_worker(self):
        g=self.gateway();now=[100.0];g.telegram_retry_at=105.0;sleeps=[]
        def sleep(seconds):
            sleeps.append(seconds);now[0]+=seconds
            if len(sleeps)==1:g.telegram_retry_at=110.0
        request=Mock(return_value={'ok':True})
        with patch('guest_gateway.time.monotonic',side_effect=lambda:now[0]),patch('guest_gateway.time.sleep',side_effect=sleep):
            g._telegram_request(request)
        self.assertEqual(sleeps,[5.0,5.0]);self.assertEqual(now[0],110.0)
        request.assert_called_once()

    def test_bad_api_version_is_rejected_before_network(self):
        with self.assertRaisesRegex(ValueError,'HERMES_API_URL'):
            validate_hermes_url('http://localhost:8642/p/guest-agent/v0/chat/completions')
        for suffix in ['/v1','/v1/chat/completions','/p/guest-agent/v1/chat/completions','/api/proxy/v1/chat/completions']:
            validate_hermes_url('http://localhost:8642'+suffix)

    def test_readiness_fails_for_wrong_bot_or_profile(self):
        g=self.gateway();g.cfg.hermes_use_runs=True
        g.tg=Mock(return_value={'result':{'supports_guest_queries':True,'username':'other'}})
        with self.assertRaisesRegex(RuntimeError,'USERNAME'):g.check()
        g.tg=Mock(return_value={'result':{'supports_guest_queries':True,'username':'guest_bot'}})
        with patch('guest_gateway.http_json',side_effect=RuntimeError('HTTP 404 profile')):
            with self.assertRaisesRegex(RuntimeError,'404'):g.check()
        with patch('guest_gateway.http_json',side_effect=[{'status':'ok'},{'data':[{'id':'model'}]}]):g.check()

    def test_generic_completions_preserves_custom_endpoint_without_hermes_routes(self):
        g=self.gateway();g.cfg.hermes_use_runs=False;g.cfg.hermes_url='https://example.test/custom/completions?version=2'
        g.tg=Mock(return_value={'result':{'supports_guest_queries':True,'username':'guest_bot'}})
        with patch('guest_gateway.http_json') as http:
            g.check()
        http.assert_not_called()

    def test_terminal_sse_wakes_final_reconciliation(self):
        g=self.gateway();state=RunEventState()
        response=io.BytesIO(b'data: {"event":"run.completed","output":"not trusted here"}\n\n')
        callback=Mock()
        with patch('guest_gateway.urllib.request.urlopen',return_value=response):
            g._consume_hermes_run_events('http://localhost/v1','run-test',{},callback,state)
        self.assertTrue(state.wake.is_set());self.assertFalse(state.connected.is_set())
        callback.assert_not_called()

    def test_admission_wakes_sse_wait_for_steering(self):
        g=self.gateway();wake=threading.Event();g.run_events['run-test']=wake
        self.job(g)
        self.assertTrue(wake.is_set())

    def test_sse_keeps_reconciliation_but_reduces_poll_count(self):
        g=self.gateway();g.cfg.hermes_poll_interval=.5;clock=[0.0];polls=[]
        state=RunEventState();state.connected.set()
        state.wake.wait=lambda seconds: clock.__setitem__(0,clock[0]+seconds)
        def http(url,payload=None,**kw):
            if url.endswith('/runs'):return {'run_id':'run-test'}
            polls.append(clock[0])
            return {'status':'completed' if clock[0]>=20 else 'running','output':'done'}
        msg=update(500,'q-500','task')['guest_message']
        with patch.object(g,'_start_hermes_run_event_stream',return_value=(state,None)),patch('guest_gateway.http_json',side_effect=http),patch('guest_gateway.time.monotonic',side_effect=lambda:clock[0]):
            self.assertEqual(g._call_hermes_run_once(msg,'test'),'done')
        self.assertEqual(polls,[0,5,10,15,20])

    def test_compaction_preserves_unverified_history_and_routing(self):
        import sqlite3
        from pathlib import Path
        from compact_guest_state import compact
        g=self.gateway();db_path=g.tmp_path/'hermes.db'
        with sqlite3.connect(db_path) as db:
            db.execute('create table messages(id integer,session_id text,role text,content text)')
            db.execute('insert into messages values(1,"verified","user","hello")')
            db.execute('insert into messages values(2,"verified","assistant","hi")')
        state={'offset':12,'context_threads':{'anchor':{'thread_id':'keep'}},'reply_sessions':{
            'verified':{'messages':[{'role':'user','content':'hello'},{'role':'assistant','content':'hi'}]},
            'missing':{'messages':[{'role':'user','content':'not in Hermes'}]}}}
        g.state_path.write_text(json.dumps(state));original=g.state_path.read_bytes()
        result=compact(g.state_path,db_path,apply=True);after=json.loads(g.state_path.read_text())
        self.assertEqual(result['verified_sessions'],1);self.assertEqual(result['retained_sessions'],1)
        self.assertEqual(after['context_threads'],state['context_threads'])
        self.assertEqual(after['reply_sessions']['missing'],state['reply_sessions']['missing'])
        self.assertEqual(after['reply_sessions']['verified']['messages'],[])
        self.assertEqual(Path(result['archive']).read_bytes(),original)

if __name__=='__main__':unittest.main()
