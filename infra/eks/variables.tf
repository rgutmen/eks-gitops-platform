variable "region" {
  type        = string
  description = "AWS region of the cluster."
}

variable "cluster_name" {
  type        = string
  default     = "eks-platform"
  description = "EKS cluster name. Must match the network stack's cluster_name tags."
}

variable "kubernetes_version" {
  type        = string
  default     = "1.34"
  description = "Kubernetes minor version."
}

variable "vpc_id" {
  type        = string
  description = "VPC ID from the network stack (output vpc_id)."
}

variable "private_subnet_ids" {
  type        = list(string)
  description = "Private subnets from the network stack (output private_subnet_ids)."
}

variable "public_endpoint_cidrs" {
  type        = list(string)
  default     = []
  description = <<-EOT
    CIDRs allowed to reach the Kubernetes API from the internet, for example
    your own IP as ["203.0.113.10/32"]. Empty keeps the API private-only, so
    kubectl must run from inside the VPC (VPN or bastion).
  EOT

  validation {
    condition     = !contains(var.public_endpoint_cidrs, "0.0.0.0/0")
    error_message = "Never open the Kubernetes API to the whole internet."
  }
}

variable "admin_principal_arns" {
  type        = list(string)
  default     = []
  description = "IAM role or user ARNs granted cluster-admin through EKS access entries."
}

variable "system_instance_types" {
  type        = list(string)
  default     = ["t3.large"]
  description = "Instance types for the system node group."
}

variable "system_node_count" {
  type        = number
  default     = 2
  description = "Desired nodes in the system node group (one per AZ is the minimum for resilience)."
}
