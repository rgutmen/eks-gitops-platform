# Optional compliance archive (var.archive_enabled).
#
# Every object written to the inbound bucket is replicated to a separate bucket
# with Object Lock in COMPLIANCE mode and stored directly in Glacier Deep
# Archive. Nobody, including the account root user, can delete or shorten the
# retention of an archived object until it expires.

resource "aws_s3_bucket" "archive" {
  count               = var.archive_enabled ? 1 : 0
  bucket              = local.archive_bucket
  object_lock_enabled = true
}

resource "aws_s3_bucket_public_access_block" "archive" {
  count                   = var.archive_enabled ? 1 : 0
  bucket                  = aws_s3_bucket.archive[0].id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "archive" {
  count  = var.archive_enabled ? 1 : 0
  bucket = aws_s3_bucket.archive[0].id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "archive" {
  count  = var.archive_enabled ? 1 : 0
  bucket = aws_s3_bucket.archive[0].id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_versioning" "archive" {
  count  = var.archive_enabled ? 1 : 0
  bucket = aws_s3_bucket.archive[0].id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_object_lock_configuration" "archive" {
  count  = var.archive_enabled ? 1 : 0
  bucket = aws_s3_bucket.archive[0].id
  rule {
    default_retention {
      mode  = "COMPLIANCE"
      years = var.archive_retention_years
    }
  }
  depends_on = [aws_s3_bucket_versioning.archive]
}

resource "aws_s3_bucket_policy" "archive" {
  count      = var.archive_enabled ? 1 : 0
  bucket     = aws_s3_bucket.archive[0].id
  policy     = data.aws_iam_policy_document.archive[0].json
  depends_on = [aws_s3_bucket_public_access_block.archive]
}

data "aws_iam_policy_document" "archive" {
  count = var.archive_enabled ? 1 : 0

  statement {
    sid       = "DenyInsecureTransport"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [aws_s3_bucket.archive[0].arn, "${aws_s3_bucket.archive[0].arn}/*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

# --- Replication role ---------------------------------------------------------

data "aws_iam_policy_document" "replication_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["s3.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }
}

resource "aws_iam_role" "replication" {
  count              = var.archive_enabled ? 1 : 0
  name               = "${var.name}-archive-replication"
  assume_role_policy = data.aws_iam_policy_document.replication_trust.json
}

data "aws_iam_policy_document" "replication" {
  count = var.archive_enabled ? 1 : 0

  statement {
    actions   = ["s3:GetReplicationConfiguration", "s3:ListBucket"]
    resources = [aws_s3_bucket.inbound.arn]
  }
  statement {
    actions = [
      "s3:GetObjectVersionForReplication",
      "s3:GetObjectVersionAcl",
      "s3:GetObjectVersionTagging",
      "s3:GetObjectRetention",
      "s3:GetObjectLegalHold",
    ]
    resources = ["${aws_s3_bucket.inbound.arn}/*"]
  }
  statement {
    actions   = ["s3:ReplicateObject", "s3:ReplicateTags", "s3:ObjectOwnerOverrideToBucketOwner"]
    resources = ["${aws_s3_bucket.archive[0].arn}/*"]
  }
}

resource "aws_iam_role_policy" "replication" {
  count  = var.archive_enabled ? 1 : 0
  name   = "replicate-to-archive"
  role   = aws_iam_role.replication[0].id
  policy = data.aws_iam_policy_document.replication[0].json
}

# Replicates every customer upload. Decrypted plaintext lives in a separate
# bucket (decrypted.tf), so it is never archived.
resource "aws_s3_bucket_replication_configuration" "inbound_to_archive" {
  count  = var.archive_enabled ? 1 : 0
  bucket = aws_s3_bucket.inbound.id
  role   = aws_iam_role.replication[0].arn

  rule {
    id     = "archive-uploads"
    status = "Enabled"

    filter {}

    delete_marker_replication {
      status = "Disabled"
    }

    destination {
      bucket        = aws_s3_bucket.archive[0].arn
      storage_class = "DEEP_ARCHIVE"
    }
  }

  depends_on = [aws_s3_bucket_versioning.inbound, aws_s3_bucket_object_lock_configuration.archive]
}
