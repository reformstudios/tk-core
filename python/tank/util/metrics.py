# Copyright (c) 2016 Shotgun Software Inc.
#
# CONFIDENTIAL AND PROPRIETARY
#
# This work is provided "AS IS" and subject to the Shotgun Pipeline Toolkit
# Source Code License included in this distribution package. See LICENSE.
# By accessing, using, copying or modifying this work you indicate your
# agreement to the Shotgun Pipeline Toolkit Source Code License. All rights
# not expressly granted therein are reserved by Shotgun Software Inc.

"""Classes and functions for logging Toolkit metrics.

Internal Use Only - We provide no guarantees that the classes and functions
here will be backwards compatible. These objects are also subject to change and
are not part of the public Sgtk API.

"""


###############################################################################
# imports

from collections import deque
from threading import Event, Thread, Lock
import urllib2
 
from . import constants

# use api json to cover py 2.5
from tank_vendor import shotgun_api3
json = shotgun_api3.shotgun.json


###############################################################################
# Metrics Queue, Dispatcher, and worker thread classes

class MetricsQueueSingleton(object):
    """A FIFO queue for logging metrics.

    This is a singleton class, so any instantiation will return the same object
    instance within the current process.

    """

    # keeps track of the single instance of the class
    __instance = None

    # A set of log identifier strings used to check whether a metric has been
    # logged already.
    __logged_metrics = set()

    def __new__(cls, *args, **kwargs):
        """Ensures only one instance of the metrics queue exists."""

        # create the queue instance if it hasn't been created already
        if not cls.__instance:

            # remember the instance so that no more are created
            metrics_queue = super(MetricsQueueSingleton, cls).__new__(
                cls, *args, **kwargs)

            metrics_queue._lock = Lock()

            # The underlying collections.deque instance
            metrics_queue._queue = deque()

            cls.__instance = metrics_queue

        return cls.__instance

    def log(self, metric, log_once=False):
        """Add the metric to the queue for dispatching.

        If ``log_once`` is set to ``True``, this will only log the metric if it
        is the first attempt to log it.

        :param EventMetric metric: The metric to log.
        :param bool log_once: ``True`` if this metric should be ignored if it
            has already been logged. ``False`` otherwise. Defaults to ``False``.
        """

        # This assumes that supplied object's classes implement __repr__
        # to return consistent results when building objects with the same
        # internal data. See the UserActivityMetric and UserAttributeMetric
        # classes below.
        metric_identifier = repr(metric)

        if log_once and metric_identifier in self.__logged_metrics:
            # the metric is already logged! nothing to do.
            return

        self._lock.acquire()
        try:
            self._queue.append(metric)

            # remember that we've logged this one already
            self.__logged_metrics.add(metric_identifier)
        except:
            pass
        finally:
            self._lock.release()

    def get_metrics(self, count=None):
        """Return `count` metrics.

        :param int count: The number of pending metrics to return.

        If `count` is not supplied, or greater than the number of pending
        metrics, returns all metrics.

        Should never raise an exception.

        """

        metrics = []

        self._lock.acquire()
        try:
            num_pending = len(self._queue)

            # there are pending metrics
            if num_pending:

                # determine how many metrics to retrieve
                if not count or count > num_pending:
                    count = num_pending

                # would be nice to be able to pop N from deque. oh well.
                metrics = [self._queue.popleft() for i in range(0, count)]
        except:
            pass
        finally:
            self._lock.release()

        return metrics


class MetricsDispatcher(object):
    """This class manages 1 or more worker threads dispatching toolkit metrics.

    After initializing the object, the `start()` method is called to
    spin up worker threads for dispatching logged metrics. The `stop()` method
    is later called to stop the worker threads.

    """

    def __init__(self, engine, num_workers=1):
        """Initialize the dispatcher object.

        :param engine: An engine instance for logging, and api access
        :param workers: The number of worker threads to start.

        """

        self._engine = engine
        self._num_workers = num_workers
        self._workers = []
        self._dispatching = False

    def start(self):
        """Starts up the workers for dispatching logged metrics.

        If called on an already dispatching instance, then result is a no-op.

        """

        if self._dispatching:
            self._engine.log_debug(
                "Metrics dispatching already started. Doing nothing.")
            return

        # Now check that we have a valid authenticated user, which is
        # required for metrics dispatch. This is to ensure certain legacy
        # and edge case scenarios work, for example the 
        # shotgun_cache_actions tank command which runs un-authenticated.
        from ..api import get_authenticated_user
        if not get_authenticated_user():
            return

        # start the dispatch workers to use this queue
        for i in range(self._num_workers):
            worker = MetricsDispatchWorkerThread(self._engine)
            worker.start()
            self._engine.log_debug("Added worker thread: %s" % (worker,))
            self._workers.append(worker)

        self._dispatching = True

    def stop(self):
        """Instructs all worker threads to stop processing metrics."""
        for worker in self.workers:
            worker.halt()

        self._dispatching = False
        self._workers = []

    @property
    def dispatching(self):
        """True if started and dispatching metrics."""
        return self._dispatching

    @property
    def workers(self):
        """A list of workers threads dispatching metrics from the queue."""
        return self._workers


