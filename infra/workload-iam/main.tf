# IRSA roles for workloads running in EKS.
#
# Each role can only be assumed by one Kubernetes ServiceAccount and grants
# the exact actions on the exact resources that workload needs.

locals {
  secrets_arn = "arn:aws:secretsmanager:${var.region}:${data.aws_caller_identity.current.account_id}:secret:${var.pgp_secret_prefix}*"
}

# --- PGP worker ---------------------------------------------------------------

module "pgp_worker" {
  source  = "terraform-aws-modules/iam/aws//modules/iam-role-for-service-accounts"
  version = ">= 6.0, < 7.0"

  name            = "${var.name}-pgp-worker"
  use_name_prefix = false

  oidc_providers = {
    main = {
      provider_arn               = var.oidc_provider_arn
      namespace_service_accounts = [var.worker_service_account]
    }
  }

  create_inline_policy = true
  inline_policy_permissions = {
    ConsumeQueue = {
      actions = [
        "sqs:ReceiveMessage",
        "sqs:DeleteMessage",
        "sqs:ChangeMessageVisibility",
        "sqs:GetQueueAttributes",
      ]
      resources = [var.queue_arn]
    }
    ReadCatalog = {
      actions   = ["dynamodb:GetItem"]
      resources = [var.catalog_table_arn]
    }
    ReadPgpKeys = {
      actions   = ["secretsmanager:GetSecretValue"]
      resources = [local.secrets_arn]
    }
    ReadUploads = {
      actions   = ["s3:GetObject"]
      resources = ["${var.inbound_bucket_arn}/*"]
    }
    WritePlaintext = {
      actions   = ["s3:PutObject", "s3:GetObject"]
      resources = ["${var.decrypted_bucket_arn}/*"]
    }
    # Without ListBucket, HEAD on a missing key returns 403 instead of 404.
    ListPlaintext = {
      actions   = ["s3:ListBucket"]
      resources = [var.decrypted_bucket_arn]
    }
  }
}

# --- KEDA operator ------------------------------------------------------------

module "keda" {
  source  = "terraform-aws-modules/iam/aws//modules/iam-role-for-service-accounts"
  version = ">= 6.0, < 7.0"

  name            = "${var.name}-keda"
  use_name_prefix = false

  oidc_providers = {
    main = {
      provider_arn               = var.oidc_provider_arn
      namespace_service_accounts = [var.keda_service_account]
    }
  }

  # Read queue depth only. KEDA never receives or deletes messages.
  create_inline_policy = true
  inline_policy_permissions = {
    ReadQueueDepth = {
      actions   = ["sqs:GetQueueAttributes"]
      resources = [var.queue_arn]
    }
  }
}

# --- Admin console ------------------------------------------------------------
#
# Manages SFTP users end to end: the catalog item, the Transfer user and its
# keys, and the client allowlist. EC2 writes are limited to security groups
# tagged as members of the allowlist pool, inside the SFTP VPC.

locals {
  account_id = data.aws_caller_identity.current.account_id
  # try(): the transfer stack may already be gone when this stack is destroyed.
  server_id    = try(element(split("/", var.transfer_server_arn), 1), "none")
  pool_tag_key = "sftp-platform/pool"
  pool_tag     = "sftp-clients"
  sg_arns      = "arn:aws:ec2:${var.region}:${local.account_id}:security-group/*"
}

module "console" {
  source  = "terraform-aws-modules/iam/aws//modules/iam-role-for-service-accounts"
  version = ">= 6.0, < 7.0"

  name            = "${var.name}-console"
  use_name_prefix = false

  oidc_providers = {
    main = {
      provider_arn               = var.oidc_provider_arn
      namespace_service_accounts = [var.web_service_account]
    }
  }

  create_inline_policy = true
  inline_policy_permissions = {
    Catalog = {
      actions   = ["dynamodb:GetItem", "dynamodb:PutItem"]
      resources = [var.catalog_table_arn]
    }
    DescribeServer = {
      actions   = ["transfer:DescribeServer"]
      resources = [var.transfer_server_arn]
    }
    ManageUsers = {
      actions = [
        "transfer:CreateUser",
        "transfer:UpdateUser",
        "transfer:DescribeUser",
        "transfer:DeleteUser",
        "transfer:ImportSshPublicKey",
        "transfer:DeleteSshPublicKey",
      ]
      resources = ["arn:aws:transfer:${var.region}:${local.account_id}:user/${local.server_id}/*"]
    }
    # Users can only be given the single Transfer access role.
    PassUserRole = {
      actions   = ["iam:PassRole"]
      resources = [var.transfer_user_role_arn]
      condition = [{
        test     = "StringEquals"
        variable = "iam:PassedToService"
        values   = ["transfer.amazonaws.com"]
      }]
    }
    # Describe calls have no resource-level permissions.
    DescribeSecurityGroups = {
      actions   = ["ec2:DescribeSecurityGroups", "ec2:DescribeSecurityGroupRules"]
      resources = ["*"]
    }
    EditPoolRules = {
      actions = [
        "ec2:AuthorizeSecurityGroupIngress",
        "ec2:RevokeSecurityGroupIngress",
        "ec2:RevokeSecurityGroupEgress",
      ]
      resources = [local.sg_arns]
      condition = [{
        test     = "StringEquals"
        variable = "aws:ResourceTag/${local.pool_tag_key}"
        values   = [local.pool_tag]
      }]
    }
    # New pool members must be created already tagged, in the SFTP VPC.
    CreatePoolGroup = {
      actions   = ["ec2:CreateSecurityGroup"]
      resources = [local.sg_arns]
      condition = [{
        test     = "StringEquals"
        variable = "aws:RequestTag/${local.pool_tag_key}"
        values   = [local.pool_tag]
      }]
    }
    CreatePoolGroupInVpc = {
      actions   = ["ec2:CreateSecurityGroup"]
      resources = ["arn:aws:ec2:${var.region}:${local.account_id}:vpc/${var.transfer_vpc_id}"]
    }
    TagOnCreate = {
      actions   = ["ec2:CreateTags"]
      resources = [local.sg_arns]
      condition = [{
        test     = "StringEquals"
        variable = "ec2:CreateAction"
        values   = ["CreateSecurityGroup"]
      }]
    }
    AttachPoolGroup = {
      actions   = ["ec2:ModifyVpcEndpoint"]
      resources = ["arn:aws:ec2:${var.region}:${local.account_id}:vpc-endpoint/${var.transfer_vpc_endpoint_id}"]
    }
    AttachPoolGroupMember = {
      actions   = ["ec2:ModifyVpcEndpoint"]
      resources = [local.sg_arns]
      condition = [{
        test     = "StringEquals"
        variable = "aws:ResourceTag/${local.pool_tag_key}"
        values   = [local.pool_tag]
      }]
    }
  }
}
