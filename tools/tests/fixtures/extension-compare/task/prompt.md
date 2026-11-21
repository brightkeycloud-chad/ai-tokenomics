Users report HTTP 500 errors on the Service Status Board (https://tokenomics.awsf.brightkey.cloud). GET /api/services fails. GET /healthz is still OK.

Find the cause from the application logs in CloudWatch Logs: log group /ecs/tokenomics-status-board, region us-east-1. This copy of the application code is for reference. Do not change files or AWS resources.

Answer in at most 5 lines:
Cause: <the root cause, with the AWS resource that is wrong>
Evidence: <one log line that shows it>
Fix: <one action>
