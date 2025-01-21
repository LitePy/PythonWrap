Concurrency
===========

This section covers tools for managing concurrency.

@run_in_thread
--------------
Run a function asynchronously in a separate thread.

**Parameters:**
- **func (Callable):** The function to be wrapped. Automatically passed when used as a decorator.

**Usage as a Decorator:**

.. code-block:: python

   @run_in_thread
   def background_task():
       print("This task is running in a separate thread.")

**Usage as a Function:**

.. code-block:: python

   def background_task():
       print("This task is running in a separate thread.")

   threaded_function = run_in_thread(background_task)
   threaded_function()  # Runs in a separate thread

@transactional
--------------
Ensure atomic operations within a transaction.

**Parameters:**
- **func (Callable):** The function to be wrapped. Automatically passed when used as a decorator.

**Usage as a Decorator:**

.. code-block:: python

   @transactional
   def update_user_balance(user_id, amount):
       print(f"Updating user {user_id} balance by {amount}.")

**Usage as a Function:**

.. code-block:: python

   def update_user_balance(user_id, amount):
       print(f"Updating user {user_id} balance by {amount}.")

   transactional_function = transactional(update_user_balance)
   transactional_function(42, 100)  # Ensures atomic execution