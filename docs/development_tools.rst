Development Tools
=================

This section covers tools for development and testing.

@deprecate
----------
Mark a function as deprecated.

**Parameters:**
- **func (Callable):** The function to be wrapped. Automatically passed when used as a decorator.

**Usage as a Decorator:**

.. code-block:: python

   @deprecate
   def old_function():
       print("This function is deprecated.")

**Usage as a Function:**

.. code-block:: python

   def old_function():
       print("This function is deprecated.")

   deprecated_function = deprecate(old_function)
   deprecated_function()  # Outputs a deprecation warning

@no_debug
---------
Disable debug output for a function.

**Parameters:**
- **func (Callable):** The function to be wrapped. Automatically passed when used as a decorator.

**Usage as a Decorator:**

.. code-block:: python

   @no_debug
   def sensitive_function():
       print("This is a production-safe function.")

**Usage as a Function:**

.. code-block:: python

   def sensitive_function():
       print("This is a production-safe function.")

   no_debug_function = no_debug(sensitive_function)
   no_debug_function()  # Executes without debug output

@mock_data
----------
Mock the output of a function for testing.

**Parameters:**
- **data (Any):** The data to return when the function is called. Can be a value or a callable.
- **func (Callable):** The function to be wrapped. Automatically passed when used as a decorator.

**Usage as a Decorator:**

.. code-block:: python

   @mock_data(return_value={"id": 1, "name": "Mock User"})
   def get_user_data(user_id):
       pass

**Usage as a Function:**

.. code-block:: python

   def get_user_data(user_id):
       pass

   mocked_function = mock_data(return_value={"id": 1, "name": "Mock User"})(get_user_data)
   result = mocked_function(42)  # Returns {"id": 1, "name": "Mock User"}

@rate_limit
-----------
Limit the frequency of function execution.

**Parameters:**
- **calls (int):** The maximum number of calls allowed within the specified period.
- **period (float):** The time period (in seconds) for rate limiting.
- **func (Callable):** The function to be wrapped. Automatically passed when used as a decorator.

**Usage as a Decorator:**

.. code-block:: python

   @rate_limit(calls=5, period=10)
   def api_request():
       print("API request made")

**Usage as a Function:**

.. code-block:: python

   def api_request():
       print("API request made")

   rate_limited_function = rate_limit(calls=5, period=10)(api_request)
   rate_limited_function()  # Enforces rate limiting