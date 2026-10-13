"""
Employee Attendance & Analytics API - Production Implementation
Contract: v2.0.0 | FastAPI + MongoDB 6.0+

Run: uvicorn app.main:app --port 8000
"""

from __future__ import annotations

import calendar
import math
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Optional, Union

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

# --------------------------------------------------------------------------- #
# Constants & Timezone Configuration
# --------------------------------------------------------------------------- #
IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

# Global MongoDB references managed by lifespan
client: Optional[MongoClient] = None
db: Any = None


# --------------------------------------------------------------------------- #
# Helper Functions & Business Rules (R1 - R8)
# --------------------------------------------------------------------------- #
def epoch_ms_to_datetime(ms: int) -> datetime:
    """Convert integer epoch milliseconds to timezone-aware UTC datetime."""
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)


def datetime_to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    """Convert datetime (naive or aware) to integer epoch milliseconds."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def truncate_to_seconds_ms(ms: int) -> int:
    """R1: Instants are truncated to whole seconds before any calculation."""
    return (ms // 1000) * 1000


def get_ist_datetime(dt: datetime) -> datetime:
    """Convert datetime to IST timezone."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST)


def resolve_attendance_date(punch_in_ms: int, shift_start_str: str, shift_end_str: str) -> str:
    """
    R1: Time zone and attendance date.
    Shift times and attendance dates are IST (UTC+05:30).
    A record's date is the IST calendar date of its punch_in instant.
    Overnight shifts (shift_end <= shift_start, e.g. 22:00 - 06:00):
    a punch-in earlier than shift_end belongs to the shift that started
    the previous calendar day.
    """
    punch_in_dt_ist = get_ist_datetime(epoch_ms_to_datetime(punch_in_ms))
    punch_in_date = punch_in_dt_ist.date()
    punch_in_time = punch_in_dt_ist.time()

    sh_start_h, sh_start_m = map(int, shift_start_str.split(":"))
    sh_end_h, sh_end_m = map(int, shift_end_str.split(":"))

    is_overnight = (sh_end_h, sh_end_m) <= (sh_start_h, sh_start_m)
    if is_overnight and (punch_in_time.hour, punch_in_time.minute, punch_in_time.second) < (sh_end_h, sh_end_m, 0):
        attendance_date = punch_in_date - timedelta(days=1)
    else:
        attendance_date = punch_in_date

    return attendance_date.isoformat()


def compute_late_minutes(punch_in_ms: int, attendance_date_str: str, shift_start_str: str) -> int:
    """
    R2: Late. Grace period 10 minutes.
    Late only if punch-in is strictly more than 10:00 after shift_start;
    then late_minutes = whole minutes (floor) since shift_start, not since
    grace period ended. Shift 09:30: 09:40:00 -> 0; 09:40:01 -> 10; 10:15:59 -> 45.
    """
    attendance_date = date.fromisoformat(attendance_date_str)
    sh_h, sh_m = map(int, shift_start_str.split(":"))
    shift_start_dt_ist = datetime.combine(attendance_date, time(sh_h, sh_m), tzinfo=IST)
    punch_in_dt_ist = get_ist_datetime(epoch_ms_to_datetime(punch_in_ms))

    diff_sec = int((punch_in_dt_ist - shift_start_dt_ist).total_seconds())
    if diff_sec > 600:
        return diff_sec // 60
    return 0


def compute_overtime_minutes(punch_out_ms: int, attendance_date_str: str, shift_start_str: str, shift_end_str: str) -> int:
    """
    R3: Overtime.
    Whole minutes (floor) from shift_end to punch_out, counted only if that is >= 30; otherwise 0.
    For overnight shifts, shift_end falls on the next calendar day.
    """
    attendance_date = date.fromisoformat(attendance_date_str)
    sh_start_h, sh_start_m = map(int, shift_start_str.split(":"))
    sh_end_h, sh_end_m = map(int, shift_end_str.split(":"))

    is_overnight = (sh_end_h, sh_end_m) <= (sh_start_h, sh_start_m)
    if is_overnight:
        shift_end_date = attendance_date + timedelta(days=1)
    else:
        shift_end_date = attendance_date

    shift_end_dt_ist = datetime.combine(shift_end_date, time(sh_end_h, sh_end_m), tzinfo=IST)
    punch_out_dt_ist = get_ist_datetime(epoch_ms_to_datetime(punch_out_ms))

    diff_sec = int((punch_out_dt_ist - shift_end_dt_ist).total_seconds())
    minutes = diff_sec // 60
    if minutes >= 30:
        return minutes
    return 0


