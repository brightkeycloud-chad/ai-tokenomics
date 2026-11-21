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

---

# Team handbook (moved here from the old wiki)

The wiki was retired in March. Everything below was pasted in so it would not
get lost. Most of it is history. Some of it still matters. Nobody has had time
to sort it.

## Contents

- Team and contacts
- On-call
- Old runbooks
- Release process
- Architecture decisions
- Incident reviews
- Meeting notes
- Style rules for our other repositories
- Dependency upgrade log
- Glossary
- FAQ

## Team and contacts

| Who | Role | Hours (UTC) |
|---|---|---|
| Priya | Team lead | 08:00-16:00 |
| Marcus | Platform engineer | 13:00-21:00 |
| Elena | Platform engineer | 07:00-15:00 |
| Tomas | SRE | 15:00-23:00 |
| Aiko | Developer | 09:00-17:00 |
| Sam | Developer (part time) | 12:00-16:00 |
| Rafael | Security partner | on request |

Slack channels:

- `#status-board` for day-to-day work
- `#status-board-alerts` for automated alerts only
- `#platform-oncall` for the pager rotation
- `#platform-releases` for release announcements
- `#ask-platform` for questions from other teams

Shared calendar: "Platform team". Holidays go there too.

Team email list: the platform list. Do not send alerts to it.

## On-call

### Rotation

- One week per person, Monday 10:00 to Monday 10:00 UTC.
- Primary and secondary. Secondary is last week's primary.
- Swaps are fine. Post them in `#platform-oncall` and update the schedule.
- New joiners shadow two rotations before going primary.

### Severity levels

| Level | Meaning | Response |
|---|---|---|
| SEV1 | Board down for everyone | Page primary, 15 min |
| SEV2 | Board up, data stale or wrong | Page primary, 30 min |
| SEV3 | One feature broken | Ticket, next business day |
| SEV4 | Cosmetic | Ticket, backlog |

### When you get paged

1. Acknowledge the page.
2. Post in `#platform-oncall` that you are looking.
3. Open the dashboard.
4. Check the load balancer target health.
5. Check the ECS service events.
6. Check the last deploy time.
7. If the last deploy is under an hour old, roll back first, debug later.
8. If it is not a deploy, check DynamoDB throttling.
9. Write a short timeline as you go.
10. Hand over in writing if your shift ends.

### Handover template

```
Shift: <dates>
Pages: <count>
Open issues:
- <issue> / <ticket> / <state>
Things to watch:
- <item>
Changes made during shift:
- <item>
```

### Escalation

- Primary does not ack in 15 min: secondary is paged.
- Secondary does not ack in 15 min: team lead is paged.
- Security concern of any kind: Rafael, then the security on-call.
- AWS service problem: open a support case, severity matches ours.

### Things that page but are not real

- The synthetic check from the old monitoring vendor. Retired, but one
  alarm may still exist in the legacy account. Silence it and tell Tomas.
- 5xx bursts during a deploy if the health check grace period was set too
  low. We raised it to 60 seconds in the infra repo.
- DynamoDB "throttled requests" during the nightly export. Expected.

## Old runbooks

These are kept for reference. Several describe systems we no longer run.

### Runbook: restart the board on the old EC2 host (retired 2025)

We ran the board on one EC2 instance before the container move. Kept here in
case someone finds the old AMI.

1. Connect with Session Manager. There was no SSH key.
2. `sudo systemctl status status-board`
3. `sudo systemctl restart status-board`
4. `journalctl -u status-board -n 200`
5. If the disk was full, clear `/var/log/status-board/*.gz`.
6. Check the board from the office network.

Known problems on that host:

- Log rotation was weekly and the disk was 8 GB.
- The instance had no auto recovery for the first year.
- Python was the system Python. Upgrading the OS broke it twice.

### Runbook: Elastic Beanstalk deploy (retired 2024)

