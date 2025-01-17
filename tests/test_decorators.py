"""
Tests for the PythonWrap decorators.
"""

import time
import pytest
from python_wrap.decorators import (
    timing,
    retry,
    memoize,
    log_args,
    log_return,
    validate_args,
    cache,
    once,
    deprecate,
    check_type,
    retry_on_exception,
    measure_memory,
    profile,
    rate_limit,
    mock_data,
    benchmark,
    run_in_thread,
    no_debug,
    transactional,
    revert_on_failure,
    audit,
    trace,
    timeout,
)
from datetime import timedelta

def test_timing():
    @timing
    def slow_function():
        time.sleep(0.1)
        return True
    
    assert slow_function() is True

def test_retry():
    attempts = 0
    
    @retry(max_attempts=3, delay=0.1)
    def failing_function():
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise ValueError("Temporary error")
        return True
    
    assert failing_function() is True
    assert attempts == 3

def test_memoize():
    calls = 0
    
    @memoize
    def expensive_function(x):
        nonlocal calls
        calls += 1
        return x * 2
    
    assert expensive_function(5) == 10
    assert expensive_function(5) == 10
    assert calls == 1

def test_validate_args():
    @validate_args(x=lambda x: x > 0)
    def positive_only(x):
        return x
    
    assert positive_only(5) == 5
    with pytest.raises(ValueError):
        positive_only(-1)

def test_cache():
    calls = 0
    
    @cache(duration=timedelta(seconds=1))
    def cached_function():
        nonlocal calls
        calls += 1
        return calls
    
    assert cached_function() == 1
    assert cached_function() == 1
    time.sleep(1.1)
    assert cached_function() == 2

def test_once():
    calls = 0
    
    @once
    def run_once():
        nonlocal calls
        calls += 1
        return calls
    
    assert run_once() == 1
    assert run_once() == 1
    assert calls == 1

def test_check_type():
    @check_type(x=int, y=str)
    def typed_function(x, y):
        return f"{x}{y}"
    
    assert typed_function(1, "2") == "12"
    with pytest.raises(TypeError):
        typed_function("1", 2)

def test_mock_data():
    @mock_data(42)
    def real_function():
        return 0
    
    assert real_function() == 42

def test_timeout():
    @timeout(1)
    def quick_function():
        return True
    
    @timeout(1)
    def slow_function():
        time.sleep(2)
        return True
    
    assert quick_function() is True
    with pytest.raises(TimeoutError):
        slow_function()

# Add more tests for other decorators as needed