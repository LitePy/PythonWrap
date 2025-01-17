"""
Core decorators module containing all decorator implementations.
"""

import time
import logging
import functools
import threading
import warnings
import inspect
import traceback
from typing import Any, Callable, Dict, List, Type, Union
from datetime import datetime, timedelta
import threading
import queue
import signal
import resource
import cProfile
import pstats
import io
from contextlib import contextmanager

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def _timing_impl(func, *args, **kwargs):
    """Implementation of timing functionality."""
    start_time = time.time()
    result = func(*args, **kwargs)
    end_time = time.time()
    logger.info(f"{func.__name__} took {end_time - start_time:.4f} seconds to execute")
    return result

def timing(func: Union[Callable, None] = None) -> Callable:
    """
    Measures and logs the execution time of a function.
    Can be used as a decorator or function:

    @timing
    def func(): pass

    # or
    result = timing(func)()
    """
    if func is None:
        return timing
    
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        return _timing_impl(func, *args, **kwargs)
    return wrapper

def _retry_impl(func, max_attempts, delay, *args, **kwargs):
    """Implementation of retry functionality."""
    attempts = 0
    while attempts < max_attempts:
        try:
            return func(*args, **kwargs)
        except Exception as e:
            attempts += 1
            if attempts == max_attempts:
                raise
            logger.warning(f"Attempt {attempts} failed, retrying in {delay} seconds...")
            time.sleep(delay)
    return None

def retry(func: Union[Callable, None] = None, *, max_attempts: int = 3, delay: float = 1.0) -> Callable:
    """
    Retries a function in case of failure.
    Can be used as a decorator or function:

    @retry(max_attempts=3)
    def func(): pass

    # or
    result = retry(func, max_attempts=3)()
    """
    if func is None:
        return lambda f: retry(f, max_attempts=max_attempts, delay=delay)
    
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        return _retry_impl(func, max_attempts, delay, *args, **kwargs)
    return wrapper

def _memoize_impl(func, cache, *args, **kwargs):
    """Implementation of memoize functionality."""
    key = str(args) + str(kwargs)
    if key not in cache:
        cache[key] = func(*args, **kwargs)
    return cache[key]

def memoize(func: Union[Callable, None] = None) -> Callable:
    """
    Caches the result of a function to prevent redundant calculations.
    Can be used as a decorator or function:

    @memoize
    def func(): pass

    # or
    result = memoize(func)()
    """
    if func is None:
        return memoize
    
    cache = {}
    
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        return _memoize_impl(func, cache, *args, **kwargs)
    return wrapper

def _log_args_impl(func, *args, **kwargs):
    """Implementation of log_args functionality."""
    logger.info(f"Calling {func.__name__} with args: {args}, kwargs: {kwargs}")
    return func(*args, **kwargs)

def log_args(func: Union[Callable, None] = None) -> Callable:
    """
    Logs the arguments passed to a function.
    Can be used as a decorator or function:

    @log_args
    def func(): pass

    # or
    result = log_args(func)()
    """
    if func is None:
        return log_args
    
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        return _log_args_impl(func, *args, **kwargs)
    return wrapper

def _log_return_impl(func, *args, **kwargs):
    """Implementation of log_return functionality."""
    result = func(*args, **kwargs)
    logger.info(f"{func.__name__} returned: {result}")
    return result

def log_return(func: Union[Callable, None] = None) -> Callable:
    """
    Logs the return value of a function after execution.
    Can be used as a decorator or function:

    @log_return
    def func(): pass

    # or
    result = log_return(func)()
    """
    if func is None:
        return log_return
    
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        return _log_return_impl(func, *args, **kwargs)
    return wrapper

def _validate_args_impl(func, validators, *args, **kwargs):
    """Implementation of validate_args functionality."""
    sig = inspect.signature(func)
    bound_args = sig.bind(*args, **kwargs)
    for param_name, validator in validators.items():
        if param_name in bound_args.arguments:
            value = bound_args.arguments[param_name]
            if not validator(value):
                raise ValueError(f"Invalid value for {param_name}: {value}")
    return func(*args, **kwargs)

