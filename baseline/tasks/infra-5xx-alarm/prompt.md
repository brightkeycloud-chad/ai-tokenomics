Add a CloudWatch alarm that tells the team when the load balancer of the Status Board returns 5xx errors.

Requirements:

1. Put the new Terraform in `terraform/alarms.tf`.
2. Add an SNS topic for alarm notifications, as `aws_sns_topic.alarms`.
3. Add the alarm as `aws_cloudwatch_metric_alarm.alb_5xx`. It watches the 5xx responses that the load balancer itself returns (`HTTPCode_ELB_5XX_Count`), for the load balancer of this stack.
4. Use the sum over 60 seconds. The alarm starts when the sum is equal to or more than a new variable, `alb_5xx_threshold`. Its default is 5. It must be a whole number of 1 or more.
5. Missing data must not start the alarm.
6. Send the alarm notification and the OK notification to the topic.
7. Add an output, `alarm_topic_arn`, with the ARN of the topic.

Read `AGENTS.md` before you start, and obey its conventions.
Terraform is initialized in `terraform/`. Check your work with `terraform fmt -check` and `terraform validate` in that directory.
There are no AWS credentials. Do not run `terraform init`, `plan`, or `apply`.
Do not commit.
