Error Handling & Reliability
============================

This section covers tools for improving the reliability of your functions.

@retry
------
Retry a function if it fails.

**Parameters:**
- **max_attempts (int):** The maximum number of retry attempts. Default is ``3``.
- **delay (float):** The delay (in seconds) between retry attempts. Default is ``1.0``.
- **func (Callable):** The function to be wrapped. Automatically passed when used as a decorator.

**Usage as a Decorator:**

.. code-block:: python

   @retry(max_attempts=3, delay=2.0)
   def unreliable_function():
       # Code that may fail
       pass

**Usage as a Function:**

.. code-block:: python

   def unreliable_function():
       # Code that may fail
       pass

   retried_function = retry(max_attempts=3, delay=2.0)(unreliable_function)
   retried_function()  # Retries up to 3 times with a 2-second delay

@retry_on_exception
-------------------
Retry a function only when specific exceptions are raised.

**Parameters:**
- **exceptions (tuple):** One or more exception types that trigger a retry.
- **func (Callable):** The function to be wrapped. Automatically passed when used as a decorator.

**Usage as a Decorator:**

.. code-block:: python

   @retry_on_exception(ValueError, KeyError)
   def operation_with_exceptions():
       # Code that may raise exceptions
       pass

**Usage as a Function:**

.. code-block:: python

   def operation_with_exceptions():
       # Code that may raise exceptions
       pass

   retried_function = retry_on_exception(ValueError, KeyError)(operation_with_exceptions)
   retried_function()  # Retries only on ValueError or KeyError

@timeout
--------
Limit the execution time of a function.

**Parameters:**
- **seconds (int):** The maximum execution time for the function.
- **func (Callable):** The function to be wrapped. Automatically passed when used as a decorator.

**Usage as a Decorator:**

.. code-block:: python

   @timeout(seconds=5)
   def time_sensitive_task():
       # Code that should finish within 5 seconds
       pass

**Usage as a Function:**

.. code-block:: python

   def time_sensitive_task():
       # Code that should finish within 5 seconds
       pass

   timeout_function = timeout(seconds=5)(time_sensitive_task)
   timeout_function()  # Raises a timeout exception if execution exceeds 5 seconds

@revert_on_failure
------------------
Roll back the system state if a failure occurs.

**Parameters:**
- **func (Callable):** The function to be wrapped. Automatically passed when used as a decorator.

**Usage as a Decorator:**

.. code-block:: python

   @revert_on_failure
   def critical_operation():
       # Code that needs rollback on failure
       pass

**Usage as a Function:**

.. code-block:: python

   def critical_operation():
       # Code that needs rollback on failure
       pass

   revert_function = revert_on_failure(critical_operation)
   revert_function()  # Rolls back state on failure