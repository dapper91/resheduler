import asyncio
import contextlib
import logging
import pickle
import time
import uuid
from typing import Callable, Dict, List, Optional, Type

import aiojobs
import aioredis
import async_lru

from .job import Job

logger = logging.getLogger(__package__)


class TimelineIsEmptyError(aioredis.ReplyError):
    """
    Timeline is empty.
    """

    MATCH_REPLY = 'TIMELINE EMPTY'


class JobDeletedError(aioredis.ReplyError):
    """
    Job has been already deleted.
    """

    MATCH_REPLY = 'JOB DELETED'


class ScriptNotFoundError(aioredis.ReplyError):
    """
    Script not found in redis cache.
    """

    MATCH_REPLY = 'NOSCRIPT'


class Scheduler:
    """
    Redis job scheduler.

    :param conn_pool: redis connection pool
    :param job_callback: callback to be called to execute a job
    :param sid: scheduler identifier (random uuid is used if omitted)
    :param retry_interval: service retry interval
    :param heartbeat_interval: heartbeat sending interval
    :param polling_interval: timeline polling interval
    :param dead_interval: missing heartbeat time interval after which a scheduler to be considered as dead
    :param worker_limit: workers number limit
    :param use_keyspace_notifications: whether to use redis keyspace notifications for more precise job scheduling
    :param job_dumper: job serializer
    :param job_loader: job deserializer
    :param heartbeats_key: redis key to be used as heartbeats storage
    :param stash_key: redis key to be used as worker job stash
    :param timeline_key: redis key to be used as timeline
    :param jobs_key: redis key to be used as job storage
    :param failed_key: redis key to be used as failed job storage
    """

    def __init__(
            self,
            conn_pool: aioredis.ConnectionsPool,
            job_callback: Callable,
            sid: Optional[str] = None,
            use_keyspace_notifications: bool = False,
            retry_interval: float = 5.0,
            heartbeat_interval: float = 5.0,
            polling_interval: float = 2.0,
            dead_interval: float = 10.0,
            worker_limit: int = 100,
            graceful_shutdown_timeout: float = 30,
            job_dumper=pickle.dumps,
            job_loader=pickle.loads,
            heartbeats_key='heartbeats',
            stash_key='stash',
            timeline_key='timeline',
            jobs_key='jobs',
            failed_key='failed',
    ):
        self._conn_pool = conn_pool
        self._job_callback = job_callback
        self._sid = sid or str(uuid.uuid4())
        self._use_keyspace_notifications = use_keyspace_notifications

        self._worker_limit = worker_limit
        self._retry_interval = retry_interval
        self._heartbeat_interval = heartbeat_interval
        self._polling_interval = polling_interval
        self._dead_interval = dead_interval
        self._graceful_shutdown_timeout = graceful_shutdown_timeout

        self._job_dumper = job_dumper
        self._job_loader = job_loader

        self._heartbeats_key = heartbeats_key
        self._stash_key = stash_key
        self._timeline_key = timeline_key
        self._jobs_key = jobs_key
        self._failed_key = failed_key

        self._timeline_event = asyncio.Event()
        self._stopped_event = asyncio.Event()
        self._services: List[asyncio.Task] = []
        self._executor: Optional[aiojobs.Scheduler] = None
        self._job_counter = 0

    async def __aenter__(self) -> 'Scheduler':
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.stop()

    async def start(self) -> None:
        """
        Starts scheduler.
        """

        logger.info("starting scheduler '%s'", self._sid)

        await self._cleanup_self()

        self._executor = await aiojobs.create_scheduler(
            close_timeout=self._graceful_shutdown_timeout,
            limit=self._worker_limit,
            pending_limit=self._worker_limit,
        )

        self._services.append(self._start_heartbeat_sender())

        if self._use_keyspace_notifications:
            self._services.append(self._start_notification_watcher())

        self._services.append(self._start_timeline_processor())

    async def stop(self, wait: float = 0) -> None:
        """
        Stops scheduler.

        :param wait: graceful shutdown timeout. If zero - default timeout is used
        """

        logger.info("stopping scheduler...")

        self._stopped_event.set()
        if wait:
            done, pending = await asyncio.wait(self._services, timeout=wait or self._graceful_shutdown_timeout)
            for service in pending:
                logger.warning("service is not responding. Cancelling...")
                service.cancel()

        await self._cleanup_self()
        await self._executor.close()

    async def add_job(self, job: Job, delay: float = 0.0) -> None:
        """
        Adds a new job to scheduler.

        :param job: job to be added
        :param delay: first execution delay
        """

        run_at = time.time() + delay

        with await self._conn_pool as conn:
            tx = conn.multi_exec()
            tx.zadd(self._timeline_key, run_at, job.id)
            tx.hset(self._jobs_key, job.id, self._job_dumper(job))
            await tx.execute()

        logger.debug("job added: job=%r", job)

    async def cancel_job(self, job_id: str) -> None:
        """
        Cancels a scheduled job.

        :param job_id: job identifier
        """

        with await self._conn_pool as conn:
            tx = conn.multi_exec()
            tx.hdel(self._jobs_key, job_id)
            tx.zrem(self._timeline_key, job_id)
            await tx.execute()

        logger.debug("job canceled: id=%s", job_id)

    async def get_job(self, job_id: str) -> Job:
        """
        Returns a scheduler job

        :param job_id: job identifier
        :return: job instance
        """

        with await self._conn_pool as conn:
            job_data = await conn.hget(self._jobs_key, job_id)
            if job_data is None:
                raise KeyError("job '%s' not found", job_id)

        return self._job_loader(job_data)

    async def get_jobs(self) -> Dict[str, Job]:
        """
        Returns all scheduler jobs.

        :return: job map
        """

        with await self._conn_pool as conn:
            jobs = await conn.hgetall(self._jobs_key)

        return {job_id: self._job_loader(job_data) for job_id, job_data in jobs.items()}

    async def retry_failed_job(self, job_id: str, delay: float = 0) -> None:
        """
        Returns failed job back to the timeline.

        :param job_id: job identifier
        :param delay: first execution delay
        """

        job = await self.get_job(job_id)
        job.errors = 0
        await self._update_job(job)

        run_at = time.time() + delay

        with await self._conn_pool as conn:
            tx = conn.multi_exec()
            tx.zadd(self._timeline_key, run_at, job_id)
            tx.srem(self._failed_key, job_id)
            await tx.execute()

    async def _guard(self, func: Callable, *args, exception: Type[Exception] = Exception, **kwargs):
        while True:
            try:
                await func(*args, **kwargs)
            except exception as e:
                logger.exception("guard exception caught. Backing off...", e)
                await self._wait(timeout=self._retry_interval)
            else:
                break

    def _start_heartbeat_sender(self) -> asyncio.Task:
        heartbeat_sender = asyncio.create_task(self._guard(self._send_heartbeat))
        logger.info("heartbeat sender started")

        return heartbeat_sender

    async def _send_heartbeat(self) -> None:
        with contextlib.suppress(asyncio.CancelledError):
            while not self._stopped_event.is_set():
                with await self._conn_pool as conn:
                    await conn.hset(self._heartbeats_key, self._sid, time.time())

                    heartbeats = await conn.hgetall(self._heartbeats_key, encoding='utf8')
                    for sid, last_ping in heartbeats.items():
                        last_ping = float(last_ping)
                        delay = time.time() - last_ping

                        if delay > self._dead_interval:
                            logger.warning("scheduler '%s' is dead: last ping %f sec ago", sid, delay)
                            await self._cleanup(conn, sid=sid)

                await self._wait(timeout=self._heartbeat_interval)

        logger.debug("heartbeat sender stopped")

    def _start_notification_watcher(self) -> asyncio.Task:
        watcher = asyncio.create_task(self._guard(self._watch_notification))
        logger.info("notification watcher started")

        return watcher

    async def _watch_notification(self) -> None:
        with contextlib.suppress(asyncio.CancelledError):
            while not self._stopped_event.is_set():
                with await self._conn_pool as conn:
                    channel, *_ = await conn.psubscribe(f'__keyspace@{self._conn_pool.db}__:timeline')
                    try:
                        await self._wait(channel.wait_message())
                        logger.debug("keyspace event received")
                        self._timeline_event.set()
                    finally:
                        if not conn.closed:
                            await conn.punsubscribe(f'__keyspace@{self._conn_pool.db}__:timeline')

        logger.debug("notification watcher stopped")

    def _start_timeline_processor(self) -> asyncio.Task:
        watcher = asyncio.create_task(self._guard(self._handle_timeline))
        logger.info("timeline processor started")

        return watcher

    async def _handle_timeline(self):
        with contextlib.suppress(asyncio.CancelledError):
            while not self._stopped_event.is_set():
                await self._wait_notification(await self._process_timeline_item())

        logger.debug("timeline processor stopped")

    async def _wait_notification(self, timeout: Optional[float] = None):
        logger.debug("waiting for notification...")

        if self._use_keyspace_notifications:
            try:
                await self._wait(self._timeline_event.wait(), timeout=timeout)
                logger.debug("keyspace notification received")

            except asyncio.TimeoutError:
                pass
            finally:
                self._timeline_event.clear()
        else:
            await self._wait(timeout=min(float('inf') if timeout is None else timeout, self._polling_interval))

    async def _process_timeline_item(self) -> Optional[float]:
        logger.debug("processing timeline...")

        script_hash = await self._load_timeline_processing_script()
        worker_id = self._get_worker_id()

        try:
            with await self._conn_pool as conn:
                backoff_timeout, job = await conn.evalsha(
                    script_hash,
                    keys=[self._timeline_key, self._jobs_key, self._stash_key, worker_id],
                    args=[time.time()],
                )
                backoff_timeout = float(backoff_timeout)
                if backoff_timeout > 0.0:
                    return backoff_timeout

        except ScriptNotFoundError:
            logger.warning('script not found')
            self._load_timeline_processing_script.cache_clear()
            return 0.0

        except TimelineIsEmptyError:
            logger.debug('timeline is empty')
            return None

        except JobDeletedError:
            logger.debug('job already deleted')
            return 0.0

        job = self._job_loader(job)

        logger.debug("spawning job: id=%s", job.id)
        await self._executor.spawn(self._run_job(worker_id, job))

        return 0.0

    def _get_worker_id(self) -> str:
        self._job_counter = (self._job_counter + 1) % (2**32)

        return f'{self._sid}#{self._job_counter}'

    async def _run_job(self, worker_id: str, job: Job) -> None:
        started_at = time.time()

        try:
            await self._job_callback(job)
        except Exception as e:
            logger.exception("job execution error: %s", e)

            job.errors += 1
            await self._update_job(job)

            if job.max_errors is not None and job.errors >= job.max_errors:
                logger.warning("job error limit exceeded: id=%s", job.id)
                await self._fail_stashed_job(worker_id, job)
            else:
                run_at = job.trigger.next_fire_time(job, started_at, failed=True)
                await self._release_stashed_job(worker_id, job, run_at)

        else:
            logger.debug("job complete: id=%s", job.id)

            job.succeeded += 1
            await self._update_job(job)

            run_at = job.trigger.next_fire_time(job, started_at)
            if run_at is None:
                logger.debug("job finished: id=%s", job.id)
                await self._remove_stashed_job(worker_id, job)

            else:
                logger.debug("releasing job: id=%s, run_at=%s", job.id, run_at)
                await self._release_stashed_job(worker_id, job, run_at)

    async def _fail_stashed_job(self, worker_id: str, job: Job) -> None:
        with await self._conn_pool as conn:
            tx = conn.multi_exec()
            tx.sadd(self._failed_key, job.id)
            tx.hdel(self._stash_key, worker_id)
            await tx.execute()

    async def _release_stashed_job(self, worker_id: str, job: Job, run_at: float) -> None:
        with await self._conn_pool as conn:
            tx = conn.multi_exec()
            tx.zadd(self._timeline_key, run_at, job.id)
            tx.hdel(self._stash_key, worker_id)
            await tx.execute()

    async def _remove_stashed_job(self, worker_id: str, job: Job) -> None:
        with await self._conn_pool as conn:
            tx = conn.multi_exec()
            tx.hdel(self._stash_key, worker_id)
            tx.hdel(self._jobs_key, job.id)
            await tx.execute()

    async def _update_job(self, job: Job):
        script = '''
            if redis.call('HEXISTS', KEYS[1], ARGV[1]) then
                redis.call('HSET', KEYS[1], ARGV[1], ARGV[2])
            end
        '''

        with await self._conn_pool as conn:
            await conn.eval(
                script,
                keys=[self._jobs_key],
                args=[job.id, self._job_dumper(job)],
            )

        logger.debug("job updated: job=%r", job)

    @async_lru.alru_cache()
    async def _load_timeline_processing_script(self) -> str:
        logger.debug("loading timeline processing script...")

        with await self._conn_pool as conn:
            return await conn.script_load("""
                local timeline_key = KEYS[1]
                local jobs_key = KEYS[2]
                local stash_key = KEYS[3]
                local worker_id = KEYS[4]

                local now = tonumber(ARGV[1])

                local items = redis.call('ZRANGE', timeline_key, 0, 0, 'WITHSCORES')
                if #items == 0 then
                    return redis.error_reply('TIMELINE EMPTY')
                end

                local job_id = items[1]
                local run_at = tonumber(items[2])

                local job = redis.call('HGET', jobs_key, job_id)
                if not job then
                    redis.call('ZREM', timeline_key, job_id)
                    return redis.error_reply('JOB DELETED')
                end

                if now < run_at then
                    return {tostring(run_at - now), '{}'}
                end

                redis.call('HSET', stash_key, worker_id, job_id .. ',' .. run_at)
                redis.call('ZPOPMIN', timeline_key, 1)

                return {tostring(0.0), job}
            """)

    async def _cleanup_self(self) -> None:
        with await self._conn_pool as conn:
            await self._cleanup(conn, self._sid)

    async def _cleanup(self, conn: aioredis.Redis, sid: str) -> None:
        logger.debug("cleaning up scheduler data: sid=%s", sid)

        async for worker_id, job_data in conn.ihscan(self._stash_key, match=f'{sid}#*'):
            job_id, run_at = job_data.decode().split(',')

            logger.debug("releasing dead job: sid=%s, id=%s, run_at=%s", sid, job_id, run_at)

            with await self._conn_pool as conn:
                tx = conn.multi_exec()
                tx.zadd(self._timeline_key, float(run_at), job_id)
                tx.hdel(self._stash_key, worker_id)
                await tx.execute()

        await conn.hdel(self._heartbeats_key, field=sid)

    async def _wait(self, *aws, timeout: Optional[float] = None):
        stopped = False

        wait_stop_event = asyncio.create_task(self._stopped_event.wait())
        done, pending = await asyncio.wait({wait_stop_event, *aws}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        for aw in done:
            if aw is wait_stop_event:
                stopped = True
                await aw

        if stopped:
            raise asyncio.CancelledError
