output "server_id" {
  value = aws_transfer_server.sftp.id
}

output "server_arn" {
  value = aws_transfer_server.sftp.arn
}

output "sftp_public_ips" {
  value       = aws_eip.sftp[*].public_ip
  description = "Fixed IPs that clients allowlist and connect to."
}

output "client_security_group_id" {
  value       = aws_security_group.clients.id
  description = "First member of the client allowlist pool."
}

output "vpc_id" {
  value       = aws_vpc.this.id
  description = "Needed by the admin web to create additional pool security groups."
}

output "user_access_role_arn" {
  value       = aws_iam_role.user_access.arn
  description = "Role the admin web assigns to every SFTP user."
}

output "host_key_fingerprint" {
  value       = aws_transfer_server.sftp.host_key_fingerprint
  description = "Share with clients so they can verify the server on first connection."
}
