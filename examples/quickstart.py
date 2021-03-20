import asyncio

import aioredis
from rescheduler import Scheduler, Job, CronTrigger


def tick():
    print("tick")


def tack():
    print("tack")


async def callback(job: Job):
    if job.data['method'] == 'tick':
        tick()

    elif job.data['method'] == 'tack':
        tack()


async def main():
    conn_pool = await aioredis.create_redis_pool(('localhost', 6379), maxsize=20)
    async with Scheduler(conn_pool=conn_pool, job_callback=callback, use_keyspace_notifications=True) as scheduler:
        await scheduler.add_job(
            Job(
                id='tick',
                trigger=CronTrigger.parse(expr='*/10 * * * * *', seconds_ext=True),
                data={'method': 'tick'},
            ),
        )

        await scheduler.add_job(
            Job(
                id='tack',
                trigger=CronTrigger.parse(expr='*/10 * * * * *', seconds_ext=True),
                data={'method': 'tack'},
            ),
            delay=5.0,
        )

        await asyncio.sleep(60)

        await scheduler.cancel_job(job_id='tick')
        await scheduler.cancel_job(job_id='tack')

        print(await scheduler.get_jobs())

    conn_pool.close()
    await conn_pool.wait_closed()


if __name__ == '__main__':
    asyncio.run(main())
