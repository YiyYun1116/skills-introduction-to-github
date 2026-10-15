"""Single-instance background collection, independent of open browser tabs."""
import asyncio
import logging
import threading
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from . import storage
from .sources import SOURCE_CATALOG, fetch_all

log = logging.getLogger('signal_atlas')


class Collector:
    def __init__(self):
        self.lock = threading.Lock()
        self.state_lock = threading.Lock()
        self.schedule = {'enabled':False,'interval_minutes':0,'next_run_at':None,'scheduler_running':False}
        self.last_error = None

    def configure(self, minutes):
        with self.state_lock:
            self.schedule = {'enabled':bool(minutes),'interval_minutes':minutes,
                             'next_run_at':None,'scheduler_running':False}
            self.last_error = None

    def reserve(self):
        return self.lock.acquire(blocking=False)

    def status(self):
        result = storage.get_metadata('ingestion', {'last_run':None,'results':[]})
        result['running'] = self.lock.locked()
        result['history'] = storage.list_ingestion_runs()
        with self.state_lock:
            result['schedule'] = dict(self.schedule)
            if self.last_error:
                result['error'] = self.last_error
        return result

    def run_reserved(self, source_ids=None, trigger='manual'):
        """Called only after reserve(); always release even if storage is unavailable."""
        selected = list(dict.fromkeys(source_ids)) if source_ids is not None else [
            source['id'] for source in SOURCE_CATALOG if source['enabled']]
        record = {'id':uuid4().hex,'trigger':trigger,'source_ids':selected,'source_count':len(selected),
                  'started_at':storage.now(),'finished_at':None,'status':'running',
                  'new_count':0,'updated_count':0,'unchanged_count':0,
                  'successful_sources':0,'failed_sources':0}
        previous = {}
        try:
            previous = storage.get_metadata('ingestion', {})
            storage.begin_ingestion_run(record)
            result = fetch_all(selected)
            with storage.connection() as db:
                counts = storage.save_articles(result['articles'], db=db)
                merged = {row['source_id']:row for row in previous.get('results',[])}
                last_results = []
                for row in result['results']:
                    old = merged.get(row['source_id'], {})
                    success = row['status'] in {'ok','empty'}
                    updated = dict(row,
                        last_success_at=row['checked_at'] if success else old.get('last_success_at'),
                        consecutive_failures=0 if success else old.get('consecutive_failures',0)+1,
                        **counts['by_source'].get(row['source_id'], {'new_count':0,'updated_count':0,'unchanged_count':0}))
                    # Upgrade historical source states without losing a known success.
                    if not success and not updated['last_success_at'] and old.get('status') in {'ok','empty'}:
                        updated['last_success_at'] = old.get('checked_at')
                    merged[row['source_id']] = updated
                    last_results.append(updated)
                successful = sum(row['status'] in {'ok','empty'} for row in last_results)
                failed = len(last_results) - successful
                record.update({key:counts[key] for key in ('new_count','updated_count','unchanged_count')})
                record.update(finished_at=storage.now(),successful_sources=successful,failed_sources=failed,
                              status='partial' if failed and successful else 'error' if failed else 'ok')
                storage.finish_ingestion_run(record, {'last_run':record['finished_at'],
                    'last_run_summary':record,'results':list(merged.values()),'last_run_results':last_results}, db=db)
            with self.state_lock:
                self.last_error = None
        except Exception:
            log.exception('Ingestion failed')
            error = '采集未完成；已有数据已保留，后续轮次将继续尝试。'
            record.update(status='error',finished_at=storage.now(),error=error,
                          new_count=0,updated_count=0,unchanged_count=0,successful_sources=0)
            with self.state_lock:
                self.last_error = error
            try:
                storage.finish_ingestion_run(record, {'last_run':record['finished_at'],
                    'last_run_summary':record,'results':previous.get('results',[]),'last_run_results':[],
                    'error':error})
            except Exception:
                log.exception('Could not persist ingestion failure')
        finally:
            self.lock.release()

    async def run_schedule(self, minutes, sleep=asyncio.sleep, startup_delay=3):
        delay = startup_delay
        with self.state_lock:
            self.schedule['scheduler_running'] = True
        try:
            while True:
                with self.state_lock:
                    self.schedule['next_run_at'] = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat()
                await sleep(delay)
                with self.state_lock:
                    self.schedule['next_run_at'] = None
                if self.reserve():
                    worker = asyncio.create_task(asyncio.to_thread(self.run_reserved, None, 'scheduled'))
                    try:
                        await asyncio.shield(worker)
                    except asyncio.CancelledError:
                        # A Python thread cannot be cancelled safely mid-transaction.
                        await worker
                        raise
                    delay = minutes * 60
                else:
                    # A manual run already covers this interval; check again after
                    # the interval instead of piling up additional collection work.
                    delay = minutes * 60
        finally:
            with self.state_lock:
                self.schedule['scheduler_running'] = False
                self.schedule['next_run_at'] = None


collector = Collector()