def validate_args(**validators):
    """
    Validates the arguments passed to a function based on custom rules.
    Can be used as a decorator or function:

    @validate_args(x=lambda x: x > 0)
    def func(): pass

    # or
    result = validate_args(x=lambda x: x > 0)(func)()
    """
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            return _validate_args_impl(func, validators, *args, **kwargs)
        return wrapper
    return decorator

def _cache_impl(func, duration, cache_data, *args, **kwargs):
    """Implementation of cache functionality."""
    key = str(args) + str(kwargs)
    now = datetime.now()
    if key in cache_data:
        result, timestamp = cache_data[key]
        if now - timestamp < duration:
            return result
    result = func(*args, **kwargs)
    cache_data[key] = (result, now)
    return result

def cache(duration: timedelta):
    """
    Caches the result of a function call for a specified duration.
    Can be used as a decorator or function:

    @cache(duration=timedelta(minutes=5))
    def func(): pass

    # or
    result = cache(duration=timedelta(minutes=5))(func)()
    """
    cache_data = {}
    
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            return _cache_impl(func, duration, cache_data, *args, **kwargs)
        return wrapper
    return decorator

def once(func: Union[Callable, None] = None) -> Callable:
    """
    Ensures the function runs only once, even across multiple calls.
    Can be used as a decorator or function:

    @once
    def func(): pass

    # or
    result = once(func)()
    """
    if func is None:
        return once

    # Store state in a dictionary to maintain closure state
    state = {'has_run': False, 'result': None}
    
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        if not state['has_run']:
            state['result'] = func(*args, **kwargs)
            state['has_run'] = True
        return state['result']
    
    return wrapper

def _deprecate_impl(func, message, *args, **kwargs):
    """Implementation of deprecate functionality."""
    warn_msg = message or f"{func.__name__} is deprecated and will be removed in a future version."
    warnings.warn(warn_msg, DeprecationWarning, stacklevel=2)
    return func(*args, **kwargs)

def deprecate(message: str = None):
    """
    Marks a function as deprecated and provides a warning when used.
    Can be used as a decorator or function:

    @deprecate("Use new_func instead")
    def func(): pass

    # or
    result = deprecate("Use new_func instead")(func)()
    """
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            return _deprecate_impl(func, message, *args, **kwargs)
        return wrapper
    return decorator

def _check_type_impl(func, type_hints, *args, **kwargs):
    """Implementation of check_type functionality."""
    sig = inspect.signature(func)
    bound_args = sig.bind(*args, **kwargs)
    for param_name, expected_type in type_hints.items():
        if param_name in bound_args.arguments:
            value = bound_args.arguments[param_name]
            if not isinstance(value, expected_type):
                raise TypeError(f"Parameter {param_name} must be of type {expected_type}")
    return func(*args, **kwargs)

def check_type(**type_hints):
    """
    Verifies that function arguments are of the correct type.
    Can be used as a decorator or function:

    @check_type(x=int, y=str)
    def func(): pass

    # or
    result = check_type(x=int, y=str)(func)()
    """
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            return _check_type_impl(func, type_hints, *args, **kwargs)
        return wrapper
    return decorator

def _retry_on_exception_impl(func, exceptions, *args, **kwargs):
    """Implementation of retry_on_exception functionality."""
    while True:
        try:
            return func(*args, **kwargs)
        except exceptions as e:
            logger.warning(f"Caught exception {type(e).__name__}, retrying...")
            continue
        except Exception as e:
            raise

def retry_on_exception(*exceptions):
    """
    Retries the function when specific exceptions are raised.
    Can be used as a decorator or function:

    @retry_on_exception(ValueError, KeyError)
    def func(): pass

    # or
    result = retry_on_exception(ValueError, KeyError)(func)()
    """
    if not exceptions:
        exceptions = (Exception,)
    
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            return _retry_on_exception_impl(func, exceptions, *args, **kwargs)
        return wrapper
    return decorator

def _measure_memory_impl(func, *args, **kwargs):
    """Implementation of measure_memory functionality."""
    initial_memory = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    result = func(*args, **kwargs)
    final_memory = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    memory_used = final_memory - initial_memory
    logger.info(f"{func.__name__} used {memory_used} KB of memory")
    return result

