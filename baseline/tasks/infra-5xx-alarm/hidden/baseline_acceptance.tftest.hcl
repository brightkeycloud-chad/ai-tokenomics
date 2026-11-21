# Hidden acceptance test for the infra-5xx-alarm baseline task.
# The harness copies this file into terraform/tests/ after the agent stops.
# The mock provider makes the test free: no AWS account and no credentials.

mock_provider "aws" {
  mock_resource "aws_sns_topic" {
    defaults = {
      arn = "arn:aws:sns:us-east-1:123456789012:mock-alarms"
    }
  }
  mock_resource "aws_iam_role" {
    defaults = {
      arn = "arn:aws:iam::123456789012:role/mock-role"
    }
  }
  mock_resource "aws_lb" {
    defaults = {
      arn        = "arn:aws:elasticloadbalancing:us-east-1:123456789012:loadbalancer/app/mock-alb/0123456789abcdef"
      arn_suffix = "app/mock-alb/0123456789abcdef"
    }
  }
  mock_resource "aws_lb_target_group" {
    defaults = {
      arn = "arn:aws:elasticloadbalancing:us-east-1:123456789012:targetgroup/mock-tg/0123456789abcdef"
    }
  }
  override_data {
    target = data.aws_availability_zones.available
    values = {
      names = ["us-east-1a", "us-east-1b", "us-east-1c"]
    }
  }
  override_data {
    target = data.aws_caller_identity.current
    values = {
      account_id = "123456789012"
    }
  }
  override_data {
    target = data.aws_acm_certificate.board
    values = {
      arn = "arn:aws:acm:us-east-1:123456789012:certificate/00000000-0000-0000-0000-000000000000"
    }
  }
  override_data {
    target = data.aws_route53_zone.board
    values = {
      zone_id = "Z0000000000000000000"
    }
  }
}

run "alarm_watches_the_load_balancer" {
  command = apply

  assert {
    condition     = aws_cloudwatch_metric_alarm.alb_5xx.namespace == "AWS/ApplicationELB"
    error_message = "The alarm must use the AWS/ApplicationELB namespace."
  }
  assert {
    condition     = aws_cloudwatch_metric_alarm.alb_5xx.metric_name == "HTTPCode_ELB_5XX_Count"
    error_message = "The alarm must watch HTTPCode_ELB_5XX_Count."
  }
  assert {
    condition     = try(aws_cloudwatch_metric_alarm.alb_5xx.dimensions["LoadBalancer"], "") == aws_lb.this.arn_suffix
    error_message = "The LoadBalancer dimension must be the ARN suffix of aws_lb.this."
  }
  assert {
    condition     = aws_cloudwatch_metric_alarm.alb_5xx.statistic == "Sum" && aws_cloudwatch_metric_alarm.alb_5xx.period == 60
    error_message = "The alarm must use the Sum over 60 seconds."
  }
  assert {
    condition     = aws_cloudwatch_metric_alarm.alb_5xx.comparison_operator == "GreaterThanOrEqualToThreshold" && aws_cloudwatch_metric_alarm.alb_5xx.threshold == 5
    error_message = "The alarm must start at 5 or more errors (the default of alb_5xx_threshold)."
  }
  assert {
    condition     = aws_cloudwatch_metric_alarm.alb_5xx.treat_missing_data == "notBreaching"
    error_message = "Missing data must not start the alarm."
  }
  assert {
    condition     = contains(aws_cloudwatch_metric_alarm.alb_5xx.alarm_actions, aws_sns_topic.alarms.arn)
    error_message = "The alarm must notify aws_sns_topic.alarms."
  }
  assert {
    condition     = contains(coalesce(aws_cloudwatch_metric_alarm.alb_5xx.ok_actions, []), aws_sns_topic.alarms.arn)
    error_message = "The alarm must also notify aws_sns_topic.alarms when it returns to OK."
  }
  assert {
    condition     = output.alarm_topic_arn == aws_sns_topic.alarms.arn
    error_message = "The output alarm_topic_arn must give the topic ARN."
  }
}

run "threshold_is_a_variable" {
  command = plan

  variables {
    alb_5xx_threshold = 12
  }

  assert {
    condition     = aws_cloudwatch_metric_alarm.alb_5xx.threshold == 12
    error_message = "The threshold must come from var.alb_5xx_threshold."
  }
}