def compute_work_hours(punch_in_ms: int, punch_out_ms: int) -> float:
    """
    R4: Work hours.
    (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up.
    """
    seconds = (punch_out_ms - punch_in_ms) / 1000.0
    raw_hours = Decimal(str(seconds)) / Decimal("3600")
    rounded = raw_hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(rounded)


def compute_half_day(work_hours: Optional[float]) -> bool:
    """
    R5: Half day.
    After punch-out, half_day is true when the rounded work_hours < 4.50.
    """
    if work_hours is None:
        return False
    return work_hours < 4.50


def round_half_up(val: Optional[Union[float, Decimal]], decimals: int = 2) -> Optional[float]:
    """R8: Rounding of reported numbers half-up."""
    if val is None:
        return None
    d = Decimal(str(val))
    target = Decimal("0." + "0" * (decimals - 1) + "1") if decimals > 0 else Decimal("1")
    return float(d.quantize(target, rounding=ROUND_HALF_UP))


def last_day_of_month(month_str: str) -> str:
    """Return the last date YYYY-MM-DD for a month YYYY-MM."""
    y, m = map(int, month_str.split("-"))
    _, last_day = calendar.monthrange(y, m)
    return f"{month_str}-{last_day:02d}"


def count_working_days(joined_on_str: str, month_str: str) -> int:
    """
    R7: Working days Monday - Friday, no holiday calendar.
    For an employee who joined mid-month, working days are counted from joined_on.
    """
    start_date_str = f"{month_str}-01"
    end_date_str = last_day_of_month(month_str)

    if joined_on_str > end_date_str:
        return 0

    cur = date.fromisoformat(max(start_date_str, joined_on_str))
    end = date.fromisoformat(end_date_str)

    working_days = 0
    while cur <= end:
        if cur.weekday() < 5:  # Monday to Friday
            working_days += 1
        cur += timedelta(days=1)
    return working_days


# --------------------------------------------------------------------------- #
# Document Formatters (MongoDB BSON <-> API Schemas)
# --------------------------------------------------------------------------- #
def format_employee_doc(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": datetime_to_epoch_ms(doc.get("created_at")),
    }


def format_attendance_doc(doc: dict) -> dict:
    history_entries = []
    for h in doc.get("history", []):
        formatted_changes = {}
        for k, v in h.get("changes", {}).items():
            from_v = v.get("from")
            to_v = v.get("to")
            if k in ("punch_in", "punch_out"):
                if isinstance(from_v, datetime):
                    from_v = datetime_to_epoch_ms(from_v)
                if isinstance(to_v, datetime):
                    to_v = datetime_to_epoch_ms(to_v)
            formatted_changes[k] = {"from": from_v, "to": to_v}

        at_val = h.get("at")
        if isinstance(at_val, datetime):
            at_val = datetime_to_epoch_ms(at_val)

        history_entries.append({
            "at": at_val,
            "by": h.get("by", ""),
            "reason": h.get("reason", ""),
            "changes": formatted_changes,
        })

    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": datetime_to_epoch_ms(doc.get("punch_in")),
        "punch_out": datetime_to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": history_entries,
    }


# --------------------------------------------------------------------------- #
# Pydantic Schemas matching openapi.yaml
# --------------------------------------------------------------------------- #
def validate_epoch_ms(val: Any) -> int:
    if val is None:
        return val
    if isinstance(val, bool) or not isinstance(val, int):
        raise ValueError("Must be integer epoch milliseconds")
    if val < 100000000000 or val > 4102444800000:
        raise ValueError("Epoch milliseconds out of valid range")
    return val