class MetricsDispatchWorkerThread(Thread):
    """Worker thread for dispatching metrics to sg logging endpoint.

    Once started this worker will dispatch logged metrics to the shotgun api
    endpoint. The worker retrieves any pending metrics after the
    `DISPATCH_INTERVAL` and sends them all in a single request to sg.

    In the case metrics dispatch isn't supported by the shotgun server,
    the worker thread will exit early.
    """

    API_ENDPOINT = "api3/track_metrics/"

    DISPATCH_INTERVAL = 5
    """Worker will wait this long between metrics dispatch attempts."""

    DISPATCH_BATCH_SIZE = 10
    """
    Worker will dispatch this many metrics at a time, or all if <= 0.
    NOTE: that current SG server code reject batches larger than 10.
    """

    def __init__(self, engine):
        """
        Initialize the worker thread.

        :params engine: Engine instance
        """

        super(MetricsDispatchWorkerThread, self).__init__()

        self._engine = engine

        # Make this thread a daemon. This means the process won't wait for this
        # thread to complete before exiting. In most cases, proper engine
        # shutdown should halt the worker correctly. In cases where an engine
        # is improperly shut down, this will prevent the process from hanging.
        self.daemon = True

        # makes possible to halt the thread
        self._halt_event = Event()

    def run(self):
        """Runs a loop to dispatch metrics that have been logged."""

        # first of all, check if metrics dispatch is supported
        # connect to shotgun and probe for server version
        sg_connection = self._engine.shotgun
        metrics_ok = (
            hasattr(sg_connection, "server_caps") and
            sg_connection.server_caps.version and
            sg_connection.server_caps.version >= (7, 4, 0)
        )
        if not metrics_ok:
            # metrics not supported
            return

        # run until halted
        while not self._halt_event.isSet():

            # get the next available metric and dispatch it
            try:
                metrics = MetricsQueueSingleton().get_metrics(
                    self.DISPATCH_BATCH_SIZE)
                if metrics:
                    self._dispatch(metrics)
            except Exception:
                pass
            finally:
                # wait, checking for halt event before more processing
                self._halt_event.wait(self.DISPATCH_INTERVAL)

    def halt(self):
        """Indiate that the worker thread should halt as soon as possible."""
        self._halt_event.set()

    def _dispatch(self, metrics):
        """Dispatch the supplied metric to the sg api registration endpoint.

        :param Metric metrics: The Toolkit metric to dispatch.

        """

        # get this thread's sg connection via tk api
        sg_connection = self._engine.tank.shotgun

        # handle proxy setup by pulling the proxy details from the main
        # shotgun connection
        if sg_connection.config.proxy_handler:
            opener = urllib2.build_opener(sg_connection.config.proxy_handler)
            urllib2.install_opener(opener)

        # build the full endpoint url with the shotgun site url
        url = "%s/%s" % (sg_connection.base_url, self.API_ENDPOINT)

        # construct the payload with the auth args and metrics data
        payload = {
            "auth_args": {
                "session_token": sg_connection.get_session_token()
            },
            "metrics": [m.data for m in metrics]
        }
        payload_json = json.dumps(payload)

        header = {'Content-Type': 'application/json'}
        try:
            request = urllib2.Request(url, payload_json, header)
            urllib2.urlopen(request)
        except urllib2.HTTPError:
            # fire and forget, so if there's an error, ignore it.
            pass

        # execute the log_metrics core hook
        self._engine.tank.execute_core_hook(
            constants.TANK_LOG_METRICS_HOOK_NAME,
            metrics=[m.data for m in metrics]
        )


###############################################################################
# ToolkitMetric classes and subclasses

