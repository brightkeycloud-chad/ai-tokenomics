resource "aws_dynamodb_table" "services" {
  name         = "${local.name}-services"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "service_id"

  attribute {
    name = "service_id"
    type = "S"
  }

  tags = { Name = "${local.name}-services" }
}

resource "aws_dynamodb_table" "incidents" {
  name         = "${local.name}-incidents"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "service_id"

  # Sort key of timestamp plus incident id, so two updates recorded in the same
  # instant cannot overwrite each other.
  range_key = "incident_key"

  attribute {
    name = "service_id"
    type = "S"
  }

  attribute {
    name = "incident_key"
    type = "S"
  }

  tags = { Name = "${local.name}-incidents" }
}