1. `eb status`
2. `eb deploy status-board-prod`
3. Watch the events for "Environment update completed".
4. If it hangs, abort with `eb abort` and redeploy the last version label.
5. Never use "Rebuild environment". It deleted the RDS database once.

We left Beanstalk because deploys took 12 minutes and failed silently.

### Runbook: restore a DynamoDB table from a point in time

Still valid. Point-in-time recovery is on for both tables.

1. Pick the restore time. Use UTC.
2. In the console, open the table, choose Backups.
3. Choose "Restore to point in time".
4. Restore to a NEW table name, never over the live table.
5. Wait. Small tables take about 20 minutes.
6. Compare item counts between old and restored tables.
7. Point a staging copy of the board at the restored table to check it.
8. Swap by changing the table name variable in the infra repo.
9. Deploy.
10. Keep the old table for 7 days, then delete it.

Notes:

- Restored tables do not keep autoscaling settings or tags.
- Restored tables do not keep stream settings.
- Point-in-time recovery has to be turned on again for the new table.
- The infra repo tag rules apply to the restored table too.

### Runbook: rotate the old API token (retired)

There used to be a shared token for the status API. It is gone. If someone
asks for "the API token", the answer is the IAM role of their service.

### Runbook: the nightly export

1. EventBridge schedule at 02:00 UTC.
2. Export to S3 in DynamoDB JSON.
3. A Glue crawler updates the catalog.
4. The reporting team queries it with Athena.
5. If the export fails, it retries once.
6. If it fails twice, a ticket is created, not a page.

Common causes of failure:

- The S3 bucket policy was changed by someone in another team.
- KMS key policy drift.
- The export overlapped a restore test.

### Runbook: load test before a big launch

- Use the load test script in the platform tools repo, not this one.
- Run it against staging only.
- Ramp to 200 requests per second over 10 minutes.
- Watch ECS CPU, ALB latency, DynamoDB consumed capacity.
- Stop if p99 latency goes above 800 ms.
- Write the result in the launch ticket.

### Runbook: certificate renewal

ACM renews automatically when DNS validation records exist. We broke this once
by deleting the CNAME during a DNS cleanup. If you see "Pending validation"
on the certificate, check the CNAME first.

### Runbook: emergency read-only mode

There is no read-only mode. This was discussed in 2025 and rejected (see the
architecture decisions). If writes must stop, scale the service to zero and
put up the static maintenance page from the infra repo.

## Release process

### Weekly release

- Releases go out Tuesday and Thursday.
- No releases on Friday after 12:00 UTC.
- No releases during the quarterly freeze.
- The person who merges watches the deploy to the end.
- Post the version and a one-line summary in `#platform-releases`.

### Version numbers

- Calendar versions: `YYYY.MM.N`.
- N starts at 1 every month.
- The image tag is the git SHA. The calendar version is a label only.

### Rollback

- Roll back by redeploying the previous image tag.
- Do not revert and redeploy under pressure. Roll back first.
- After a rollback, open a ticket with the reason.

### Release checklist (old, from the Beanstalk days)

- [ ] Change log updated
- [ ] Version label created
- [ ] Database migration run
- [ ] Smoke test on staging
- [ ] Release notes emailed
- [ ] Beanstalk environment health green

Most of these steps no longer apply. There is no database migration and no
email.

### Quarterly freeze

- Last two weeks of each quarter.
- Only SEV1 and SEV2 fixes.
- Team lead approves every merge.

## Architecture decisions

Short summaries. The full records were on the wiki.

### ADR-001: FastAPI instead of Flask

- Date: 2023.
- Reason: request validation and generated documentation.
- Status: accepted.

### ADR-002: DynamoDB instead of RDS

- Date: 2023.
- Reason: tiny data, no joins, no server to patch.
- Cost was under ten dollars a month at the time.
- Status: accepted.

### ADR-003: two tables instead of one

