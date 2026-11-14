variable "region" {
  type        = string
  description = "AWS region for the container registry."
}

variable "name" {
  type        = string
  default     = "sftp-platform"
  description = "Prefix for resource names."
}

variable "github_repository" {
  type        = string
  description = "GitHub repository allowed to push images, as owner/name."

  validation {
    condition     = can(regex("^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", var.github_repository))
    error_message = "Use the owner/name form, e.g. octocat/my-repo."
  }
}

variable "github_branch" {
  type        = string
  default     = "main"
  description = "Only workflows running on this branch can assume the push role."
}

variable "repositories" {
  type        = list(string)
  default     = ["pgp-worker"]
  description = "ECR repositories to create."
}

variable "keep_image_count" {
  type        = number
  default     = 30
  description = "Most recent tagged images kept per repository; older ones expire."
}

variable "force_delete_repositories" {
  type        = bool
  default     = false
  description = "Allow destroy to delete repositories that still contain images."
}
