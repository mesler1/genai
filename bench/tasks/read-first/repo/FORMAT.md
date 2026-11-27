# The JSON report

Other tools read this, so it must be exactly this shape.

- One JSON object, printed on one line, with keys in this order: `version`, `users`, `totals`.
- `version` is the number 2.
- `users` is a list, sorted by `runs`, most first; users with the same number of runs are sorted by name.
- Each user is an object with keys in this order: `name`, `runs`, `failed`, `failure_rate`, `hours`, `last_run`.
  - `failure_rate` is `failed / runs`, rounded to 2 decimal places.
  - `hours` is the minutes divided by 60, rounded to 1 decimal place.
  - `last_run` is the latest `ended` value, as a date only (`YYYY-MM-DD`), without the time.
- `totals` is an object with keys `runs`, `failed` and `hours`, the same as for a user, over everyone.
- A user with no failed runs still has `"failed": 0` and `"failure_rate": 0.0`.
- Separators are `", "` and `": "` (Python's default), and nothing else is printed.