class EmployeeCreate(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")

    @model_validator(mode="after")
    def validate_shift_times(self) -> EmployeeCreate:
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class Employee(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str
    shift_end: str
    joined_on: str
    created_at: int


class EmployeePage(BaseModel):
    items: List[Employee]
    total: int
    page: int
    page_size: int


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"

    @field_validator("punched_at")
    @classmethod
    def check_punched_at(cls, v: Any) -> Optional[int]:
        return validate_epoch_ms(v)

    @field_validator("status")
    @classmethod
    def check_status(cls, v: str) -> str:
        if v not in ("PRESENT", "WFH", "ON_DUTY"):
            raise ValueError("Status at punch-in must be PRESENT, WFH, or ON_DUTY")
        return v


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None

    @field_validator("punched_at")
    @classmethod
    def check_punched_at(cls, v: Any) -> Optional[int]:
        return validate_epoch_ms(v)


class RegularizeRequest(BaseModel):
    status: Optional[str] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)

    @field_validator("punch_in")
    @classmethod
    def check_punch_in(cls, v: Any) -> Optional[int]:
        return validate_epoch_ms(v)

    @field_validator("punch_out")
    @classmethod
    def check_punch_out(cls, v: Any) -> Optional[int]:
        return validate_epoch_ms(v)

    @field_validator("status")
    @classmethod
    def check_status(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in ("PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"):
            raise ValueError("Invalid status value")
        return v


class HistoryEntry(BaseModel):
    at: int
    by: str
    reason: str
    changes: Dict[str, Dict[str, Any]]


class AttendanceRecord(BaseModel):
    emp_code: str
    date: str
    status: str
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    work_hours: Optional[float] = None
    late_minutes: int
    overtime_minutes: int
    half_day: bool
    history: List[HistoryEntry]


class AttendancePage(BaseModel):
    items: List[AttendanceRecord]
    total: int
    page: int
    page_size: int


class EmployeeMonthly(BaseModel):
    emp_code: str
    month: str
    working_days: int
    present_days: float
    leave_days: int
    late_count: int
    total_late_minutes: int
    total_overtime_minutes: int
    attendance_pct: Optional[float] = None


class DepartmentSummaryItem(BaseModel):
    department: str
    headcount: int
    present_days: float
    avg_work_hours: Optional[float] = None
    late_count: int
    total_late_minutes: int
    leave_count: int
    on_duty_count: int


class DepartmentSummary(BaseModel):
    month: str
    items: List[DepartmentSummaryItem]


class LeaderboardItem(BaseModel):
    rank: int
    emp_code: str
    name: str
    department: str
    total_late_minutes: int
    late_count: int


class Leaderboard(BaseModel):
    month: str
    items: List[LeaderboardItem]


class TrendItem(BaseModel):
    date: str
    is_working_day: bool
    headcount: int
    present_count: float
    late_count: int
    attendance_rate: Optional[float] = None
    moving_avg_7d: Optional[float] = None


class Trend(BaseModel):
    department: str
    items: List[TrendItem]


class ExplainResponse(BaseModel):
    endpoint: str
    collection: str
    explain: Dict[str, Any]


# --------------------------------------------------------------------------- #
# Application Lifespan & Index Creation
# --------------------------------------------------------------------------- #
def init_indexes(database: Any) -> None:
    """Ensure all required indexes exist idempotently at startup."""
    # employees indexes
    database.employees.create_index([("emp_code", ASCENDING)], unique=True, name="idx_employees_emp_code_unique")
    database.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="idx_employees_dept_joined")
    database.employees.create_index([("joined_on", ASCENDING)], name="idx_employees_joined_on")

    # attendance_logs indexes
    database.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True,
        name="idx_attendance_emp_date_unique"
    )
    database.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)],
        name="idx_attendance_date_desc_emp_asc"
    )
    database.attendance_logs.create_index(
        [("date", ASCENDING), ("late_minutes", ASCENDING)],
        name="idx_attendance_date_late"
    )
    database.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("punch_in", DESCENDING)],
        name="idx_attendance_emp_punch_in_desc"
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    global client, db
    uri = os.getenv("MONGO_URI", "mongodb://localhost:27017")
    db_name = os.getenv("MONGO_DB", "attendance_db")
    client = MongoClient(uri, tz_aware=True, serverSelectionTimeoutMS=5000)
    db = client[db_name]
    try:
        init_indexes(db)
    except Exception:
        pass
    yield
    if client is not None:
        try:
            client.close()
        except Exception:
            pass
        client = None
        db = None


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    description="Production-grade API for HROne Engineering Assignment",
    lifespan=lifespan,
)


def get_db():
    global client, db
    if client is None or getattr(client, "_closed", False):
        uri = os.getenv("MONGO_URI", "mongodb://localhost:27017")
        db_name = os.getenv("MONGO_DB", "attendance_db")
        client = MongoClient(uri, tz_aware=True, serverSelectionTimeoutMS=5000)
        db = client[db_name]
    return db


# --------------------------------------------------------------------------- #
# Endpoints: System & Health
# --------------------------------------------------------------------------- #
@app.get("/health", tags=["system"], summary="Readiness probe")
def health():
    database = get_db()
    try:
        database.command("ping")
        return {"status": "ok"}
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Database unreachable: {str(exc)}"
        )


# --------------------------------------------------------------------------- #
# Endpoints: Employees
# --------------------------------------------------------------------------- #
@app.post("/employees", response_model=Employee, status_code=status.HTTP_201_CREATED, tags=["employees"])
def create_employee(body: EmployeeCreate):
    database = get_db()
    now_ms = truncate_to_seconds_ms(int(datetime.now(timezone.utc).timestamp() * 1000))
    now_dt = epoch_ms_to_datetime(now_ms)

    doc = body.model_dump()
    doc["created_at"] = now_dt

    try:
        database.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="emp_code already exists")

    return format_employee_doc(doc)


