# Hidden rules check for the infra-5xx-alarm baseline task: the house rules in
# AGENTS.md (names, tags, variable validation). The harness copies this file into
# terraform/tests/ after the acceptance test. The mock provider makes it free.

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

run "names_follow_the_convention" {
  command = plan

  assert {
    condition     = aws_cloudwatch_metric_alarm.alb_5xx.alarm_name == lower(aws_cloudwatch_metric_alarm.alb_5xx.alarm_name) && startswith(aws_cloudwatch_metric_alarm.alb_5xx.alarm_name, "tokenomics-status-board-")
    error_message = "Rule 1: the alarm name must be {env}-{service}-{resource}, built from local.name."
  }
  assert {
    condition     = aws_sns_topic.alarms.name == lower(aws_sns_topic.alarms.name) && startswith(aws_sns_topic.alarms.name, "tokenomics-status-board-")
    error_message = "Rule 1: the topic name must be {env}-{service}-{resource}, built from local.name."
  }
}

run "names_follow_the_env_variable" {
  command = plan

  variables {
    env = "rulecheck"
  }

  assert {
    condition     = startswith(aws_cloudwatch_metric_alarm.alb_5xx.alarm_name, "rulecheck-status-board-") && startswith(aws_sns_topic.alarms.name, "rulecheck-status-board-")
    error_message = "Rule 1: the names must come from local.name, not from fixed text."
  }
}

run "tags_come_from_default_tags" {
  command = plan

  assert {
    condition     = alltrue([for k in keys(coalesce(aws_cloudwatch_metric_alarm.alb_5xx.tags, {})) : k == "Name"])
    error_message = "Rule 2: tag the alarm only with Name. The provider default_tags give the other tags."
  }
  assert {
    condition     = alltrue([for k in keys(coalesce(aws_sns_topic.alarms.tags, {})) : k == "Name"])
    error_message = "Rule 2: tag the topic only with Name. The provider default_tags give the other tags."
  }
}

run "threshold_has_validation" {
  command = plan

  variables {
    alb_5xx_threshold = 0
  }

  expect_failures = [var.alb_5xx_threshold]
}
