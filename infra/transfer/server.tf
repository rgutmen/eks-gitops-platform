# AWS Transfer Family SFTP server.
#
# Users and their SSH keys are SERVICE_MANAGED and created by the admin web,
# not by Terraform. Client source IPs are allowed through a pool of security
# groups also managed by the web; Terraform only creates the first, empty one.

resource "aws_cloudwatch_log_group" "sftp" {
  name              = "/aws/transfer/${var.name}"
  retention_in_days = var.log_retention_days
}

# First security group of the client allowlist pool.
# Ingress rules (port 22 per client CIDR, described "sftp-user:<name>") and any
# additional pool members are owned by the admin web. Terraform creates it with
# no egress, so the endpoint can never initiate outbound connections.
resource "aws_security_group" "clients" {
  name        = "${var.name}-sftp-clients-0"
  description = "SFTP client allowlist (managed by the admin web)"
  vpc_id      = aws_vpc.this.id

  tags = {
    Name                    = "${var.name}-sftp-clients-0"
    "sftp-platform/pool"    = "sftp-clients"
    "sftp-platform/managed" = "admin-web"
  }

  lifecycle {
    ignore_changes = [ingress, egress]
  }
}

resource "aws_transfer_server" "sftp" {
  protocols              = ["SFTP"]
  identity_provider_type = "SERVICE_MANAGED"
  domain                 = "S3"
  endpoint_type          = "VPC"
  security_policy_name   = "TransferSecurityPolicy-2024-01"

  endpoint_details {
    vpc_id                 = aws_vpc.this.id
    subnet_ids             = aws_subnet.public[*].id
    address_allocation_ids = aws_eip.sftp[*].id
    security_group_ids     = [aws_security_group.clients.id]
  }

  logging_role                = aws_iam_role.logging.arn
  structured_log_destinations = ["${aws_cloudwatch_log_group.sftp.arn}:*"]

  tags = { Name = "${var.name}-sftp" }

  # The admin web attaches extra pool security groups as clients grow.
  lifecycle {
    ignore_changes = [endpoint_details[0].security_group_ids]
  }

  depends_on = [aws_internet_gateway.this]
}
