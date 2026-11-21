resource "aws_vpc" "this" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = { Name = "${local.name}-vpc" }
}

resource "aws_internet_gateway" "this" {
  vpc_id = aws_vpc.this.id

  tags = { Name = "${local.name}-igw" }
}

# Public subnets carry the load balancer only.
resource "aws_subnet" "public" {
  for_each = local.public_subnets

  vpc_id            = aws_vpc.this.id
  availability_zone = each.key
  cidr_block        = each.value

  tags = { Name = "${local.name}-public-${each.key}" }
}

# Private subnets carry the application tasks. They have no route to the
# internet gateway and the tasks are given no public address.
resource "aws_subnet" "private" {
  for_each = local.private_subnets

  vpc_id            = aws_vpc.this.id
  availability_zone = each.key
  cidr_block        = each.value

  tags = { Name = "${local.name}-private-${each.key}" }
}

# A regional NAT gateway is one gateway that spans the Availability Zones,
# rather than one zonal gateway per zone. It needs no subnet of its own and no
# Elastic IP addresses declared here: left in automatic mode it expands and
# contracts across zones as subnets appear and disappear, and manages its own
# addresses. Declaring the addresses by hand would trade that away and hit a
# known provider ordering bug when the zone count changes.
resource "aws_nat_gateway" "this" {
  vpc_id            = aws_vpc.this.id
  availability_mode = "regional"

  tags = { Name = "${local.name}-nat" }

  depends_on = [aws_internet_gateway.this]
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.this.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.this.id
  }

  tags = { Name = "${local.name}-public-rt" }
}

# One route table serves every private subnet, because a single regional NAT
# gateway serves every zone.
resource "aws_route_table" "private" {
  vpc_id = aws_vpc.this.id

  route {
    cidr_block     = "0.0.0.0/0"
    nat_gateway_id = aws_nat_gateway.this.id
  }

  tags = { Name = "${local.name}-private-rt" }
}

resource "aws_route_table_association" "public" {
  for_each = aws_subnet.public

  subnet_id      = each.value.id
  route_table_id = aws_route_table.public.id
}

resource "aws_route_table_association" "private" {
  for_each = aws_subnet.private

  subnet_id      = each.value.id
  route_table_id = aws_route_table.private.id
}
