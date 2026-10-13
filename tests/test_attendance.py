from datetime import datetime, timezone, timedelta

IST = timezone(timedelta(hours=5, minutes=30))

def test_punch_in_success(client):
    client.post("/employees", json={
        "emp_code": "EMP0001",
        "name": "Asha Rao",
        "email": "asha@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-05",
    })

    # Punch in 2026-07-06 09:28:00 IST -> 03:58:00 UTC
    dt_utc = datetime(2026, 7, 6, 3, 58, 0, tzinfo=timezone.utc)
    ts_ms = int(dt_utc.timestamp() * 1000)

    res = client.post("/attendance/punch-in", json={
        "emp_code": "EMP0001",
        "punched_at": ts_ms,
        "status": "PRESENT"
    })
    assert res.status_code == 201
    data = res.json()
    assert data["emp_code"] == "EMP0001"
    assert data["date"] == "2026-07-06"
    assert data["status"] == "PRESENT"
    assert data["late_minutes"] == 0
    assert data["work_hours"] is None
    assert data["punch_out"] is None
    assert data["half_day"] is False
    assert data["history"] == []
    assert "_id" not in data
    assert "id" not in data


def test_punch_in_unknown_employee(client):
    res = client.post("/attendance/punch-in", json={
        "emp_code": "EMP9999",
        "status": "PRESENT"
    })
    assert res.status_code == 404


def test_punch_in_duplicate_same_date(client):
    client.post("/employees", json={
        "emp_code": "EMP0001",
        "name": "Asha",
        "email": "a@example.com",
        "department": "IT",
        "joined_on": "2026-01-01",
    })

    dt_utc = datetime(2026, 7, 6, 4, 0, 0, tzinfo=timezone.utc)
    ts_ms = int(dt_utc.timestamp() * 1000)

    r1 = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ts_ms})
    assert r1.status_code == 201

    r2 = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ts_ms + 1000})
    assert r2.status_code == 409


def test_punch_in_r2_late_grace_boundaries(client):
    client.post("/employees", json={
        "emp_code": "EMP0001",
        "name": "Asha",
        "email": "a@example.com",
        "department": "IT",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01",
    })

    # Shift 09:30:
    # 09:40:00 IST -> late_minutes = 0
    # 09:40:01 IST -> late_minutes = 10
    # 10:15:59 IST -> late_minutes = 45

    # Test 09:40:00 IST (04:10:00 UTC)
    t1 = int(datetime(2026, 7, 6, 4, 10, 0, tzinfo=timezone.utc).timestamp() * 1000)
    r1 = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": t1})
    assert r1.status_code == 201
    assert r1.json()["late_minutes"] == 0

    # Clean and test 09:40:01 IST (04:10:01 UTC) on next day 2026-07-07
    t2 = int(datetime(2026, 7, 7, 4, 10, 1, tzinfo=timezone.utc).timestamp() * 1000)
    r2 = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": t2})
    assert r2.status_code == 201
    assert r2.json()["late_minutes"] == 10

    # Test 10:15:59 IST (04:45:59 UTC) on 2026-07-08
    t3 = int(datetime(2026, 7, 8, 4, 45, 59, tzinfo=timezone.utc).timestamp() * 1000)
    r3 = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": t3})
    assert r3.status_code == 201
    assert r3.json()["late_minutes"] == 45


def test_punch_in_r1_overnight_shift(client):
    client.post("/employees", json={
        "emp_code": "EMP0005",
        "name": "Meera",
        "email": "m@example.com",
        "department": "Support",
        "shift_start": "22:00",
        "shift_end": "06:00",
        "joined_on": "2026-03-10",
    })

    # Punch-in on 2026-07-07 05:30 IST (which is earlier than shift_end 06:00)
    # Belongs to shift that started previous day: 2026-07-06
    dt_ist = datetime(2026, 7, 7, 5, 30, 0, tzinfo=IST)
    ts_ms = int(dt_ist.astimezone(timezone.utc).timestamp() * 1000)

    res = client.post("/attendance/punch-in", json={
        "emp_code": "EMP0005",
        "punched_at": ts_ms,
        "status": "PRESENT"
    })
    assert res.status_code == 201
    assert res.json()["date"] == "2026-07-06"


def test_list_attendance(client, seed_sample_data):
    res = client.get("/attendance?page=1&page_size=20")
    assert res.status_code == 200
    data = res.json()
    assert data["total"] == 9
    assert len(data["items"]) == 9

    # Verify sort order: date desc, emp_code asc
    dates = [x["date"] for x in data["items"]]
    assert dates == sorted(dates, reverse=True)

    # Date range error check: date_from > date_to
    bad_range = client.get("/attendance?date_from=2026-07-10&date_to=2026-07-05")
    assert bad_range.status_code == 422
