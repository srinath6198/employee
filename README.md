# Employee Attendance & Analytics API

A REST API for managing employee records, attendance, manual attendance corrections, and attendance analytics. The
application uses FastAPI and MongoDB.

## Contents

- [Features](#features)
- [Requirements](#requirements)
- [Project layout](#project-layout)
- [Configuration](#configuration)
- [Run the API](#run-the-api)
- [Verify the MongoDB connection](#verify-the-mongodb-connection)
- [MongoDB version troubleshooting](#mongodb-version-troubleshooting)
- [API endpoints](#api-endpoints)
- [Example requests](#example-requests)
- [Attendance rules](#attendance-rules)
- [Database and sample data](#database-and-sample-data)
- [Errors](#errors)
- [API documentation](#api-documentation)

## Features

- Create and list employees.
- Record attendance punch-ins and punch-outs.
- Correct an attendance record with an append-only audit history.
- List and filter attendance records with pagination.
- View employee-month and department analytics, a late-arrival leaderboard, and daily department trends.
- Inspect MongoDB query execution plans for supported analytics and listing queries.
- Create database indexes at startup to support uniqueness, pagination, and analytics.

## Requirements

- Python 3.10 or newer.
- MongoDB 6.0 or newer.
- Python dependencies from `requirements.txt`.

Install the Python dependencies from the project root:

```powershell
python -m pip install -r requirements.txt
```

## Project layout

```text
.
├── app/
│   ├── __init__.py
│   └── main.py                 # FastAPI application and endpoint implementations
├── sample_data/
│   ├── attendance_logs.json    # Example attendance documents
│   └── employees.json          # Example employee documents
├── DATA_MODEL.md               # MongoDB collections and stored document shapes
├── DECISIONS.md                # Implementation and indexing decisions
├── openapi.yaml                # API contract and detailed business rules
├── PROBLEM_STATEMENT.docx      # Original project brief
├── REVIEW.md                   # Review notes
├── requirements.txt            # Python dependencies
└── sample_seed.py              # Optional sample data loader
```

## Configuration

Set `MONGO_URI` and `MONGO_DB` in the environment or in an untracked `.env` file in the project root:

```dotenv
MONGO_URI=mongodb://localhost:27017/
MONGO_DB=employee_att
```

The application defaults to `mongodb://localhost:27017` and database `attendance_db` if the variables are not set.
An environment variable already set in the process takes precedence over the value in `.env`. Restart the API after
changing either setting.

Do not commit `.env` files or database credentials.

Each `.env` entry must use `NAME=value` syntax. If startup reports that `python-dotenv` could not parse a statement,
remove or correct the malformed line; valid settings after it may still load.

## Run the API

Start MongoDB, then run the API from the project root:

```powershell
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

The API listens at `http://127.0.0.1:8000`. The root path `/` is not an API endpoint and returns `404`; use `/docs` for
the interactive API documentation or `/health` to check readiness.

## Verify the MongoDB connection

Open `http://127.0.0.1:8000/health`. A successful response is:

```json
{
  "status": "ok"
}
```

If readiness fails, confirm that the MongoDB service is running, that `MONGO_URI` points to the correct server, and that
the server version is supported. The API's startup log also reports whether it connected and created indexes.

## MongoDB version troubleshooting

The application requires MongoDB 6.0 or newer. Older MongoDB servers may accept a TCP connection and answer a basic
`mongosh` ping but still be incompatible with the installed PyMongo driver. For example, MongoDB 4.2 can return a
PyMongo wire-version compatibility error, causing API requests to return `503 Service Unavailable`.

To resolve a server-version compatibility error:

1. Upgrade the local MongoDB server to version 6.0 or newer, following MongoDB's upgrade instructions for your
   operating system and installation.
2. Confirm the new server version with `mongosh --quiet --eval "db.version()"`.
3. Restart MongoDB if needed, then restart Uvicorn.
4. Check `/health` again before retrying the request.

The API reports an actionable `503` message when PyMongo detects an incompatible server version. For other connection
problems, check that the server is running and that the effective `MONGO_URI` is correct.

## API endpoints

Interactive schemas, parameters, and response definitions are available at `/docs`. The complete API contract and
business rules are in [openapi.yaml](./openapi.yaml).

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/health` | Check MongoDB readiness. |
| `POST` | `/employees` | Create an employee. |
| `GET` | `/employees` | List employees; supports department filtering and pagination. |
| `POST` | `/attendance/punch-in` | Record a punch-in. |
| `POST` | `/attendance/punch-out` | Record a punch-out and calculate work totals. |
| `GET` | `/attendance` | List attendance records with filters and pagination. |
| `PATCH` | `/attendance/{emp_code}/{date}` | Correct an attendance record and append to its audit history. |
| `GET` | `/analytics/employees/{emp_code}/monthly` | Get one employee's monthly attendance summary. |
| `GET` | `/analytics/departments/summary` | Get a department summary for a month. |
| `GET` | `/analytics/leaderboard/late` | Rank employees by late minutes for a month. |
| `GET` | `/analytics/departments/{department}/trend` | Get daily attendance trends for a department and date range. |
| `GET` | `/admin/explain/{endpoint}` | Inspect execution statistics for a supported MongoDB query. |

## Example requests

The examples use PowerShell. Replace the example employee details with your own values.

### Create an employee

```powershell
$body = @{
    emp_code   = "EMP0001"
    name       = "Example Employee"
    email      = "employee@example.com"
    department = "Engineering"
    shift_start = "09:30"
    shift_end   = "18:30"
    joined_on   = "2026-01-05"
} | ConvertTo-Json

Invoke-RestMethod -Method Post `
    -Uri "http://127.0.0.1:8000/employees" `
    -ContentType "application/json" `
    -Body $body
```

### Record a punch-in

`punched_at` is optional. If supplied, use a 24-hour `HH:MM` time in IST. `status` defaults to `PRESENT`.

```powershell
$body = @{
    emp_code  = "EMP0001"
    punched_at = "09:35"
    status    = "PRESENT"
} | ConvertTo-Json

Invoke-RestMethod -Method Post `
    -Uri "http://127.0.0.1:8000/attendance/punch-in" `
    -ContentType "application/json" `
    -Body $body
```

### List attendance

Date filters are inclusive. `page` starts at 1 and `page_size` defaults to 20 (maximum 100).

```powershell
Invoke-RestMethod `
    -Uri "http://127.0.0.1:8000/attendance?emp_code=EMP0001&page=1&page_size=20"
```

## Attendance rules

- Attendance dates are `YYYY-MM-DD`, months are `YYYY-MM`, and shift times use 24-hour `HH:MM`. Shift times and punch
  times are interpreted in Indian Standard Time (IST).
- Punch-in creates a record for that employee and attendance day. A unique MongoDB index prevents duplicate daily
  records.
- `PRESENT`, `WFH`, and `ON_DUTY` count as presence. `ABSENT` and `LEAVE` records can come from seeded/back-office data
  or a correction; punch endpoints do not create them.
- Late minutes, overtime, work hours, and half-day status are derived by the API. A 10-minute grace period applies to
  lateness; overtime is recorded only when it reaches 30 minutes. Work hours are rounded to two decimal places using
  half-up rounding.
- Manual corrections append an audit entry. Punch-in and punch-out operations do not add correction history.
- Working days are Monday through Friday; there is no holiday calendar. For overnight shifts, an attendance date
  belongs to the day the shift started.
- API timestamps are epoch milliseconds. Calendar dates and times are strings.

See [openapi.yaml](./openapi.yaml) for all rules, validation constraints, and response schemas.

## Database and sample data

The API uses the database named by `MONGO_DB` and these collections:

- `employees`
- `attendance_logs`

Indexes are created idempotently when the app connects to MongoDB. For collection fields and stored BSON types, see
[DATA_MODEL.md](./DATA_MODEL.md).

To load the included sample data into the configured database:

```powershell
python sample_seed.py
```

**Warning:** `sample_seed.py` deletes all existing documents in both `employees` and `attendance_logs` before loading
the examples. Use it only with a disposable development database. The sample data illustrates document shapes; it is
not a comprehensive analytics test dataset.

## Errors

Errors use FastAPI's `{"detail": "..."}` response format.

| Status | Meaning |
| --- | --- |
| `404` | The requested employee or attendance record does not exist. |
| `409` | A unique record already exists or a concurrent operation conflicts. |
| `422` | Request data or query parameters do not meet validation or business rules. |
| `503` | MongoDB is unavailable, misconfigured, or incompatible with the application. |

## API documentation

- Interactive OpenAPI UI: `http://127.0.0.1:8000/docs`
- ReDoc UI: `http://127.0.0.1:8000/redoc`
- Contract source: [openapi.yaml](./openapi.yaml)
- MongoDB storage model: [DATA_MODEL.md](./DATA_MODEL.md)
- Design decisions: [DECISIONS.md](./DECISIONS.md)
