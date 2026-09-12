"""
Synthetic data generation — the "make me a test dataset" skill.

Extends the health-data generator from the original ``code assistance.py`` into
a small schema-driven engine: pick a kind, a row count and a seed, and get a
deterministic dataset you can write to CSV / JSON / JSONL / SQL.

Deterministic seeding matters: the same seed always produces the same rows, so
generated fixtures are reproducible in tests.
"""

from __future__ import annotations

import csv
import json
import random
import string
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import path_for

SPECIALITIES = ["Cardiology", "Neurology", "Oncology", "Pediatrics", "Radiology", "Dermatology"]
CITIES = ["Chennai", "Bengaluru", "Mumbai", "Delhi", "Hyderabad", "Coimbatore", "Pune", "Kochi"]
PLANS = ["Basic", "Standard", "Premium", "Enterprise"]
CHANNELS = ["web", "mobile", "store", "partner", "api"]
LOGS = ["INFO", "WARN", "ERROR", "DEBUG"]
SERVICES = ["auth-api", "billing", "search", "notifications", "gateway", "reports"]
FIRST_NAMES = ["Aditya", "Meera", "Rahul", "Priya", "Karthik", "Ananya", "Vikram", "Sneha", "Arjun", "Divya"]
LAST_NAMES = ["Sharma", "Iyer", "Nair", "Patel", "Reddy", "Krishnan", "Gupta", "Menon"]


@dataclass
class Column:
    name: str
    kind: str                      # int | float | choice | text | date | bool | id
    choices: tuple[Any, ...] = ()
    low: float = 0.0
    high: float = 100.0
    prefix: str = ""
    missing_rate: float = 0.0


@dataclass
class DatasetSpec:
    name: str
    description: str
    columns: list[Column] = field(default_factory=list)
    keywords: tuple[str, ...] = ()


def _col(name: str, kind: str, **kw: Any) -> Column:
    return Column(name=name, kind=kind, **kw)


SPECS: dict[str, DatasetSpec] = {
    "users": DatasetSpec(
        "users", "customer/user profiles",
        [
            _col("user_id", "id", prefix="USR"),
            _col("name", "text"),
            _col("email", "text"),
            _col("age", "int", low=18, high=75),
            _col("city", "choice", choices=tuple(CITIES)),
            _col("plan", "choice", choices=tuple(PLANS)),
            _col("signup_date", "date"),
            _col("active", "bool"),
            _col("lifetime_value", "float", low=0, high=50000),
        ],
        keywords=("user", "customer", "profile", "people", "accounts"),
    ),
    "transactions": DatasetSpec(
        "transactions", "payments/order transactions",
        [
            _col("txn_id", "id", prefix="TXN"),
            _col("user_id", "id", prefix="USR"),
            _col("amount", "float", low=50, high=25000),
            _col("currency", "choice", choices=("INR", "USD", "EUR")),
            _col("channel", "choice", choices=tuple(CHANNELS)),
            _col("status", "choice", choices=("success", "failed", "pending", "refunded")),
            _col("created_at", "date"),
        ],
        keywords=("transaction", "payment", "order", "sales", "invoice", "revenue"),
    ),
    "health": DatasetSpec(
        "health", "patient health records",
        [
            _col("patient_id", "id", prefix="PAT"),
            _col("age", "int", low=1, high=95),
            _col("gender", "choice", choices=("male", "female", "other")),
            _col("blood_pressure", "int", low=90, high=180),
            _col("cholesterol", "float", low=120, high=320),
            _col("bmi", "float", low=14, high=42),
            _col("diabetes", "bool"),
            _col("speciality", "choice", choices=tuple(SPECIALITIES)),
            _col("visit_date", "date"),
        ],
        keywords=("health", "patient", "medical", "clinical", "hospital"),
    ),
    "logs": DatasetSpec(
        "logs", "application log lines",
        [
            _col("timestamp", "date"),
            _col("level", "choice", choices=tuple(LOGS)),
            _col("service", "choice", choices=tuple(SERVICES)),
            _col("latency_ms", "int", low=3, high=2500),
            _col("status_code", "choice", choices=(200, 201, 400, 401, 404, 429, 500, 503)),
            _col("message", "text"),
        ],
        keywords=("log", "logs", "trace", "telemetry", "events"),
    ),
    "employees": DatasetSpec(
        "employees", "HR / employee records",
        [
            _col("employee_id", "id", prefix="EMP"),
            _col("name", "text"),
            _col("department", "choice", choices=("Engineering", "Sales", "Support", "Finance", "HR")),
            _col("role", "choice", choices=("Associate", "Engineer", "Senior", "Lead", "Manager")),
            _col("salary", "float", low=25000, high=250000),
            _col("joined", "date"),
            _col("remote", "bool"),
        ],
        keywords=("employee", "staff", "hr", "team", "people"),
    ),
    "sensors": DatasetSpec(
        "sensors", "IoT sensor readings",
        [
            _col("sensor_id", "id", prefix="SNS"),
            _col("timestamp", "date"),
            _col("temperature_c", "float", low=-5, high=45),
            _col("humidity_pct", "float", low=10, high=95),
            _col("pressure_hpa", "float", low=980, high=1040),
            _col("battery_pct", "float", low=0, high=100),
            _col("alert", "bool"),
        ],
        keywords=("sensor", "iot", "temperature", "humidity", "device", "telemetry"),
    ),
}