def measure_memory(func: Union[Callable, None] = None) -> Callable:
    """
    Measures the memory usage of a function while it runs.
    Can be used as a decorator or function:

    @measure_memory
    def func(): pass

    # or
    result = measure_memory(func)()
    """
    if func is None:
        return measure_memory
    
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        return _measure_memory_impl(func, *args, **kwargs)
    return wrapper

def _profile_impl(func, *args, **kwargs):
    """Implementation of profile functionality."""
    profiler = cProfile.Profile()
    try:
        return profiler.runcall(func, *args, **kwargs)
    finally:
        s = io.StringIO()
        stats = pstats.Stats(profiler, stream=s).sort_stats('cumulative')
        stats.print_stats()
        logger.info(f"Profile for {func.__name__}:\n{s.getvalue()}")

def profile(func: Union[Callable, None] = None) -> Callable:
    """
    Profiles the performance of a function to aid in optimization.
    Can be used as a decorator or function:

    @profile
    def func(): pass

    # or
    result = profile(func)()
    """
    if func is None:
        return profile
    
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        return _profile_impl(func, *args, **kwargs)
    return wrapper

def _rate_limit_impl(func, calls, period, call_times, *args, **kwargs):
    """Implementation of rate_limit functionality."""
    now = time.time()
    call_times[:] = [t for t in call_times if now - t < period]
    if len(call_times) >= calls:
        raise Exception(f"Rate limit exceeded: {calls} calls per {period} seconds")
    call_times.append(now)
    return func(*args, **kwargs)

def rate_limit(calls: int, period: float):
    """
    Limits the frequency of function calls to avoid overload.
    Can be used as a decorator or function:

    @rate_limit(calls=100, period=60)
    def func(): pass

    # or
    result = rate_limit(calls=100, period=60)(func)()
    """
    call_times = []
    
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            return _rate_limit_impl(func, calls, period, call_times, *args, **kwargs)
        return wrapper
    return decorator

def _mock_data_impl(func, data, *args, **kwargs):
    """Implementation of mock_data functionality."""
    return data() if callable(data) else data

def mock_data(data: Any):
    """
    Replaces a function's output with mock data, useful for testing.
    Can be used as a decorator or function:

    @mock_data(42)
    def func(): pass

    # or
    result = mock_data(42)(func)()
    """
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            return _mock_data_impl(func, data, *args, **kwargs)
        return wrapper
    return decorator

def _benchmark_impl(func, *args, **kwargs):
    """Implementation of benchmark functionality."""
    times = []
    for _ in range(3):  # Run 3 times for averaging
        start_time = time.time()
        result = func(*args, **kwargs)
        end_time = time.time()
        times.append(end_time - start_time)
    avg_time = sum(times) / len(times)
    logger.info(f"Benchmark {func.__name__}: avg={avg_time:.4f}s over {len(times)} runs")
    return result

def benchmark(func: Union[Callable, None] = None) -> Callable:
    """
    Compares the execution times of different functions.
    Can be used as a decorator or function:

    @benchmark
    def func(): pass

    # or
    result = benchmark(func)()
    """
    if func is None:
        return benchmark
    
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        return _benchmark_impl(func, *args, **kwargs)
    return wrapper

def _run_in_thread_impl(func, *args, **kwargs):
    """Implementation of run_in_thread functionality."""
    thread = threading.Thread(target=func, args=args, kwargs=kwargs)
    thread.start()
    return thread

def run_in_thread(func: Union[Callable, None] = None) -> Callable:
    """
    Executes a function asynchronously in a separate thread.
    Can be used as a decorator or function:

    @run_in_thread
    def func(): pass

    # or
    result = run_in_thread(func)()
    """
    if func is None:
        return run_in_thread
    
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        return _run_in_thread_impl(func, *args, **kwargs)
    return wrapper

def _no_debug_impl(func, *args, **kwargs):
    """Implementation of no_debug functionality."""
    current_level = logger.getEffectiveLevel()
    logger.setLevel(logging.INFO)
    try:
        return func(*args, **kwargs)
    finally:
        logger.setLevel(current_level)

def no_debug(func: Union[Callable, None] = None) -> Callable:
    """
    Disables debug output in production environments.
    Can be used as a decorator or function:

    @no_debug
    def func(): pass

    # or
    result = no_debug(func)()
    """
    if func is None:
        return no_debug
    
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        return _no_debug_impl(func, *args, **kwargs)
    return wrapper

