# Forward only new .pgp/.gpg uploads from the inbound bucket to the worker queue.
resource "aws_cloudwatch_event_rule" "pgp_uploads" {
  name        = "${var.name}-pgp-uploads"
  description = "Encrypted files uploaded to the SFTP inbound bucket"

  event_pattern = jsonencode({
    source      = ["aws.s3"]
    detail-type = ["Object Created"]
    detail = {
      bucket = { name = [aws_s3_bucket.inbound.id] }
      object = {
        key = [{ suffix = ".pgp" }, { suffix = ".gpg" }]
      }
    }
  })
}

resource "aws_cloudwatch_event_target" "pgp_uploads" {
  rule      = aws_cloudwatch_event_rule.pgp_uploads.name
  target_id = "pgp-worker-queue"
  arn       = aws_sqs_queue.pgp_events.arn
}