@app.get("/employees", response_model=EmployeePage, tags=["employees"])
def list_employees(
    department: Optional[str] = Query(None, description="Exact, case-sensitive match."),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    database = get_db()
    q: Dict[str, Any] = {}
    if department:
        q["department"] = department

    total = database.employees.count_documents(q)
    skip = (page - 1) * page_size
    cursor = database.employees.find(q).sort("emp_code", ASCENDING).skip(skip).limit(page_size)
    items = [format_employee_doc(d) for d in cursor]

    return {"items": items, "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Endpoints: Attendance Operations
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", response_model=AttendanceRecord, status_code=status.HTTP_201_CREATED, tags=["attendance"])
def punch_in(body: PunchInRequest):
    database = get_db()
    emp = database.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")

    now_raw = int(datetime.now(timezone.utc).timestamp() * 1000)
    ts_ms = truncate_to_seconds_ms(body.punched_at if body.punched_at is not None else now_raw)

    attendance_date = resolve_attendance_date(ts_ms, emp["shift_start"], emp["shift_end"])

    # Duplicate check
    existing = database.attendance_logs.find_one({"emp_code": body.emp_code, "date": attendance_date})
    if existing:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="already punched in for this date")

    late_min = compute_late_minutes(ts_ms, attendance_date, emp["shift_start"])

    doc = {
        "emp_code": body.emp_code,
        "date": attendance_date,
        "status": body.status,
        "punch_in": epoch_ms_to_datetime(ts_ms),
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_min,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        database.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="already punched in for this date")

    return format_attendance_doc(doc)


@app.post("/attendance/punch-out", response_model=AttendanceRecord, tags=["attendance"])
def punch_out(body: PunchOutRequest):
    database = get_db()
    emp = database.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")

    now_raw = int(datetime.now(timezone.utc).timestamp() * 1000)
    ts_ms = truncate_to_seconds_ms(body.punched_at if body.punched_at is not None else now_raw)
    ts_dt = epoch_ms_to_datetime(ts_ms)

    # Closes employee's most recent record whose punch_in is at or before punched_at
    record = database.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts_dt}},
        sort=[("punch_in", DESCENDING)]
    )
    if not record:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No matching punch-in found")

    if record.get("punch_out") is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Record is already punched out")

    punch_in_ms = datetime_to_epoch_ms(record["punch_in"])
    if punch_in_ms is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No punch-in recorded")

    # Timestamp validity: punched_at <= punch_in or > 24 hours after
    if ts_ms <= punch_in_ms:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="punch_out must be strictly greater than punch_in")

    if ts_ms > punch_in_ms + (24 * 3600 * 1000):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="punch_out cannot be more than 24 hours after punch_in")

    work_hours = compute_work_hours(punch_in_ms, ts_ms)
    overtime_min = compute_overtime_minutes(ts_ms, record["date"], emp["shift_start"], emp["shift_end"])
    half_day = compute_half_day(work_hours)

    # Atomic update to guarantee concurrency safety
    updated = database.attendance_logs.find_one_and_update(
        {"emp_code": body.emp_code, "date": record["date"], "punch_out": None},
        {
            "$set": {
                "punch_out": ts_dt,
                "work_hours": work_hours,
                "overtime_minutes": overtime_min,
                "half_day": half_day,
            }
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Record was punched out concurrently")

    return format_attendance_doc(updated)


@app.get("/attendance", response_model=AttendancePage, tags=["attendance"])
def list_attendance(
    emp_code: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    status_filter: Optional[str] = Query(None, alias="status"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    database = get_db()
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="date_from must not be greater than date_to")

    q: Dict[str, Any] = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status_filter:
        q["status"] = status_filter

    total = database.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    cursor = (
        database.attendance_logs.find(q)
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip(skip)
        .limit(page_size)
    )
    items = [format_attendance_doc(d) for d in cursor]

    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date_str}", response_model=AttendanceRecord, tags=["attendance"])
def regularize_attendance(
    emp_code: str = Path(...),
    date_str: str = Path(..., alias="date_str"),
    body: RegularizeRequest = ...,
):
    database = get_db()
    emp = database.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")

    record = database.attendance_logs.find_one({"emp_code": emp_code, "date": date_str})
    if not record:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Attendance record not found for date")

    curr_status = record.get("status")
    curr_punch_in_ms = datetime_to_epoch_ms(record.get("punch_in"))
    curr_punch_out_ms = datetime_to_epoch_ms(record.get("punch_out"))
    curr_work_hours = record.get("work_hours")
    curr_late_minutes = record.get("late_minutes", 0)
    curr_overtime_minutes = record.get("overtime_minutes", 0)
    curr_half_day = record.get("half_day", False)

    target_status = body.status if body.status is not None else curr_status

    if target_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Cannot supply punch_in or punch_out when setting status to ABSENT or LEAVE"
            )
        target_punch_in_ms = None
        target_punch_out_ms = None
        target_work_hours = None
        target_late_minutes = 0
        target_overtime_minutes = 0
        target_half_day = False
    else:
        target_punch_in_ms = (
            truncate_to_seconds_ms(body.punch_in)
            if body.punch_in is not None
            else curr_punch_in_ms
        )
        target_punch_out_ms = (
            truncate_to_seconds_ms(body.punch_out)
            if body.punch_out is not None
            else curr_punch_out_ms
        )

        if target_punch_in_ms is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Presence status requires a punch_in"
            )

        calc_date = resolve_attendance_date(target_punch_in_ms, emp["shift_start"], emp["shift_end"])
        if calc_date != date_str:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"punch_in belongs to attendance date {calc_date}, but target record date is {date_str}"
            )

        if target_punch_out_ms is not None:
            if target_punch_out_ms <= target_punch_in_ms:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="punch_out must be strictly greater than punch_in"
                )
            if target_punch_out_ms > target_punch_in_ms + (24 * 3600 * 1000):
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="punch_out must be within 24 hours of punch_in"
                )

        target_late_minutes = compute_late_minutes(target_punch_in_ms, date_str, emp["shift_start"])
        if target_punch_out_ms is not None:
            target_work_hours = compute_work_hours(target_punch_in_ms, target_punch_out_ms)
            target_overtime_minutes = compute_overtime_minutes(target_punch_out_ms, date_str, emp["shift_start"], emp["shift_end"])
            target_half_day = compute_half_day(target_work_hours)
        else:
            target_work_hours = None
            target_overtime_minutes = 0
            target_half_day = False

    # Compute exact field changes
    changes: Dict[str, Dict[str, Any]] = {}
    mongo_changes: Dict[str, Dict[str, Any]] = {}

    if curr_status != target_status:
        changes["status"] = {"from": curr_status, "to": target_status}
        mongo_changes["status"] = {"from": curr_status, "to": target_status}

    if curr_punch_in_ms != target_punch_in_ms:
        changes["punch_in"] = {"from": curr_punch_in_ms, "to": target_punch_in_ms}
        mongo_changes["punch_in"] = {
            "from": epoch_ms_to_datetime(curr_punch_in_ms) if curr_punch_in_ms else None,
            "to": epoch_ms_to_datetime(target_punch_in_ms) if target_punch_in_ms else None,
        }

    if curr_punch_out_ms != target_punch_out_ms:
        changes["punch_out"] = {"from": curr_punch_out_ms, "to": target_punch_out_ms}
        mongo_changes["punch_out"] = {
            "from": epoch_ms_to_datetime(curr_punch_out_ms) if curr_punch_out_ms else None,
            "to": epoch_ms_to_datetime(target_punch_out_ms) if target_punch_out_ms else None,
        }

    if curr_work_hours != target_work_hours:
        changes["work_hours"] = {"from": curr_work_hours, "to": target_work_hours}
        mongo_changes["work_hours"] = {"from": curr_work_hours, "to": target_work_hours}

    if curr_late_minutes != target_late_minutes:
        changes["late_minutes"] = {"from": curr_late_minutes, "to": target_late_minutes}
        mongo_changes["late_minutes"] = {"from": curr_late_minutes, "to": target_late_minutes}

    if curr_overtime_minutes != target_overtime_minutes:
        changes["overtime_minutes"] = {"from": curr_overtime_minutes, "to": target_overtime_minutes}
        mongo_changes["overtime_minutes"] = {"from": curr_overtime_minutes, "to": target_overtime_minutes}

    if curr_half_day != target_half_day:
        changes["half_day"] = {"from": curr_half_day, "to": target_half_day}
        mongo_changes["half_day"] = {"from": curr_half_day, "to": target_half_day}

    if not changes:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Request resulted in no changes to the attendance record"
        )

    now_raw = int(datetime.now(timezone.utc).timestamp() * 1000)
    now_ms = truncate_to_seconds_ms(now_raw)
    now_dt = epoch_ms_to_datetime(now_ms)

    history_entry = {
        "at": now_dt,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": mongo_changes,
    }

    # Optimistic locking against history length to avoid losing concurrent history entries
    curr_hist_len = len(record.get("history", []))
    match_query = {
        "emp_code": emp_code,
        "date": date_str,
        f"history.{curr_hist_len}": {"$exists": False},
    }

    update_payload = {
        "$set": {
            "status": target_status,
            "punch_in": epoch_ms_to_datetime(target_punch_in_ms) if target_punch_in_ms else None,
            "punch_out": epoch_ms_to_datetime(target_punch_out_ms) if target_punch_out_ms else None,
            "work_hours": target_work_hours,
            "late_minutes": target_late_minutes,
            "overtime_minutes": target_overtime_minutes,
            "half_day": target_half_day,
        },
        "$push": {"history": history_entry},
    }

    res = database.attendance_logs.update_one(match_query, update_payload)
    if res.matched_count == 0:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Concurrent modification detected on attendance record"
        )

    updated_doc = database.attendance_logs.find_one({"emp_code": emp_code, "date": date_str})
    return format_attendance_doc(updated_doc)


