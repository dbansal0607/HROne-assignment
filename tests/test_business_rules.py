import concurrent.futures
from datetime import datetime, timezone, timedelta
from app.main import (
    compute_work_hours,
    round_half_up,
    compute_late_minutes,
    compute_overtime_minutes,
    resolve_attendance_date,
    IST,
)

def test_r4_r8_half_up_rounding():
    # Verify half-up rounding (not banker's round-half-to-even)
    # Python round(2.555, 2) is 2.55, but half-up is 2.56
    assert round_half_up(2.555, 2) == 2.56
    assert round_half_up(2.554, 2) == 2.55
    assert round_half_up(1.005, 2) == 1.01
    assert round_half_up(0.00005, 4) == 0.0001
    assert round_half_up(0.66666, 4) == 0.6667
    assert round_half_up(None, 2) is None


def test_r7_weekend_records_presence_vs_totals(client):
    # Employee created
    client.post("/employees", json={
        "emp_code": "EMP0200",
        "name": "Weekend Warrior",
        "email": "ww@example.com",
        "department": "Support",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-07-01",
    })

    # Saturday: 2026-07-11
    # Punch in 10:00 IST (30 min late), out 19:30 IST (60 min overtime)
    t_sat_in = int(datetime(2026, 7, 11, 4, 30, 0, tzinfo=timezone.utc).timestamp() * 1000)
    t_sat_out = int(datetime(2026, 7, 11, 14, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
    client.post("/attendance/punch-in", json={"emp_code": "EMP0200", "punched_at": t_sat_in})
    client.post("/attendance/punch-out", json={"emp_code": "EMP0200", "punched_at": t_sat_out})

    # Sunday: 2026-07-12
    # Punch in 09:30 IST (0 min late), out 19:10 IST (40 min overtime)
    t_sun_in = int(datetime(2026, 7, 12, 4, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
    t_sun_out = int(datetime(2026, 7, 12, 13, 40, 0, tzinfo=timezone.utc).timestamp() * 1000)
    client.post("/attendance/punch-in", json={"emp_code": "EMP0200", "punched_at": t_sun_in})
    client.post("/attendance/punch-out", json={"emp_code": "EMP0200", "punched_at": t_sun_out})

    # Query monthly analytics for 2026-07
    res = client.get("/analytics/employees/EMP0200/monthly?month=2026-07")
    assert res.status_code == 200
    data = res.json()

    # R7: present_days counts ONLY Mon-Fri records! Weekend records do not count.
    assert data["present_days"] == 0.0
    # Weekend records STILL contribute to late and overtime totals!
    assert data["total_late_minutes"] == 30
    assert data["total_overtime_minutes"] == 100
    assert data["late_count"] == 1


def test_r9_headcount_zero_logs_and_future_joiners(client):
    # EMP A joined in July, has 0 logs
    client.post("/employees", json={
        "emp_code": "EMP0301",
        "name": "Zero Log",
        "email": "zero@example.com",
        "department": "Finance",
        "joined_on": "2026-07-05",
    })

    # EMP B joins in August (future joiner relative to July)
    client.post("/employees", json={
        "emp_code": "EMP0302",
        "name": "Future Joiner",
        "email": "future@example.com",
        "department": "Finance",
        "joined_on": "2026-08-01",
    })

    res = client.get("/analytics/departments/summary?month=2026-07&department=Finance")
    assert res.status_code == 200
    items = res.json()["items"]
    assert len(items) == 1
    # Only EMP0301 is counted in headcount! EMP0302 joined after July 31 so excluded.
    assert items[0]["headcount"] == 1
    assert items[0]["present_days"] == 0.0
    assert items[0]["avg_work_hours"] is None


def test_zero_working_days_attendance_pct_null(client):
    # Employee joined in August 2026
    client.post("/employees", json={
        "emp_code": "EMP0401",
        "name": "Late Joiner",
        "email": "late@example.com",
        "department": "HR",
        "joined_on": "2026-08-15",
    })

    # Query for July 2026
    res = client.get("/analytics/employees/EMP0401/monthly?month=2026-07")
    assert res.status_code == 200
    data = res.json()
    assert data["working_days"] == 0
    assert data["attendance_pct"] is None


def test_concurrent_regularization_optimistic_locking(client):
    client.post("/employees", json={
        "emp_code": "EMP0600",
        "name": "Patch Race",
        "email": "pr@example.com",
        "department": "DevOps",
        "joined_on": "2026-01-01"
    })

    t_in = int(datetime(2026, 7, 6, 4, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
    t_out = int(datetime(2026, 7, 6, 13, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
    client.post("/attendance/punch-in", json={"emp_code": "EMP0600", "punched_at": t_in})
    client.post("/attendance/punch-out", json={"emp_code": "EMP0600", "punched_at": t_out})

    patch1 = {
        "punch_out": t_out + 60000,
        "reason": "Adjustment A",
        "regularized_by": "admin.a"
    }
    patch2 = {
        "punch_out": t_out + 120000,
        "reason": "Adjustment B",
        "regularized_by": "admin.b"
    }

    # Execute concurrent regularizations
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(lambda: client.patch("/attendance/EMP0600/2026-07-06", json=patch1).status_code)
        f2 = executor.submit(lambda: client.patch("/attendance/EMP0600/2026-07-06", json=patch2).status_code)
        codes = [f1.result(), f2.result()]

    # If executed concurrently on the same snapshot, loser gets 409.
    # If serialized, both succeed (200) and both history entries are preserved.
    # Under no circumstances is any history entry silently overwritten or lost.
    if 409 in codes:
        assert 200 in codes
        rec = client.get("/attendance?emp_code=EMP0600").json()["items"][0]
        assert len(rec["history"]) == 1
    else:
        assert codes == [200, 200]
        rec = client.get("/attendance?emp_code=EMP0600").json()["items"][0]
        assert len(rec["history"]) == 2
