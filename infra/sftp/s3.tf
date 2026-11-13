locals {
  account_id       = data.aws_caller_identity.current.account_id
  inbound_bucket   = "${var.name}-inbound-${local.account_id}"
  decrypted_bucket = "${var.name}-decrypted-${local.account_id}"
  archive_bucket   = "${var.name}-archive-${local.account_id}"
}

# --- Inbound bucket: files uploaded through Transfer Family -------------------

resource "aws_s3_bucket" "inbound" {
  bucket        = local.inbound_bucket
  force_destroy = var.force_destroy_inbound
}

resource "aws_s3_bucket_public_access_block" "inbound" {
  bucket                  = aws_s3_bucket.inbound.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "inbound" {
  bucket = aws_s3_bucket.inbound.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "inbound" {
  bucket = aws_s3_bucket.inbound.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# Versioning is required by replication and protects against overwrites.
resource "aws_s3_bucket_versioning" "inbound" {
  bucket = aws_s3_bucket.inbound.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "inbound" {
  bucket = aws_s3_bucket.inbound.id

  rule {
    id     = "expire-noncurrent-versions"
    status = "Enabled"
    filter {}
    noncurrent_version_expiration {
      noncurrent_days = 30
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

# Emits "Object Created" events to EventBridge for the PGP pipeline.
resource "aws_s3_bucket_notification" "inbound" {
  bucket      = aws_s3_bucket.inbound.id
  eventbridge = true
}

resource "aws_s3_bucket_policy" "inbound" {
  bucket     = aws_s3_bucket.inbound.id
  policy     = data.aws_iam_policy_document.inbound.json
  depends_on = [aws_s3_bucket_public_access_block.inbound]
}

data "aws_iam_policy_document" "inbound" {
  statement {
    sid       = "DenyInsecureTransport"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [aws_s3_bucket.inbound.arn, "${aws_s3_bucket.inbound.arn}/*"]
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