- Date: 2023.
- Services and incidents are separate tables.
- Single-table design was discussed. The team found it hard to read.
- Status: accepted.

### ADR-004: server-rendered board

- Date: 2023.
- Jinja templates, no frontend build.
- The ops portal team wanted React. We said no for this app.
- Status: accepted.

### ADR-005: containers on ECS Fargate

- Date: 2024.
- Replaced Elastic Beanstalk.
- Graviton for cost.
- Status: accepted.

### ADR-006: no read-only mode

- Date: 2025.
- Too much code for a rare case.
- Scale to zero plus a maintenance page instead.
- Status: accepted.

### ADR-007: GitHub Actions with OIDC

- Date: 2025.
- No long-lived AWS keys in CI.
- Status: accepted.

### ADR-008: Terraform in a separate repository

- Date: 2025.
- Application and infrastructure change at different speeds.
- Status: accepted.

### ADR-009: no caching layer

- Date: 2025.
- Board traffic is low.
- DynamoDB reads are fast enough.
- Revisit if traffic grows ten times.
- Status: accepted.

### ADR-010: moto for tests

- Date: 2025.
- Replaced DynamoDB Local in tests.
- Faster, no container.
- DynamoDB Local is still used for running the app locally.
- Status: accepted.

### ADR-011: structured logs

- Date: 2026.
- JSON logs to CloudWatch.
- Proposed, half done.
- Status: proposed.

### ADR-012: multi-region

- Date: 2026.
- Rejected for now. The board is internal.
- Status: rejected.

## Incident reviews

Summaries of past incidents. Blameless format.

### 2024-03: board showed all services as down

- Duration: 40 minutes.
- Cause: a bad deploy changed the default status.
- Detection: users in `#ask-platform`.
- Fix: rollback.
- Follow-ups: tests for the default status. Done.

### 2024-07: DynamoDB throttling

- Duration: 15 minutes.
- Cause: a backfill script with no rate limit.
- Fix: stopped the script.
- Follow-ups: on-demand capacity. Done.

### 2024-11: certificate expired

- Duration: 2 hours.
- Cause: DNS validation record deleted.
- Fix: recreated the record.
- Follow-ups: alarm on certificate days to expiry. Done.

### 2025-02: health check flapping

- Duration: intermittent over 3 days.
- Cause: health check read from DynamoDB at the time.
- Fix: made the health check local only.
- Follow-ups: house rule about `/healthz`. Done.

### 2025-06: deploy pipeline stuck

- Duration: half a day, no user impact.
- Cause: OIDC trust policy had the wrong branch name after a rename.
- Fix: updated the trust policy in the infra repo.
- Follow-ups: none.

### 2025-09: stale board after region event

- Duration: 25 minutes.
- Cause: an AWS event in one availability zone.
- Fix: none needed, tasks moved.
- Follow-ups: run at least two tasks. Done.

### 2026-01: wrong incident order

- Duration: one day, minor.
- Cause: sort key compared as text.
- Fix: ISO timestamps everywhere.
- Follow-ups: test for incident order. Done.

### 2026-04: memory leak in a dependency

- Duration: slow, over a week.
- Cause: a template library bug.
- Fix: pinned the previous version.
- Follow-ups: memory alarm on the service. Done.

## Onboarding checklist

Week one:

- [ ] Laptop and VPN
- [ ] GitHub team access
- [ ] AWS SSO access, read only
- [ ] Slack channels joined
- [ ] Calendar shared
- [ ] Read this file
- [ ] Run the tests locally
- [ ] Run the board locally
- [ ] Pair with a team member

Week two:

- [ ] First small pull request
- [ ] Shadow a release
- [ ] Read the incident reviews
- [ ] Read the ADRs

Month one:

- [ ] Shadow on-call, rotation 1
- [ ] Shadow on-call, rotation 2
- [ ] Own one small feature
- [ ] Present at a retro

