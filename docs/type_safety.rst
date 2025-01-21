Type Safety & Validation
========================

This section covers tools for runtime type checking and argument validation.

@check_type
-----------
Enable runtime type checking for function arguments and return values.

**Parameters:**
- **func (Callable):** The function to be wrapped. Automatically passed when used as a decorator.

**Usage as a Decorator:**

.. code-block:: python

   @check_type
   def add_numbers(a: int, b: int) -> int:
       return a + b

**Usage as a Function:**

.. code-block:: python

   def add_numbers(a: int, b: int) -> int:
       return a + b

   checked_function = check_type(add_numbers)
   result = checked_function(10, 20)  # Ensures types are checked at runtime

@validate_args
--------------
Add custom validation logic for function arguments.

**Parameters:**
- **func (Callable):** The function to be wrapped. Automatically passed when used as a decorator.

**Usage as a Decorator:**

.. code-block:: python

   @validate_args
   def create_user(username: str, age: int):
       if age < 18:
           raise ValueError("Age must be 18 or above")
       print(f"User {username} created")

**Usage as a Function:**

.. code-block:: python

   def create_user(username: str, age: int):
       if age < 18:
           raise ValueError("Age must be 18 or above")
       print(f"User {username} created")

   validated_function = validate_args(create_user)
   validated_function("JohnDoe", 25)  # Validates arguments before execution