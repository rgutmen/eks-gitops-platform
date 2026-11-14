output "repository_urls" {
  value       = { for name, repo in aws_ecr_repository.this : name => repo.repository_url }
  description = "Image repository URLs, without tag."
}

output "pgp_worker_repository_url" {
  value       = aws_ecr_repository.this["pgp-worker"].repository_url
  description = "PGP worker image repository, without tag."
}

output "github_push_role_arn" {
  value       = aws_iam_role.github_push.arn
  description = "Set as the AWS_ROLE_ARN variable of the GitHub repository."
}
