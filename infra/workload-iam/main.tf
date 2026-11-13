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
