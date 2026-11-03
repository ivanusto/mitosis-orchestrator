The directory `stdlib/` in the current working directory is a read-only copy of four packages of a Python standard library: `asyncio`, `email`, `http` and `logging`. Answer the questions below from the files in that tree only. Do not answer from memory: the copy is the reference and exact values matter.

Write your answers to `answers.json` in the current working directory, as one JSON object that maps each question id to its answer. A location is written as `path:line`, with the path relative to `stdlib/` (for example `http/server.py:123`) and a 1-based line number. A line number refers to the line that holds the `def` keyword or the assignment.

- q1: When `BaseEventLoop._sock_sendfile_fallback` is called with a falsy `count`, how many bytes does it allocate for its read buffer? Give an integer; the number is not written literally in `base_events.py`.
- q2: What is the value of `server_version` for `SimpleHTTPRequestHandler`, after evaluating the expression that defines it? Give a string.
- q3: When a response carries more header lines than `http.client` accepts, `_read_headers` raises an exception. Give its message text, with the limit filled in. Give a string.
- q4: `email.policy.SMTPUTF8` is built by a chain of `clone()` calls that starts from `email.policy.default`. None of those calls sets `max_line_length`. Give the location of the assignment that supplies the value `SMTPUTF8` ends up with.
- q5: In `email.charset`, what are the two integers that `CHARSETS` stores for `'utf-8'` as its header encoding and its body encoding? Give a JSON list `[header, body]`, resolving the names to their numeric values.
- q6: `logging` registers one function to run at interpreter exit. Give the location of that function's `def` line.
- q7: What is the sum of the default port of `logging.config.listen()` and the value of `DEFAULT_SOAP_LOGGING_PORT` in `logging.handlers`? Give an integer.
- q8: `BaseHTTPRequestHandler.send_error` calls one method of the same class to begin the response with the status code and message (the method that also writes the `Server` and `Date` headers). Give the location of that method's `def` line.
