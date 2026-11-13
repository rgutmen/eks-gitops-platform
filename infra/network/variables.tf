variable "region" {
  type        = string
  description = "AWS region for the VPC."
}

variable "name" {
  type        = string
  default     = "eks-platform"
  description = "Prefix for resource names."

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9-]{2,30}$", var.name))
    error_message = "Use 3-31 lowercase letters, digits or hyphens."
  }
}

variable "cluster_name" {
  type        = string
  default     = "eks-platform"
  description = "EKS cluster name, used for subnet discovery tags."
}

variable "vpc_cidr" {
  type        = string
  default     = "10.20.0.0/16"
  description = "VPC CIDR. Each AZ gets a /20 private and a /24 public subnet."

  validation {
    condition     = can(cidrhost(var.vpc_cidr, 0)) && tonumber(split("/", var.vpc_cidr)[1]) <= 16
    error_message = "Use a valid CIDR of /16 or larger."
  }
}

variable "az_count" {
  type        = number
  default     = 2
  description = "Number of availability zones."

  validation {
    condition     = var.az_count >= 2 && var.az_count <= 3
    error_message = "EKS needs at least 2 AZs; this stack supports up to 3."
  }
}

variable "single_nat_gateway" {
  type        = bool
  default     = true
  description = "One shared NAT gateway (cheaper, single-AZ egress) instead of one per AZ."
}
