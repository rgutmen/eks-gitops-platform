"""Source-IP allowlist for the SFTP endpoint, spread over a pool of security groups.

A security group holds a limited number of inbound rules, and an endpoint can
carry only a few groups. The pool hides both limits: callers describe what
each user may reach, and the pool places one ingress rule per (user, CIDR) on
TCP 22 across as many tagged groups as needed. Operators never see a group.

Rules carry the owner in their description ("sftp-user:<name>"). A CIDR shared
by several users gets one rule per user, so removing one user never revokes
another user's access.

New groups attach to the server's VPC endpoint with ec2:ModifyVpcEndpoint;
Transfer Family's UpdateServer cannot change security groups after creation.
"""

from dataclasses import dataclass

SFTP_PORT = 22
POOL_TAG = "sftp-platform/pool"
POOL_VALUE = "sftp-clients"
RULE_PREFIX = "sftp-user:"


class PoolExhausted(RuntimeError):
    """The endpoint already carries the maximum number of security groups."""


@dataclass(frozen=True)
class Rule:
    group_id: str
    rule_id: str
    cidr: str
    owner: str


class SecurityGroupPool:
    def __init__(self, ec2, vpc_id: str, vpc_endpoint_id: str, name: str,
                 rules_per_group: int = 60, max_groups: int = 5):
        self.ec2 = ec2
        self.vpc_id = vpc_id
        self.vpc_endpoint_id = vpc_endpoint_id
        self.name = name
        self.rules_per_group = rules_per_group
        self.max_groups = max_groups

    # --- reads ---------------------------------------------------------------

    def groups(self) -> list[str]:
        response = self.ec2.describe_security_groups(Filters=[
            {"Name": "vpc-id", "Values": [self.vpc_id]},
            {"Name": f"tag:{POOL_TAG}", "Values": [POOL_VALUE]},
        ])
        return sorted(g["GroupId"] for g in response["SecurityGroups"])

    def rules(self) -> list[Rule]:
        groups = self.groups()
        if not groups:
            return []
        paginator = self.ec2.get_paginator("describe_security_group_rules")
        found = []
        for page in paginator.paginate(Filters=[{"Name": "group-id", "Values": groups}]):
            for r in page["SecurityGroupRules"]:
                description = r.get("Description") or ""
                if r.get("IsEgress") or not description.startswith(RULE_PREFIX):
                    continue
                found.append(Rule(r["GroupId"], r["SecurityGroupRuleId"],
                                  r["CidrIpv4"], description[len(RULE_PREFIX):]))
        return found

    def cidrs_for(self, username: str) -> list[str]:
        return sorted(r.cidr for r in self.rules() if r.owner == username)

    def usage(self) -> dict[str, int]:
        """Rules per group, for capacity reporting."""
        counts = {g: 0 for g in self.groups()}
        for r in self.rules():
            counts[r.group_id] = counts.get(r.group_id, 0) + 1
        return counts

    # --- writes --------------------------------------------------------------

    def apply(self, username: str, cidrs: list[str]) -> None:
        """Make the user's allowlist exactly `cidrs`, adding before revoking."""
        current = [r for r in self.rules() if r.owner == username]
        have = {r.cidr for r in current}
        wanted = set(cidrs)
        for cidr in sorted(wanted - have):
            self._authorize(self._group_with_room(), username, cidr)
        stale = [r for r in current if r.cidr not in wanted]
        self._revoke(stale)

    def remove(self, username: str) -> None:
        self._revoke([r for r in self.rules() if r.owner == username])

    def _authorize(self, group_id: str, username: str, cidr: str) -> None:
        self.ec2.authorize_security_group_ingress(
            GroupId=group_id,
            IpPermissions=[{
                "IpProtocol": "tcp",
                "FromPort": SFTP_PORT,
                "ToPort": SFTP_PORT,
                "IpRanges": [{"CidrIp": cidr, "Description": f"{RULE_PREFIX}{username}"}],
            }],
        )

    def _revoke(self, rules: list[Rule]) -> None:
        by_group: dict[str, list[str]] = {}
        for r in rules:
            by_group.setdefault(r.group_id, []).append(r.rule_id)
        for group_id, rule_ids in by_group.items():
            self.ec2.revoke_security_group_ingress(GroupId=group_id, SecurityGroupRuleIds=rule_ids)

    def _group_with_room(self) -> str:
        usage = self.usage()
        for group_id, used in sorted(usage.items()):
            if used < self.rules_per_group:
                return group_id
        if len(usage) >= self.max_groups:
            raise PoolExhausted(
                f"All {len(usage)} allowlist groups are full ({self.rules_per_group} rules each). "
                "Raise the security-groups-per-interface quota to extend the pool.")
        return self._create_group(len(usage))

    def _create_group(self, index: int) -> str:
        name = f"{self.name}-sftp-clients-{index}"
        group_id = self.ec2.create_security_group(
            GroupName=name,
            Description="SFTP client allowlist (managed by the admin console)",
            VpcId=self.vpc_id,
            TagSpecifications=[{"ResourceType": "security-group", "Tags": [
                {"Key": "Name", "Value": name},
                {"Key": POOL_TAG, "Value": POOL_VALUE},
                {"Key": "sftp-platform/managed", "Value": "admin-web"},
            ]}],
        )["GroupId"]
        # The endpoint must never initiate connections.
        egress = self.ec2.describe_security_group_rules(Filters=[{"Name": "group-id", "Values": [group_id]}])
        egress_ids = [r["SecurityGroupRuleId"] for r in egress["SecurityGroupRules"] if r.get("IsEgress")]
        if egress_ids:
            self.ec2.revoke_security_group_egress(GroupId=group_id, SecurityGroupRuleIds=egress_ids)
        self.ec2.modify_vpc_endpoint(VpcEndpointId=self.vpc_endpoint_id, AddSecurityGroupIds=[group_id])
        return group_id
