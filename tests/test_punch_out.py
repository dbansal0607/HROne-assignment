from datetime import datetime, timezone, timedelta

IST = timezone(timedelta(hours=5, minutes=30))

def test_punch_out_success(client):
    client.post("/employees", json={
        "emp_code": "EMP0001",
        "name": "Asha Rao",
        "email": "asha@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-05",
    })

    # Punch in 09:28 IST (03:58 UTC)
    t_in = int(datetime(2026, 7, 6, 3, 58, 0, tzinfo=timezone.utc).timestamp() * 1000)
    client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": t_in})

    # Punch out 18:35 IST (13:05 UTC)
    t_out = int(datetime(2026, 7, 6, 13, 5, 0, tzinfo=timezone.utc).timestamp() * 1000)
    res = client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": t_out})

    assert res.status_code == 200
    data = res.json()
    assert data["work_hours"] == 9.12
    assert data["overtime_minutes"] == 0  # 5 min is < 30
    assert data["half_day"] is False
    assert data["punch_out"] == t_out


def test_punch_out_half_day_and_overtime(client):
    client.post("/employees", json={
        "emp_code": "EMP0004",
        "name": "Rohit Das",
        "email": "rohit@example.com",
        "department": "Sales",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-02-01",
    })

    # 3.5 hours worked: 09:30 IST to 13:00 IST
    t_in = int(datetime(2026, 7, 7, 4, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
    t_out = int(datetime(2026, 7, 7, 7, 30, 0, tzinfo=timezone.utc).timestamp() * 1000)

    client.post("/attendance/punch-in", json={"emp_code": "EMP0004", "punched_at": t_in, "status": "WFH"})
    res = client.post("/attendance/punch-out", json={"emp_code": "EMP0004", "punched_at": t_out})

    assert res.status_code == 200
    data = res.json()
    assert data["work_hours"] == 3.5
    assert data["half_day"] is True


def test_punch_out_overtime_30_min_minimum(client):
    client.post("/employees", json={
        "emp_code": "EMP0003",
        "name": "Neha Iyer",
        "email": "neha@example.com",
        "department": "Sales",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-02-01",
    })

    # Punch in 10:05 IST, Punch out 19:10 IST (40 min past 18:30)
    t_in = int(datetime(2026, 7, 6, 4, 35, 0, tzinfo=timezone.utc).timestamp() * 1000)
    t_out = int(datetime(2026, 7, 6, 13, 40, 0, tzinfo=timezone.utc).timestamp() * 1000)

    client.post("/attendance/punch-in", json={"emp_code": "EMP0003", "punched_at": t_in})
    res = client.post("/attendance/punch-out", json={"emp_code": "EMP0003", "punched_at": t_out})

    assert res.status_code == 200
    data = res.json()
    assert data["late_minutes"] == 35
    assert data["overtime_minutes"] == 40
    assert data["work_hours"] == 9.08


def test_punch_out_errors(client):
    client.post("/employees", json={
        "emp_code": "EMP0001",
        "name": "Asha",
        "email": "a@example.com",
        "department": "IT",
        "joined_on": "2026-01-01",
    })

    # Unknown employee
    r404 = client.post("/attendance/punch-out", json={"emp_code": "EMP9999"})
    assert r404.status_code == 404

    # No punch-in found
    r_no_punch = client.post("/attendance/punch-out", json={"emp_code": "EMP0001"})
    assert r_no_punch.status_code == 404

    # Punch in
    t_in = int(datetime(2026, 7, 6, 4, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
    client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": t_in})

    # punched_at <= punch_in -> 422
    r_leq = client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": t_in})
    assert r_leq.status_code == 422

    # punched_at > punch_in + 24 hours -> 422
    r_25h = client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": t_in + (25 * 3600 * 1000)})
    assert r_25h.status_code == 422

    # Valid punch out
    t_out = t_in + (8 * 3600 * 1000)
    r_valid = client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": t_out})
    assert r_valid.status_code == 200

    # Repeated punch out -> 409
    r_dup = client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": t_out + 1000})
    assert r_dup.status_code == 409
