from datetime import datetime, timezone

def test_employee_monthly_sample(client, seed_sample_data):
    # EMP0001 joined Jan 2026 -> full month working days in July 2026 = 23
    res1 = client.get("/analytics/employees/EMP0001/monthly?month=2026-07")
    assert res1.status_code == 200
    d1 = res1.json()
    assert d1["working_days"] == 23
    assert d1["present_days"] == 2.0
    assert d1["leave_days"] == 0
    assert d1["late_count"] == 0
    assert d1["attendance_pct"] == 8.70

    # EMP0002 joined July 13, 2026 -> working days counted from July 13 = 15
    res2 = client.get("/analytics/employees/EMP0002/monthly?month=2026-07")
    assert res2.status_code == 200
    d2 = res2.json()
    assert d2["working_days"] == 15
    assert d2["present_days"] == 1.0
    assert d2["attendance_pct"] == 6.67

    # Unknown employee -> 404
    res_404 = client.get("/analytics/employees/EMP9999/monthly?month=2026-07")
    assert res_404.status_code == 404


def test_department_summary_sample(client, seed_sample_data):
    res = client.get("/analytics/departments/summary?month=2026-07")
    assert res.status_code == 200
    data = res.json()
    assert data["month"] == "2026-07"
    items = {item["department"]: item for item in data["items"]}

    assert "Engineering" in items
    assert items["Engineering"]["headcount"] == 2
    assert items["Engineering"]["present_days"] == 3.0
    assert items["Engineering"]["avg_work_hours"] == 9.04
    assert items["Engineering"]["late_count"] == 0

    assert "Sales" in items
    assert items["Sales"]["headcount"] == 2
    assert items["Sales"]["present_days"] == 1.5
    assert items["Sales"]["avg_work_hours"] == 6.29
    assert items["Sales"]["late_count"] == 1
    assert items["Sales"]["total_late_minutes"] == 35
    assert items["Sales"]["leave_count"] == 1

    assert "Support" in items
    assert items["Support"]["headcount"] == 2
    assert items["Support"]["present_days"] == 2.0
    assert items["Support"]["avg_work_hours"] == 8.75

    # Filter by specific department
    res_eng = client.get("/analytics/departments/summary?month=2026-07&department=Engineering")
    assert res_eng.status_code == 200
    assert len(res_eng.json()["items"]) == 1
    assert res_eng.json()["items"][0]["department"] == "Engineering"


def test_late_leaderboard_competition_ranking_and_ties(client):
    # Setup employees
    client.post("/employees", json={"emp_code": "EMP0001", "name": "Emp A", "email": "a@ex.com", "department": "IT", "joined_on": "2026-01-01"})
    client.post("/employees", json={"emp_code": "EMP0002", "name": "Emp B", "email": "b@ex.com", "department": "IT", "joined_on": "2026-01-01"})
    client.post("/employees", json={"emp_code": "EMP0003", "name": "Emp C", "email": "c@ex.com", "department": "IT", "joined_on": "2026-01-01"})

    # EMP0001: 45 min late (10:15:00 IST -> 04:45:00 UTC)
    t1 = int(datetime(2026, 7, 6, 4, 45, 0, tzinfo=timezone.utc).timestamp() * 1000)
    client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": t1})

    # EMP0002: 45 min late (tied with EMP0001)
    client.post("/attendance/punch-in", json={"emp_code": "EMP0002", "punched_at": t1})

    # EMP0003: 20 min late (09:50:00 IST -> 04:20:00 UTC)
    t3 = int(datetime(2026, 7, 6, 4, 20, 0, tzinfo=timezone.utc).timestamp() * 1000)
    client.post("/attendance/punch-in", json={"emp_code": "EMP0003", "punched_at": t3})

    # Fetch leaderboard
    res = client.get("/analytics/leaderboard/late?month=2026-07&limit=10")
    assert res.status_code == 200
    items = res.json()["items"]
    assert len(items) == 3

    # Standard competition ranking: ranks must be 1, 1, 3 (next rank skipped!)
    assert items[0]["emp_code"] == "EMP0001"
    assert items[0]["rank"] == 1
    assert items[0]["total_late_minutes"] == 45

    assert items[1]["emp_code"] == "EMP0002"
    assert items[1]["rank"] == 1
    assert items[1]["total_late_minutes"] == 45

    assert items[2]["emp_code"] == "EMP0003"
    assert items[2]["rank"] == 3
    assert items[2]["total_late_minutes"] == 20

    # Test limit cutoff with ties: limit=1 should return BOTH EMP0001 and EMP0002
    res_lim1 = client.get("/analytics/leaderboard/late?month=2026-07&limit=1")
    assert res_lim1.status_code == 200
    items_lim1 = res_lim1.json()["items"]
    assert len(items_lim1) == 2
    assert [x["rank"] for x in items_lim1] == [1, 1]


def test_department_trend(client, seed_sample_data):
    res = client.get("/analytics/departments/Engineering/trend?from=2026-07-05&to=2026-07-15")
    assert res.status_code == 200
    data = res.json()
    assert data["department"] == "Engineering"
    items = data["items"]

    # 11 calendar days from 07-05 to 07-15 inclusive
    assert len(items) == 11
    assert items[0]["date"] == "2026-07-05"
    assert items[0]["is_working_day"] is False
    assert items[0]["attendance_rate"] is None
    assert items[0]["moving_avg_7d"] is None

    # Headcount jumps from 1 to 2 on 2026-07-13 when EMP0002 joins
    item_12 = next(x for x in items if x["date"] == "2026-07-12")
    item_13 = next(x for x in items if x["date"] == "2026-07-13")
    assert item_12["headcount"] == 1
    assert item_13["headcount"] == 2

    # Validation errors
    # 404 for unknown department
    r404 = client.get("/analytics/departments/UnknownDept/trend?from=2026-07-01&to=2026-07-10")
    assert r404.status_code == 404

    # 422 for to < from
    r_bad_dates = client.get("/analytics/departments/Engineering/trend?from=2026-07-10&to=2026-07-01")
    assert r_bad_dates.status_code == 422

    # 422 for range > 92 days
    r_long_range = client.get("/analytics/departments/Engineering/trend?from=2026-01-01&to=2026-05-01")
    assert r_long_range.status_code == 422