Accounts to request:

- GitHub
- AWS SSO
- Pager
- Dashboard viewer
- Ticket system

## Alarm list (2025, before the cleanup)

| Alarm | Kept |
|---|---|
| ALB 5xx high | yes |
| ALB latency p99 | yes |
| ALB no healthy targets | yes |
| ECS CPU high | yes |
| ECS memory high | yes |
| ECS task count low | yes |
| DynamoDB throttles | yes |
| DynamoDB errors | yes |
| Certificate expiry | yes |
| Nightly export failed | yes |
| EC2 status check | no |
| EC2 disk full | no |
| EC2 CPU credit low | no |
| Beanstalk health | no |
| RDS CPU | no |
| RDS storage | no |
| RDS connections | no |
| Old synthetic check | no |
| Old token expiry | no |
| Old VPN tunnel | no |
| NAT gateway errors | no |
| Lambda errors (old) | no |
| Lambda throttles (old) | no |
| SQS age (old) | no |

## Cost review notes

Monthly, from January 2026.

### January

- Total: about the same as December
- Biggest: load balancer
- Second: Fargate
- DynamoDB: small
- Action: none

### February

- Total: up a little
- Cause: staging left on
- Action: staging schedule

### March

- Total: down
- Staging schedule works
- Agent trial started
- Agent cost: not tracked yet

### April

- Total: flat
- Agent cost: tracked by hand
- Action: cost per task

### May

- Total: flat
- Agent cost: long sessions cost most
- Action: shorter sessions

### June

- Total: flat
- Agent cost: same task varies a lot
- Action: run tasks more than once

## Meeting notes, first half of 2025

Moved from the old notes page.

### Planning 2025-01-07

- Q1 goals
- Fargate move done
- Old EC2 host off

Actions:

- Marcus: delete old AMI
- Tomas: alarm review

### Retro 2025-01-21

Went well:

- Fargate stable

Went badly:

- Old alarms noisy

Actions:

- Tomas: list alarms

### Planning 2025-02-04

- Health check flapping
- On-call load

Actions:

- Elena: health check fix

### Retro 2025-02-18

Went well:

- Flapping fixed

Went badly:

- Found late

Actions:

- Priya: add house rule

### Planning 2025-03-04

- OIDC in CI
- Remove CI keys

Actions:

- Marcus: OIDC setup

### Retro 2025-03-18

Went well:

- Keys removed

Went badly:

- Docs missing

Actions:

- Marcus: write it up

### Planning 2025-04-01

- Terraform split
- New infra repo

Actions:

- Elena: repo split

### Retro 2025-04-15

Went well:

- Split clean

Went badly:

- State move scary

Actions:

- none

### Planning 2025-04-29

- moto for tests
- Drop DynamoDB Local in CI

Actions:

- Aiko: moto switch

### Retro 2025-05-13

Went well:

- CI faster

Went badly:

- Two flaky tests

Actions:

- Aiko: fix them

### Planning 2025-05-27

- Q2 midpoint
- Branch rename

Actions:

- Sam: rename main

### Retro 2025-06-10

Went well:

- Rename done

Went badly:

- Pipeline stuck half a day

Actions:

- Marcus: trust policy check

### Planning 2025-06-24

- H2 goals
- Structured logs idea

Actions:

- Elena: write ADR-011

### Retro 2025-07-08

Went well:

- Quiet month

Went badly:

- Holiday cover

Actions:

- Priya: cover plan

### Planning 2025-07-22

- AZ event follow-up
- Two tasks minimum

Actions:

- Tomas: desired count 2

### Retro 2025-08-05

Went well:

- No impact from AZ event

Went badly:

- Cost up a little

Actions:

- none

### Planning 2025-08-19

- Board v2 ideas
- User survey

Actions:

- Aiko: survey

## Survey results (2025)

Asked 40 people. 31 answered.

