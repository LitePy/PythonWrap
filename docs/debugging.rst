Debugging & Logging
===================

This section covers tools for debugging and logging function execution.

@log_args
---------
Log the arguments passed to a function.

**Parameters:**
- **func (Callable):** The function to be wrapped. Automatically passed when used as a decorator.

**Usage as a Decorator:**

.. code-block:: python

   @log_args
   def calculate_sum(a, b):
       return a + b

**Usage as a Function:**

.. code-block:: python

   def calculate_sum(a, b):
       return a + b

   logged_function = log_args(calculate_sum)
   result = logged_function(10, 20)  # Logs arguments: a=10, b=20

@log_return
-----------
Log the return value of a function.

**Parameters:**
- **func (Callable):** The function to be wrapped. Automatically passed when used as a decorator.

**Usage as a Decorator:**

.. code-block:: python

   @log_return
   def fetch_data():
       return {"key": "value"}

**Usage as a Function:**

.. code-block:: python

   def fetch_data():
       return {"key": "value"}

   logged_function = log_return(fetch_data)
   result = logged_function()  # Logs return value: {"key": "value"}

@trace
------
Log the call stack trace of a function.

**Parameters:**
- **func (Callable):** The function to be wrapped. Automatically passed when used as a decorator.

**Usage as a Decorator:**

.. code-block:: python

   @trace
   def perform_task():
       # Perform a task
       print("Task performed")

**Usage as a Function:**

.. code-block:: python

   def perform_task():
       # Perform a task
       print("Task performed")

   traced_function = trace(perform_task)
   traced_function()  # Logs the call stack trace

@audit
------
Provide comprehensive auditing for function calls.

**Parameters:**
- **func (Callable):** The function to be wrapped. Automatically passed when used as a decorator.

**Usage as a Decorator:**

.. code-block:: python

   @audit
   def process_data(data):
       # Process the input data
       return len(data)

**Usage as a Function:**

.. code-block:: python

   def process_data(data):
       # Process the input data
       return len(data)

   audited_function = audit(process_data)
   result = audited_function(["item1", "item2"])  # Logs arguments, return value, and execution details