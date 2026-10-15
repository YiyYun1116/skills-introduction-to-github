import asyncio
import os
import tempfile
import threading
import unittest
from unittest.mock import patch

from app import storage
from app.ingestion import Collector


def article(title='Company quarterly results', source='fed_press'):
    return {'id':'record-'+source,'source_id':source,'title':title,'published_at':None,
            'ingested_at':'2026-10-01T00:00:00+00:00','summary':'Original disclosure',
            'url':'https://www.federalreserve.gov/a','is_demo':False}


def fetched(articles=(), error=False):
    return {'articles':list(articles),'results':[{'source_id':'fed_press',
        'status':'error' if error else 'ok' if articles else 'empty',
        'checked_at':storage.now(),'article_count':len(articles),'attempts':1,
        'error':'暂时无法连接' if error else None}]}


class IngestionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {'SIGNAL_ATLAS_DB':self.temp.name+'/test.sqlite3'})
        self.env.start()
        storage.initialize()
        self.collector = Collector()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def collect(self, result):
        with patch('app.ingestion.fetch_all',return_value=result):
            self.assertTrue(self.collector.reserve())
            self.collector.run_reserved(['fed_press'])
        return self.collector.status()

    def test_incremental_counts_ignore_fetch_time_and_keep_first_seen(self):
        first = self.collect(fetched([article()]))
        self.assertEqual(first['last_run_summary']['new_count'],1)
        second = self.collect(fetched([dict(article(),ingested_at='2026-10-02T00:00:00+00:00')]))
        self.assertEqual(second['last_run_summary']['new_count'],0)
        self.assertEqual(second['last_run_summary']['unchanged_count'],1)
        third = self.collect(fetched([article(title='Revised company results')]))
        self.assertEqual(third['last_run_summary']['updated_count'],1)
        self.assertEqual(storage.list_articles()[0]['ingested_at'],'2026-10-01T00:00:00+00:00')
        self.assertEqual(len(third['history']),3)
        # A new process can read the same finished history, without browser activity.
        self.assertEqual(Collector().status()['history'],third['history'])

    def test_partial_failures_keep_articles_and_last_success(self):
        success = self.collect(fetched([article()]))['results'][0]['last_success_at']
        failed = self.collect(fetched(error=True))
        self.assertEqual(failed['results'][0]['last_success_at'],success)
        self.assertEqual(failed['results'][0]['consecutive_failures'],1)
        self.assertEqual(len(storage.list_articles()),1)
        mixed = fetched([article(source='ecb_press')],error=True)
        mixed['results'].append({'source_id':'ecb_press','status':'ok','checked_at':storage.now(),'article_count':1})
        state = self.collect(mixed)
        self.assertEqual(state['last_run_summary']['status'],'partial')
        self.assertEqual(state['results'][0]['consecutive_failures'],2)
        recovered = self.collect(fetched())
        self.assertEqual(recovered['results'][0]['consecutive_failures'],0)
        self.assertEqual(recovered['last_run_summary']['new_count'],0)
        self.assertEqual(len(recovered['results']),2)  # manual sync preserves other sources

    def test_database_failure_releases_lock_and_does_not_expose_exception(self):
        with patch('app.ingestion.storage.get_metadata',side_effect=OSError('private credential')):
            with self.assertLogs('signal_atlas',level='ERROR'):
                self.assertTrue(self.collector.reserve())
                self.collector.run_reserved(['fed_press'])
        self.assertFalse(self.collector.lock.locked())
        self.assertNotIn('private credential',str(self.collector.status()))

    def test_restart_marks_running_history_interrupted_and_retains_data(self):
        storage.save_articles([article()])
        storage.begin_ingestion_run({'id':'old-run','started_at':storage.now(),'status':'running'})
        storage.recover_ingestion_runs()
        self.assertEqual(storage.list_ingestion_runs()[0]['status'],'interrupted')
        self.assertEqual(len(storage.list_articles()),1)

    def test_interruption_between_articles_and_run_rolls_back_both(self):
        with patch('app.ingestion.fetch_all',return_value=fetched([article()])):
            with patch('app.ingestion.storage.finish_ingestion_run',side_effect=SystemExit('simulated process exit')):
                self.assertTrue(self.collector.reserve())
                with self.assertRaises(SystemExit):
                    self.collector.run_reserved(['fed_press'])
        self.assertFalse(self.collector.lock.locked())
        self.assertEqual(storage.list_articles(),[])
        storage.recover_ingestion_runs()
        self.assertEqual(storage.list_ingestion_runs()[0]['status'],'interrupted')
        state = self.collect(fetched([article()]))
        self.assertEqual(state['last_run_summary']['new_count'],1)
        self.assertEqual(sum(run['new_count'] for run in state['history']),1)

    def test_history_is_bounded(self):
        for index in range(105):
            record = {'id':str(index),'started_at':f'2026-10-01T00:00:{index:03d}','status':'ok'}
            storage.finish_ingestion_run(record,{})
        history = storage.list_ingestion_runs(limit=200)
        self.assertEqual(len(history),100)
        self.assertEqual(history[0]['id'],'104')

    def test_scheduled_collection_without_browser_survives_a_failed_round(self):
        async def scenario():
            self.collector.configure(30)
            sleeps = []
            async def advance(seconds):
                sleeps.append(seconds)
                self.assertIsNotNone(self.collector.status()['schedule']['next_run_at'])
                if len(sleeps)==3:
                    raise asyncio.CancelledError()
            with patch('app.ingestion.fetch_all',side_effect=[RuntimeError('private upstream error'),fetched([article()])]):
                with self.assertLogs('signal_atlas',level='ERROR'):
                    with self.assertRaises(asyncio.CancelledError):
                        await self.collector.run_schedule(30,sleep=advance)
            self.assertEqual(sleeps,[3,1800,1800])
            state = self.collector.status()
            self.assertFalse(state['schedule']['scheduler_running'])
            self.assertIsNone(state['schedule']['next_run_at'])
            self.assertEqual(state['last_run_summary']['trigger'],'scheduled')
            self.assertEqual(state['last_run_summary']['new_count'],1)
            self.assertEqual({row['status'] for row in state['history']},{'ok','error'})
            self.assertNotIn('private upstream error',str(state))
        asyncio.run(scenario())

    def test_overlapping_manual_and_scheduled_run_is_not_duplicated(self):
        self.assertTrue(self.collector.reserve())
        self.assertFalse(self.collector.reserve())
        async def scenario():
            calls = 0
            async def advance(_):
                nonlocal calls
                calls += 1
                if calls==2:
                    raise asyncio.CancelledError()
            with patch('app.ingestion.fetch_all') as fetch:
                with self.assertRaises(asyncio.CancelledError):
                    await self.collector.run_schedule(30,sleep=advance)
                fetch.assert_not_called()
        try:
            asyncio.run(scenario())
        finally:
            self.collector.lock.release()

    def test_shutdown_waits_for_inflight_transaction(self):
        started, finish = threading.Event(), threading.Event()
        def request(_):
            started.set()
            finish.wait(timeout=5)
            return fetched([article()])
        async def scenario():
            with patch('app.ingestion.fetch_all',side_effect=request):
                task = asyncio.create_task(self.collector.run_schedule(30,startup_delay=0))
                self.assertTrue(await asyncio.to_thread(started.wait,3))
                task.cancel()
                await asyncio.sleep(0)
                self.assertTrue(self.collector.lock.locked())
                finish.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertFalse(self.collector.lock.locked())
                self.assertEqual(storage.list_ingestion_runs()[0]['status'],'ok')
        asyncio.run(scenario())


if __name__=='__main__':
    unittest.main()
