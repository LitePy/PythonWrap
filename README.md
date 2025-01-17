# PythonWrap

A comprehensive collection of Python decorators and utility functions to enhance your development workflow.

## Installation

```bash
pip install python-wrap
```

## Features

PythonWrap provides a rich set of decorators that can be used either as decorators or as standalone functions:

### Performance & Profiling
- `@timing`: Measure function execution time
- `@profile`: Profile function performance
- `@benchmark`: Compare execution times
- `@measure_memory`: Track memory usage

### Error Handling & Reliability
- `@retry`: Retry failed operations
- `@retry_on_exception`: Retry on specific exceptions
- `@timeout`: Set execution time limits
- `@revert_on_failure`: Automatic state rollback on failure

### Caching & Optimization
- `@memoize`: Cache function results
- `@cache`: Time-based result caching
- `@once`: Single execution guarantee

### Debugging & Logging
- `@log_args`: Log function arguments
- `@log_return`: Log return values
- `@trace`: Log call stack traces
- `@audit`: Comprehensive function call auditing

### Type Safety & Validation
- `@check_type`: Runtime type checking
- `@validate_args`: Custom argument validation

### Development Tools
- `@deprecate`: Mark deprecated functions
- `@no_debug`: Disable debug output
- `@mock_data`: Easy data mocking
- `@rate_limit`: Control execution frequency

### Concurrency
- `@run_in_thread`: Asynchronous execution
- `@transactional`: Atomic operations

## Usage Examples

### Using as Decorators

```python
from python_wrap import timing, retry, memoize

# As a decorator
@timing
def slow_operation():
    time.sleep(1)
    return "Done"

# As a decorator with parameters
@retry(max_attempts=3, delay=1.0)
def unreliable_operation():
    return api_call()

# Simple decorator
@memoize
def fibonacci(n):
    if n < 2:
        return n
    return fibonacci(n-1) + fibonacci(n-2)
```

### Using as Functions

```python
from python_wrap import timing, retry, memoize

# Using timing as a function
def slow_operation():
    time.sleep(1)
    return "Done"

result = timing(slow_operation)()

# Using retry as a function
def unreliable_operation():
    return api_call()

result = retry(unreliable_operation, max_attempts=3, delay=1.0)()

# Using memoize as a function
def fibonacci(n):
    if n < 2:
        return n
    return fibonacci(n-1) + fibonacci(n-2)

memoized_fib = memoize(fibonacci)
result = memoized_fib(10)
```

### Advanced Usage

```python
from python_wrap import validate_args, check_type, timeout

# As decorators
@validate_args(x=lambda x: x > 0, y=lambda y: y < 100)
def process_numbers(x, y):
    return x + y

@check_type(name=str, age=int)
def create_user(name, age):
    return {"name": name, "age": age}

# As functions
def long_running_task():
    process_large_dataset()

timed_task = timeout(5)(long_running_task)
result = timed_task()
```

### Combining Multiple Functions

```python
from python_wrap import timing, retry, log_args

# As decorators
@timing
@retry(max_attempts=3)
@log_args
def complex_operation(x, y):
    return expensive_calculation(x, y)

# As functions
def complex_operation(x, y):
    return expensive_calculation(x, y)

# Compose functions
monitored_op = timing(retry(log_args(complex_operation), max_attempts=3))
result = monitored_op(1, 2)
```

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

## License

This project is licensed under the MIT License - see the LICENSE file for details.