class ToolkitMetric(object):
    """Simple class representing tk metric data."""

    def __init__(self, data):
        """Initialize the object with a dictionary of metric data.
        
        :param dict data: A dictionary of metric data.
        
        """
        self._data = data

    def __str__(self):
        """Readable str representation of the metric."""
        return "%s: %s" % (self.__class__, self._data)

    @property
    def data(self):
        """The underlying data this metric represents."""
        return self._data


class EventMetric(ToolkitMetric):
    """
    Convenience class for creating a metric event to be logged on a Shotgun site.

    Use this helper class to create a suitable metric structure that you can
    then pass to the `tank.utils.metrics.log_metric_event` method.

    The simplest usage of this class is simply to provide an event group and
    event name to the constructor:

    Optionally, you can add your own specific metrics by using the
    `add_event_property` method. The latter simply takes a key-value
    pair and properly structure the entry into the metric structure.

    metric.add_event_property("MyAppVersion", "10.13.1")
    metric.add_event_property("ViewedPageCount", 99 )

    Below is a complete typical usage:

    ```
    metric = EventMetric(event_group="App", event_name="Logged In")
    metric.add_event_property("MyAppVersion", "10.13.1")
    metric.add_event_property("ViewedPageCount", 99 )
    log_event_metric(metric)
    ```

    TODO: How about event type which is defined in the 'Shotgun Event Taxonomy' document as event property???
    """

    def __init__(self, group, name, properties=None):
        """
        Initialize a metric event using the specified parameters. Use this helper class
        to create a suitable metric structure to be used with the

        :param str group: A group or category this metric event falls into.
        Although any values can be used, we encourage usage of the GROUP_*
        definitions above.
=
        :param: str name: A short descriptive event name or performed action.
            The complete list can be found in the 'Shotgun Event Taxonomy' document.
            Below are a few examples:
                'Viewed Login Page'
                'Logged In'
                'Created Project'
                'Toggled Project Favorite'
                'Edited Task Status'
                'Read Inbox Item'

        :param: dict properties: An optional dictionary of extra properties to be attached to the metric event.
        """

        """
        We also add an empty event property dictionary that will be populated
        with either or both specified properties or add system info properties.
        """
        super(EventMetric, self).__init__({
            "event_group": str(group),
            "event_name": str(name),
            "event_property": {}
        })

        if properties:
            if not type(properties) is dict:
                raise TypeError("The `properties` parameter must be None or a dictionary")

            # Deliberately looping through items rather than simply
            # assigning the dictionnary so we can pre-inpect and sanitize
            # if necessary.
            for key in properties.keys():
                value = properties[key]
                self._add_event_property(key, value)


        self._add_system_info_properties()

    def _add_event_property(self, name, value):
        """
        Helper method allowing some verification and sanitization of the
        specified properties.

        :param str name:
        :param object value:
        """
        # TODO: escape non-ascii7 strings here or in Dispatcher ?
        self._data["event_property"][name] = value

    def _add_system_info_properties(self):
        """
        Helper method adding a number of system information metrics such as:
        - OS type
        - OS version
        - System architechture
        - Manufacturer etc.

        The method takes care of gathering relevant information and properly
        formatting them for submission to a Shotgun site
        """

        # TODO: find a descent library and implement method
        # TODO: Can't really send Amplitude-usable system information metrics
        #       as we can only send 'event_properties'
        pass

    def __repr__(self):
        """Official str representation of the user activity metric."""
        return "%s:%s" % (self._data["event_group"], self._data["event_name"])


###############################################################################
# metrics logging convenience functions

def log_event_metric(metric_event, log_once=False):
    """ Log a Toolkit metric event now using the Amplitude service.

    This method adds the metric event to a dispatch queue meaning that the
    metric doesn't get posted on the web right away. The can add a few
    metrics together in a single payload. The dispatcher processes the
    queue every 5-15 seconds (subject to change).

    :param EventMetric metric: A metric event structure to add the the queue.

    :param bool log_once: ``True`` if this metric should be ignored if it has
        already been logged. Defaults to ``False``.

    """
    if not isinstance(metric_event, EventMetric):
        raise TypeError("The `metric_event` parameter must be an instance of the `EventMetric`class")

    MetricsQueueSingleton().log(metric_event, log_once=log_once)


def log_metric(metric, log_once=False):
    """ Depricated method, use the `log_metric_event` method."""
    pass


def log_user_activity_metric(module, action, log_once=False):
    """ Depricated method, use the `log_metric_event` method."""
    pass


def log_user_attribute_metric(attr_name, attr_value, log_once=False):
    """ Depricated method, use the `log_metric_event` method."""
    pass
