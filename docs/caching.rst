Caching & Optimization
======================

This section covers tools for caching and optimizing function execution.

@memoize
--------
Cache the results of a function.

**Parameters:**
- **func (Callable):** The function to be wrapped. Automatically passed when used as a decorator.

**Usage as a Decorator:**

.. code-block:: python

   @memoize
   def expensive_calculation(x, y):
       # Perform a costly operation
       return x * y

**Usage as a Function:**

.. code-block:: python

   def expensive_calculation(x, y):
       # Perform a costly operation
       return x * y

   memoized_function = memoize(expensive_calculation)
   result = memoized_function(5, 10)  # Caches the result for (5, 10)

@cache
------
Cache the results of a function for a specified duration.

**Parameters:**
- **duration (timedelta):** The time duration for which the cached result is valid.
- **func (Callable):** The function to be wrapped. Automatically passed when used as a decorator.

**Usage as a Decorator:**

.. code-block:: python

   from datetime import timedelta

   @cache(duration=timedelta(minutes=5))
   def fetch_data():
       # Retrieve data from an API or database
       return {"key": "value"}

**Usage as a Function:**

.. code-block:: python

   from datetime import timedelta

   def fetch_data():
       # Retrieve data from an API or database
       return {"key": "value"}

   cached_function = cache(duration=timedelta(minutes=5))(fetch_data)
   result = cached_function()  # Caches the result for 5 minutes

@once
-----
Ensure a function is executed only once.

**Parameters:**
- **func (Callable):** The function to be wrapped. Automatically passed when used as a decorator.

**Usage as a Decorator:**

.. code-block:: python

   @once
   def initialize_service():
       # Perform a one-time initialization
       print("Service initialized")

**Usage as a Function:**

.. code-block:: python

   def initialize_service():
       # Perform a one-time initialization
       print("Service initialized")

   one_time_function = once(initialize_service)
   one_time_function()  # Executes the function
   one_time_function()  # Does nothing, as the function has already been executed