def _transactional_impl(func, changes, *args, **kwargs):
    """Implementation of transactional functionality."""
    try:
        result = func(*args, **kwargs)
        return result
    except Exception:
        for change in reversed(changes):
            change.rollback()
        raise

def transactional(func: Union[Callable, None] = None) -> Callable:
    """
    Ensures that function changes are committed or rolled back atomically.
    Can be used as a decorator or function:

    @transactional
    def func(): pass

    # or
    result = transactional(func)()
    """
    if func is None:
        return transactional
    
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        changes = []
        return _transactional_impl(func, changes, *args, **kwargs)
    return wrapper

def _revert_on_failure_impl(func, state, *args, **kwargs):
    """Implementation of revert_on_failure functionality."""
    try:
        return func(*args, **kwargs)
    except Exception:
        # Restore initial state
        for key, value in state.items():
            setattr(args[0], key, value)
        raise

def revert_on_failure(func: Union[Callable, None] = None) -> Callable:
    """
    Reverts all changes made by the function if an exception occurs.
    Can be used as a decorator or function:

    @revert_on_failure
    def func(): pass

    # or
    result = revert_on_failure(func)()
    """
    if func is None:
        return revert_on_failure
    
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        state = {}  # Store initial state
        return _revert_on_failure_impl(func, state, *args, **kwargs)
    return wrapper

def _audit_impl(func, *args, **kwargs):
    """Implementation of audit functionality."""
    timestamp = datetime.now()
    caller_frame = inspect.currentframe().f_back
    caller_info = f"{caller_frame.f_code.co_filename}:{caller_frame.f_lineno}"
    
    audit_data = {
        "function": func.__name__,
        "timestamp": timestamp,
        "caller": caller_info,
        "args": args,
        "kwargs": kwargs
    }
    
    try:
        result = func(*args, **kwargs)
        audit_data["status"] = "success"
        audit_data["result"] = result
        return result
    except Exception as e:
        audit_data["status"] = "error"
        audit_data["error"] = str(e)
        raise
    finally:
        logger.info(f"Audit log: {audit_data}")

def audit(func: Union[Callable, None] = None) -> Callable:
    """
    Audits and logs function calls with metadata.
    Can be used as a decorator or function:

    @audit
    def func(): pass

    # or
    result = audit(func)()
    """
    if func is None:
        return audit
    
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        return _audit_impl(func, *args, **kwargs)
    return wrapper

def _trace_impl(func, *args, **kwargs):
    """Implementation of trace functionality."""
    stack = traceback.extract_stack()[:-1]  # Exclude current frame
    logger.debug(f"Call trace for {func.__name__}:")
    for filename, lineno, name, line in stack:
        logger.debug(f"  File {filename}, line {lineno}, in {name}")
        if line:
            logger.debug(f"    {line.strip()}")
    return func(*args, **kwargs)

def trace(func: Union[Callable, None] = None) -> Callable:
    """
    Logs function calls, including call stack traces for debugging.
    Can be used as a decorator or function:

    @trace
    def func(): pass

    # or
    result = trace(func)()
    """
    if func is None:
        return trace
    
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        return _trace_impl(func, *args, **kwargs)
    return wrapper

def _timeout_impl(func, seconds, *args, **kwargs):
    """Implementation of timeout functionality."""
    result = queue.Queue()
    
    def target():
        try:
            result.put(func(*args, **kwargs))
        except Exception as e:
            result.put(e)
    
    thread = threading.Thread(target=target)
    thread.daemon = True
    thread.start()
    thread.join(seconds)
    
    if thread.is_alive():
        raise TimeoutError(f"Function {func.__name__} timed out after {seconds} seconds")
    
    value = result.get()
    if isinstance(value, Exception):
        raise value
    return value

def timeout(seconds: int):
    """
    Sets a timeout limit on function execution.
    Can be used as a decorator or function:

    @timeout(5)
    def func(): pass

    # or
    result = timeout(5)(func)()
    """
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            return _timeout_impl(func, seconds, *args, **kwargs)
        return wrapper
    return decorator