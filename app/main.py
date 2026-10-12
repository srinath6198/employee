"""Employee attendance and analytics API."""

import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Literal, Optional

from bson.decimal128 import Decimal128
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import ConfigurationError, ConnectionFailure, DuplicateKeyError, PyMongoError
from starlette.responses import JSONResponse

load_dotenv()

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
PRESENT_STATUSES = ("PRESENT", "WFH", "ON_DUTY")
TIME_PATTERN = r"^(?:[01]\d|2[0-3]):[0-5]\d$"
MONTH_PATTERN = r"^\d{4}-(?:0[1-9]|1[0-2])$"
logger = logging.getLogger(__name__)
indexes_ready = False

client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    serverSelectionTimeoutMS=5000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]


def create_indexes() -> None:
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="employee_code_unique")
    db.employees.create_index(
        [("department", ASCENDING), ("joined_on", ASCENDING)],
        name="employee_department_joined",
    )
    db.employees.create_index(
        [("department", ASCENDING), ("emp_code", ASCENDING)],
        name="employee_department_code",
    )
    db.employees.create_index([("joined_on", ASCENDING)], name="employee_joined")
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True,
        name="attendance_employee_date_unique",
    )
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)],
        name="attendance_date_employee",
    )
    db.attendance_logs.create_index(
        [("date", ASCENDING), ("late_minutes", DESCENDING), ("emp_code", ASCENDING)],
        name="attendance_month_late",
    )


def ensure_indexes() -> None:
    global indexes_ready
    if not indexes_ready:
        create_indexes()
        indexes_ready = True


@asynccontextmanager
async def lifespan(app: FastAPI):
    del app
    try:
        client.admin.command("ping")
    except PyMongoError as exc:
        logger.error(
            "MongoDB NOT CONNECTED: %s: %s",
            type(exc).__name__,
            str(exc).splitlines()[0],
        )
    else:
        try:
            ensure_indexes()
        except PyMongoError as exc:
            logger.error(
                "MongoDB connected, but index setup failed: %s: %s",
                type(exc).__name__,
                str(exc).splitlines()[0],
            )
        else:
            logger.info("MongoDB CONNECTED (database=%s)", db.name)
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


@app.exception_handler(ConnectionFailure)
@app.exception_handler(ConfigurationError)
async def mongo_unavailable_handler(
    request: Request, exc: ConnectionFailure | ConfigurationError
) -> JSONResponse:
    logger.warning("MongoDB unavailable during %s %s: %s", request.method, request.url.path, exc)
    if isinstance(exc, ConfigurationError):
        if "reports wire version" in str(exc):
            detail = (
                "The MongoDB server version is incompatible with this application. "
                "Upgrade MongoDB to version 6.0 or newer."
            )
        else:
            detail = "MongoDB configuration error. Check MONGO_URI and the MongoDB server version."
    else:
        detail = (
            "MongoDB is unavailable. Check that the server is running and that MONGO_URI "
            "points to the correct address."
        )
    return JSONResponse(
        status_code=503,
        content={"detail": detail},
    )


class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=TIME_PATTERN)
    shift_end: str = Field(default="18:30", pattern=TIME_PATTERN)
    joined_on: date

    @field_validator("shift_end")
    @classmethod
    def shift_must_have_duration(cls, value: str, info):
        if "shift_start" in info.data and info.data["shift_start"] == value:
            raise ValueError("shift_start and shift_end must differ")
        return value


class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str
    punched_at: Optional[str] = Field(default=None, pattern=TIME_PATTERN)
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"

    @model_validator(mode="after")
    def reject_null_time(self):
        if "punched_at" in self.model_fields_set and self.punched_at is None:
            raise ValueError("punched_at must be omitted or a valid HH:MM time")
        return self


class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str
    punched_at: Optional[str] = Field(default=None, pattern=TIME_PATTERN)

    @model_validator(mode="after")
    def reject_null_time(self):
        if "punched_at" in self.model_fields_set and self.punched_at is None:
            raise ValueError("punched_at must be omitted or a valid HH:MM time")
        return self


