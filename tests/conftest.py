import os
import pathlib
import sys
import pytest
from bson import json_util
from fastapi.testclient import TestClient
from pymongo import MongoClient

# Add workspace root to sys.path
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

# Use isolated test database
os.environ["MONGO_URI"] = "mongodb://localhost:27017"
os.environ["MONGO_DB"] = "test_attendance_db"

from app.main import app, get_db, init_indexes

@pytest.fixture(scope="session")
def mongo_client():
    client = MongoClient("mongodb://localhost:27017", tz_aware=True)
    yield client
    client.close()

@pytest.fixture(scope="session")
def test_db(mongo_client):
    db = mongo_client["test_attendance_db"]
    init_indexes(db)
    yield db
    mongo_client.drop_database("test_attendance_db")

@pytest.fixture(autouse=True)
def clean_db(test_db):
    test_db.employees.delete_many({})
    test_db.attendance_logs.delete_many({})
    init_indexes(test_db)
    yield

@pytest.fixture
def client(test_db):
    with TestClient(app) as c:
        yield c

@pytest.fixture
def seed_sample_data(test_db):
    here = pathlib.Path(__file__).parent.parent / "candidate_kit" / "sample_data"
    opts = json_util.JSONOptions(tz_aware=True)
    for name in ("employees", "attendance_logs"):
        path = here / f"{name}.json"
        if path.exists():
            docs = json_util.loads(path.read_text(encoding="utf-8"), json_options=opts)
            test_db[name].delete_many({})
            if docs:
                test_db[name].insert_many(docs)
    init_indexes(test_db)
