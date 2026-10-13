def test_create_employee_success(client):
    payload = {
        "emp_code": "EMP0100",
        "name": "Arjun Patel",
        "email": "arjun@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-03-01",
    }
    res = client.post("/employees", json=payload)
    assert res.status_code == 201
    data = res.json()
    assert data["emp_code"] == "EMP0100"
    assert data["name"] == "Arjun Patel"
    assert "created_at" in data
    assert isinstance(data["created_at"], int)
    assert "_id" not in data


def test_create_employee_duplicate(client):
    payload = {
        "emp_code": "EMP0101",
        "name": "Bhavna S",
        "email": "bhavna@example.com",
        "department": "Sales",
        "joined_on": "2026-03-01",
    }
    res1 = client.post("/employees", json=payload)
    assert res1.status_code == 201

    res2 = client.post("/employees", json=payload)
    assert res2.status_code == 409
    assert "detail" in res2.json()


def test_create_employee_validation_errors(client):
    # Invalid emp_code regex (less than 4 digits)
    res = client.post("/employees", json={
        "emp_code": "EMP12",
        "name": "Test",
        "email": "test@example.com",
        "department": "IT",
        "joined_on": "2026-01-01",
    })
    assert res.status_code == 422

    # shift_start equal to shift_end
    res2 = client.post("/employees", json={
        "emp_code": "EMP0102",
        "name": "Test",
        "email": "test@example.com",
        "department": "IT",
        "shift_start": "09:30",
        "shift_end": "09:30",
        "joined_on": "2026-01-01",
    })
    assert res2.status_code == 422


def test_list_employees(client):
    # Insert 3 employees across 2 departments
    emps = [
        {"emp_code": "EMP0003", "name": "C", "email": "c@example.com", "department": "Sales", "joined_on": "2026-01-01"},
        {"emp_code": "EMP0001", "name": "A", "email": "a@example.com", "department": "Engineering", "joined_on": "2026-01-01"},
        {"emp_code": "EMP0002", "name": "B", "email": "b@example.com", "department": "Engineering", "joined_on": "2026-01-01"},
    ]
    for e in emps:
        r = client.post("/employees", json=e)
        assert r.status_code == 201

    # List all sorted by emp_code asc
    res = client.get("/employees?page=1&page_size=10")
    assert res.status_code == 200
    data = res.json()
    assert data["total"] == 3
    assert len(data["items"]) == 3
    assert [x["emp_code"] for x in data["items"]] == ["EMP0001", "EMP0002", "EMP0003"]

    # Filter by department
    res_eng = client.get("/employees?department=Engineering")
    assert res_eng.status_code == 200
    data_eng = res_eng.json()
    assert data_eng["total"] == 2
    assert [x["emp_code"] for x in data_eng["items"]] == ["EMP0001", "EMP0002"]