class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[Annotated[StrictInt, Field(ge=100_000_000_000, le=4_102_444_800_000)]] = None
    punch_out: Optional[Annotated[StrictInt, Field(ge=100_000_000_000, le=4_102_444_800_000)]] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def reject_null_changes(self):
        for field in ("status", "punch_in", "punch_out"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null")
        return self


def month_bounds(month: str) -> tuple[str, str]:
    if not re.fullmatch(MONTH_PATTERN, month):
        raise HTTPException(422, "month must be YYYY-MM")
    year, month_number = map(int, month.split("-"))
    start = date(year, month_number, 1)
    end = date(year + (month_number == 12), month_number % 12 + 1, 1) - timedelta(days=1)
    return start.isoformat(), end.isoformat()


def datetime_from_epoch_ms(value: int) -> datetime:
    return datetime.fromtimestamp(value / 1000, UTC).replace(microsecond=0)


def datetime_to_epoch_ms(value: Optional[datetime]) -> Optional[int]:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return int(value.timestamp() * 1000)


def truncate_seconds(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.replace(microsecond=0)


def parse_clock(value: str) -> time:
    hour, minute = map(int, value.split(":"))
    return time(hour, minute)


def shift_start_instant(punch_in: datetime, shift_start: str, shift_end: str) -> datetime:
    local = truncate_seconds(punch_in).astimezone(IST)
    start_day = local.date()
    if shift_end <= shift_start and local.time().replace(tzinfo=None) < parse_clock(shift_end):
        start_day -= timedelta(days=1)
    return datetime.combine(start_day, parse_clock(shift_start), IST)


def shift_end_instant(attendance_date: str, shift_start: str, shift_end: str) -> datetime:
    end_day = date.fromisoformat(attendance_date)
    if shift_end <= shift_start:
        end_day += timedelta(days=1)
    return datetime.combine(end_day, parse_clock(shift_end), IST)


def compute_late_minutes(punch_in: datetime, shift_start: str, shift_end: str) -> int:
    punch_in = truncate_seconds(punch_in)
    shift_start_at = shift_start_instant(punch_in, shift_start, shift_end)
    elapsed_seconds = int((punch_in.astimezone(IST) - shift_start_at).total_seconds())
    return elapsed_seconds // 60 if elapsed_seconds > 600 else 0


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    local = truncate_seconds(punch_in).astimezone(IST)
    start_day = local.date()
    if shift_end <= shift_start and local.time().replace(tzinfo=None) < parse_clock(shift_end):
        start_day -= timedelta(days=1)
    return start_day.isoformat()


def round_half_up(value: float | int, digits: int = 2) -> float:
    quantum = Decimal(1).scaleb(-digits)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def round_quotient_half_up(
    numerator: float | int, denominator: float | int, digits: int = 2
) -> float:
    quantum = Decimal(1).scaleb(-digits)
    quotient = Decimal(str(numerator)) / Decimal(str(denominator))
    return float(quotient.quantize(quantum, rounding=ROUND_HALF_UP))


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = int((truncate_seconds(punch_out) - truncate_seconds(punch_in)).total_seconds())
    hours = Decimal(seconds) / Decimal(3600)
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(punch_out: datetime, shift_start: str, shift_end: str, date_str: str) -> int:
    end = shift_end_instant(date_str, shift_start, shift_end)
    minutes = int((truncate_seconds(punch_out).astimezone(IST) - end).total_seconds()) // 60
    return minutes if minutes >= 30 else 0


def resolve_punched_at(punched_at: Optional[str]) -> datetime:
    now = datetime.now(IST)
    if punched_at is None:
        return truncate_seconds(now)
    return datetime.combine(now.date(), parse_clock(punched_at), IST)


def serialize_employee(document: dict) -> dict:
    result = {key: value for key, value in document.items() if key != "_id"}
    if "created_at" in result:
        result["created_at"] = datetime_to_epoch_ms(result["created_at"])
    return result


def serialize_history(history: list) -> list:
    serialized = []
    for entry in history or []:
        item = dict(entry)
        item["at"] = datetime_to_epoch_ms(item.get("at"))
        changes = {}
        for field, values in item.get("changes", {}).items():
            values = dict(values)
            if field in {"punch_in", "punch_out"}:
                values["from"] = datetime_to_epoch_ms(values.get("from"))
                values["to"] = datetime_to_epoch_ms(values.get("to"))
            changes[field] = values
        item["changes"] = changes
        serialized.append(item)
    return serialized


def serialize_attendance(document: dict) -> dict:
    return {
        "emp_code": document["emp_code"],
        "date": document["date"],
        "status": document["status"],
        "punch_in": datetime_to_epoch_ms(document.get("punch_in")),
        "punch_out": datetime_to_epoch_ms(document.get("punch_out")),
        "work_hours": document.get("work_hours"),
        "late_minutes": document.get("late_minutes", 0),
        "overtime_minutes": document.get("overtime_minutes", 0),
        "half_day": document.get("half_day", False),
        "history": serialize_history(document.get("history", [])),
    }


def response_safe(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: response_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [response_safe(item) for item in value]
    if isinstance(value, Decimal128):
        return float(value.to_decimal())
    if hasattr(value, "as_integer_ratio"):
        return value
    if value.__class__.__name__ == "ObjectId":
        return str(value)
    return value


def validate_month_dates(month: str) -> tuple[str, str]:
    try:
        return month_bounds(month)
    except ValueError as exc:
        raise HTTPException(422, "month must be YYYY-MM") from exc


@app.get("/health")
def health():
    client.admin.command("ping")
    ensure_indexes()
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    if body.shift_start == body.shift_end:
        raise HTTPException(422, "shift_start and shift_end must differ")
    document = body.model_dump()
    document["joined_on"] = body.joined_on.isoformat()
    document["created_at"] = truncate_seconds(datetime.now(UTC))
    try:
        db.employees.insert_one(document)
    except DuplicateKeyError as exc:
        raise HTTPException(409, "emp_code already exists") from exc
    return serialize_employee(document)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query = {"department": department} if department is not None else {}
    cursor = (
        db.employees.find(query, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "items": [serialize_employee(item) for item in cursor],
        "total": db.employees.count_documents(query),
        "page": page,
        "page_size": page_size,
    }


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if employee is None:
        raise HTTPException(404, "employee not found")
    punched_at = resolve_punched_at(body.punched_at)
    day = attendance_date(punched_at, employee["shift_start"], employee["shift_end"])
    document = {
        "emp_code": body.emp_code,
        "date": day,
        "status": body.status,
        "punch_in": punched_at,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(
            punched_at, employee["shift_start"], employee["shift_end"]
        ),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(document)
    except DuplicateKeyError as exc:
        raise HTTPException(409, "already punched in for this date") from exc
    return serialize_attendance(document)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if employee is None:
        raise HTTPException(404, "employee not found")
    punched_out = resolve_punched_at(body.punched_at)
    if db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "date": punched_out.date().isoformat(),
            "punch_in": {"$gt": punched_out},
        }
    ):
        raise HTTPException(422, "punched_at must be after punch-in")
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punched_out}},
        sort=[("punch_in", DESCENDING)],
    )
    if record is None:
        raise HTTPException(404, "no punch-in found")
    punch_in_at = record.get("punch_in")
    if punch_in_at is None:
        raise HTTPException(404, "no punch-in found")
    if record.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    seconds = (punched_out - truncate_seconds(punch_in_at)).total_seconds()
    if seconds <= 0 or seconds > 24 * 60 * 60:
        raise HTTPException(422, "punched_at must be after punch-in and within 24 hours")
    work_hours = compute_work_hours(punch_in_at, punched_out)
    update = {
        "$set": {
            "punch_out": punched_out,
            "work_hours": work_hours,
            "overtime_minutes": compute_overtime(
                punched_out,
                employee["shift_start"],
                employee["shift_end"],
                record["date"],
            ),
            "half_day": work_hours < 4.5,
        }
    }
    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        update,
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return serialize_attendance(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must be on or before date_to")
    query = attendance_list_query(emp_code, date_from, date_to, status)
    cursor = (
        db.attendance_logs.find(query)
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "items": [serialize_attendance(item) for item in cursor],
        "total": db.attendance_logs.count_documents(query),
        "page": page,
        "page_size": page_size,
    }


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: date, body: RegularizeIn):
    day = date.isoformat()
    employee = db.employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(404, "employee not found")
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": day})
    if record is None:
        raise HTTPException(404, "attendance record not found")

    original = {
        "status": record["status"],
        "punch_in": record.get("punch_in"),
        "punch_out": record.get("punch_out"),
        "work_hours": record.get("work_hours"),
        "late_minutes": record.get("late_minutes", 0),
        "overtime_minutes": record.get("overtime_minutes", 0),
        "half_day": record.get("half_day", False),
    }
    updated_values = dict(original)
    changes_requested = body.model_fields_set
    if body.status is not None:
        updated_values["status"] = body.status
    if "punch_in" in changes_requested:
        updated_values["punch_in"] = (
            datetime_from_epoch_ms(body.punch_in) if body.punch_in is not None else None
        )
    if "punch_out" in changes_requested:
        updated_values["punch_out"] = (
            datetime_from_epoch_ms(body.punch_out) if body.punch_out is not None else None
        )

    if updated_values["status"] in {"ABSENT", "LEAVE"}:
        if ("punch_in" in changes_requested and body.punch_in is not None) or (
            "punch_out" in changes_requested and body.punch_out is not None
        ):
            raise HTTPException(422, "ABSENT and LEAVE cannot have punch times")
        updated_values["punch_in"] = None
        updated_values["punch_out"] = None
    else:
        if updated_values["punch_in"] is None:
            raise HTTPException(422, "presence status requires punch_in")
        if attendance_date(
            updated_values["punch_in"], employee["shift_start"], employee["shift_end"]
        ) != day:
            raise HTTPException(422, "punch_in must belong to the record attendance date")
        if updated_values["punch_out"] is not None:
            elapsed = (
                updated_values["punch_out"] - updated_values["punch_in"]
            ).total_seconds()
            if elapsed <= 0 or elapsed > 24 * 60 * 60:
                raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")

    if updated_values["status"] in {"ABSENT", "LEAVE"}:
        updated_values.update(
            work_hours=None, late_minutes=0, overtime_minutes=0, half_day=False
        )
    else:
        updated_values["late_minutes"] = compute_late_minutes(
            updated_values["punch_in"], employee["shift_start"], employee["shift_end"]
        )
        if updated_values["punch_out"] is None:
            updated_values.update(work_hours=None, overtime_minutes=0, half_day=False)
        else:
            updated_values["work_hours"] = compute_work_hours(
                updated_values["punch_in"], updated_values["punch_out"]
            )
            updated_values["overtime_minutes"] = compute_overtime(
                updated_values["punch_out"],
                employee["shift_start"],
                employee["shift_end"],
                day,
            )
            updated_values["half_day"] = updated_values["work_hours"] < 4.5

    changes = {}
    for field, new_value in updated_values.items():
        old_value = original[field]
        if old_value != new_value:
            changes[field] = {
                "from": (
                    datetime_to_epoch_ms(old_value)
                    if field in {"punch_in", "punch_out"}
                    else old_value
                ),
                "to": (
                    datetime_to_epoch_ms(new_value)
                    if field in {"punch_in", "punch_out"}
                    else new_value
                ),
            }
    if not changes:
        raise HTTPException(422, "request does not change the record")

    old_history = record.get("history", [])
    history_query = {"history": old_history} if "history" in record else {"history": {"$exists": False}}
    optimistic_filter = {
        "_id": record["_id"],
        "status": record["status"],
        "punch_in": record.get("punch_in"),
        "punch_out": record.get("punch_out"),
        **history_query,
    }
    set_fields = {field: updated_values[field] for field in updated_values}
    result = db.attendance_logs.find_one_and_update(
        optimistic_filter,
        {
            "$set": set_fields,
            "$push": {
                "history": {
                    "at": truncate_seconds(datetime.now(UTC)),
                    "by": body.regularized_by,
                    "reason": body.reason,
                    "changes": changes,
                }
            },
        },
        return_document=ReturnDocument.AFTER,
    )
    if result is None:
        raise HTTPException(409, "attendance record changed concurrently")
    return serialize_attendance(result)