# --------------------------------------------------------------------------- #
# Pipelines & Analytics Endpoints (MongoDB Aggregation Pipelines)
# --------------------------------------------------------------------------- #
def build_employee_monthly_pipeline(emp_code: str, month: str) -> list:
    start_date = f"{month}-01"
    end_date = last_day_of_month(month)
    return [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": start_date, "$lte": end_date},
            }
        },
        {
            "$project": {
                "date": 1,
                "status": 1,
                "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
                "half_day": {"$ifNull": ["$half_day", False]},
                "dayOfWeek": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$in": ["$dayOfWeek", [2, 3, 4, 5, 6]]},
                                ]
                            },
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"},
            }
        },
    ]


@app.get("/analytics/employees/{emp_code}/monthly", response_model=EmployeeMonthly, tags=["analytics"])
def employee_monthly(
    emp_code: str = Path(...),
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    database = get_db()
    emp = database.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")

    working_days = count_working_days(emp["joined_on"], month)
    pipe = build_employee_monthly_pipeline(emp_code, month)
    agg_res = list(database.attendance_logs.aggregate(pipe))

    if agg_res:
        row = agg_res[0]
        present_days = float(row["present_days"])
        leave_days = int(row["leave_days"])
        late_count = int(row["late_count"])
        total_late = int(row["total_late_minutes"])
        total_ot = int(row["total_overtime_minutes"])
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late = 0
        total_ot = 0

    if working_days > 0:
        pct = round_half_up((present_days / working_days) * 100.0, 2)
    else:
        pct = None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": round_half_up(present_days, 2),
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late,
        "total_overtime_minutes": total_ot,
        "attendance_pct": pct,
    }


