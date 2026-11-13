# Values consumed by the PGP worker Deployment (environment variables).
output "queue_url" {
  value = aws_sqs_queue.pgp_events.id
}

output "catalog_table" {
  value = aws_dynamodb_table.catalog.name
}

output "allowed_buckets" {
  value = aws_s3_bucket.inbound.id
}

output "output_bucket" {
  value = aws_s3_bucket.decrypted.id
}

# Inputs for the IAM roles created with the EKS stack.
output "queue_arn" {
  value = aws_sqs_queue.pgp_events.arn
}

output "dlq_arn" {
  value = aws_sqs_queue.pgp_events_dlq.arn
}

output "catalog_table_arn" {
  value = aws_dynamodb_table.catalog.arn
}

output "inbound_bucket_arn" {
  value = aws_s3_bucket.inbound.arn
}

output "decrypted_bucket_arn" {
  value = aws_s3_bucket.decrypted.arn
}

output "archive_bucket" {
  value = var.archive_enabled ? aws_s3_bucket.archive[0].id : null
}