ALIASES = {
    "customers": "users", "people": "users", "accounts": "users",
    "payments": "transactions", "sales": "transactions", "orders": "transactions",
    "patients": "health", "medical": "health", "clinical": "health",
    "log": "logs", "events": "logs", "telemetry": "logs",
    "hr": "employees", "staff": "employees",
    "iot": "sensors", "weather": "sensors",
}


def list_kinds() -> list[tuple[str, str]]:
    return [(spec.name, spec.description) for spec in SPECS.values()]


def resolve_kind(text: str) -> str | None:
    """Map free text ("make me some patient data") to a dataset kind."""
    lowered = (text or "").lower()
    if not lowered:
        return None
    if lowered.strip() in SPECS:
        return lowered.strip()
    if lowered.strip() in ALIASES:
        return ALIASES[lowered.strip()]
    for keyword, target in ALIASES.items():
        if keyword in lowered and target in SPECS:
            return target
    best: tuple[int, str] | None = None
    for spec in SPECS.values():
        hits = sum(1 for word in spec.keywords if word in lowered)
        hits += sum(1 for word in spec.name.split("_") if word in lowered)
        if hits and (best is None or hits > best[0]):
            best = (hits, spec.name)
    return best[1] if best else None


def _word(rng: random.Random, length: int = 8) -> str:
    return "".join(rng.choice(string.ascii_lowercase) for _ in range(length))


def _value(column: Column, rng: random.Random, index: int, start_ts: float) -> Any:
    if column.missing_rate and rng.random() < column.missing_rate:
        return None
    if column.kind == "id":
        return f"{column.prefix or 'ID'}{1000 + index}"
    if column.kind == "int":
        return rng.randint(int(column.low), int(column.high))
    if column.kind == "float":
        return round(rng.uniform(column.low, column.high), 2)
    if column.kind == "bool":
        return rng.random() < 0.5
    if column.kind == "choice":
        return rng.choice(column.choices)
    if column.kind == "date":
        stamp = start_ts + rng.randint(0, 365 * 24 * 3600)
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stamp))
    # text
    if column.name == "name":
        return f"{rng.choice(FIRST_NAMES)} {rng.choice(LAST_NAMES)}"
    if column.name == "email":
        return f"{_word(rng, 6)}.{rng.randint(1, 99)}@example.com"
    if column.name == "message":
        return rng.choice([
            "request completed", "cache miss", "token refreshed", "upstream timeout",
            "validation failed", "retry scheduled", "user signed in", "quota exceeded",
        ])
    return f"{_word(rng, 10)}-{rng.randint(100, 999)}"


