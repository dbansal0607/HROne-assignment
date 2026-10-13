# Requirements Analysis: HROne Employee Attendance & Analytics API

**Document Version:** 1.0  
**Specification Sources:**
- `PROBLEM_STATEMENT.docx` (HROne Software Engineer Hiring, Contract v2.0)
- `openapi.yaml` (Employee Attendance & Analytics API Contract v2.0.0)
- `DATA_MODEL.md` (MongoDB data model, collection specifications, BSON types)
- `candidate_kit/app/main.py` (Starter codebase)
- `candidate_kit/REVIEW.md` and `candidate_kit/DECISIONS.md` (Evaluation templates)

---

## 1. Scope & System Overview

The objective is to implement a production-grade, highly performant Employee Attendance & Analytics API using **Python 3.11+**, **FastAPI**, and **MongoDB 6.0+** (supporting 7.0/8.0/9.0).
The service is structured to run from the repository root via:
```bash
uvicorn app.main:app --port 8000
```
It connects to MongoDB using environment variables `MONGO_URI` (default: `mongodb://localhost:27017`) and `MONGO_DB` (default: `attendance_db`).

---

## 2. Core Business Rules (R1 - R10)

| Rule ID | Rule Name | Description & Invariants | Verification Test Case | Status |
|---|---|---|---|---|
| **R1** | Time Zone & Attendance Date | Times and dates are IST (UTC+05:30). A record's `date` (`YYYY-MM-DD`) is the IST calendar date of its `punch_in` instant.<br>**Overnight Shift Exception**: If `shift_end <= shift_start` (e.g., 22:00 - 06:00), a punch-in whose IST time is *earlier than* `shift_end` belongs to the shift that started the previous calendar day. The shift ends on the next calendar day.<br>**Truncation**: Instants are truncated to whole seconds (e.g. 09:40:00.900 -> 09:40:00) before any calculation, stored, and returned. | `test_r1_overnight_shift_date`, `test_r1_timestamp_truncation` | Completed & Verified |
| **R2** | Late Calculation | Grace period is 10 minutes. A punch-in is late only if strictly more than 10:00 after `shift_start`.<br>When late: `late_minutes = floor(minutes elapsed since shift_start)` (not since grace period ended). Otherwise `0`.<br>Examples (shift 09:30):<br>- 09:40:00 -> 0 min<br>- 09:40:01 -> 10 min<br>- 10:15:59 -> 45 min | `test_r2_late_grace_boundaries` | Completed & Verified |
| **R3** | Overtime Calculation | `overtime_minutes = floor(minutes elapsed between shift_end and punch_out)`, counted only if >= 30; otherwise `0`.<br>For overnight shifts, `shift_end` falls on the next calendar day after `date`. | `test_r3_overtime_thresholds` | Completed & Verified |
| **R4** | Work Hours Calculation | `(punch_out - punch_in) in seconds / 3600`, rounded to 2 decimals **half-up** (using `decimal.Decimal` with `ROUND_HALF_UP`, not banker's rounding). `null` until punch-out. | `test_r4_work_hours_half_up` | Completed & Verified |
| **R5** | Half Day Calculation | After punch-out, `half_day = true` when rounded `work_hours < 4.50`. A half day counts as `0.5` of a present day. Missing in legacy data is treated as `false`. | `test_r5_half_day_flag` | Completed & Verified |
| **R6** | Presence Statuses | `PRESENT`, `WFH`, and `ON_DUTY` count as present.<br>`ABSENT` and `LEAVE` do not count as present. | `test_r6_presence_counting` | Completed & Verified |
| **R7** | Working Days & Presence Counting | Working days are Monday through Friday (no holiday calendar).<br>Mid-month joiners: working days are counted starting from `joined_on`.<br>`present_days` only counts records that fall on Mon-Fri (weekend records still add to late and overtime totals, but not present_days). | `test_r7_working_days_and_weekend_records` | Completed & Verified |
| **R8** | Reported Number Rounding | 2 decimals half-up for counts/percentages/hours; 4 decimals half-up for rates (`attendance_rate`, `moving_avg_7d`). | `test_r8_rounding_precision` | Completed & Verified |
| **R9** | Headcount Calculation | All employees with `joined_on <= end_of_period` (or given day for trend), **whether or not they have any logs**. | `test_r9_headcount_zero_logs_and_joiners` | Completed & Verified |
| **R10** | Pagination | `page` starts at 1 (1-indexed). `page_size` default 20, max 100. `total` always reflects the applied filter count. | `test_r10_pagination_invariants` | Completed & Verified |

---

## 3. API Endpoints Specification & Verification Matrix

### 3.1 System
- **`GET /health`**
  - Contract: 200 `{"status": "ok"}` only when MongoDB answers ping (`db.command('ping')`); else 503 `{"detail": ...}`.
  - Validation: Verify ping failure returns 503 and live connection returns 200.

### 3.2 Employees
- **`POST /employees`**
  - Contract: 201 Created `Employee`, 409 Conflict if duplicate `emp_code`, 422 if invalid.
  - Invariants: Unique index on `emp_code`. `shift_start != shift_end` (422 if equal). `created_at` set by server (epoch ms / BSON UTC datetime).
- **`GET /employees`**
  - Contract: 200 `EmployeePage` (`items`, `total`, `page`, `page_size`).
  - Invariants: Filter by `department` (exact, case-sensitive), sorted by `emp_code` ascending. `total` reflects filter. Page 1-indexed.

### 3.3 Attendance Operations
- **`POST /attendance/punch-in`**
  - Contract: 201 Created `AttendanceRecord`, 404 unknown employee, 409 already punched in for this date, 422 invalid input.
  - Invariants: Atomic / compound unique index on `(emp_code, date)` guarantees race-safety: exactly one 201 and rest 409. Status must be `PRESENT`, `WFH`, or `ON_DUTY`. `punched_at` in epoch ms (reject seconds, floats, strings with 422). Instant truncated to whole seconds (R1).
- **`POST /attendance/punch-out`**
  - Contract: 200 OK `AttendanceRecord`, 404 unknown employee or no open punch-in, 409 already punched out, 422 invalid timestamps (`punched_at <= punch_in` or > 24 hours after).
  - Invariants: Closes most recent record whose `punch_in <= punched_at`. Works across midnight. Atomic update (`find_one_and_update` with filter `{"emp_code": ..., "date": ..., "punch_out": None}`) ensures concurrent requests yield exactly one 200 and rest 409. Computes `work_hours` (R4), `overtime_minutes` (R3), `half_day` (R5).
- **`GET /attendance`**
  - Contract: 200 `AttendancePage`.
  - Invariants: Filters: `emp_code`, `date_from`, `date_to`, `status`. Validation: `date_from > date_to` -> 422. Sorted by `date` descending, then `emp_code` ascending. Must use database cursor skip/limit (IXSCAN, no memory bloat on 100k records).
- **`PATCH /attendance/{emp_code}/{date}`**
  - Contract: 200 `AttendanceRecord`, 404 not found, 409 conflict, 422 validation.
  - Invariants: Natural key `(emp_code, date)`. Reason (5-200 chars) and `regularized_by` (1-50 chars). Setting ABSENT/LEAVE clears punch times (error 422 if punch times provided). Presence status requires valid punch_in. Punch_in must remain on same attendance date (R1). Recomputes derived fields. Appends single audit entry to `history`. Concurrency: optimistic locking against history length prevents lost audit trails.

### 3.4 Analytics (MongoDB Aggregation Pipelines)
- **`GET /analytics/employees/{emp_code}/monthly`**
  - Aggregation pipeline over `attendance_logs`. Computes `working_days` (R7 from `joined_on`), `present_days` (Mon-Fri only, half_day=0.5), `leave_days`, `late_count`, `total_late_minutes`, `total_overtime_minutes`, `attendance_pct` (R8 rounding). 404 if employee missing.
- **`GET /analytics/departments/summary`**
  - Cross-collection aggregation starting from `employees` or `$lookup`.
  - Headcount includes employees with zero logs (R9). Excludes employees who haven't joined yet (`joined_on > last_day_of_month`).
  - `avg_work_hours`: mean over records that are present and have `work_hours != null`. Null if none.
  - Groups by department, sorted by `department` ascending.
- **`GET /analytics/leaderboard/late`**
  - Aggregation with window function `$setWindowFields` using `$rank`.
  - Competition ranking (1, 2, 2, 4 ties). Limit applied AFTER ranking (`rank <= limit`). Ties at cutoff all included.
  - Rows ordered by `total_late_minutes` descending, then `emp_code` ascending.
- **`GET /analytics/departments/{department}/trend`**
  - Exactly one row per calendar day from `from` to `to` inclusive (calendar generation via `$densify` or range array unwind).
  - Mon-Fri `is_working_day`. `headcount` per day (R9). `attendance_rate` on working days with headcount > 0 (null otherwise, 0.0 if 0 present).
  - 7-day moving average `moving_avg_7d` over non-null rates in window `[-6, 0]` within range. 4 decimals half-up. 404 for unknown department, 422 if range > 92 days or `to < from`.

### 3.5 Admin
- **`GET /admin/explain/{endpoint}`**
  - Runs MongoDB `.explain("executionStats")` on the query/pipeline built identically to the target endpoint.
  - Verifies presence of `IXSCAN` and absence of `COLLSCAN`.

---

## 4. Implementation Checklist

- [x] Requirements Analysis & Specification Alignment (`docs/REQUIREMENTS_ANALYSIS.md`)
- [x] Environment Setup & Dependency Installation (`requirements.txt`, `.venv`)
- [x] Database Connection & Startup Index Initialization
- [x] Starter Code Review & Bug Identification (`REVIEW.md`)
- [x] Core Helper Utilities & Business Rule Math (`R1`-`R10`)
- [x] Pydantic Schemas & Request/Response Validation
- [x] CRUD & Attendance Endpoints (`/health`, `/employees`, `/attendance/punch-in`, `/attendance/punch-out`, `/attendance`, `/attendance/{emp_code}/{date}`)
- [x] Analytics Endpoints with Pure Aggregation Pipelines (`employee_monthly`, `department_summary`, `late_leaderboard`, `department_trend`)
- [x] Explain Endpoint (`/admin/explain/{endpoint}`)
- [x] Engineering Decisions Documentation (`DECISIONS.md`)
- [x] Test Suite Implementation (`tests/`)
- [x] Local Verification & Benchmarking
- [x] Comprehensive Project Documentation (`README.md`)
