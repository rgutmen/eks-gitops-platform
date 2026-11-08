# eks-gitops-platform

Kubernetes platform on Amazon EKS managed through GitOps, running a secure SFTP service with PGP decryption.

## Requirements

- Python 3.14+
- [uv](https://docs.astral.sh/uv/)

## Development

```
make setup   # install dependencies from uv.lock
make unit    # run the test suite
```