def working_days_pipeline(emp_code: str, month_start: str, month_end: str) -> list:
    start_expr = {
        "$cond": [
            {"$gt": ["$joined_on", month_start]},
            {"$dateFromString": {"dateString": "$joined_on"}},
            {"$dateFromString": {"dateString": month_start}},
        ]
    }
    end_expr = {"$dateFromString": {"dateString": month_end}}
    return [
        {"$match": {"emp_code": emp_code}},
        {
            "$set": {
                "_period_start": start_expr,
                "_period_end": end_expr,
            }
        },
        {
            "$set": {
                "_day_offsets": {
                    "$range": [
                        0,
                        {
                            "$max": [
                                0,
                                {
                                    "$add": [
                                        {
                                            "$dateDiff": {
                                                "startDate": "$_period_start",
                                                "endDate": "$_period_end",
                                                "unit": "day",
                                            }
                                        },
                                        1,
                                    ]
                                },
                            ]
                        },
                    ]
                }
            }
        },
        {
            "$set": {
                "_weekdays": {
                    "$filter": {
                        "input": {
                            "$map": {
                                "input": "$_day_offsets",
                                "as": "offset",
                                "in": {
                                    "$isoDayOfWeek": {
                                        "$dateAdd": {
                                            "startDate": "$_period_start",
                                            "unit": "day",
                                            "amount": "$$offset",
                                        }
                                    }
                                },
                            }
                        },
                        "as": "weekday",
                        "cond": {"$lte": ["$$weekday", 5]},
                    }
                }
            }
        },
        {"$project": {"_id": 0, "working_days": {"$size": "$_weekdays"}}},
    ]


