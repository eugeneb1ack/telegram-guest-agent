"""Failure injection for durable attachments, state integrity and nonblocking intake."""
import json
import threading
import unittest
from unittest.mock import Mock, patch

from guest_gateway import GuestGateway, StateLoadError
from test_guest_gateway_queue import FakeGateway, update


FLOOD = RuntimeError('HTTP 429 {"error_code":429,"parameters":{"retry_after":21}}')


class ReliabilityTests(unittest.TestCase):
    def gateway(self):
        g = FakeGateway()
        self.addCleanup(g._tmp_finalizer)
        g.cfg.reactions_enabled = False
        g.cfg.progress_enabled = False
        g.cfg.placeholder_enabled = True
        g.cfg.owner_media_enabled = True
        g.cfg.owner_media_allowed_dirs = (g.tmp_path,)
        g._send_owner_message = Mock()
        return g

    def media_job(self, g, count=2):
        paths = [g.tmp_path / f'picture-{i}.png' for i in range(count)]
        for path in paths:
            path.write_bytes(path.name.encode())
        text = 'Pictures ready.\n' + '\n'.join('MEDIA:' + str(p) for p in paths)
        g.call_hermes = Mock(return_value=text)
        g.handle_guest(update(501, 'media-job', 'create pictures'))
        return g.pending_jobs['update:501'], paths

    @staticmethod
    def drain(g):
        g.running = False
        g._worker_loop()

    def test_corrupt_state_stops_startup_without_overwriting_evidence(self):
        g = self.gateway()
        for raw in [b'{"pending_jobs":', b'[]', b'null', b'\xff',
                    b'{"offset":"12"}', b'{"offset":true}',
                    b'{"pending_jobs":[]}', b'{"pending_jobs":{"bad":[]}}',
                    b'{"pending_jobs":{"bad":{}}}']:
            with self.subTest(raw=raw):
                g.state_path.write_bytes(raw)
                with self.assertRaises(StateLoadError):
                    GuestGateway(g.cfg, g.state_path)
                self.assertEqual(g.state_path.read_bytes(), raw)

    def test_missing_state_is_a_valid_first_start(self):
        g = self.gateway()
        restored = GuestGateway(g.cfg, g.state_path)
        self.assertIsNone(restored.offset)
        self.assertFalse(restored.pending_jobs)

    def test_partial_upload_restarts_without_reuploading_successful_file_or_model(self):
        g = self.gateway()
        job, paths = self.media_job(g)
        uploaded = []
        def upload(method, fields, field, path, **kwargs):
            uploaded.append(path.name)
            if len(uploaded) == 2:
                raise FLOOD
            return {'ok': True, 'result': {'message_id': len(uploaded),
                'photo': [{'file_id': 'file-' + path.stem, 'width': 10, 'height': 10}]}}
        g.tg_upload = upload
        self.drain(g)
        self.assertEqual(job.delivery['phase'], 'retry')
        self.assertEqual([r['status'] for r in job.delivery['uploads']], ['uploaded', 'retry'])
        self.assertFalse([m for m, p in g.calls if m == 'editMessageText'])
        g.call_hermes.assert_called_once()
        restored = GuestGateway(g.cfg, g.state_path)
        restored.call_hermes = Mock(side_effect=AssertionError('must not rerun model'))
        restored.tg_upload = upload
        restored.tg = Mock(return_value={'ok': True, 'result': True})
        with patch('guest_gateway.time.time', return_value=job.delivery['next_at'] + 1):
            restored._schedule_delivery_retries()
        self.drain(restored)
        self.assertEqual(uploaded, [paths[0].name, paths[1].name, paths[1].name])
        self.assertFalse(restored.pending_jobs)
        payload = json.dumps(restored.tg.call_args.args[1])
        self.assertIn('file-picture-0', payload)
        self.assertIn('file-picture-1', payload)
        restored.call_hermes.assert_not_called()

    def test_ambiguous_upload_failure_never_marks_answer_delivered(self):
        for error in [RuntimeError('HTTP 503 upload unavailable'), RuntimeError('timed out')]:
            with self.subTest(error=error):
                g = self.gateway()
                job, _ = self.media_job(g, count=1)
                g.tg_upload = Mock(side_effect=error)
                self.drain(g)
                self.assertIn(job.key, g.pending_jobs)
                self.assertEqual(job.delivery['phase'], 'review')
                self.assertEqual(job.delivery['uploads'][0]['status'], 'review')
                self.assertFalse([m for m, p in g.calls if m == 'editMessageText'])
                restored = GuestGateway(g.cfg, g.state_path)
                self.assertTrue(restored.jobs.empty())

    def test_restart_during_second_upload_keeps_first_receipt_and_holds_uncertain_file(self):
        g = self.gateway()
        job, paths = self.media_job(g)
        calls = []
        def upload(method, fields, field, path, **kwargs):
            calls.append(path.name)
            if len(calls) == 2:
                raise KeyboardInterrupt('simulated process interruption')
            return {'ok': True, 'result': {'photo': [{'file_id': 'saved-first'}]}}
        g.tg_upload = upload
        with self.assertRaises(KeyboardInterrupt):
            self.drain(g)
        restored = GuestGateway(g.cfg, g.state_path)
        delivery = restored.pending_jobs[job.key].delivery
        self.assertEqual(delivery['phase'], 'review')
        self.assertEqual(delivery['uploads'][0]['media']['file_id'], 'saved-first')
        self.assertEqual(delivery['uploads'][1]['status'], 'sending')
        self.assertTrue(restored.jobs.empty())

    def test_changed_file_is_not_uploaded_as_the_saved_result(self):
        g = self.gateway()
        job, paths = self.media_job(g, count=1)
        g.tg_upload = Mock(side_effect=FLOOD)
        self.drain(g)
        paths[0].write_bytes(b'different generated content')
        restored = GuestGateway(g.cfg, g.state_path)
        restored.tg_upload = Mock(side_effect=AssertionError('must not upload changed file'))
        restored._send_owner_message = Mock()
        with patch('guest_gateway.time.time', return_value=job.delivery['next_at'] + 1):
            restored._schedule_delivery_retries()
        self.drain(restored)
        self.assertEqual(restored.pending_jobs[job.key].delivery['phase'], 'review')
        restored.tg_upload.assert_not_called()

    def test_missing_upload_receipt_is_not_silently_dropped(self):
        g = self.gateway()
        job, _ = self.media_job(g, count=1)
        g.tg_upload = Mock(return_value={'ok': True, 'result': {'message_id': 123}})
        self.drain(g)
        self.assertEqual(job.delivery['phase'], 'review')
        self.assertIn('no reusable file_id', job.delivery['error'])

    def test_steering_and_completion_do_not_wait_for_inflight_ack(self):
        g = self.gateway()
        g.cfg.hermes_use_runs = True
        first = update(601, 'parent', 'task')
        g.handle_guest(first)
        parent = g.pending_jobs['update:601']
        parent.run_id = 'active-run'
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        def answer(*args, **kwargs):
            entered.set()
            try:
                if not release.wait(3):
                    raise AssertionError('intake blocked on acknowledgement')
                return 'child-inline'
            finally:
                finished.set()
        g.answer_guest = Mock(side_effect=answer)
        second = update(602, 'child', 'addition')
        second['guest_message']['reply_to_message'] = first['guest_message']
        try:
            g.handle_guest(second)
            self.assertTrue(entered.wait(1))
            self.assertFalse(finished.is_set())
            child = g.pending_jobs['update:602']
            self.assertNotIn(child.key, g.admitting_jobs)
            with patch('guest_gateway.http_json', return_value={'accepted': True}) as http:
                g._drain_steering(parent)
            http.assert_called_once()
            self.assertEqual(child.steer_status, 'accepted')
            g._complete_steered_jobs(parent, failed=False)
            self.assertFalse(g.pending_jobs)
        finally:
            release.set()
            g.notices.join()
        g.answer_guest.assert_called_once()
        self.assertEqual(g.calls[-1][0], 'editMessageText')
        self.assertIn('Учтено в основном ответе', json.dumps(g.calls[-1][1], ensure_ascii=False))

    def test_cosmetic_flood_wait_does_not_sleep_or_delay_steering(self):
        g = self.gateway()
        g.cfg.hermes_use_runs = True
        first = update(701, 'parent', 'task')
        g.handle_guest(first)
        parent = g.pending_jobs['update:701']; parent.run_id = 'active-run'
        second = update(702, 'child', 'addition')
        second['guest_message']['reply_to_message'] = first['guest_message']
        g.telegram_retry_at = 121.5
        g.tg = lambda *a, **kw: GuestGateway.tg(g, *a, **kw)
        with patch('guest_gateway.time.monotonic', return_value=100), patch('guest_gateway.time.sleep') as sleep, \
                patch('guest_gateway.http_json', return_value={'accepted': True}) as http:
            g.handle_guest(second)
            g.notices.join()
            g._drain_steering(parent)
            g.notices.join()
        sleep.assert_not_called()
        http.assert_called_once()
        self.assertTrue(http.call_args.args[0].endswith('/steer'))
        self.assertEqual(g.pending_jobs['update:702'].steer_status, 'accepted')

    def test_completed_notice_reaction_also_skips_flood_wait(self):
        g = self.gateway()
        g.handle_guest(update(801, 'notice', 'task'))
        job = g.pending_jobs['update:801']; job.inline_message_id = 'known-inline'
        g.cfg.reactions_enabled = True
        g.telegram_retry_at = 121.5
        g.tg = lambda *a, **kw: GuestGateway.tg(g, *a, **kw)
        with patch('guest_gateway.time.monotonic', return_value=100), \
                patch('guest_gateway.time.sleep', side_effect=AssertionError('cosmetic wait')) as sleep, \
                patch('guest_gateway.http_json') as http:
            g._queue_notice(job, 'completed', 'Учтено.')
            g.notices.join()
        sleep.assert_not_called()
        http.assert_not_called()


if __name__ == '__main__':
    unittest.main()
