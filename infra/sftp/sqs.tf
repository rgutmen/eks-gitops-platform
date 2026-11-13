# Event pipeline: S3 Object Created -> EventBridge rule -> SQS -> worker pods (KEDA).

resource "aws_sqs_queue" "pgp_events_dlq" {
  name                      = "${var.name}-pgp-events-dlq"
  message_retention_seconds = 1209600 # 14 days, the SQS maximum
  sqs_managed_sse_enabled   = true
}

resource "aws_sqs_queue" "pgp_events" {
  name                       = "${var.name}-pgp-events"
  visibility_timeout_seconds = var.queue_visibility_timeout
  message_retention_seconds  = 345600 # 4 days
  receive_wait_time_seconds  = 20
  sqs_managed_sse_enabled    = true

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.pgp_events_dlq.arn
    maxReceiveCount     = var.queue_max_receive_count
  })
}

# Only the DLQ's own source queue may redrive into it.
resource "aws_sqs_queue_redrive_allow_policy" "pgp_events_dlq" {
  queue_url = aws_sqs_queue.pgp_events_dlq.id
  redrive_allow_policy = jsonencode({
    redrivePermission = "byQueue"
    sourceQueueArns   = [aws_sqs_queue.pgp_events.arn]
  })
}

# Only the EventBridge rule below may send messages to the queue.
resource "aws_sqs_queue_policy" "pgp_events" {
  queue_url = aws_sqs_queue.pgp_events.id
  policy    = data.aws_iam_policy_document.pgp_events_queue.json
}

data "aws_iam_policy_document" "pgp_events_queue" {
  statement {
    sid       = "AllowEventBridgeRule"
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.pgp_events.arn]
    principals {
      type        = "Service"
      identifiers = ["events.amazonaws.com"]
    }
    condition {
      test     = "ArnEquals"
      variable = "aws:SourceArn"
      values   = [aws_cloudwatch_event_rule.pgp_uploads.arn]
    }
  }
}
