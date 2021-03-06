#!/usr/bin/python
# -*- coding: utf-8 -*-
# author:   Jan Hybs
# ----------------------------------------------
import json
import math
import time
import threading

# ----------------------------------------------
from scripts.yamlc.yaml_config import ConfigCase
from scripts.core import monitors
from scripts.core.base import Printer, Paths, Command, DynamicSleep, IO, PathFilters
from scripts.serialization import PyPyResult, ResultHolderResult, RuntestTripletResult, ResultParallelThreads
from utils.counter import ProgressCounter
from utils.events import Event
from utils.globals import wait_for
# ----------------------------------------------


class ProcessState(object):
    NOT_STARTED = 0
    STARTED = 1
    FINISHED = 2


class ExtendedThread(threading.Thread):
    """
    Class ExtendedThread is Thread class with extra functionality added
    """

    def __init__(self, name, target=None):
        super(ExtendedThread, self).__init__(name=name)
        self.started = None
        self.target = target

        self.state = ProcessState.NOT_STARTED
        self._returncode = None

        self.start_time = None
        self.end_time = None

        # create event objects
        self.on_start = Event()
        self.on_complete = Event()
        self.on_update = Event()

    def _run(self):
        if self.target:
            self.target()
            self.returncode = 0

    @property
    def returncode(self):
        return self._returncode

    @property
    def duration(self):
        if self.start_time is None:
            return 0.0
        if self.end_time is None:
            return time.time() - self.start_time

        return self.end_time - self.start_time

    @returncode.setter
    def returncode(self, value):
        self._returncode = value

    def start(self):
        self.state = ProcessState.STARTED
        super(ExtendedThread, self).start()

    def run(self):
        self.state = ProcessState.STARTED
        self.on_start(self)
        self.start_time = time.time()
        self._run()
        self.end_time = time.time()
        self.on_complete(self)
        self.state = ProcessState.FINISHED

    def is_over(self):
        return self.state == ProcessState.FINISHED

    def is_running(self):
        return self.state == ProcessState.STARTED

    def __repr__(self):
        return "<{self.__class__.__name__}:{running} E:{self.returncode}>".format(
            self=self, running="RUNNING" if self.is_alive() else "STOPPED")

    def to_json(self):
        return dict(
            returncode=self.returncode,
            name=self.name
        )

    def __nonzero__(self):
        return not self.with_error()

    def was_successful(self):
        """
        Return True if and only if returncode is 0
        :rtype: bool
        """
        return self.returncode == 0

    def with_error(self):
        """
        Return True if returncode is not 0 and is not None
        :rtype: bool
        """
        return self.returncode not in (0, None)


class BrokenProcess(object):
    """
    Class BrokenProcess Dummy object when process execution fails
    """

    def __init__(self, exception=None):
        self.exception = exception
        self.pid = -1
        self.returncode = 666

    @staticmethod
    def is_running():
        return False


class MultiThreads(ExtendedThread):
    """
    Class MultiThreads is base class when executing threads in group
    :type threads: list[scripts.core.threads.ExtendedThread]
    """

    def __init__(self, name, progress=False):
        super(MultiThreads, self).__init__(name)
        self.threads = list()
        self.returncodes = dict()
        self.running = 0
        self.stop_on_error = False
        self.counter = None
        self.progress = progress
        self.counter = None
        self.index = 0
        self.stopped = False
        self.separate = False

    def run_next(self):
        if self.stopped:
            return False
        if self.index >= self.total:
            return False

        self.index += 1

        if self.counter:
            if self.separate:
                Printer.all.sep()
            self.counter.next(locals())

        self.threads[self.index - 1].start()
        return True

    def add(self, thread):
        """
        :type thread: scripts.core.threads.ExtendedThread
        """
        self.threads.append(thread)
        self.returncodes[thread] = None
        try:
            thread.on_start += self.on_thread_start
            thread.on_complete += self.on_thread_complete
        except: pass

    @property
    def current_thread(self):
        return self.threads[self.index - 1]

    @property
    def returncode(self):
        return max(self.returncodes.values()) if self.returncodes else 0

    @property
    def total(self):
        return len(self.threads)

    def on_thread_start(self, thread):
        """
        :type thread: scripts.core.threads.ExtendedThread
        """
        self.running += 1

    def on_thread_complete(self, thread):
        self.returncodes[thread] = thread.returncode
        self.running -= 1

    # aliases
    __len__ = total
    __iadd__ = add
    append = add


