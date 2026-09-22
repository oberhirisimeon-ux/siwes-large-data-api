"""Render API for SIWES large CSV datasets.

The browser uploads files directly to private Supabase Storage with resumable
uploads. It then supplies a short-lived signed URL to this service. The service
downloads and imports the CSV into DuckDB on Render's persistent disk.
"""
import json
import os
import re
import sqlite3
import threading
import uuid
from pathlib import Path
from typing import Any

import duckdb
import httpx
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, HttpUrl

DATA_DIR = Path(os.getenv("DATA_DIR", "/var/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
META_DB = DATA_DIR / "metadata.sqlite"
SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SERVICE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")

app = FastAPI(title="SIWES Large Dataset API", version="1.0")

class ImportRequest(BaseModel):
    source_url: HttpUrl
    original_name: str

class ReplaceRequest(BaseModel):
    source_url: HttpUrl
    original_name: str

class FilterRequest(BaseModel):
    where: str = ""
    limit: int = 100

class EditRequest(BaseModel):
    column: str
    value: Any
    where: str

def init_metadata() -> None:
    with sqlite3.connect(META_DB) as db:
        db.execute("""create table if not exists datasets (
          id text primary key, user_id text not null, original_name text not null,
          status text not null, error text, row_count integer, column_count integer,
          created_at text default current_timestamp, updated_at text default current_timestamp
        )""")

def require_user(authorization: str | None) -> str:
    if not SUPABASE_URL or not SERVICE_KEY:
        raise HTTPException(500, "Server secrets are not configured.")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Sign in before using large dataset analysis.")
    response = httpx.get(
        f"{SUPABASE_URL}/auth/v1/user",
        headers={"Authorization": authorization, "apikey": SERVICE_KEY}, timeout=20,
    )
    if response.status_code != 200:
        raise HTTPException(401, "Your account session could not be verified.")
    return response.json()["id"]

def dataset_path(dataset_id: str) -> Path:
    return DATA_DIR / f"{dataset_id}.duckdb"

def owned_dataset(dataset_id: str, user_id: str) -> sqlite3.Row:
    with sqlite3.connect(META_DB) as db:
        db.row_factory = sqlite3.Row
        row = db.execute("select * from datasets where id=? and user_id=?", (dataset_id, user_id)).fetchone()
    if not row:
        raise HTTPException(404, "Dataset not found.")
    return row

def import_csv(dataset_id: str, user_id: str, source_url: str) -> None:
    csv_path = DATA_DIR / f"{dataset_id}.csv"
    try:
        with httpx.stream("GET", source_url, timeout=None, follow_redirects=True) as response:
            response.raise_for_status()
            with csv_path.open("wb") as file:
                for chunk in response.iter_bytes(1024 * 1024):
                    file.write(chunk)
        connection = duckdb.connect(str(dataset_path(dataset_id)))
        connection.execute("create or replace table data as select * from read_csv_auto(?, ignore_errors=true)", [str(csv_path)])
        columns = connection.execute("describe data").fetchall()
        row_count = connection.execute("select count(*) from data").fetchone()[0]
        connection.close()
        with sqlite3.connect(META_DB) as db:
            db.execute("update datasets set status='ready', row_count=?, column_count=?, updated_at=current_timestamp where id=? and user_id=?", (row_count, len(columns), dataset_id, user_id))
    except Exception as error:
        with sqlite3.connect(META_DB) as db:
            db.execute("update datasets set status='failed', error=?, updated_at=current_timestamp where id=? and user_id=?", (str(error)[:1000], dataset_id, user_id))
    finally:
        csv_path.unlink(missing_ok=True)

def safe_identifier(name: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        raise HTTPException(400, "Invalid column name.")
    return '"' + name + '"'

@app.on_event("startup")
def startup() -> None:
    init_metadata()

@app.get("/health")
def health() -> dict:
    return {"status": "ok"}

@app.post("/api/datasets")
def create_dataset(payload: ImportRequest, authorization: str | None = Header(default=None)) -> dict:
    user_id = require_user(authorization)
    dataset_id = str(uuid.uuid4())
    with sqlite3.connect(META_DB) as db:
        db.execute("insert into datasets (id,user_id,original_name,status) values (?,?,?,'importing')", (dataset_id, user_id, payload.original_name))
    threading.Thread(target=import_csv, args=(dataset_id, user_id, str(payload.source_url)), daemon=True).start()
    return {"id": dataset_id, "status": "importing"}

@app.get("/api/datasets/{dataset_id}")
def dataset_status(dataset_id: str, authorization: str | None = Header(default=None)) -> dict:
    row = owned_dataset(dataset_id, require_user(authorization))
    return dict(row)

@app.get("/api/datasets/{dataset_id}/schema")
def schema(dataset_id: str, authorization: str | None = Header(default=None)) -> dict:
    user_id = require_user(authorization)
    owned_dataset(dataset_id, user_id)
    connection = duckdb.connect(str(dataset_path(dataset_id)), read_only=True)
    columns = [{"name": row[0], "type": row[1]} for row in connection.execute("describe data").fetchall()]
    connection.close()
    return {"columns": columns}

@app.post("/api/datasets/{dataset_id}/preview")
def preview(dataset_id: str, payload: FilterRequest, authorization: str | None = Header(default=None)) -> dict:
    user_id = require_user(authorization)
    owned_dataset(dataset_id, user_id)
    if payload.where and (";" in payload.where or "--" in payload.where):
        raise HTTPException(400, "Unsafe filter expression.")
    limit = min(max(payload.limit, 1), 1000)
    connection = duckdb.connect(str(dataset_path(dataset_id)), read_only=True)
    query = f"select * from data" + (f" where {payload.where}" if payload.where else "") + f" limit {limit}"
    result = connection.execute(query)
    names = [item[0] for item in result.description]
    rows = [dict(zip(names, row)) for row in result.fetchall()]
    connection.close()
    return {"rows": rows}

@app.post("/api/datasets/{dataset_id}/edit")
def edit(dataset_id: str, payload: EditRequest, authorization: str | None = Header(default=None)) -> dict:
    user_id = require_user(authorization)
    owned_dataset(dataset_id, user_id)
    if not payload.where or ";" in payload.where or "--" in payload.where:
        raise HTTPException(400, "A safe row filter is required for edits.")
    connection = duckdb.connect(str(dataset_path(dataset_id)))
    connection.execute(f"update data set {safe_identifier(payload.column)}=? where {payload.where}", [payload.value])
    changed = connection.execute("select changes()").fetchone()[0]
    connection.close()
    return {"updated_rows": changed}

@app.post("/api/datasets/{dataset_id}/replace")
def replace_dataset(dataset_id: str, payload: ReplaceRequest, authorization: str | None = Header(default=None)) -> dict:
    user_id = require_user(authorization)
    owned_dataset(dataset_id, user_id)
    with sqlite3.connect(META_DB) as db:
        db.execute("update datasets set original_name=?, status='importing', error=null, updated_at=current_timestamp where id=? and user_id=?", (payload.original_name, dataset_id, user_id))
    dataset_path(dataset_id).unlink(missing_ok=True)
    threading.Thread(target=import_csv, args=(dataset_id, user_id, str(payload.source_url)), daemon=True).start()
    return {"id": dataset_id, "status": "importing"}