def employee_monthly_pipeline(emp_code: str, start: str, end: str) -> list:
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start, "$lte": end}}},
        {
            "$set": {
                "_weekday": {
                    "$lte": [
                        {"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
                        5,
                    ]
                }
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    "$_weekday",
                                    {"$in": ["$status", list(PRESENT_STATUSES)]},
                                ]
                            },
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]},
                            0,
                        ]
                    }
                },
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }
        },
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month: str = Query(pattern=MONTH_PATTERN),
):
    start, end = validate_month_dates(month)
    employee = db.employees.find_one({"emp_code": emp_code}, {"_id": 0})
    if employee is None:
        raise HTTPException(404, "employee not found")
    rows = list(db.attendance_logs.aggregate(employee_monthly_pipeline(emp_code, start, end)))
    result = rows[0] if rows else {}
    working_day_rows = list(
        db.employees.aggregate(working_days_pipeline(emp_code, start, end))
    )
    days = working_day_rows[0]["working_days"] if working_day_rows else 0
    present_days = round_half_up(result.get("present_days", 0), 2)
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": days,
        "present_days": present_days,
        "leave_days": result.get("leave_days", 0),
        "late_count": result.get("late_count", 0),
        "total_late_minutes": result.get("total_late_minutes", 0),
        "total_overtime_minutes": result.get("total_overtime_minutes", 0),
        "attendance_pct": (
            round_quotient_half_up(present_days * 100, days, 2) if days else None
        ),
    }


