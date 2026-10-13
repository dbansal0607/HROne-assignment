# DECISIONS.md

1. **Indexes.**
   I created a unique index on `employees.emp_code` for business identity enforcement and O(1) lookups; a compound index on `employees(department, joined_on)` to support department-scoped queries and headcount filtering; a compound unique index on `attendance_logs(emp_code, date)` ensuring at most one record per employee per attendance day and race-safe punch-ins; a compound index on `attendance_logs(date, emp_code)` backing chronological pagination and index scans (`IXSCAN`); and `attendance_logs(date, late_minutes)` for rapid leaderboard filtering. I considered and rejected a TTL index because attendance records are permanent audit entities, and an index on `punch_in` alone since all queries constrain by calendar date or `(emp_code, date)`.

2. **Punch-in race.**
   Both requests pass employee validation concurrently and attempt `insert_one`. Because `attendance_logs` enforces a compound unique index on `(emp_code, date)`, MongoDB's storage engine atomically commits the first write and throws `DuplicateKeyError` on the second. The application catches this exception and returns HTTP 409 Conflict without creating corrupt duplicates or unhandled 500 errors.

3. **Ties.**
   The leaderboard applies standard competition ranking (`1, 2, 2, 4`) via MongoDB `$setWindowFields` using `$rank`. The query-parameter `limit` filters rows strictly *after* ranking (`rank <= limit`), ordering tied rows by `emp_code` ascending. If two employees tie at the cutoff rank, both are returned because both satisfy `rank <= limit`, deliberately producing more than `limit` rows as specified in the contract.

4. **Headcount.**
   The department summary evaluates headcount directly against `employees` where `joined_on` is on or before the month's final calendar day, rather than inspecting log records alone. By combining employee documents with log metrics via `$unionWith` (or lookup from `employees`), employees without any attendance logs still contribute to the department's headcount count.

5. **One thing you would change for 100x the data.**
   At 100x scale (~10M attendance records), I would maintain pre-aggregated daily and monthly rollup collections updated incrementally through MongoDB Change Streams, avoiding costly ad-hoc multi-stage cross-collection aggregations over raw punch records during user requests.