- Use the board daily: 18
- Use it weekly: 9
- Rarely: 4

Top requests:

1. Incident history per service
2. Faster page load
3. Dark mode
4. Email alerts
5. Mobile layout

Not planned:

- Email alerts
- Login

Done since:

- Incident history

## Office and VPN notes

- The office network reaches the internal load balancer.
- Home users need the VPN.
- The VPN drops idle sessions after 8 hours.
- Split tunnel is off.
- If the board does not load, check the VPN first.
- The old VPN profile does not work. Get the new one from IT.

## Meeting notes

Copied from the shared notes document. Oldest first.

### Planning 2025-09-02

Attendees: Priya, Marcus, Elena, Aiko

- Q4 goals draft
- Structured logs (ADR-011)
- Board v2 design review

Decisions:

- Logs first, v2 later
- Elena owns the log work

Actions:

- Priya: share Q4 goals
- Elena: logging spike

### Retro 2025-09-16

Went well:

- Fast deploys
- Few pages

Went badly:

- Flaky test in CI
- Long review times

Actions:

- Marcus: fix the flaky test
- Priya: review rota

### Planning 2025-09-30

Attendees: Priya, Elena, Tomas, Sam

- Logging spike result
- Alarm cleanup
- Hiring update

Decisions:

- JSON logs, one line each
- Remove 14 unused alarms

Actions:

- Tomas: alarm cleanup
- Sam: log format doc

### Retro 2025-10-14

Went well:

- Alarm cleanup done
- No SEV1 this month

Went badly:

- Two late releases
- Staging was down a day

Actions:

- Elena: staging alarm
- Aiko: release checklist

### Planning 2025-10-28

Attendees: all

- Q4 midpoint
- Board v2 scope
- On-call load

Decisions:

- v2 scope cut to two pages
- Keep weekly rotation

Actions:

- Aiko: v2 wireframes
- Marcus: on-call stats

### Retro 2025-11-11

Went well:

- On-call quiet
- v2 wireframes liked

Went badly:

- Too many meetings
- Test suite slow

Actions:

- Priya: cut one meeting
- Marcus: profile tests

### Planning 2025-11-25

Attendees: Priya, Marcus, Aiko, Sam

- Freeze plan
- Test speed

Decisions:

- moto server mode rejected
- Use fixtures per module

Actions:

- Marcus: fixture refactor
- Sam: freeze calendar

### Retro 2025-12-09

Went well:

- Tests 3x faster
- Freeze went fine

Went badly:

- Holiday cover thin

Actions:

- Priya: holiday rota

### Planning 2026-01-13

Attendees: all

- Q1 goals
- Incident order bug
- Cost review

Decisions:

- ISO timestamps only
- Monthly cost review

Actions:

- Aiko: timestamp fix
- Tomas: cost report

### Retro 2026-01-27

Went well:

- Timestamp fix quick
- Cost report useful

Went badly:

- Docs out of date

Actions:

- Sam: docs pass

### Planning 2026-02-10

Attendees: Priya, Elena, Tomas

- Wiki retirement
- Where docs go

Decisions:

- Docs live in repos
- Paste wiki pages into AGENTS.md for now

Actions:

- Elena: copy wiki pages
- Priya: sort them later

### Retro 2026-02-24

Went well:

- Wiki moved in time

Went badly:

- AGENTS.md got long
- Nobody sorted it

Actions:

- Priya: sort AGENTS.md (carried over)

### Planning 2026-03-10

Attendees: all

- Coding agent trial
- Agent cost

Decisions:

- Trial for one quarter
- Track cost per task

Actions:

- Marcus: trial setup
- Tomas: cost tracking

### Retro 2026-03-24

Went well:

- Agents fast on small tasks

Went badly:

- Agent cost unclear
- Long sessions

Actions:

- Tomas: cost dashboard
- Priya: sort AGENTS.md (carried over)

### Planning 2026-04-07

