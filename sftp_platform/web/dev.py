"""Run the admin console locally against in-memory AWS (moto).

    uv run python -m sftp_platform.web.dev

Seeds synthetic example clients and starts on http://127.0.0.1:8080 with the
operator `operator` / password `operator-demo`. Nothing touches a real AWS
account: credentials are forced to dummies and every call goes to moto.
moto has no Transfer UpdateUser, so it is emulated on top of moto's backend.
"""

import base64
import os
import secrets
import struct

for name in ("AWS_PROFILE", "AWS_DEFAULT_PROFILE", "AWS_SESSION_TOKEN"):
    os.environ.pop(name, None)
os.environ.update(AWS_ACCESS_KEY_ID="dev", AWS_SECRET_ACCESS_KEY="dev", AWS_DEFAULT_REGION="eu-west-1",
                  AWS_CONFIG_FILE=os.devnull, AWS_SHARED_CREDENTIALS_FILE=os.devnull)

import boto3  # noqa: E402
import uvicorn  # noqa: E402
from moto import mock_aws  # noqa: E402

from sftp_platform.domain import Mapping, SFTPUser  # noqa: E402
from sftp_platform.dynamo import DynamoCatalog  # noqa: E402
from sftp_platform.sgpool import POOL_TAG, POOL_VALUE, SecurityGroupPool  # noqa: E402
from sftp_platform.transfer import TransferUsers  # noqa: E402
from sftp_platform.web.app import Settings, create_app  # noqa: E402
from sftp_platform.web.auth import Authenticator, hash_password  # noqa: E402
from sftp_platform.web.console import Console  # noqa: E402

INBOUND = "sftp-platform-inbound-example"
PARTNER = "sftp-platform-partner-example"


def demo_key(seed: str, comment: str) -> str:
    material = (seed.encode() * 32)[:32]
    blob = struct.pack(">I", 11) + b"ssh-ed25519" + struct.pack(">I", 32) + material
    return f"ssh-ed25519 {base64.b64encode(blob).decode()} {comment}"


class EmulatedTransfer:
    """moto's Transfer client plus the update_user call it lacks."""

    def __init__(self, client):
        self._client = client
        self.exceptions = client.exceptions

    def __getattr__(self, name):
        return getattr(self._client, name)

    def update_user(self, **args):
        from moto.transfer.models import transfer_backends
        backend = transfer_backends["123456789012"]["eu-west-1"]
        user = next(u for u in backend.servers[args["ServerId"]]._users if u.user_name == args["UserName"])
        user.home_directory_mappings = args["HomeDirectoryMappings"]
        user.policy = args["Policy"]


def seed(console: Console) -> None:
    examples = [
        SFTPUser("acme", [demo_key("acme1", "ops@acme"), demo_key("acme2", "rotation-2026")],
                 [Mapping("/inbox", INBOUND, "clients/acme/inbox", pgp="inherit"),
                  Mapping("/partner", PARTNER, "acme/exchange", pgp="sftp/acme/partner"),
                  Mapping("/reports", INBOUND, "clients/acme/reports", access="read")],
                 ["52.18.10.24/32", "52.18.10.25/32", "34.240.12.0/28"], pgp_secret_ref="sftp/acme/pgp"),
        SFTPUser("northwind", [demo_key("nwnd1", "sftp@northwind")],
                 [Mapping("/upload", INBOUND, "clients/northwind/upload")],
                 ["18.200.31.7/32"]),
        SFTPUser("globex-ledger", [demo_key("glbx1", "finance@globex")],
                 [Mapping("/statements", INBOUND, "clients/globex/statements", pgp="sftp/globex/pgp"),
                  Mapping("/archive", INBOUND, "clients/globex/archive", access="read")],
                 [f"63.35.{i}.0/24" for i in range(1, 9)]),
        SFTPUser("initech", [demo_key("intc1", "batch@initech")],
                 [Mapping("/drop", PARTNER, "initech/drop")],
                 ["3.248.90.14/32", "3.248.90.15/32"]),
    ]
    for user in examples:
        console.save(user, edit=False, expected_revision=None)
    # Show a held client: its last change did not reach AWS.
    held = SFTPUser("umbrella", [demo_key("umbr1", "it@umbrella")],
                    [Mapping("/in", INBOUND, "clients/umbrella/in")], ["54.76.120.3/32"])
    console.catalog.save(held, lambda _user: (_ for _ in ()).throw(RuntimeError("Transfer unavailable")))


def build():
    s3 = boto3.client("s3")
    for bucket in (INBOUND, PARTNER):
        s3.create_bucket(Bucket=bucket, CreateBucketConfiguration={"LocationConstraint": "eu-west-1"})
    ddb = boto3.resource("dynamodb")
    table = ddb.create_table(TableName="catalog", KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
                             AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
                             BillingMode="PAY_PER_REQUEST")
    ec2 = boto3.client("ec2")
    vpc = ec2.create_vpc(CidrBlock="10.30.0.0/25")["Vpc"]["VpcId"]
    subnet = ec2.create_subnet(VpcId=vpc, CidrBlock="10.30.0.0/26")["Subnet"]["SubnetId"]
    sg = ec2.create_security_group(GroupName="sftp-platform-sftp-clients-0", Description="pool", VpcId=vpc,
                                   TagSpecifications=[{"ResourceType": "security-group",
                                                       "Tags": [{"Key": POOL_TAG, "Value": POOL_VALUE}]}])["GroupId"]
    endpoint = ec2.create_vpc_endpoint(VpcId=vpc, ServiceName="com.amazonaws.eu-west-1.transfer.server",
                                       VpcEndpointType="Interface", SubnetIds=[subnet],
                                       SecurityGroupIds=[sg])["VpcEndpoint"]["VpcEndpointId"]
    transfer = boto3.client("transfer")
    server = transfer.create_server(IdentityProviderType="SERVICE_MANAGED", Protocols=["SFTP"])["ServerId"]
    buckets = [INBOUND, PARTNER]
    console = Console(
        catalog=DynamoCatalog(table, buckets),
        transfer=TransferUsers(EmulatedTransfer(transfer), server, "arn:aws:iam::123456789012:role/sftp-user"),
        pool=SecurityGroupPool(ec2, vpc, endpoint, "sftp-platform", rules_per_group=60, max_groups=5),
        buckets=buckets,
    )
    seed(console)
    auth = Authenticator({"operator": hash_password("operator-demo")}, secrets.token_urlsafe(48))
    return create_app(console, auth, Settings(hostnames=["63.32.67.223", "54.195.17.102"], cookie_secure=False))


def main() -> None:
    with mock_aws():
        app = build()
        port = int(os.environ.get("PORT", "8080"))
        host = os.environ.get("HOST", "127.0.0.1")
        print(f"SFTP Console (synthetic data) on http://{host}:{port}  operator / operator-demo")
        uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
