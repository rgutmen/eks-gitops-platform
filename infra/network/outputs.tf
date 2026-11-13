output "vpc_id" {
  value = aws_vpc.this.id
}

output "vpc_cidr" {
  value = aws_vpc.this.cidr_block
}

output "private_subnet_ids" {
  value = aws_subnet.private[*].id
}

output "public_subnet_ids" {
  value = aws_subnet.public[*].id
}

output "nat_public_ips" {
  value       = aws_eip.nat[*].public_ip
  description = "Egress IPs of the cluster, for allowlists on external services."
}
