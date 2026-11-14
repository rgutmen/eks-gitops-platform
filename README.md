# eks-gitops-platform

Kubernetes platform on Amazon EKS managed through GitOps, running a secure SFTP service with PGP decryption.

## Requirements

- Python 3.14+ and [uv](https://docs.astral.sh/uv/)
- OpenTofu, AWS CLI, kubectl, Helm, Docker

## Development

```
make setup                          # install dependencies from uv.lock
make unit                           # run the test suite
uv run python -m sftp_platform.web.dev   # console on synthetic data, no AWS
```

## Deploy

One-time: copy `infra/backend.hcl.example` to `infra/backend.hcl` and run
`make infra-tfvars`, then review each `infra/<stack>/terraform.tfvars`.

```
make up REGION=eu-west-1      # every stack, KEDA, image, both workloads
make console                  # admin console on http://localhost:8080
```

Each stack can also be deployed alone (`make deploy-<stack>`), see the
Makefile header for the order.

## Admin console

Operators create SFTP clients with their SSH keys, source addresses and
virtual folders. Saving a client creates or updates the AWS Transfer Family
user and opens its addresses on port 22; the client can connect right away.
The register can be exported to Excel.

The console has no public endpoint. It is reached with `kubectl port-forward`
and an operator login (`make k8s-console-secret` sets the password).

## Tear down

```
make destroy-transfer         # stop only the SFTP endpoint (the costly part)
make down                     # delete everything except the state bucket
```

`make down` removes stored files, the client catalog and all container images.
It asks you to type `destroy` and then confirms each stack.
