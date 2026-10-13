def test_explain_attendance_list(client, seed_sample_data):
    res = client.get("/admin/explain/attendance_list?date_from=2026-07-01&date_to=2026-07-31")
    assert res.status_code == 200
    data = res.json()
    assert data["endpoint"] == "attendance_list"
    assert data["collection"] == "attendance_logs"
    assert "explain" in data
    # Winning plan uses index
    plan = str(data["explain"])
    assert "IXSCAN" in plan
    assert "COLLSCAN" not in plan


def test_explain_employee_monthly(client, seed_sample_data):
    res = client.get("/admin/explain/employee_monthly?emp_code=EMP0001&month=2026-07")
    assert res.status_code == 200
    data = res.json()
    assert data["endpoint"] == "employee_monthly"
    assert data["collection"] == "attendance_logs"
    assert "explain" in data


def test_explain_department_summary(client, seed_sample_data):
    res = client.get("/admin/explain/department_summary?month=2026-07")
    assert res.status_code == 200
    data = res.json()
    assert data["endpoint"] == "department_summary"
    assert data["collection"] == "employees"
    assert "explain" in data


def test_explain_late_leaderboard(client, seed_sample_data):
    res = client.get("/admin/explain/late_leaderboard?month=2026-07")
    assert res.status_code == 200
    data = res.json()
    assert data["endpoint"] == "late_leaderboard"
    assert data["collection"] == "attendance_logs"
    assert "explain" in data


def test_explain_department_trend(client, seed_sample_data):
    res = client.get("/admin/explain/department_trend?department=Engineering&from=2026-07-01&to=2026-07-10")
    assert res.status_code == 200
    data = res.json()
    assert data["endpoint"] == "department_trend"
    assert data["collection"] == "employees"
    assert "explain" in data


def test_explain_missing_params(client):
    res = client.get("/admin/explain/employee_monthly")
    assert res.status_code == 422

    res_invalid = client.get("/admin/explain/non_existent_endpoint")
    assert res_invalid.status_code == 422
