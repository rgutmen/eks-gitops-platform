terraform {
  required_version = ">= 1.10.0"

  # Settings come from infra/backend.hcl (see backend.hcl.example).
  backend "s3" {}

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 6.0, < 7.0"
    }
  }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project   = var.name
      ManagedBy = "terraform"
      Component = "network"
    }
  }
}

data "aws_availability_zones" "available" {
  state = "available"
}
