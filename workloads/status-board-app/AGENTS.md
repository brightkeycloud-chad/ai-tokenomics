# Service Status Board

A small internal web application showing the current health of our production
services, with the incident history behind each one. FastAPI, served from a
container, storing data in two DynamoDB tables.

## Layout

| Path | What lives there |
|---|---|
| `app/main.py` | FastAPI application and every HTTP route |
| `app/store.py` | The only module that talks to DynamoDB |
| `app/validators.py` | The only module that validates request input |
| `app/models.py` | Pydantic request and response models |
| `app/templates/` | Jinja2 templates for the rendered board |
| `app/static/` | Stylesheet for the board |
| `scripts/` | Local helpers for creating tables and loading sample data |
| `tests/` | pytest suite, run against a mocked DynamoDB |

## Getting set up

```bash
python3 -m venv venv
./venv/bin/pip install -r requirements-dev.txt
```

## Running the tests

```bash
./venv/bin/python -m pytest
```

The suite mocks DynamoDB with moto, so it needs no AWS account, no credentials
and no network.

## Running it locally

The application reads `DYNAMODB_ENDPOINT_URL`, so the same image runs against a
local DynamoDB and against AWS with no code change.

### With Docker

```bash
docker compose up --build -d
./venv/bin/python -m scripts.create_tables
./venv/bin/python -m scripts.seed
```

The board is at `http://localhost:8080`, the generated API documentation at
`http://localhost:8080/docs`, and the local DynamoDB listens on port 8001.
Storage is in memory, so every `docker compose up` starts from an empty board.

Stop it and remove the data with:

```bash
docker compose down -v
```

### Without Docker

`moto` provides a DynamoDB endpoint that needs no container:

```bash
export AWS_ACCESS_KEY_ID=local AWS_SECRET_ACCESS_KEY=local AWS_REGION=us-east-1
export DYNAMODB_ENDPOINT_URL=http://127.0.0.1:5001

./venv/bin/python -m moto.server -p 5001 &
./venv/bin/python -m scripts.create_tables
./venv/bin/python -m scripts.seed
./venv/bin/python -m uvicorn app.main:app --port 8080
```

### Building the image by hand

Fargate runs this on Graviton, so build the matching architecture:

```bash
docker build --platform linux/arm64 -t status-board:local .
```

## Configuration

Every setting comes from the environment. Nothing is hard-coded, and there are
no credentials in this repository.

| Variable | Default | Purpose |
|---|---|---|
| `SERVICES_TABLE` | `status-board-services` | DynamoDB table holding services |
| `INCIDENTS_TABLE` | `status-board-incidents` | DynamoDB table holding incidents |
| `AWS_REGION` | `us-east-1` | Region for the DynamoDB client |
| `DYNAMODB_ENDPOINT_URL` | unset | Set only for local development |
| `LOG_LEVEL` | `INFO` | Logging level |

In AWS the container receives its permissions from an IAM task role. It never
holds an access key.

## Conventions

These are the house rules. A change that breaks one of them should not be
merged.

1. **Validate through `app/validators.py`.** Routes never check request input
   inline. Add a `check_*` helper to that module and call it, so the rule lives
   in one place and is covered by one set of tests.

2. **Reach DynamoDB through `app/store.py`.** Routes never call boto3
   directly. Never use PartiQL (`execute_statement`): the typed resource API
   sends values as parameters, so a value that came from a request cannot be
   concatenated into a statement.

3. **Return a model from `app/models.py`.** Routes return a declared Pydantic
   model rather than a bare dictionary, so the shape of the API is written down
   and appears in the generated documentation.

4. **Test every route.** A new route needs a test in `tests/test_api.py`
   covering the success path and the failure the caller is most likely to hit.

5. **Keep `/healthz` free of dependencies.** It answers for this process and
   nothing else. It must never read from DynamoDB, because a failing health
   check has to mean the load balancer cannot reach this container, and never
   that something downstream is unhappy.

## Style

Short functions, plain names, and a docstring on anything public. Log with the
module logger rather than `print`. Error messages say what failed, what value
caused it, and what was expected.
