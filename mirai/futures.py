from concurrent import futures
from concurrent.futures import TimeoutError
import threading
import time

from .exceptions import MiraiError, SafeFunction, AlreadyResolvedError
from .utils import proxyto

# Future methods:
#   cancel()
#   cancelled()
#   running()
#   done()
#   result(timeout=None)
#   exception(timeout=None)
#   add_done_callback(fn)
#   set_result(result)
#   set_exception(exception)
#
# futures.wait(fs, timeout=None, return_when=ALL_COMPLETED)
#   (alt: FIRST_COMPLETED, FIRST_EXCEPTION)
# futures.as_completed(fs, timeout=None)
#
# Executor methods:
#   submit(fn *args, **kwargs)
#   map(fn, iterables)
#   shutdown()


class Promise(object):
  """
  A `Promise` encapsulates the result of an asynchronous computation. Think of
  it as a single-use mailbox -- you receive a promise which will later contain
  a message.::

    import requests
    from mirai import Promise

    def async_request(method, url, *args, **kwargs):
      "fetch a url asynchronously using `requests`"

      # construct a promise to fill later
      promise = Promise()

      def sync_request():
        "fetches synchronously & propagates exceptions"
        try:
          response = requests.request(method, url, *args, **kwargs)
          promise.setvalue(response)
        except Exception as e:
          promise.setexception(e)

      # start asynchronous computation
      Promise.call(sync_request)

      # return read-only version of promise
      return promise.future()
  """

  EXECUTOR = futures.ThreadPoolExecutor(max_workers=10)

  __slots__ = ['_future', '_lock']

  def __init__(self, future=None):
    self._future = future or futures.Future()
    self._lock   = threading.Lock()

  def andthen(self, fn):
    """
    Apply a function with a single argument: the value this Promise resolves to.
    The function must return another future.  If this Promise fails, `fn` will
    not be called. Same as as `Promise.flatmap`.

    Parameters
    ----------
    fn : (value,) -> Promise
        Function to apply. Takes 1 positional argument. Must return a Promise.

    Returns
    -------
    result : Future
        Promise `fn` will return.
    """
    return self.flatmap(fn)

  def __call__(self, timeout=None):
    """
    Retrieve value of Promise; block until it's ready or `timeout` seconds
    have passed. If `timeout` seconds pass, then a `TimeoutError` will
    be raised. If this Promise failed, the set exception will be raised.

    Parameters
    ----------
    timeout : number or None
        Number of seconds to wait until raising a `TimeoutError`. If `None`,
        then wait indefinitely.

    Returns
    -------
    result : object
        Contents of this future if it resolved successfully.

    Raises
    ------
    Exception
        Set exception if this future failed.
    """
    return self.get(timeout)

  def ensure(self, fn):
    """
    Ensure that no-argument function `fn` is called when this Promise resolves,
    regardless of whether or not it completes successfuly.

    Parameters
    ----------
    fn : (,) -> None
        function to apply upon Promise completion. takes no arguments. Return
        value ignored.

    Returns
    -------
    self : Future
    """
    def ensure(v):
      try:
        Promise.call(fn)
      except Exception as e:
        pass

    return self.onsuccess(ensure).onfailure(ensure)

  def filter(self, fn):
    """
    Construct a new Promise that fails if `fn` doesn't evaluate truthily when
    given `self.get()` as its only argument. If `fn` evaluates falsily, then
    the resulting Promise fails with a `MiraiError`.

    Parameters
    ----------
    fn : (value,) -> bool
        function used to check `self.get()`. Must return a boolean-like value.

    Returns
    -------
    result : Future
        Future whose contents are the contents of this Promise if `fn` evaluates
        truth on this Promise's contents.
    """
    return (
      self
      .flatmap(lambda v: Promise.collect([
        Promise.value(v),
        Promise.call(fn, v),
      ]))
      .flatmap(lambda (v, b):
        Promise.value(v) if b
        else Promise.exception(MiraiError("Value {} was filtered out".format(v)))
      )
    )

  def flatmap(self, fn):
    """
    Apply a function with a single argument: the value this Promise resolves to.
    The function must return another future.  If this Promise fails, `fn` will
    not be called.

    Parameters
    ----------
    fn : (value,) -> Promise
        Function to apply. Takes 1 positional argument. Must return a Promise.

    Returns
    -------
    result : Future
        Future containing return result of `fn`.
    """

    result = Promise()

    def populate(v):
      def setvalue(fut):
        try:
          fut.proxyto(result)
        except Exception as e:
          result.setexception(e)

      try:
        (
          Promise.call(fn, v)
          .onsuccess(setvalue)
          .onfailure(result.setexception)
        )
      except Exception as e:
        result.setexception(e)

    self.onsuccess(populate)
    self.onfailure(result.setexception)

    return result.future()

  def foreach(self, fn):
    """
    Apply a function if this Promise resolves successfully. The function
    receives the contents of this Promise as its only argument.

    Parameters
    ----------
    fn : (value,) -> None
        Function to apply to this Promise's contents. Return value ignored.

    Returns
    -------
    self : Promise
    """
    return self.onsuccess(fn)

  def future(self):
    """
    Retrieve a `Future` encapsulating this promise. A `Future` is a read-only
    version of the exact same thing.

    Returns
    -------
    future : Future
        Future encapsulating this Promise.
    """
    return Future(self)

  def get(self, timeout=None):
    """
    Retrieve value of Promise; block until it's ready or `timeout` seconds
    have passed. If `timeout` seconds pass, then a `TimeoutError` will
    be raised. If this Promise failed, the set exception will be raised.

    Parameters
    ----------
    timeout : number or None
        Number of seconds to wait until raising a `TimeoutError`. If `None`,
        then wait indefinitely.

    Returns
    -------
    result : anything
        Contents of this future if it resolved successfully.

    Raises
    ------
    Exception
        Set exception if this future failed.
    """
    return self._future.result(timeout)

  def getorelse(self, default):
    """
    Like `Promise.get`, but instead of raising an exception when this Promise
    fails, returns a default value.

    Parameters
    ----------
    default : anything
        default value to return in case of time
    """
    try:
      return self.get(timeout=0)
    except Exception as e:
      return default

  def handle(self, fn):
    """
    If this Promise fails, call `fn` on the ensuing exception to obtain a
    successful value.

    Parameters
    ----------
    fn : (exception,) -> anything
        Function applied to recover from a failed exception. Its return value
        will be the value of the resulting Promise.

    Returns
    -------
    result : Future
        Resulting Future returned by applying `fn` to the exception, then
        setting the return value to `result`'s value. If this Promise is
        already successful, its value is propagated onto `result`.
    """
    return self.rescue(lambda v: Promise.call(fn, v))

  def isdefined(self):
    """
    Return `True` if this Promise has already been resolved, successfully or
    unsuccessfully.

    Returns
    -------
    result : bool
    """
    return self._future.done()

  def isfailure(self):
    """
    Return `True` if this Promise failed, `False` if it succeeded, and `None` if
    it's not yet resolved.

    Returns
    -------
    result : bool
    """
    v = self.issuccess()
    if v is None : return None
    else         : return not v

  def issuccess(self):
    """
    Return `True` if this Promise succeeded, `False` if it failed, and `None` if
    it's not yet resolved.

    Returns
    -------
    result : bool
    """
    if not self.isdefined():
      return None
    else:
      try:
        self.get()
        return True
      except Exception as e:
        return False

  def join_(self, *others):
    """
    Combine values of this Promise and 1 or more other Promises into a list.
    Results are in the same order `[self] + others` is in.

    Parameters
    ----------
    others : 1 or more Promises
        Promises to combine with this Promise.

    Returns
    -------
    result : Future
        Future resolving to a list of containing the values of this Promise and
        all other Promises. If any Promise fails, `result` holds the exception in
        the one which fails soonest.
    """
    return Promise.collect([self] + list(others))

  def map(self, fn):
    """
    Transform this Promise by applying a function to its value. If this Promise
    contains an exception, `fn` is not applied.

    Parameters
    ----------
    fn : (value,) -> anything
        Function to apply to this Promise's value on completion.

    Returns
    -------
    result : Future
        Future containing `fn` applied to this Promise's value. If this Promise
        fails, the exception is propagated.
    """
    result = Promise()

    def map(v):
      try:
        Promise.call(fn, v).proxyto(result)
      except Exception as e:
        result.setexception(e)

    self.onsuccess(map)
    self.onfailure(result.setexception)

    return result.future()

  def onfailure(self, fn):
    """
    Apply a callback if this Promise fails. Callbacks can be added after this
    Promise has resolved.

    Parameters
    ----------
    fn : (exception,) -> None
        Function to call upon failure. Its only argument is the exception set
        to this Promise. If this future succeeds, `fn` will not be called.

    Returns
    -------
    self : Promise
    """
    def onfailure(fut):
      try:
        fut.result()
      except Exception as e:
        Promise.call(fn, e)

    self._future.add_done_callback(onfailure)
    return self

  def onsuccess(self, fn):
    """
    Apply a callback if this Promise succeeds. Callbacks can be added after this
    Promise has resolved.

    Parameters
    ----------
    fn : (value,) -> None
        Function to call upon success. Its only argument is the value set
        to this Promise. If this future fails, `fn` will not be called.

    Returns
    -------
    self : Promise
    """
    def onsuccess(fut):
      try:
        Promise.call(fn, fut.result())
      except Exception as e:
        pass

    self._future.add_done_callback(onsuccess)
    return self

  def or_(self, *others):
    """
    Return the first Promise that finishes among this Promise and one or more
    other Promises.

    Parameters
    ----------
    others : one or more Promises
        Other futures to consider.

    Returns
    -------
    result : Future
        First future that is resolved, successfully or otherwise.
    """
    result = Promise()

    def or_():
      def setresult(v):
        try:
          v[0].proxyto(result)
        except Exception as e:
          result.setexception(e)

      try:
        (
          Promise
          .select([self] + list(others))
          .onsuccess(setresult)
          .onfailure(result.setexception)
        )
      except Exception as e:
        result.setexception(e)

    Promise.call(or_)
    return result.future()

  def proxyto(self, other):
    """
    Copy the state of this Promise to another.

    Parameters
    ----------
    other : Promise
        Another Promise to copy the state of this Promise to, upon completion.

    Returns
    -------
    self : Promise
    """
    return self.onsuccess(other.setvalue).onfailure(other.setexception)

  def rescue(self, fn):
    """
    If this Promise fails, call `fn` on the ensuing exception to recover another
    (potentially successful) Promise. Same as `Promise.handle`.

    Parameters
    ----------
    fn : (exception,) -> Promise
        Function applied to recover from a failed exception. Must return a Promise.

    Returns
    -------
    result : Future
        Resulting Future returned by apply `fn` to the exception this Promise
        contains. If this Promise is successful, its value is propagated onto
        `result`.
    """
    result = Promise()

    def rescue(e):
      def setvalue(fut):
        try:
          fut.proxyto(result)
        except Exception as e:
          result.setexception(e)

      try:
        (
          Promise.call(fn, e)
          .onsuccess(setvalue)
          .onfailure(result.setexception)
        )
      except Exception as e:
        result.setexception(e)

    self.onsuccess(result.setvalue)
    self.onfailure(rescue)

    return result.future()

  def respond(self, fn):
    """
    Apply a function to this Promise when it's resolved.

    Parameters
    ----------
    fn : (future,) -> None
        Function to apply to this Promise upon completion. Return value is ignored

    Returns
    -------
    self : Promise
    """
    def respond(fut):
      Promise.call(fn, Promise(fut))

    self._future.add_done_callback(respond)
    return self

  def select_(self, *others):
    """
    Return the first Promise that finishes among this Promise and one or more
    other Promises.

    Parameters
    ----------
    others : one or more Promises
        Other futures to consider.

    Returns
    -------
    result : Future
        First future that is resolved, successfully or otherwise.
    """
    return self.or_(*others)

  def setexception(self, e):
    """
    Set the state of this Promise as failed with a given Exception. State can
    only be set once; once a Promise is defined, it cannot be redefined. This
    operation is thread (but not process) safe.

    Parameters
    ----------
    e : Exception

    Returns
    -------
    self : Promise
    """
    with self._lock:
      if self.isdefined():
        raise AlreadyResolvedError("Promise is already resolved; you cannot set its status again.")
      else:
        self._future.set_exception(e)
        return self

  def setvalue(self, val):
    """
    Set the state of this Promise as successful with a given value. State can
    only be set once; once a Promise is defined, it cannot be redefined. This
    operation is thread (but not process) safe.

    Parameters
    ----------
    val : value

    Returns
    -------
    self : Promise
    """
    with self._lock:
      if self.isdefined():
        raise AlreadyResolvedError("Promise is already resolved; you cannot set its status again.")
      else:
        self._future.set_result(val)
        return self

  def unit(self):
    """
    Convert this Promise to another that disregards its result.

    Returns
    -------
    result : Future
        Promise with a value of `None` if this Promise succeeds. If this Promise
        fails, the exception is propagated.
    """
    return self.map(lambda v: None)

  def update(self, other):
    """
    Populate this Promise with the contents of another.

    Parameters
    ----------
    other : Promise
        Promise to copy

    Returns
    -------
    self : Promise
    """
    other.proxyto(self)
    return self

  def updateifempty(self, other):
    """
    Like `Promise.update`, but update only if this Promise isn't already defined.

    Parameters
    ----------
    other : Promise
        Promise to copy, if necessary.

    Returns
    -------
    self : Promise
    """
    def setvalue(v):
      try:
        self.setvalue(v)
      except AlreadyResolvedError as e:
        pass
      except Exception as e:
        self.setexception(e)

    def setexception(e):
      try:
        self.setexception(e)
      except AlreadyResolvedError as e_:
        pass
      except Exception as e_:
        self.setexception(e_)

    other.onsuccess(setvalue).onfailure(setexception)
    return self

  def within(self, duration):
    """
    Return a Promise whose state is guaranteed to be resolved within `duration`
    seconds. If this Promise completes before `duration` seconds expire, it will
    contain this Promise's contents. If this Promise is not resolved by then, the
    resulting Promise will fail with a `TimeoutError`.

    Parameters
    ----------
    duration : number
        Number of seconds to wait before resolving a `TimeoutError`

    Returns
    -------
    result : Promise
        Promise guaranteed to resolve in `duration` seconds.
    """
    e = TimeoutError("Promise did not finish in {} seconds".format(duration))
    return self.or_(Promise.wait(duration).flatmap(lambda v: Promise.exception(e)))

  # CONSTRUCTORS
  @classmethod
  def value(cls, val):
    """
    Construct a Promise that is already resolved successfully to a value.

    Parameters
    ----------
    val : anything
        Value to resolve new Promise to.

    Returns
    -------
    result : Future
        Future containing `val` as its value.
    """
    f = cls()
    f.setvalue(val)
    return f.future()

  @classmethod
  def wait(cls, duration):
    """
    Construct a Promise that succeeds in `duration` seconds with value `None`.

    Parameters
    ----------
    duration : number
        Number of seconds to wait before resolving a `TimeoutError`

    Returns
    -------
    result : Future
        Promise that will resolve in `duration` seconds with value `None`.
    """
    def wait():
      try:
        time.sleep(duration)
        return None
      except Exception as e:
        raise e

    return Promise.call(wait).future()

  @classmethod
  def exception(cls, exc):
    """
    Construct a Promise that has already failed with a given exception.

    Parameters
    ----------
    exc : Exception
        Exception to fail new Promise with

    Returns
    -------
    result : Future
        New Promise that has already failed with the given exception.
    """
    f = cls()
    f.setexception(exc)
    return f.future()

  # COMBINING
  @classmethod
  def _wait(cls, fs, timeout=None, return_when=futures.FIRST_EXCEPTION):
    """
    Return a future that contains a partition of `fs` into complete and
    incomplete promises.

    Parameters
    ----------
    fs : [Promise]
        List of promises to wait upon
    timeout : float or None
        number of seconds to wait before setting result's value

    Returns
    -------
    result : Future
        Future containing a 2-element tuple. The first element is a list of
        compeleted concurrent.futures.Future, the second is a list of
        incomplete ones.
    """

    result = cls()

    def wait():
      try:
        _futures = [f._future for f in fs]
        complete, incomplete = futures.wait(_futures, timeout=timeout, return_when=return_when)
        result.setvalue( (list(complete), list(incomplete)) )
      except Exception as e:
        result.setexception(e)

    # This method needs to live outside of the Promise.EXECUTOR as a race
    # condition can arise if there len(fs) == n and max_workers == n as
    # Promise.call(select) would be the n+1 st thread, causing a lock.
    thread = threading.Thread(target=wait)
    thread.daemon = True
    thread.start()

    return result.future()

  @classmethod
  def collect(cls, fs, timeout=None):
    """
    Convert a sequence of Promises into a Promise containing a sequence of
    values, one per Promise in `fs`. The resulting Promise resolves once all
    Promises in `fs` resolve successsfully or upon the first failure. In the
    latter case, the failing Promise's exception is propagated.

    Parameters
    ----------
    fs : [Promise]
        List of Promises to merge.
    timeout : number or None
        Number of seconds to wait before registering a `TimeoutError` with the
        resulting Promise. If `None`, wait indefinitely.

    Returns
    -------
    result : Future
        Future containing values of all Futures in `fs`. If any Future in `fs`
        fails, `result` fails with the same exception. If `timeout` seconds
        pass before all Futures in `fs` resolve, `result` fails with a
        `TimeoutError`.
    """

    def collect((complete, incomplete)):
      try:
        failed = [c for c in complete if cls(c).isfailure()]
        if len(failed) > 0:
          # one or more futures failed
          return cls(failed[0])

        elif len(incomplete) > 0:
          # not all futures finished
          m, n = len(complete), len(incomplete)
          return cls.exception(
            TimeoutError(
              "{} of {} futures failed to complete in {} seconds."
              .format(n, n+m, timeout)
            )
          )

        else:
          # all futures succeeded
          return cls.value([f.get(timeout=0) for f in fs])
      except Exception as e:
        return cls.exception(e)

    return (
      cls
      ._wait(fs, timeout=timeout)
      .flatmap(collect)
    )

  @classmethod
  def join(cls, fs, timeout=None):
    """
    Construct a Promise that resolves when all Promises in `fs` have resolved. If
    any Promise in `fs` fails, the error is propagated into the resulting
    Promise. If `timeout` seconds pass before all Promises have resolved, the
    resulting Promise fails with a `TimeoutError`.

    Parameters
    ----------
    fs : [Promise]
        List of Promises to merge.
    timeout : number or None
        Number of seconds to wait before registering a `TimeoutError` with the
        resulting Promise. If `None`, wait indefinitely.

    Returns
    -------
    result : Future
        Future containing None if all Futures in `fs` succeed, the exception of
        the first failing Future in `fs`, or a `TimeoutError` if `timeout`
        seconds pass before all Futures in `fs` resolve.
    """
    return cls.collect(fs, timeout=timeout).map(lambda v: None)

  @classmethod
  def select(cls, fs, timeout=None):
    """
    Return a Promise containing a tuple of 2 elements. The first is the first
    Promise in `fs` to resolve; the second is all remaining Promises that may or
    may not be resolved yet. If `timeout` seconds pass before any Promise
    resolves, the resulting Promise fails with a `TimeoutError`. The resolved
    Promise is not guaranteed to have completed successfully.

    Parameters
    ----------
    fs : [Promise]
        List of Promises to merge.
    timeout : number or None
        Number of seconds to wait before registering a `TimeoutError` with the
        resulting Promise. If `None`, wait indefinitely.

    Returns
    -------
    result : Future
        Future containing the first Future in `fs` to finish and all remaining
        (potentially) unresolved Futures as a tuple of 2 elements for its value
        or a `TimeoutError` for its exception.
    """

    def select((complete, incomplete)):
      try:
        if len(complete) > 0:
          complete, incomplete = complete[0], incomplete + complete[1:]
          return cls.value( (cls(complete), map(cls, incomplete)) )
        else:
          return cls.exception(
            TimeoutError(
              "No future finished in Promise.select in {} seconds"
              .format(timeout)
            )
          )
      except Exception as e:
        return cls.exception(e)

    return (
      cls
      ._wait(fs, timeout=timeout, return_when=futures.FIRST_COMPLETED)
      .flatmap(select)
    )

  @classmethod
  def eval(cls, fn, *args, **kwargs):
    """
    Call a function (synchronously) and return a Promise with its result. If an
    exception is thrown inside `fn`, a new exception type will be constructed
    inheriting both from `MiraiError` and the exception's original type. The
    new exception is the same the original, except that it also contains a
    `context` attribute detailing the stack at the time the exception was
    thrown.

    Parameters
    ----------
    fn : function
        Function to be called
    *args : arguments
    **kwargs : keyword arguments

    Returns
    -------
    result : Future
        Future containing the result of `fn(*args, **kwargs)` as its value or
        the exception thrown as its exception.
    """
    fn = SafeFunction(fn)
    try:
      v = fn(*args, **kwargs)
    except Exception as e:
      return cls.exception(e)
    else:
      return cls.value(v)

  @classmethod
  def call(cls, fn, *args, **kwargs):
    """
    Call a function asynchronously and return a Promise with its result. If an
    exception is thrown inside `fn`, a new exception type will be constructed
    inheriting both from `MiraiError` and the exception's original type. The
    new exception is the same the original, except that it also contains a
    `context` attribute detailing the stack at the time the exception was
    thrown.

    Parameters
    ----------
    fn : function
        Function to be called
    *args : arguments
    **kwargs : keyword arguments

    Returns
    -------
    result : Future
        Future containing the result of `fn(*args, **kwargs)` as its value or
        the exception thrown as its exception.
    """
    return cls(cls.EXECUTOR.submit(SafeFunction(fn), *args, **kwargs)).future()

  @classmethod
  def executor(cls, executor=None):
    """
    Set/Get the EXECUTOR Promise uses. If setting, the current executor is
    first shut down.

    Parameters
    ----------
    executor : concurrent.futures.Executor or None
        If None, retrieve the current executor, otherwise, shutdown the current
        Executor object and replace it with this argument.

    Returns
    -------
    executor : Executor
        Current executor
    """
    if executor is None:
      return cls.EXECUTOR
    else:
      if cls.EXECUTOR is not None:
        cls.EXECUTOR.shutdown()
      cls.EXECUTOR = executor
      return cls.EXECUTOR


class Future(Promise):
  """Read-only version of a Promise."""

  def __init__(self, promise):
    allowed_specials = [
      '__str__',
      '__unicode__',
      '__repr__',
      '__call__',
    ]
    proxyto(self, promise, allowed_specials)

  def setvalue(self, val):
    raise AttributeError("Futures are read only; Promises are writable")

  def setexception(self, val):
    raise AttributeError("Futures are read only; Promises are writable")

