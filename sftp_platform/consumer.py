"""SQS consumer that runs the PGP worker inside a Kubernetes pod.

KEDA scales the Deployment from 0 to N pods based on queue depth. Each pod
long-polls SQS, processes every S3 "Object Created" event delivered through
EventBridge and deletes the message only when processing succeeds. Failed
messages stay in the queue, so SQS redelivers them and finally moves them to
the dead-letter queue.

On SIGTERM (scale-in or rollout) the pod finishes the current batch and exits.

Configuration (environment variables):
  QUEUE_URL         SQS queue receiving the EventBridge events
  CATALOG_TABLE     DynamoDB table holding the user catalog
  ALLOWED_BUCKETS   comma-separated bucket allowlist
  OUTPUT_BUCKET     bucket receiving decrypted plaintext
  AWS_REGION        AWS region
AWS credentials come from the pod's ServiceAccount through IRSA.
"""

import json
import os
import signal

import boto3

from sftp_platform.dynamo import load_catalog
from sftp_platform.worker import S3Store, SecretStore, process_object

BATCH_SIZE = 10
WAIT_SECONDS = 20


class Consumer:
    def __init__(self, sqs, table, store, secrets, queue_url: str, buckets: list[str], output_bucket: str):
        self.sqs = sqs
        self.table = table
        self.store = store
        self.secrets = secrets
        self.queue_url = queue_url
        self.buckets = buckets
        self.output_bucket = output_bucket
        self.running = True

    def stop(self, *_):
        self.running = False

    def run(self) -> None:
        while self.running:
            messages = self.sqs.receive_message(
                QueueUrl=self.queue_url,
                MaxNumberOfMessages=BATCH_SIZE,
                WaitTimeSeconds=WAIT_SECONDS,
            ).get("Messages", [])
            if messages:
                self.handle_batch(messages)

    def handle_batch(self, messages: list[dict]) -> None:
        users, _, _ = load_catalog(self.table)
        for message in messages:
            try:
                bucket, key, etag, size = parse_event(message["Body"])
                if bucket not in self.buckets:
                    raise ValueError("bucket not in allowlist")
                result = process_object(bucket, key, etag, size, users.values(), self.store,
                                        self.secrets, self.output_bucket)
            except Exception as exc:
                log("failed", message["MessageId"], error=type(exc).__name__)
                continue
            self.sqs.delete_message(QueueUrl=self.queue_url, ReceiptHandle=message["ReceiptHandle"])
            log(result, message["MessageId"])


def parse_event(body: str) -> tuple[str, str, str, int]:
    """Extract bucket, key, ETag and size from an EventBridge S3 Object Created event."""
    detail = json.loads(body)["detail"]
    obj = detail["object"]
    return detail["bucket"]["name"], obj["key"], obj.get("etag", ""), int(obj.get("size", 0))


def log(event: str, message_id: str, **fields) -> None:
    print(json.dumps({"event": event, "message_id": message_id, **fields}), flush=True)


def main() -> None:
    region = os.environ["AWS_REGION"]
    session = boto3.Session(region_name=region)
    consumer = Consumer(
        sqs=session.client("sqs"),
        table=session.resource("dynamodb").Table(os.environ["CATALOG_TABLE"]),
        store=S3Store(session.client("s3")),
        secrets=SecretStore(session.client("secretsmanager")),
        queue_url=os.environ["QUEUE_URL"],
        buckets=os.environ["ALLOWED_BUCKETS"].split(","),
        output_bucket=os.environ["OUTPUT_BUCKET"],
    )
    signal.signal(signal.SIGTERM, consumer.stop)
    signal.signal(signal.SIGINT, consumer.stop)
    consumer.run()


if __name__ == "__main__":
    main()
