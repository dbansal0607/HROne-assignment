from datetime import datetime, timezone

def test_regularize_success(client):
    client.post("/employees", json={
        "emp_code": "EMP0001",
        "name": "Asha Rao",
        "email": "asha@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-05",
    })

    # Punch in 10:05 IST (04:35 UTC), late_minutes=35
    t_in = int(datetime(2026, 7, 7, 4, 35, 0, tzinfo=timezone.utc).timestamp() * 1000)
    t_out = int(datetime(2026, 7, 7, 13, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)

    client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": t_in})
    client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": t_out})

    # Regularize punch_in to 09:28 IST (03:58 UTC) -> late_minutes should become 0
    new_t_in = int(datetime(2026, 7, 7, 3, 58, 0, tzinfo=timezone.utc).timestamp() * 1000)

    patch_res = client.patch("/attendance/EMP0001/2026-07-07", json={
        "punch_in": new_t_in,
        "reason": "biometric glitch",
        "regularized_by": "hr.admin"
    })

    assert patch_res.status_code == 200
    data = patch_res.json()
    assert data["late_minutes"] == 0
    assert data["work_hours"] == 9.03
    assert len(data["history"]) == 1

    entry = data["history"][0]
    assert entry["by"] == "hr.admin"
    assert entry["reason"] == "biometric glitch"
    assert "punch_in" in entry["changes"]
    assert entry["changes"]["punch_in"]["from"] == t_in
    assert entry["changes"]["punch_in"]["to"] == new_t_in
    assert "late_minutes" in entry["changes"]
    assert entry["changes"]["late_minutes"] == {"from": 35, "to": 0}


def test_regularize_to_absent(client):
    client.post("/employees", json={
        "emp_code": "EMP0001",
        "name": "Asha",
        "email": "a@example.com",
        "department": "Engineering",
        "joined_on": "2026-01-01",
    })
    t_in = int(datetime(2026, 7, 7, 4, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
    client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": t_in})

    # Setting status to ABSENT clears punch times and sets work_hours to null
    patch_res = client.patch("/attendance/EMP0001/2026-07-07", json={
        "status": "ABSENT",
        "reason": "Unapproved absence",
        "regularized_by": "manager"
    })
    assert patch_res.status_code == 200
    data = patch_res.json()
    assert data["status"] == "ABSENT"
    assert data["punch_in"] is None
    assert data["punch_out"] is None
    assert data["work_hours"] is None
    assert data["late_minutes"] == 0


def test_regularize_validation_errors(client):
    client.post("/employees", json={
        "emp_code": "EMP0001",
        "name": "Asha",
        "email": "a@example.com",
        "department": "Engineering",
        "joined_on": "2026-01-01",
    })
    t_in = int(datetime(2026, 7, 7, 4, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
    client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": t_in})

    # Setting ABSENT while supplying punch_in -> 422
    r_abs = client.patch("/attendance/EMP0001/2026-07-07", json={
        "status": "ABSENT",
        "punch_in": t_in,
        "reason": "Reason here",
        "regularized_by": "admin"
    })
    assert r_abs.status_code == 422

    # Request changing nothing -> 422
    r_no_change = client.patch("/attendance/EMP0001/2026-07-07", json={
        "status": "PRESENT",
        "reason": "No actual change",
        "regularized_by": "admin"
    })
    assert r_no_change.status_code == 422

    # punch_in moved to a different calendar date -> 422
    t_wrong_day = int(datetime(2026, 7, 8, 4, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
    r_wrong_day = client.patch("/attendance/EMP0001/2026-07-07", json={
        "punch_in": t_wrong_day,
        "reason": "Wrong day",
        "regularized_by": "admin"
    })
    assert r_wrong_day.status_code == 422

    # Record not found -> 404
    r_not_found = client.patch("/attendance/EMP0001/2026-07-09", json={
        "status": "ABSENT",
        "reason": "Not found",
        "regularized_by": "admin"
    })
    assert r_not_found.status_code == 404
