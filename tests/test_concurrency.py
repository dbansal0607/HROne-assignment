import concurrent.futures
from datetime import datetime, timezone

def test_concurrent_employee_creation(client):
    payload = {
        "emp_code": "EMP0500",
        "name": "Race Tester",
        "email": "race@example.com",
        "department": "Engineering",
        "joined_on": "2026-01-01"
    }

    def do_create():
        return client.post("/employees", json=payload).status_code

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(do_create) for _ in range(5)]
        codes = [f.result() for f in futures]

    assert codes.count(201) == 1
    assert codes.count(409) == 4


def test_concurrent_punch_in(client):
    client.post("/employees", json={
        "emp_code": "EMP0501",
        "name": "Race In",
        "email": "in@example.com",
        "department": "Engineering",
        "joined_on": "2026-01-01"
    })

    t_in = int(datetime(2026, 7, 6, 4, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)

    def do_punch():
        return client.post("/attendance/punch-in", json={
            "emp_code": "EMP0501",
            "punched_at": t_in
        }).status_code

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(do_punch) for _ in range(5)]
        codes = [f.result() for f in futures]

    assert codes.count(201) == 1
    assert codes.count(409) == 4


def test_concurrent_punch_out(client):
    client.post("/employees", json={
        "emp_code": "EMP0502",
        "name": "Race Out",
        "email": "out@example.com",
        "department": "Engineering",
        "joined_on": "2026-01-01"
    })
    t_in = int(datetime(2026, 7, 6, 4, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
    client.post("/attendance/punch-in", json={"emp_code": "EMP0502", "punched_at": t_in})

    t_out = int(datetime(2026, 7, 6, 12, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)

    def do_punch_out():
        return client.post("/attendance/punch-out", json={
            "emp_code": "EMP0502",
            "punched_at": t_out
        }).status_code

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(do_punch_out) for _ in range(5)]
        codes = [f.result() for f in futures]

    assert codes.count(200) == 1
    assert codes.count(409) == 4