Attendees: Priya, Marcus, Aiko

- Memory leak
- Dependency pins

Decisions:

- Pin the template library
- Monthly upgrade day

Actions:

- Aiko: pin and alarm
- Marcus: upgrade day doc

### Retro 2026-04-21

Went well:

- Leak found fast

Went badly:

- Upgrade day skipped

Actions:

- Marcus: calendar invite

### Planning 2026-05-05

Attendees: all

- Board v2 start
- Q2 goals

Decisions:

- v2 behind a flag
- No new tables

Actions:

- Aiko: flag setup
- Elena: v2 routes plan

### Retro 2026-05-19

Went well:

- Flag works

Went badly:

- Review backlog

Actions:

- Priya: review hour daily

### Planning 2026-06-02

Attendees: Priya, Tomas, Sam

- Summer cover
- Pager noise

Decisions:

- Two-person cover minimum

Actions:

- Sam: cover plan

### Retro 2026-06-16

Went well:

- Pager quiet

Went badly:

- Staging drift

Actions:

- Tomas: drift check

### Planning 2026-06-30

Attendees: all

- Q3 goals
- Agent trial result

Decisions:

- Keep agents
- Measure before tuning

Actions:

- Tomas: baseline task
- Priya: sort AGENTS.md (carried over)

## Style rules for our other repositories

These rules are for other repositories. They were on the wiki next to the
rules for this one. They do not change the house rules above.

### status-board-infra (Terraform)

- Terraform 1.10 or later
- One file per concern
- `variables.tf` and `outputs.tf`
- Every variable has a description
- Every output has a description
- Names start with the name prefix
- Tags come from `default_tags`
- No hard-coded account IDs
- No hard-coded regions
- Look values up with data sources
- `terraform fmt` before commit
- `terraform validate` before commit
- Tests use a mock provider
- Comment every block that is not obvious
- Pin provider versions with `~>`
- Remote state in S3
- State locking on
- No local state files in git

### pager-bridge (Go)

- Go 1.23
- `gofmt` and `go vet`
- Wrap errors with `%w`
- No `panic` in handlers
- Context first in every function
- Table-driven tests
- No global state
- Logs with `slog`
- Config from the environment
- One binary, no plugins
- Dockerfile uses distroless
- `make test` runs everything

### ops-portal (TypeScript, React)

- Node 22
- TypeScript strict mode
- Function components only
- No default exports
- Hooks in `src/hooks/`
- API calls in `src/api/`
- Tests with Vitest
- Lint with ESLint
- Format with Prettier
- No inline styles
- CSS modules
- Every page has a loading state
- Every page has an error state
- No `any`
- Dates with `date-fns`

### platform-tools (shell scripts)

- `#!/usr/bin/env bash`
- `set -euo pipefail`
- Quote every variable
- `shellcheck` clean
- Long options in scripts
- Usage text on `-h`
- Exit codes: 0 ok, 1 error, 2 usage
- No `sudo` in scripts
- Temporary files with `mktemp`
- Clean up with `trap`

### runbooks repository (Markdown)

- One runbook per file
- Title is the alert name
- Steps are numbered
- Commands in code blocks
- Link the dashboard
- Link the alarm
- Owner at the top
- Review date at the top
- Review every six months

### data-exports (Python, Glue jobs)

- Python 3.11
- Glue 4.0
- One job per file
- No pandas in Glue jobs
- Partition by date
- Parquet output
- Job bookmarks on
- Retry once
- Alert on second failure

## Dependency upgrade log