def department_summary_pipeline(start: str, end: str, department: Optional[str]) -> list:
    employee_match: dict = {"joined_on": {"$lte": end}}
    if department is not None:
        employee_match["department"] = department
    return [
        {"$match": employee_match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$code"]},
                                    {"$gte": ["$date", start]},
                                    {"$lte": ["$date", end]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$group": {
                "_id": {"department": "$department", "emp_code": "$emp_code"},
                "department": {"$first": "$department"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": [{"$ifNull": ["$logs.date", None]}, None]},
                                    {"$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}}, 5]},
                                    {"$in": ["$logs.status", list(PRESENT_STATUSES)]},
                                ]
                            },
                            {"$cond": [{"$eq": ["$logs.half_day", True]}, 0.5, 1]},
                            0,
                        ]
                    }
                },
                "work_hours_sum": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", list(PRESENT_STATUSES)]},
                                    {"$ne": [{"$ifNull": ["$logs.work_hours", None]}, None]},
                                ]
                            },
                            "$logs.work_hours",
                            0,
                        ]
                    }
                },
                "work_hours_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", list(PRESENT_STATUSES)]},
                                    {"$ne": [{"$ifNull": ["$logs.work_hours", None]}, None]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
                "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
                "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}},
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": "$present_days"},
                "work_hours_sum": {"$sum": "$work_hours_sum"},
                "work_hours_count": {"$sum": "$work_hours_count"},
                "late_count": {"$sum": "$late_count"},
                "total_late_minutes": {"$sum": "$total_late_minutes"},
                "leave_count": {"$sum": "$leave_count"},
                "on_duty_count": {"$sum": "$on_duty_count"},
            }
        },
        {"$sort": {"_id": 1}},
    ]


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(pattern=MONTH_PATTERN),
    department: Optional[str] = None,
):
    start, end = validate_month_dates(month)
    rows = db.employees.aggregate(department_summary_pipeline(start, end, department))
    items = []
    for row in rows:
        if row["headcount"] == 0:
            continue
        count = row["work_hours_count"]
        items.append(
            {
                "department": row["_id"],
                "headcount": row["headcount"],
                "present_days": round_half_up(row["present_days"], 2),
                "avg_work_hours": (
                    round_quotient_half_up(row["work_hours_sum"], count, 2)
                    if count
                    else None
                ),
                "late_count": row["late_count"],
                "total_late_minutes": row["total_late_minutes"],
                "leave_count": row["leave_count"],
                "on_duty_count": row["on_duty_count"],
            }
        )
    return {"month": month, "items": items}


