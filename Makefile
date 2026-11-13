.DEFAULT_GOAL := setup
.PHONY: setup unit test \
	infra-validate infra-deploy infra-destroy \
	deploy-sftp deploy-network deploy-eks deploy-workload-iam deploy-transfer \
	destroy-sftp destroy-network destroy-eks destroy-workload-iam destroy-transfer
export TMPDIR := $(CURDIR)/tmp

# --- Python -------------------------------------------------------------------

setup:
	mkdir -p tmp
	uv sync --frozen

unit: setup
	uv run pytest -q --basetemp=tmp/pytest

test: unit

# --- Infrastructure -----------------------------------------------------------
#
# Stacks and their dependencies:
#   sftp          buckets, SQS, EventBridge, DynamoDB   (no dependencies)
#   network       EKS VPC                                (no dependencies)
#   eks           cluster                                (needs network)
#   workload-iam  IRSA roles for worker and KEDA         (needs eks, sftp)
#   transfer      SFTP server and its VPC                (needs sftp)
#
# Deploy order:  sftp -> network -> eks -> workload-iam -> transfer
# Destroy order: transfer -> workload-iam -> eks -> network -> sftp
#
# Every apply/destroy shows the plan and asks for confirmation.
# Usage: make infra-deploy REGION=eu-west-1 [AWS_PROFILE=...]

INFRA     := infra
STACKS    := sftp network eks workload-iam transfer
TF         = terraform -chdir=$(INFRA)/$(1)
REGION    ?=

define require_region
	@test -n "$(REGION)" || (echo "REGION is required, e.g. make $@ REGION=eu-west-1"; exit 1)
endef

# Variables each stack reads from the outputs of the stacks it depends on.
EKS_VARS = \
	-var 'vpc_id=$(shell $(call TF,network) output -raw vpc_id 2>/dev/null)' \
	-var 'private_subnet_ids=$(shell $(call TF,network) output -json private_subnet_ids 2>/dev/null)'
TRANSFER_VARS = \
	-var 'inbound_bucket_arn=$(shell $(call TF,sftp) output -raw inbound_bucket_arn 2>/dev/null)'
WORKLOAD_IAM_VARS = \
	-var 'oidc_provider_arn=$(shell $(call TF,eks) output -raw oidc_provider_arn 2>/dev/null)' \
	-var 'queue_arn=$(shell $(call TF,sftp) output -raw queue_arn 2>/dev/null)' \
	-var 'catalog_table_arn=$(shell $(call TF,sftp) output -raw catalog_table_arn 2>/dev/null)' \
	-var 'inbound_bucket_arn=$(shell $(call TF,sftp) output -raw inbound_bucket_arn 2>/dev/null)' \
	-var 'decrypted_bucket_arn=$(shell $(call TF,sftp) output -raw decrypted_bucket_arn 2>/dev/null)'

infra-validate:
	@for stack in $(STACKS); do \
		echo "== $$stack"; \
		terraform -chdir=$(INFRA)/$$stack fmt -check -recursive && \
		terraform -chdir=$(INFRA)/$$stack init -backend=false -input=false >/dev/null && \
		terraform -chdir=$(INFRA)/$$stack validate || exit 1; \
	done

infra-deploy: deploy-sftp deploy-network deploy-eks deploy-workload-iam deploy-transfer

infra-destroy: destroy-transfer destroy-workload-iam destroy-eks destroy-network destroy-sftp

deploy-sftp:
	$(require_region)
	$(call TF,sftp) init -input=false
	$(call TF,sftp) apply -var 'region=$(REGION)'

deploy-network:
	$(require_region)
	$(call TF,network) init -input=false
	$(call TF,network) apply -var 'region=$(REGION)'

deploy-eks: deploy-network
	$(require_region)
	$(call TF,eks) init -input=false
	$(call TF,eks) apply -var 'region=$(REGION)' $(EKS_VARS)

deploy-workload-iam: deploy-sftp deploy-eks
	$(require_region)
	$(call TF,workload-iam) init -input=false
	$(call TF,workload-iam) apply -var 'region=$(REGION)' $(WORKLOAD_IAM_VARS)

deploy-transfer: deploy-sftp
	$(require_region)
	$(call TF,transfer) init -input=false
	$(call TF,transfer) apply -var 'region=$(REGION)' $(TRANSFER_VARS)

destroy-transfer:
	$(require_region)
	$(call TF,transfer) destroy -var 'region=$(REGION)' $(TRANSFER_VARS)

destroy-workload-iam:
	$(require_region)
	$(call TF,workload-iam) destroy -var 'region=$(REGION)' $(WORKLOAD_IAM_VARS)

destroy-eks: destroy-workload-iam
	$(require_region)
	$(call TF,eks) destroy -var 'region=$(REGION)' $(EKS_VARS)

destroy-network: destroy-eks
	$(require_region)
	$(call TF,network) destroy -var 'region=$(REGION)'

# The catalog table has deletion protection and buckets refuse to delete while
# they hold objects. Both are deliberate: data is never removed by accident.
destroy-sftp: destroy-transfer destroy-workload-iam
	$(require_region)
	$(call TF,sftp) destroy -var 'region=$(REGION)'
