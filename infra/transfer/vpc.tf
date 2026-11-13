# Dedicated VPC for the SFTP endpoint.
#
# Internet Gateway: yes, so the Elastic IPs receive client connections.
# NAT gateway: no, so nothing inside this VPC can open outbound connections.
# Transfer Family writes to S3 through its service role, not through this network.

locals {
  azs          = slice(data.aws_availability_zones.available.names, 0, 2)
  subnet_cidrs = [for i in range(2) : cidrsubnet(var.vpc_cidr, 1, i)]
}

resource "aws_vpc" "this" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags                 = { Name = "${var.name}-transfer" }
}

resource "aws_default_security_group" "this" {
  vpc_id = aws_vpc.this.id
}

resource "aws_internet_gateway" "this" {
  vpc_id = aws_vpc.this.id
  tags   = { Name = "${var.name}-transfer" }
}

resource "aws_subnet" "public" {
  count             = 2
  vpc_id            = aws_vpc.this.id
  cidr_block        = local.subnet_cidrs[count.index]
  availability_zone = local.azs[count.index]
  tags              = { Name = "${var.name}-transfer-${local.azs[count.index]}" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.this.id
  tags   = { Name = "${var.name}-transfer" }
}

resource "aws_route" "internet" {
  route_table_id         = aws_route_table.public.id
  destination_cidr_block = "0.0.0.0/0"
  gateway_id             = aws_internet_gateway.this.id
}

resource "aws_route_table_association" "public" {
  count          = 2
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

# Fixed public IPs that clients can allowlist on their own firewalls.
resource "aws_eip" "sftp" {
  count  = 2
  domain = "vpc"
  tags   = { Name = "${var.name}-sftp-${local.azs[count.index]}" }
}