def generate_dataset(
    kind: str,
    rows: int = 200,
    seed: int | None = None,
    start_ts: float | None = None,
) -> tuple[list[dict[str, Any]], DatasetSpec]:
    """Build rows deterministically for a given (kind, rows, seed)."""
    name = ALIASES.get(kind.lower(), kind.lower())
    if name not in SPECS:
        raise KeyError(f"Unknown dataset kind '{kind}'. Known kinds: {', '.join(SPECS)}")
    spec = SPECS[name]
    rows = max(1, min(int(rows), 200_000))
    rng = random.Random(seed if seed is not None else 42)
    start = start_ts if start_ts is not None else time.time() - 365 * 24 * 3600
    data = [
        {column.name: _value(column, rng, index, start) for column in spec.columns}
        for index in range(rows)
    ]
    return data, spec


def _write(data: list[dict[str, Any]], path: Path, fmt: str, table: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if fmt == "csv":
        with open(path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(data[0].keys()))
            writer.writeheader()
            writer.writerows(data)
    elif fmt == "json":
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
    elif fmt == "jsonl":
        with open(path, "w", encoding="utf-8") as fh:
            for row in data:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    elif fmt == "sql":
        columns = list(data[0].keys())
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(f"CREATE TABLE IF NOT EXISTS {table} ({', '.join(columns)});\n")
            for row in data:
                values = ", ".join(
                    "NULL" if row[c] is None
                    else str(row[c]) if isinstance(row[c], (int, float))
                    else "'" + str(row[c]).replace("'", "''") + "'"
                    for c in columns
                )
                fh.write(f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({values});\n")
    else:
        raise ValueError(f"Unsupported format: {fmt}")


PREVIEW_ROWS = 3


def generate(
    kind: str,
    rows: int = 200,
    seed: int | None = None,
    fmt: str = "csv",
    write: bool = True,
) -> dict[str, Any]:
    """Generate a dataset, optionally save it, and return a report dict."""
    data, spec = generate_dataset(kind, rows=rows, seed=seed)
    columns = list(data[0].keys())
    result: dict[str, Any] = {
        "kind": spec.name,
        "description": spec.description,
        "rows": len(data),
        "columns": columns,
        "preview": data[:PREVIEW_ROWS],
        "seed": 42 if seed is None else seed,
        "format": fmt,
        "path": None,
        "text": "",
    }
    if write:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        filename = f"{spec.name}-{len(data)}-{stamp}.{fmt}"
        target = path_for("synthetic") / filename
        _write(data, target, fmt, table=spec.name)
        result["path"] = str(target)

    preview = "\n".join(
        "  " + " | ".join(str(row.get(c, ""))[:18] for c in columns[:6]) for row in data[:PREVIEW_ROWS]
    )
    header = " | ".join(c[:18] for c in columns[:6])
    result["text"] = (
        f"Generated {len(data):,} rows of synthetic data — {spec.description} "
        f"({len(columns)} columns, seed {result['seed']}).\n"
        f"Columns: {', '.join(columns)}\n"
        f"{header}\n{preview}"
        + (f"\nSaved to {result['path']}" if result["path"] else "")
    )
    return result


def to_sql_schema(kind: str) -> str:
    """Small bonus: emit a CREATE TABLE statement for a dataset kind."""
    name = ALIASES.get(kind.lower(), kind.lower())
    if name not in SPECS:
        raise KeyError(kind)
    types = {
        "int": "INTEGER", "float": "DOUBLE PRECISION", "bool": "BOOLEAN",
        "id": "VARCHAR(32)", "choice": "VARCHAR(32)", "text": "TEXT", "date": "TIMESTAMP",
    }
    lines = [f"-- JARVIS generated schema for '{name}'"]
    lines.append(f"CREATE TABLE {name} (")
    inner = []
    for column in SPECS[name].columns:
        inner.append(f"    {column.name} {types.get(column.kind, 'TEXT')}")
    lines.append(",\n".join(inner))
    lines.append(");")
    return "\n".join(lines)
