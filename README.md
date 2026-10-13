# HROne — Employee Attendance & Analytics API

A production-grade, highly performant Employee Attendance & Analytics REST API built with **Python 3.11+**, **FastAPI**, and **MongoDB 6.0+**. Designed strictly in conformance with the OpenAPI 3.0 specification (`openapi.yaml`), the BSON data model (`DATA_MODEL.md`), and the assignment requirements.

---

## 1. Overview & Architecture

The service provides comprehensive employee attendance tracking, regularization workflows, audit logging, and advanced workforce analytics powered by native MongoDB aggregation pipelines and window functions.

### Core Architectural Principles
- **FastAPI Framework**: High-throughput asynchronous routing and Pydantic v2 validation.
- **Single-Module Engine**: All core application code lives inside `app/main.py` for direct evaluability and minimal overhead.
- **Native MongoDB Aggregations**: All analytics calculations (`employee_monthly`, `department_summary`, `late_leaderboard`, `department_trend`) execute 100% inside MongoDB aggregation pipelines rather than Python loops.
- **Index-Backed Query Execution**: All query paths are backed by compound indexes achieving `IXSCAN` plans without collection scans (`COLLSCAN`), scaling effortlessly to 100k+ records.
- **Optimistic Concurrency & Atomicity**: Atomic database constraints and versioned optimistic locks guarantee race-safe operations across concurrent punch-ins, punch-outs, and manual corrections.

---

## 2. Technology Stack

- **Language:** Python 3.11+ (verified on Python 3.13)
- **Framework:** FastAPI >= 0.110.0
- **Validation:** Pydantic >= 2.6.0
- **Database:** MongoDB 6.0+ (tested on MongoDB 9.0.2)
- **Driver:** PyMongo >= 4.7.0 (with `tz_aware=True`)
- **Server:** Uvicorn >= 0.29.0
- **Testing:** Pytest >= 8.0.0, HTTPX >= 0.27.0

---

## 3. Repository Structure

```text
.
├── app/
│   ├── __init__.py
│   └── main.py                 # Complete application implementation
├── tests/
│   ├── conftest.py             # Isolated test fixtures and MongoDB client
│   ├── test_health.py          # /health readiness probe tests
│   ├── test_employees.py       # Employee creation & listing tests
│   ├── test_attendance.py      # Punch-in, attendance listing & R1/R2 tests
│   ├── test_punch_out.py       # Punch-out, work hours & R3/R4/R5 tests
│   ├── test_regularize.py      # Manual correction & audit trail tests
│   ├── test_analytics.py       # Aggregation pipelines test suite
│   ├── test_explain.py         # MongoDB explain executionStats & IXSCAN tests
│   ├── test_concurrency.py     # Parallel race-condition tests
│   └── test_business_rules.py  # Half-up rounding, weekend rules & joiner tests
├── docs/
│   └── REQUIREMENTS_ANALYSIS.md# Formal requirements & business rule mapping
├── REVIEW.md                   # 30 detailed defects identified in the starter code
├── DECISIONS.md                # Architectural & design decisions (answers to 5 questions)
├── requirements.txt            # Runtime and testing dependencies
└── README.md                   # Project documentation & runbook
```

---

## 4. Attendance Business Rules (R1 – R10)

1. **R1 Time Zone & Attendance Date:** All shift times and dates are IST (UTC+05:30). A record's date is the IST calendar date of its `punch_in`. For overnight shifts (`shift_end <= shift_start`, e.g. 22:00–06:00), a punch-in before `shift_end` belongs to the shift starting the previous day. All instants are truncated to whole seconds before computation.
2. **R2 Late Calculation:** 10-minute grace period. If punch-in is strictly > 10:00 after shift start, `late_minutes = floor(minutes since shift_start)`; otherwise `0`. (e.g. for shift 09:30: 09:40:00 -> 0; 09:40:01 -> 10; 10:15:59 -> 45).
3. **R3 Overtime Calculation:** Whole minutes (floor) between `shift_end` and `punch_out`, counted only when >= 30; otherwise `0`. Overnight shifts end on the next calendar day.
4. **R4 Work Hours:** `(punch_out - punch_in) in seconds / 3600`, rounded half-up to 2 decimals using `Decimal` with `ROUND_HALF_UP`.
5. **R5 Half Day:** `half_day = true` when rounded `work_hours < 4.50`. Counts as `0.5` present day.
6. **R6 Presence:** `PRESENT`, `WFH`, and `ON_DUTY` count as present; `ABSENT` and `LEAVE` do not.
7. **R7 Working Days:** Monday through Friday. Mid-month joiners count working days from `joined_on`. Weekend records add to overtime and late totals, but do not count towards `present_days`.
8. **R8 Rounding:** 2 decimals half-up for counts/percentages/hours; 4 decimals half-up for rates (`attendance_rate`, `moving_avg_7d`).
9. **R9 Headcount:** All employees with `joined_on <= period_end`, whether or not they have any logs.
10. **R10 Pagination:** 1-indexed `page`. `page_size` max 100, default 20. `total` reflects filter count.

---

## 5. MongoDB Data Model & Indexes

### Collections
- **`employees`**: Stores unique employee profiles (`emp_code`, `name`, `email`, `department`, `shift_start`, `shift_end`, `joined_on`, `created_at`).
- **`attendance_logs`**: Natural key `(emp_code, date)`. Stores attendance logs, calculated metrics, and the `history` audit trail.