def build_department_summary_pipeline(month: str, department: Optional[str] = None) -> list:
    start_date = f"{month}-01"
    end_date = last_day_of_month(month)

    emp_match: Dict[str, Any] = {"joined_on": {"$lte": end_date}}
    if department:
        emp_match["department"] = department

    logs_match: Dict[str, Any] = {"date": {"$gte": start_date, "$lte": end_date}}

    inner_pipeline: list = [
        {"$match": logs_match},
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
        {"$match": {"emp.joined_on": {"$lte": end_date}}},
    ]

    if department:
        inner_pipeline.append({"$match": {"emp.department": department}})

    inner_pipeline.append({
        "$project": {
            "department": "$emp.department",
            "is_emp": {"$literal": 0},
            "present_days": {
                "$cond": [
                    {
                        "$and": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {
                                "$in": [
                                    {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
                                    [2, 3, 4, 5, 6],
                                ]
                            },
                        ]
                    },
                    {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1.0]},
                    0.0,
                ]
            },
            "work_hours": {
                "$cond": [
                    {
                        "$and": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$ne": ["$work_hours", None]},
                        ]
                    },
                    "$work_hours",
                    None,
                ]
            },
            "late_count": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]},
            "total_late_minutes": {"$ifNull": ["$late_minutes", 0]},
            "leave_count": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]},
            "on_duty_count": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]},
        }
    })

    return [
        {"$match": emp_match},
        {
            "$project": {
                "department": 1,
                "is_emp": {"$literal": 1},
                "present_days": {"$literal": 0.0},
                "work_hours": {"$literal": None},
                "late_count": {"$literal": 0},
                "total_late_minutes": {"$literal": 0},
                "leave_count": {"$literal": 0},
                "on_duty_count": {"$literal": 0},
            }
        },
        {
            "$unionWith": {
                "coll": "attendance_logs",
                "pipeline": inner_pipeline,
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": "$is_emp"},
                "present_days": {"$sum": "$present_days"},
                "avg_work_hours": {"$avg": "$work_hours"},
                "late_count": {"$sum": "$late_count"},
                "total_late_minutes": {"$sum": "$total_late_minutes"},
                "leave_count": {"$sum": "$leave_count"},
                "on_duty_count": {"$sum": "$on_duty_count"},
            }
        },
        {"$match": {"headcount": {"$gt": 0}}},
        {"$sort": {"_id": ASCENDING}},
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": 1,
                "present_days": {"$round": ["$present_days", 2]},
                "avg_work_hours": {
                    "$cond": [
                        {"$eq": ["$avg_work_hours", None]},
                        None,
                        {"$round": ["$avg_work_hours", 2]},
                    ]
                },
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
    ]


