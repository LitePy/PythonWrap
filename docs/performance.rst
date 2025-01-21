Performance & Profiling
=======================

This section covers tools for measuring and optimizing the performance of your functions.

@timing
-------
Measure the execution time of a function.

**Parameters:**
- **func (Callable):** The function to be wrapped. Automatically passed when used as a decorator.

**Usage as a Decorator:**

.. code-block:: python

   @timing
   def my_function():
       # Code to measure
       pass

**Usage as a Function:**

.. code-block:: python

   def my_function():
       # Code to measure
       pass

   timed_function = timing(my_function)
   timed_function()  # Logs execution time

@profile
--------
Profile the performance of a function.

**Parameters:**
- **func (Callable):** The function to be wrapped. Automatically passed when used as a decorator.

**Usage as a Decorator:**

.. code-block:: python

   @profile
   def profile_function():
       # Function code to profile
       pass

**Usage as a Function:**

.. code-block:: python

   def profile_function():
       # Function code to profile
       pass

   profiled_function = profile(profile_function)
   profiled_function()  # Generates a performance profile

@benchmark
----------
Run a function multiple times and calculate the average execution time.

**Parameters:**
- **func (Callable):** The function to be wrapped. Automatically passed when used as a decorator.
- **iterations (int):** The number of times to run the function. Default is ``10``.

**Usage as a Decorator:**

.. code-block:: python

   @benchmark
   def benchmark_function():
       # Function code to benchmark
       pass

**Usage as a Function:**

.. code-block:: python

   def benchmark_function():
       # Function code to benchmark
       pass

   benchmarked_function = benchmark(benchmark_function, iterations=5)
   benchmarked_function()  # Calculates average execution time

@measure_memory
---------------
Track the memory usage of a function.

**Parameters:**
- **func (Callable):** The function to be wrapped. Automatically passed when used as a decorator.

**Usage as a Decorator:**

.. code-block:: python

   @measure_memory
   def memory_intensive_function():
       # Function code that uses a lot of memory
       pass

**Usage as a Function:**

.. code-block:: python

   def memory_intensive_function():
       # Function code that uses a lot of memory
       pass

   memory_measured_function = measure_memory(memory_intensive_function)
   memory_measured_function()  # Logs memory usage