import abc
import dataclasses as dc
import datetime as dt
import time
import uuid
from typing import Optional

import crontools


@dc.dataclass(frozen=True)
class Trigger(abc.ABC):
    """
    Job trigger.
    """

    @abc.abstractmethod
    def next_fire_time(self, job: 'Job', prev: Optional[float] = None, failed: bool = False) -> Optional[float]:
        """
        Returns next job run time.
        """


@dc.dataclass(frozen=True)
class OneShotTrigger(Trigger):
    """
    One shot job trigger.

    :param run_at: timestamp a job to be executed at
    """

    run_at: float

    def next_fire_time(self, job: 'Job', prev: Optional[float] = None, failed: bool = False) -> Optional[float]:
        return None if job.succeeded else self.run_at


@dc.dataclass(frozen=True)
class PeriodicTrigger(Trigger):
    """
    Periodic job trigger.

    :param interval: execution interval
    :param count: number of job executions
    """

    interval: float
    count: Optional[int] = None

    def next_fire_time(self, job: 'Job', prev: Optional[float] = None, failed: bool = False) -> Optional[float]:
        if self.count == job.succeeded:
            return None
        if failed:
            return time.time() + self.interval

        return (prev or time.time()) + self.interval


@dc.dataclass(frozen=True)
class CronTrigger(Trigger):
    """
    Cron job trigger.

    :param crontab: crontab expression
    """

    crontab: crontools.Crontab

    @classmethod
    def parse(cls, *args, **kwargs) -> 'CronTrigger':
        return cls(crontab=crontools.Crontab.parse(*args, **kwargs))

    def next_fire_time(self, job: 'Job', prev: Optional[float] = None, failed: bool = False) -> Optional[float]:
        if prev is not None:
            prev = dt.datetime.fromtimestamp(prev, tz=self.crontab.tz)
        else:
            prev = dt.datetime.now(tz=self.crontab.tz)

        return self.crontab.next_fire_time(now=prev).timestamp()


@dc.dataclass
class Job:
    """
    Scheduler job.

    :param trigger: job trigger
    :param id: job identifier
    :param data: job additional data
    :param succeeded: succeeded execution counter
    :param errors: job error counter
    :param max_errors: max error number the job to be considered as failed
    """

    trigger: Trigger = dc.field(compare=False)
    id: str = dc.field(default_factory=lambda: uuid.uuid4().hex)
    data: Optional[dict] = dc.field(default=None, repr=False, compare=False)
    succeeded: int = dc.field(default=0, repr=True, compare=False)
    errors: int = dc.field(default=0, repr=True, compare=False)
    max_errors: Optional[int] = dc.field(default=None, compare=False)