### Indexes Initialized at Startup
- `employees`:
  - `{"emp_code": 1}` (**unique**) — O(1) employee lookup and duplicate rejection.
  - `{"department": 1, "joined_on": 1}` — Supports department filtering and headcount counting.
  - `{"joined_on": 1}` — Supports monthly headcount evaluations.
- `attendance_logs`:
  - `{"emp_code": 1, "date": 1}` (**unique**) — Enforces at most one record per employee/date; makes punch-in race-safe.
  - `{"date": -1, "emp_code": 1}` — Powers sorted attendance listing and achieves `IXSCAN` without memory sort.
  - `{"date": 1, "late_minutes": 1}` — Accelerates leaderboard filtering.
  - `{"emp_code": 1, "punch_in": -1}` — Fast lookup for punch-out target matching.

---

## 6. API Endpoints

### System
- `GET /health` — Readiness probe: returns 200 `{"status": "ok"}` only if MongoDB answers a ping; else 503.

### Employees
- `POST /employees` — Creates an employee. Returns 201 Created, 409 Conflict (duplicate `emp_code`), or 422.
- `GET /employees` — Lists employees. Supports `department` filter, pagination, and sorting by `emp_code` ascending.

### Attendance
- `POST /attendance/punch-in` — Creates the day's record (R1, R2). Returns 201 Created, 404, 409, or 422.
- `POST /attendance/punch-out` — Closes open punch-in across midnight (R3, R4, R5). Returns 200, 404, 409, or 422.
- `GET /attendance` — Lists attendance records. Filters: `emp_code`, `date_from`, `date_to`, `status`. Sorted by `date` desc, `emp_code` asc.
- `PATCH /attendance/{emp_code}/{date}` — Regularizes a record. Recomputes derived fields and appends an audit entry to `history`.

### Analytics
- `GET /analytics/employees/{emp_code}/monthly` — Monthly summary for one employee (aggregation).
- `GET /analytics/departments/summary` — Cross-collection aggregation across `employees` and `attendance_logs`.
- `GET /analytics/leaderboard/late` — Late-comers ranking using `$setWindowFields` with standard competition ranking (`$rank`).
- `GET /analytics/departments/{department}/trend` — Daily time-series with gap filling (`$densify`) and 7-day moving average (`$setWindowFields`).

### Admin
- `GET /admin/explain/{endpoint}` — Runs MongoDB `explain("executionStats")` on the query/pipeline built identically to that endpoint.

---

## 7. Audit-Trail Design

- Recorded only by `PATCH /attendance/{emp_code}/{date}`. Punch-in and punch-out do not write to it.
- Each correction appends a single entry to `history`:
  - `at`: UTC instant of correction (BSON date in DB, epoch ms in API response).
  - `by`: The `regularized_by` actor identifier.
  - `reason`: Explanation string (5–200 characters).
  - `changes`: Key-value map containing only fields that **actually changed**, each with `{"from": ..., "to": ...}`.
- Concurrency: An optimistic lock against the array length (`history.{len}: {"$exists": False}`) prevents race conditions from silently losing audit entries.

---

## 8. Local Setup & Execution Instructions

### Prerequisites
- Python 3.11 or later
- MongoDB 6.0 or later running on `localhost:27017` (or remote MongoDB Atlas cluster)

### Setup & Installation
```bash
# 1. Create and activate virtual environment
python -m venv .venv
source .venv/bin/activate       # On Linux/macOS
# or: .\.venv\Scripts\Activate.ps1  # On Windows PowerShell

# 2. Install dependencies
pip install -r requirements.txt

# 3. (Optional) Configure environment variables
# export MONGO_URI="mongodb://localhost:27017"
# export MONGO_DB="attendance_db"

# 4. Start the application
uvicorn app.main:app --port 8000 --reload
```

### Access API Documentation
- Interactive Swagger UI: [http://localhost:8000/docs](http://localhost:8000/docs)
- OpenAPI Specification JSON: [http://localhost:8000/openapi.json](http://localhost:8000/openapi.json)

---

## 9. Running Automated Tests

Run the complete test suite:
```bash
pytest -v
```

All 36 tests execute against the test database and verify:
- API contracts, request validation, and status codes (200, 201, 404, 409, 422, 503).
- Business rules R1 through R10.
- Half-up rounding precision.
- Cross-collection aggregation accuracy.
- Leaderboard competition ranking with ties.
- Department trends with date densification and 7-day moving averages.
- Concurrency and race-safety.
- Index usage and `IXSCAN` query plans in the explain endpoint.

---

## 10. Verification of Evaluation Criteria

- [x] **Single-File Engine**: All application code resides in `app/main.py`.
- [x] **FastAPI & MongoDB 6.0+**: Compliant with MongoDB 6.0, 7.0, 8.0, and 9.0.
- [x] **No COLLSCAN**: List and analytics endpoints use index scans (`IXSCAN`).
- [x] **Starter Code Review**: 30 distinct defects thoroughly documented in `REVIEW.md`.
- [x] **Design Decisions**: Clear, concise explanations recorded in `DECISIONS.md`.
- [x] **Clean Repository**: No `.env` files, secrets, or Dockerfile committed.
