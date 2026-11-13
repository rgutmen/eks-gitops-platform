variable "region" {
  type        = string
  description = "AWS region for the SFTP endpoint."
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

variable "vpc_cidr" {
  type        = string
  default     = "10.30.0.0/25"
  description = "Dedicated VPC for the SFTP endpoint. Must not overlap the EKS VPC."

  validation {
    condition     = can(cidrhost(var.vpc_cidr, 0)) && tonumber(split("/", var.vpc_cidr)[1]) <= 25
    error_message = "Use a valid CIDR of /25 or larger (two /26 subnets)."
  }
}

variable "inbound_bucket_arn" {
  type        = string
  description = "Inbound bucket ARN from the sftp stack (output inbound_bucket_arn)."
}

variable "log_retention_days" {
  type        = number
  default     = 90
  description = "CloudWatch retention for SFTP session and file-transfer logs."
}
