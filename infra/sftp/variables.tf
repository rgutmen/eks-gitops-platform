variable "region" {
  type        = string
  description = "AWS region for every resource in this stack."
}

variable "name" {
  type        = string
  default     = "sftp-platform"
  description = "Prefix for resource names."

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9-]{2,30}$", var.name))
    error_message = "Use 3-31 lowercase letters, digits or hyphens."
  }
}

variable "archive_enabled" {
  type        = bool
  default     = false
  description = <<-EOT
    Replicate every inbound object to a Glacier Deep Archive bucket protected by
    S3 Object Lock in COMPLIANCE mode. Locked objects cannot be deleted by anyone,
    including the root user, until the retention period ends. Enabling this is
    effectively irreversible for every object written while it is on.
  EOT
}

variable "archive_retention_years" {
  type        = number
  default     = 7
  description = "Object Lock retention for archived objects."

  validation {
    condition     = var.archive_retention_years >= 1 && var.archive_retention_years <= 10
    error_message = "Retention must be between 1 and 10 years."
  }
}

variable "decrypted_retention_days" {
  type        = number
  default     = 30
  description = "Days decrypted plaintext is kept before S3 deletes it."
}

variable "queue_visibility_timeout" {
  type        = number
  default     = 300
  description = "Seconds a received message stays hidden. Must exceed the worst-case batch processing time."
}

variable "queue_max_receive_count" {
  type        = number
  default     = 3
  description = "Delivery attempts before a message moves to the dead-letter queue."
}

variable "catalog_deletion_protection" {
  type        = bool
  default     = true
  description = "Protect the user catalog table from deletion."
}

variable "force_destroy_inbound" {
  type        = bool
  default     = false
  description = "Allow terraform destroy to empty the inbound and decrypted buckets. Keep false outside disposable environments."
}
