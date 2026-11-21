output "board_url" {
  description = "Address of the status board."
  value       = "https://${aws_route53_record.board.name}"
}

output "name_prefix" {
  description = "Prefix of every resource name. Set as NAME_PREFIX in the application repository."
  value       = local.name
}

output "log_group_name" {
  description = "CloudWatch log group that receives the application logs."
  value       = aws_cloudwatch_log_group.app.name
}

output "alb_dns_name" {
  description = "Hostname of the load balancer, behind the Route 53 record."
  value       = aws_lb.this.dns_name
}

output "ecr_repository_url" {
  description = "Repository the application image is pushed to."
  value       = aws_ecr_repository.this.repository_url
}

output "ecs_cluster_name" {
  description = "Cluster running the application."
  value       = aws_ecs_cluster.this.name
}

output "ecs_service_name" {
  description = "Service running the application."
  value       = aws_ecs_service.this.name
}

output "task_security_group_id" {
  description = "Security group on the application tasks. Its ingress rule from the load balancer is the only way traffic reaches the task."
  value       = aws_security_group.task.id
}

output "services_table_name" {
  description = "DynamoDB table holding services."
  value       = aws_dynamodb_table.services.name
}

output "incidents_table_name" {
  description = "DynamoDB table holding incidents."
  value       = aws_dynamodb_table.incidents.name
}