def late_leaderboard_pipeline(
    start: str, end: str, limit: int, department: Optional[str]
) -> list:
    pipeline = [
        {"$match": {"date": {"$gte": start, "$lte": end}, "late_minutes": {"$gt": 0}}},
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "employee",
            }
        },
        {"$unwind": "$employee"},
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline.extend(
        [
            {
                "$setWindowFields": {
                    "sortBy": {"total_late_minutes": DESCENDING},
                    "output": {"rank": {"$rank": {}}},
                }
            },
            {"$match": {"rank": {"$lte": limit}}},
            {"$sort": {"total_late_minutes": DESCENDING, "_id": ASCENDING}},
            {
                "$project": {
                    "_id": 0,
                    "rank": 1,
                    "emp_code": "$_id",
                    "name": "$employee.name",
                    "department": "$employee.department",
                    "total_late_minutes": 1,
                    "late_count": 1,
                }
            },
        ]
    )
    return pipeline


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(pattern=MONTH_PATTERN),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    start, end = validate_month_dates(month)
    items = list(
        db.attendance_logs.aggregate(late_leaderboard_pipeline(start, end, limit, department))
    )
    return {"month": month, "items": items}


def department_trend_pipeline(department: str, start: str, end: str) -> list:
    start_date = datetime.combine(date.fromisoformat(start), time.min, IST)
    days = (date.fromisoformat(end) - date.fromisoformat(start)).days + 1
    return [
        {"$documents": [{}]},
        {
            "$set": {
                "days": {
                    "$map": {
                        "input": {"$range": [0, days]},
                        "as": "offset",
                        "in": {
                            "$dateAdd": {
                                "startDate": start_date,
                                "unit": "day",
                                "amount": "$$offset",
                                "timezone": "Asia/Kolkata",
                            }
                        },
                    }
                }
            }
        },
        {"$unwind": "$days"},
        {
            "$set": {
                "date": {
                    "$dateToString": {
                        "date": "$days",
                        "format": "%Y-%m-%d",
                        "timezone": "Asia/Kolkata",
                    }
                }
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"day": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$day"]},
                                ]
                            }
                        }
                    },
                    {"$count": "count"},
                ],
                "as": "headcount_result",
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"day": "$date"},
                "pipeline": [
                    {"$match": {"$expr": {"$eq": ["$date", "$$day"]}}},
                    {
                        "$lookup": {
                            "from": "employees",
                            "localField": "emp_code",
                            "foreignField": "emp_code",
                            "as": "employee",
                        }
                    },
                    {"$unwind": "$employee"},
                    {"$match": {"employee.department": department}},
                    {
                        "$group": {
                            "_id": None,
                            "present_count": {
                                "$sum": {
                                    "$cond": [
                                        {"$in": ["$status", list(PRESENT_STATUSES)]},
                                        {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]},
                                        0,
                                    ]
                                }
                            },
                            "late_count": {
                                "$sum": {
                                    "$cond": [
                                        {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                                        1,
                                        0,
                                    ]
                                }
                            },
                        }
                    },
                ],
                "as": "daily_logs",
            }
        },
        {
            "$set": {
                "headcount": {
                    "$ifNull": [{"$arrayElemAt": ["$headcount_result.count", 0]}, 0]
                },
                "present_count": {
                    "$ifNull": [{"$arrayElemAt": ["$daily_logs.present_count", 0]}, 0]
                },
                "late_count": {
                    "$ifNull": [{"$arrayElemAt": ["$daily_logs.late_count", 0]}, 0]
                },
                "is_working_day": {
                    "$lte": [{"$isoDayOfWeek": "$days"}, 5]
                },
            }
        },
        {
            "$set": {
                "attendance_rate": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        {
                            "$divide": [
                                {"$toDecimal": "$present_count"},
                                {"$toDecimal": "$headcount"},
                            ]
                        },
                        None,
                    ]
                }
            }
        },
        {"$sort": {"days": 1}},
        {
            "$setWindowFields": {
                "sortBy": {"days": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, 0]},
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": {
                    "$cond": [
                        {"$ne": ["$attendance_rate", None]},
                        {
                            "$divide": [
                                {
                                    "$floor": {
                                        "$add": [
                                            {
                                                "$multiply": [
                                                    "$attendance_rate",
                                                    Decimal128("10000"),
                                                ]
                                            },
                                            Decimal128("0.5"),
                                        ]
                                    }
                                },
                                Decimal128("10000"),
                            ]
                        },
                        None,
                    ]
                },
                "moving_avg_7d": {
                    "$cond": [
                        {"$ne": ["$moving_avg_7d", None]},
                        {
                            "$divide": [
                                {
                                    "$floor": {
                                        "$add": [
                                            {
                                                "$multiply": [
                                                    "$moving_avg_7d",
                                                    Decimal128("10000"),
                                                ]
                                            },
                                            Decimal128("0.5"),
                                        ]
                                    }
                                },
                                Decimal128("10000"),
                            ]
                        },
                        None,
                    ]
                },
            }
        },
    ]


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: date = Query(alias="from"),
    to_date: date = Query(alias="to"),
):
    if to_date < from_date:
        raise HTTPException(422, "to must be on or after from")
    if (to_date - from_date).days + 1 > 92:
        raise HTTPException(422, "date range cannot exceed 92 days")
    if db.employees.count_documents({"department": department}) == 0:
        raise HTTPException(404, "department not found")
    pipeline = department_trend_pipeline(department, from_date.isoformat(), to_date.isoformat())
    items = list(db.aggregate(pipeline, allowDiskUse=True))
    return {"department": department, "items": [response_safe(item) for item in items]}