class SequentialThreads(MultiThreads):
    """
    Class SequentialThreads runs multiple threads in sequential fashion
    """

    def __init__(self, name, progress=True, indent=False):
        super(SequentialThreads, self).__init__(name, progress)
        self.thread_name_property = False
        self.indent = indent

    def _run(self):
        if self.progress:
            if self.thread_name_property:
                self.counter = ProgressCounter('{self.name}: {:02d} of {self.total:02d} | {self.current_thread.name}')
            else:
                self.counter = ProgressCounter('{self.name}: {:02d} of {self.total:02d}')

        with Printer.all.with_level(1 if self.indent else 0):
            while True:
                if not self.run_next():
                    break
                self.current_thread.join()

                if self.stop_on_error and self.current_thread.returncode > 0:
                    self.stopped = True
                    break

    def to_json(self):
        items = []
        for thread in self.threads:
            items.append(thread)

        return dict(
            returncode=self.returncode,
            name=self.name,
            items=items
        )


class ParallelThreads(MultiThreads):
    """
    Class ParallelThreads run multiple threads in parallel fashion
    """

    def __init__(self, n=4, name='runner', progress=True):
        super(ParallelThreads, self).__init__(name, progress)
        self.n = n if type(n) is int else 1
        self.counter = ProgressCounter('Case {:02d} of {self.total:02d}')
        self.stop_on_error = True
        self.separate = True

    def ensure_run_count(self):
        if self.stopped:
            return False
        if self.index >= self.total:
            return False

        if self.running < self.n:
            self.run_next()
        return True

    def _run(self):
        # determine how many parallel batches will be
        # later on take cpu into account
        steps = int(math.ceil(float(self.total) / self.n))

        # run each batched
        for i in range(steps):
            batch = [x for x in range(i * self.n, i * self.n + self.n) if x < len(self.threads)]
            for t in batch:
                self.run_next()

            for t in batch:
                self.threads[t].join()

            if self.stop_on_error and self.returncode > 0:
                self.stopped = True
                # no need to stop processes, just break
                break

    def dump(self):
        return ResultParallelThreads(self)