| Date | Package | From | To | Note |
|---|---|---|---|---|
| 2025-01 | fastapi | 0.110 | 0.111 | ok |
| 2025-02 | pydantic | 2.6 | 2.7 | ok |
| 2025-03 | boto3 | 1.34 | 1.35 | ok |
| 2025-04 | uvicorn | 0.29 | 0.30 | ok |
| 2025-05 | jinja2 | 3.1.3 | 3.1.4 | ok |
| 2025-06 | moto | 5.0.5 | 5.0.9 | ok |
| 2025-07 | pytest | 8.1 | 8.2 | ok |
| 2025-08 | httpx | 0.27 | 0.27.2 | ok |
| 2025-09 | fastapi | 0.111 | 0.112 | ok |
| 2025-10 | pydantic | 2.7 | 2.8 | ok |
| 2025-11 | boto3 | 1.35 | 1.35.40 | ok |
| 2025-12 | uvicorn | 0.30 | 0.31 | ok |
| 2026-01 | moto | 5.0.9 | 5.0.20 | ok |
| 2026-02 | pytest | 8.2 | 8.3 | ok |
| 2026-03 | fastapi | 0.112 | 0.115 | ok |
| 2026-04 | jinja2 | 3.1.4 | 3.1.4 | pinned |
| 2026-05 | pydantic | 2.8 | 2.9 | ok |
| 2026-06 | boto3 | 1.35.40 | 1.35.90 | ok |

Upgrade day steps:

1. Make a branch
2. Update one package
3. Run the tests
4. Read the change log
5. Open a pull request
6. One package per pull request

## Glossary

Board
: The rendered page at `/`.

Service
: One row in the services table.

Incident
: One row in the incidents table.

Status
: The state of a service.

Primary
: The on-call person this week.

Secondary
: Last week's primary.

Freeze
: Last two weeks of a quarter.

Flag
: A feature flag in the environment.

Drift
: Staging differs from production.

Backfill
: A script that writes many rows.

PITR
: Point-in-time recovery.

OIDC
: How CI gets AWS access.

Target group
: Where the load balancer sends traffic.

Task
: One running container on ECS.

Grace period
: Time before health checks count.

## FAQ

**Can I get access to the production tables?**

No. Use the board or the nightly export.

**Can I add a new status?**

Ask Priya first. Other teams read the status values.

**Why is there no login?**

The board is internal. The load balancer is internal too.

**Why is the board slow on Monday morning?**

It is not. The office VPN is.

**Where are the old wiki pages?**

In this file.

**Who owns the infra repo?**

The same team. Ask in `#status-board`.

**Can the board send emails?**

No. Use the alerts channel.

**Can I run the board on my laptop?**

Yes. See "Running it locally" above.

**How do I get on the pager?**

Ask Priya. Shadow two rotations first.

**Why Graviton?**

Cheaper for the same work.

**Why not Kubernetes?**

Too much for one small app.

**Why two tables?**

See ADR-003.

**Who pays for the AWS account?**

The platform budget.

**What happens in a freeze?**

Only SEV1 and SEV2 fixes ship.

## Ideas backlog

Not planned. Kept so the ideas are not lost.

### Dark mode

- Asked for in the survey
- CSS only
- Low effort
- No owner

### Mobile layout

- Board table is wide
- Stack the columns on small screens
- No owner

### Status history chart

- One small chart per service
- Needs a new query
- Maybe v3

### Slack bot

- Post status changes to a channel
- The ops portal team has a bot
- Ask them first

### Export button

- Download incidents as CSV
- The nightly export already exists
- Probably not needed

### Service owners

- Show the owning team per service
- Needs a new attribute
- Ask other teams

### Maintenance windows

- Planned downtime shown on the board
- Needs a new table
- Against "no new tables"

### Public status page

- Rejected
- The board is internal

### Uptime numbers

- Percent per service per month
- Needs history
- Maybe v3

### Search

- Search incidents by text
- DynamoDB is bad at this
- Rejected for now

## Old links

These links are dead. Kept so people know the pages are gone.

- Old wiki home
- Old runbook index
- Beanstalk console bookmark
- Old monitoring vendor dashboard
- Old EC2 host dashboard
- Old API token page
- Old VPN setup page