def attendance_list_query(
    emp_code: Optional[str],
    date_from: Optional[date],
    date_to: Optional[date],
    status: Optional[str],
) -> dict:
    query: dict = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if status is not None:
        query["status"] = status
    if date_from or date_to:
        query["date"] = {}
        if date_from:
            query["date"]["$gte"] = date_from.isoformat()
        if date_to:
            query["date"]["$lte"] = date_to.isoformat()
    return query


def explain_command(collection: str | int, pipeline: list) -> dict:
    command = {"aggregate": collection, "pipeline": pipeline, "cursor": {}, "allowDiskUse": True}
    return db.command(
        "explain",
        command,
        verbosity="executionStats",
    )


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal[
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    ],
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=MONTH_PATTERN),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    from_date: Optional[date] = Query(None, alias="from"),
    to_date: Optional[date] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        if date_from and date_to and date_from > date_to:
            raise HTTPException(422, "date_from must be on or before date_to")
        query = attendance_list_query(emp_code, date_from, date_to, status)
        command = {
            "find": "attendance_logs",
            "filter": query,
            "sort": {"date": DESCENDING, "emp_code": ASCENDING},
            "skip": (page - 1) * page_size,
            "limit": page_size,
        }
        explain = db.command("explain", command, verbosity="executionStats")
        collection = "attendance_logs"
    else:
        if endpoint == "department_trend":
            if department is None or from_date is None or to_date is None:
                raise HTTPException(422, "department, from, and to are required for department_trend")
            if to_date < from_date or (to_date - from_date).days + 1 > 92:
                raise HTTPException(422, "invalid department trend range")
            pipeline = department_trend_pipeline(
                department, from_date.isoformat(), to_date.isoformat()
            )
            explain = explain_command(1, pipeline)
            collection = "database"
        else:
            if month is None:
                raise HTTPException(422, "month is required for this endpoint")
            start, end = validate_month_dates(month)
        if endpoint == "employee_monthly":
            if emp_code is None:
                raise HTTPException(422, "emp_code is required for employee_monthly")
            pipeline = employee_monthly_pipeline(emp_code, start, end)
            collection = "attendance_logs"
        elif endpoint == "department_summary":
            pipeline = department_summary_pipeline(start, end, department)
            collection = "employees"
        elif endpoint == "late_leaderboard":
            pipeline = late_leaderboard_pipeline(start, end, limit, department)
            collection = "attendance_logs"
        if endpoint != "department_trend":
            explain = explain_command(collection, pipeline)
    return {
        "endpoint": endpoint,
        "collection": collection,
        "explain": response_safe(explain),
    }
