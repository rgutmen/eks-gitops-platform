# Decrypted plaintext written by the PGP worker.
#
# Kept apart from customer uploads so plaintext is never replicated to the
# compliance archive and never reachable over SFTP. Objects expire after
# var.decrypted_retention_days.

resource "aws_s3_bucket" "decrypted" {
  bucket        = local.decrypted_bucket
  force_destroy = var.force_destroy_inbound
}

resource "aws_s3_bucket_public_access_block" "decrypted" {
  bucket                  = aws_s3_bucket.decrypted.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "decrypted" {
  bucket = aws_s3_bucket.decrypted.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "decrypted" {
  bucket = aws_s3_bucket.decrypted.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "decrypted" {
  bucket = aws_s3_bucket.decrypted.id

  rule {
    id     = "expire-plaintext"
    status = "Enabled"
    filter {}
    expiration {
      days = var.decrypted_retention_days
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }
}

resource "aws_s3_bucket_policy" "decrypted" {
  bucket     = aws_s3_bucket.decrypted.id
  policy     = data.aws_iam_policy_document.decrypted.json
  depends_on = [aws_s3_bucket_public_access_block.decrypted]
}

data "aws_iam_policy_document" "decrypted" {
  statement {
    sid       = "DenyInsecureTransport"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [aws_s3_bucket.decrypted.arn, "${aws_s3_bucket.decrypted.arn}/*"]
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
