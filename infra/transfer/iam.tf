data "aws_caller_identity" "current" {}

data "aws_iam_policy_document" "transfer_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["transfer.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }
}

# --- Logging role -------------------------------------------------------------

resource "aws_iam_role" "logging" {
  name               = "${var.name}-transfer-logging"
  assume_role_policy = data.aws_iam_policy_document.transfer_trust.json
}

resource "aws_iam_role_policy_attachment" "logging" {
  role       = aws_iam_role.logging.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSTransferLoggingAccess"
}

# --- User access role ---------------------------------------------------------
#
# Assumed by Transfer Family on behalf of every SFTP user. It grants access to
# the inbound bucket as a whole; each user is confined to their own folders by
# LOGICAL home-directory mappings and a per-user session policy set by the web.

resource "aws_iam_role" "user_access" {
  name               = "${var.name}-transfer-user-access"
  assume_role_policy = data.aws_iam_policy_document.transfer_trust.json
}

data "aws_iam_policy_document" "user_access" {
  statement {
    sid       = "ListInbound"
    actions   = ["s3:ListBucket", "s3:GetBucketLocation"]
    resources = [var.inbound_bucket_arn]
  }
  statement {
    sid = "ReadWriteInboundObjects"
    actions = [
      "s3:GetObject",
      "s3:GetObjectVersion",
      "s3:PutObject",
      "s3:DeleteObject",
      "s3:DeleteObjectVersion",
    ]
    resources = ["${var.inbound_bucket_arn}/*"]
  }
}

resource "aws_iam_role_policy" "user_access" {
  name   = "inbound-bucket"
  role   = aws_iam_role.user_access.id
  policy = data.aws_iam_policy_document.user_access.json
}
