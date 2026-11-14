variable "region" {
  type        = string
  description = "AWS region."
}

variable "name" {
  type        = string
  default     = "sftp-platform"
  description = "Prefix for role names."
}

# --- From the eks stack -------------------------------------------------------

variable "oidc_provider_arn" {
  type        = string
  description = "EKS OIDC provider ARN (eks output oidc_provider_arn)."
}

# --- From the sftp stack ------------------------------------------------------

variable "queue_arn" {
  type        = string
  description = "PGP events queue ARN (sftp output queue_arn)."
}

variable "catalog_table_arn" {
  type        = string
  description = "User catalog table ARN (sftp output catalog_table_arn)."
}

variable "inbound_bucket_arn" {
  type        = string
  description = "Inbound bucket ARN (sftp output inbound_bucket_arn)."
}

variable "decrypted_bucket_arn" {
  type        = string
  description = "Decrypted bucket ARN (sftp output decrypted_bucket_arn)."
}

# --- Kubernetes identities ----------------------------------------------------

variable "worker_service_account" {
  type        = string
  default     = "sftp-platform:pgp-worker"
  description = "namespace:serviceaccount allowed to assume the worker role."
}

variable "keda_service_account" {
  type        = string
  default     = "keda:keda-operator"
  description = "namespace:serviceaccount allowed to assume the KEDA role."
}

variable "pgp_secret_prefix" {
  type        = string
  default     = "sftp/"
  description = "Secrets Manager name prefix holding PGP keys."
}

# --- From the transfer stack --------------------------------------------------

variable "transfer_server_arn" {
  type        = string
  description = "SFTP server ARN (transfer output server_arn)."
}

variable "transfer_user_role_arn" {
  type        = string
  description = "Role assigned to every SFTP user (transfer output user_access_role_arn)."
}

variable "transfer_vpc_id" {
  type        = string
  description = "VPC of the SFTP endpoint (transfer output vpc_id)."
}

variable "transfer_vpc_endpoint_id" {
  type        = string
  description = "SFTP VPC endpoint (transfer output vpc_endpoint_id)."
}

variable "web_service_account" {
  type        = string
  default     = "sftp-platform:sftp-console"
  description = "namespace:serviceaccount allowed to assume the admin web role."
}
