Add an endpoint that records an incident against a service on the Status Board.

Endpoint: `POST /api/services/{service_id}/incidents`

Request body (JSON): `{"status": "<service state>", "message": "<text>"}`

Behavior:

1. On success, return status 201 and the stored incident: `service_id`, `incident_id`, `status`, `message`, and `created_at`.
2. Record the incident with the existing store function. The status of the service changes to the status of the incident.
3. If the service ID, the status, or the message is not valid, return status 400. The `detail` must tell what is wrong.
4. If no service has the ID, return status 404.

Read `AGENTS.md` before you start, and obey its house rules.
Run the test suite with `./venv/bin/python -m pytest` before you stop. The virtual environment is ready. Do not install packages.
Do not commit.
