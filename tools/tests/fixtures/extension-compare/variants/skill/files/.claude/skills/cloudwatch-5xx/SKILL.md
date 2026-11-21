---
name: cloudwatch-5xx
description: Find the cause of HTTP 5xx errors of the Status Board from its CloudWatch Logs, with read-only aws logs commands. Use when the board or its API returns 500 errors.
---

# Find the cause of 5xx errors in CloudWatch Logs

The application writes its logs to the log group `/ecs/tokenomics-status-board`
in `us-east-1`. Each line of a Python traceback is a separate log event.
AWS_PROFILE is already set to a read-only role. Do not add `--profile`.
Use the commands as they are written here. Do not use `$(...)` or shell
variables: the permission rules refuse them.

1. Read the error lines and the failed requests of the last 30 minutes. The
   last line of each traceback names the failed call and the reason:

   ```bash
   aws logs tail /ecs/tokenomics-status-board --since 30m --format short \
     --filter-pattern '?Error ?Exception' | head -n 20
   ```

2. If the errors started earlier, use `--since 2h`. If you need the full
   traceback of one error, use `--filter-pattern` with a word from that error
   and `| head -n 80`.

3. Name the root cause, the AWS resource that is wrong (for example an IAM
   role, a table, or an environment value), one log line as evidence, and one
   fix. Do not change any AWS resource.