class PyPy(ExtendedThread):
    """
    Class PyPy is main class which executes command having multiple monitors registered
    PyPy = BinExecutor + monitors

    :type executor : scripts.core.execution.BinExecutor
    :type case     : ConfigCase
    """

    returncode_map = {
        '0': 'SUCCESS',
        '1': 'ERROR',
        '5': 'TERM',
        'None': 'SKIP',
        '-1': 'SKIP',
    }

    def __init__(self, executor, progress=False, period=.5):
        super(PyPy, self).__init__(name='pypy')
        self.executor = executor
        self.period = period
        self.case = None
        self._progress = None
        self.status_file = None

        self.on_process_start = Event()
        self.on_process_complete = Event()
        self.on_process_update = Event()

        self.on_process_complete += self.generate_status_file

        # register monitors
        self.limit_monitor = monitors.LimitMonitor(self)
        self.start_monitor = monitors.StartInfoMonitor(self)
        self.end_monitor = monitors.EndInfoMonitor(self)
        self.progress_monitor = monitors.ProgressMonitor(self)
        self.output_monitor = monitors.OutputMonitor(self)
        self.error_monitor = monitors.ErrorMonitor(self)

        self.end_monitor.deactivate()

        self.log = False
        self.custom_error = None

        # different settings in batched mode
        self.progress = progress

        # dynamic sleeper
        self.sleeper = DynamicSleep()

        # path to full output
        self.full_output = None

    @property
    def escaped_command(self):
        return self.executor.escaped_command

    @property
    def progress(self):
        return self._progress

    @progress.setter
    def progress(self, value):
        self._progress = value

    def _run(self):
        # start executor
        self.executor.start()
        wait_for(self.executor, 'process')

        if self.executor.broken:
            Printer.all.err('Could not start command "{}": {}',
                            Command.to_string(self.executor.command),
                            self.executor.exception)
            self.returncode = self.executor.returncode

        # if process is not broken, propagate start event
        self.on_process_start(self)

        while self.executor.is_running():
            self.on_process_update(self)
            self.sleeper.sleep()

        # get return code
        rc = getattr(self.executor, 'returncode', None)
        self.returncode = rc if self.custom_error is None or str(rc) == "0" else self.custom_error

        # propagate on_complete event
        self.on_process_complete(self)

    def status(self):
        import getpass
        import platform
        result = dict(
            returncode=self.returncode,
            duration=self.duration,
            username=getpass.getuser(),
            hostname=platform.node(),
            nodename=platform.node().split('.')[0].strip('0123456789')
        )

        if self.case:
            result.update(self.case.info)
        return result

    def to_json(self):
        if self.case:
            return dict(
                returncode=self.returncode,
                name=self.case.as_string,
                case=self.case,
                log=self.full_output
            )
        json = super(PyPy, self).to_json()
        json['log'] = self.full_output
        json['type'] = 'exec'
        return json

    def dump(self):
        return PyPyResult(self)

    @classmethod
    def generate_status_file(cls, target):
        """
        Will generate status file if target has option turned on
        :type target: PyPy
        """
        if target.status_file:
            IO.write(
                target.status_file,
                json.dumps(target.status(), indent=4)
            )
            output_dir = Paths.dirname(target.status_file)
            files = Paths.browse(
                output_dir,
                [PathFilters.filter_wildcards('*/profiler_info_*.log.json')]
            )
            # profiler json is missing?
            if not files:
                IO.write(Paths.join(output_dir, 'profiler_info_dummy.log.json'), '{}')


class ComparisonMultiThread(SequentialThreads):
    """
    Class ComparisonMultiThread hold comparison results and writes them to a file
    """

    def __init__(self, output, name='Comparison', progress=True, indent=True):
        super(ComparisonMultiThread, self).__init__(name, progress, indent)
        self.output = output

    def on_thread_complete(self, thread):
        """
        :type thread: PyPy
        """
        super(ComparisonMultiThread, self).on_thread_complete(thread)

        # append ndiff to file
        with open(self.output, 'a+') as fp:
            fp.write('-' * 60 + '\n')
            fp.write(thread.name + '\n')
            fp.write('-' * 60 + '\n')
            fp.write(thread.executor.output.read())
            fp.write('\n' * 3)

    def to_json(self):
        items = []
        for thread in self.threads:
            items.append(dict(
                name=thread.name,
                returncode=thread.returncode,
                log=self.output,
            ))

        return dict(
            returncode=self.returncode,
            name=self.name,
            log=self.output,
            tests=items,
        )


class RuntestMultiThread(SequentialThreads):
    """
    Class RuntestMultiThread is simple triplet holding single ConfigCase results
    (CleanThread, PyPy, ComparisonMultiThread)
    :type clean  : scripts.prescriptions.local_run.CleanThread
    :type pypy   : PyPy
    :type comp   : ComparisonMultiThread
    """

    def __init__(self, clean, pypy, comp):
        super(RuntestMultiThread, self).__init__('test-case', progress=False, indent=False)
        self.clean = clean
        self.pypy = pypy
        self.comp = comp

        self.add(clean)
        self.add(pypy)
        self.add(comp)

    def to_json(self):
        return dict(
            type="test-case",
            returncode=self.returncode,
            clean=self.clean,
            execution=self.pypy,
            compare=self.comp
        )

    def dump(self):
        return RuntestTripletResult(self)


class ResultHolder(object):
    """
    Class ResultHolder stores object with property returncode
    """

    def __init__(self):
        self.items = list()

    def add(self, item):
        self.items.append(item)

    @property
    def returncode(self):
        rc = [None]
        for i in self.items:
            rc.append(i.returncode)
        return max(rc)

    def singlify(self):
        return self.items[0] if len(self.items) == 1 else self

    def dump(self):
        return ResultHolderResult(self)