@app.get("/analytics/departments/summary", response_model=DepartmentSummary, tags=["analytics"])
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = Query(None),
):
    database = get_db()
    pipe = build_department_summary_pipeline(month, department)
    items = list(database.employees.aggregate(pipe))
    return {"month": month, "items": items}


def build_late_leaderboard_pipeline(month: str, limit: int = 10, department: Optional[str] = None) -> list:
    start_date = f"{month}-01"
    end_date = last_day_of_month(month)

    pipe: list = [
        {
            "$match": {
                "date": {"$gte": start_date, "$lte": end_date},
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
    ]

    if department:
        pipe.append({"$match": {"emp.department": department}})

    pipe.extend([
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": DESCENDING},
                "output": {
                    "rank": {
                        "$rank": {}
                    }
                },
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": DESCENDING, "_id": ASCENDING}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ])
    return pipe


@app.get("/analytics/leaderboard/late", response_model=Leaderboard, tags=["analytics"])
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = Query(None),
):
    database = get_db()
    pipe = build_late_leaderboard_pipeline(month, limit, department)
    items = list(database.attendance_logs.aggregate(pipe))
    return {"month": month, "items": items}


def build_department_trend_pipeline(department: str, from_str: str, to_str: str) -> list:
    from_dt = datetime.fromisoformat(from_str).replace(tzinfo=timezone.utc)
    to_dt = datetime.fromisoformat(to_str).replace(tzinfo=timezone.utc)
    to_dt_plus_1 = to_dt + timedelta(days=1)

    return [
        {"$match": {"department": department}},
        {"$limit": 1},
        {
            "$project": {
                "d": {"$dateFromString": {"dateString": from_str}}
            }
        },
        {
            "$densify": {
                "field": "d",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [from_dt, to_dt_plus_1],
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$d"}},
                "dayOfWeek": {"$dayOfWeek": "$d"},
            }
        },
        {
            "$project": {
                "date": 1,
                "is_working_day": {"$in": ["$dayOfWeek", [2, 3, 4, 5, 6]]},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"cur_date": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$cur_date"]},
                                ]
                            }
                        }
                    },
                    {"$count": "count"},
                ],
                "as": "hc",
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"cur_date": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {"$eq": ["$date", "$$cur_date"]}
                        }
                    },
                    {
                        "$lookup": {
                            "from": "employees",
                            "localField": "emp_code",
                            "foreignField": "emp_code",
                            "as": "emp",
                        }
                    },
                    {"$unwind": "$emp"},
                    {
                        "$match": {
                            "emp.department": department
                        }
                    },
                    {
                        "$group": {
                            "_id": None,
                            "present_count": {
                                "$sum": {
                                    "$cond": [
                                        {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                        {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1.0]},
                                        0.0,
                                    ]
                                }
                            },
                            "late_count": {
                                "$sum": {
                                    "$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]
                                }
                            },
                        }
                    },
                ],
                "as": "logs",
            }
        },
        {
            "$project": {
                "date": 1,
                "is_working_day": 1,
                "headcount": {"$ifNull": [{"$first": "$hc.count"}, 0]},
                "present_count": {"$round": [{"$ifNull": [{"$first": "$logs.present_count"}, 0.0]}, 2]},
                "late_count": {"$ifNull": [{"$first": "$logs.late_count"}, 0]},
            }
        },
        {
            "$project": {
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                {"$eq": ["$is_working_day", True]},
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {
                            "$round": [
                                {"$divide": ["$present_count", "$headcount"]},
                                4,
                            ]
                        },
                        None,
                    ]
                },
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": ASCENDING},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {
                            "documents": [-6, 0]
                        },
                    }
                },
            }
        },
        {"$sort": {"date": ASCENDING}},
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": {
                    "$cond": [
                        {"$eq": ["$moving_avg_7d", None]},
                        None,
                        {"$round": ["$moving_avg_7d", 4]},
                    ]
                },
            }
        },
    ]


@app.get("/analytics/departments/{department}/trend", response_model=Trend, tags=["analytics"])
def department_trend(
    department: str = Path(...),
    from_date: str = Query(..., alias="from", pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    to_date: str = Query(..., alias="to", pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
):
    database = get_db()
    # Check department exists
    has_dept = database.employees.find_one({"department": department})
    if not has_dept:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown department")

    d_from = date.fromisoformat(from_date)
    d_to = date.fromisoformat(to_date)
    if d_to < d_from:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="'to' date must not be before 'from' date")

    diff_days = (d_to - d_from).days + 1
    if diff_days > 92:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Date range must not exceed 92 days")

    pipe = build_department_trend_pipeline(department, from_date, to_date)
    items = list(database.employees.aggregate(pipe))
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Endpoints: Admin & Explain
# --------------------------------------------------------------------------- #
def sanitize_bson_for_json(obj: Any) -> Any:
    """Sanitize explain BSON output into standard JSON serializable dicts."""
    import json
    return json.loads(json_util.dumps(obj))


@app.get("/admin/explain/{endpoint_name}", response_model=ExplainResponse, tags=["admin"])
def explain_endpoint(
    endpoint_name: str = Path(..., alias="endpoint_name"),
    emp_code: Optional[str] = Query(None),
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = Query(None),
    limit: Optional[int] = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    status_filter: Optional[str] = Query(None, alias="status"),
    from_date: Optional[str] = Query(None, alias="from", pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    to_date: Optional[str] = Query(None, alias="to", pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    database = get_db()
    valid_endpoints = [
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    ]
    if endpoint_name not in valid_endpoints:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid endpoint. Must be one of {valid_endpoints}"
        )

    if endpoint_name == "attendance_list":
        if date_from and date_to and date_from > date_to:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="date_from > date_to")
        q: Dict[str, Any] = {}
        if emp_code:
            q["emp_code"] = emp_code
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to
        if status_filter:
            q["status"] = status_filter

        skip = (page - 1) * page_size
        command = {
            "find": "attendance_logs",
            "filter": q,
            "sort": {"date": -1, "emp_code": 1},
            "skip": skip,
            "limit": page_size,
        }
        res = database.command("explain", command, verbosity="executionStats")
        return {
            "endpoint": endpoint_name,
            "collection": "attendance_logs",
            "explain": sanitize_bson_for_json(res),
        }

    elif endpoint_name == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="emp_code and month are required to explain employee_monthly"
            )
        pipe = build_employee_monthly_pipeline(emp_code, month)
        command = {
            "aggregate": "attendance_logs",
            "pipeline": pipe,
            "cursor": {},
        }
        res = database.command("explain", command, verbosity="executionStats")
        return {
            "endpoint": endpoint_name,
            "collection": "attendance_logs",
            "explain": sanitize_bson_for_json(res),
        }

    elif endpoint_name == "department_summary":
        if not month:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="month is required to explain department_summary"
            )
        pipe = build_department_summary_pipeline(month, department)
        command = {
            "aggregate": "employees",
            "pipeline": pipe,
            "cursor": {},
        }
        res = database.command("explain", command, verbosity="executionStats")
        return {
            "endpoint": endpoint_name,
            "collection": "employees",
            "explain": sanitize_bson_for_json(res),
        }

    elif endpoint_name == "late_leaderboard":
        if not month:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="month is required to explain late_leaderboard"
            )
        pipe = build_late_leaderboard_pipeline(month, limit or 10, department)
        command = {
            "aggregate": "attendance_logs",
            "pipeline": pipe,
            "cursor": {},
        }
        res = database.command("explain", command, verbosity="executionStats")
        return {
            "endpoint": endpoint_name,
            "collection": "attendance_logs",
            "explain": sanitize_bson_for_json(res),
        }

    elif endpoint_name == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="department, from, and to are required to explain department_trend"
            )
        d_from = date.fromisoformat(from_date)
        d_to = date.fromisoformat(to_date)
        if d_to < d_from:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="'to' before 'from'")
        pipe = build_department_trend_pipeline(department, from_date, to_date)
        command = {
            "aggregate": "employees",
            "pipeline": pipe,
            "cursor": {},
        }
        res = database.command("explain", command, verbosity="executionStats")
        return {
            "endpoint": endpoint_name,
            "collection": "employees",
            "explain": sanitize_bson_for_json(res),
        }
