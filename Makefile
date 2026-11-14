.DEFAULT_GOAL := setup
.PHONY: setup unit test \
	infra-tfvars infra-validate infra-deploy infra-destroy kubeconfig k8s-config k8s-render \
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
# Per-stack settings live in infra/<stack>/terraform.tfvars (git-ignored),
# created from terraform.tfvars.example with: make infra-tfvars
# Usage: make infra-deploy [REGION=eu-west-1]   (defaults to $AWS_REGION)

INFRA     := infra
STACKS    := sftp network eks workload-iam transfer
TOFU      ?= tofu
TF         = $(TOFU) -chdir=$(INFRA)/$(1)
REGION    ?= $(AWS_REGION)

# Remote state: shared settings in infra/backend.hcl, one key per stack.
BACKEND_CONFIG := $(CURDIR)/$(INFRA)/backend.hcl
STATE_PREFIX   ?= eks-gitops-platform
# $(call INIT,stack)
INIT = $(call TF,$(1)) init -input=false -backend-config=$(BACKEND_CONFIG) -backend-config=key=$(STATE_PREFIX)/$(1)/terraform.tfstate

define require_backend
	@test -f $(BACKEND_CONFIG) || (echo "Missing $(INFRA)/backend.hcl. Copy $(INFRA)/backend.hcl.example and set your state bucket."; exit 1)
endef

define require_region
	@test -n "$(REGION)" || (echo "REGION is required, e.g. make $@ REGION=eu-west-1"; exit 1)
endef

# $(call require_tfvars,stack): stop if the stack has no terraform.tfvars yet.
define require_tfvars
	@test -f $(INFRA)/$(1)/terraform.tfvars || (echo "Missing $(INFRA)/$(1)/terraform.tfvars. Run: make infra-tfvars"; exit 1)
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

# Create terraform.tfvars from each example. Never overwrites an existing file.
infra-tfvars:
	@for stack in $(STACKS); do \
		if [ -f $(INFRA)/$$stack/terraform.tfvars ]; then \
			echo "keep    $(INFRA)/$$stack/terraform.tfvars"; \
		else \
			cp $(INFRA)/$$stack/terraform.tfvars.example $(INFRA)/$$stack/terraform.tfvars; \
			echo "created $(INFRA)/$$stack/terraform.tfvars"; \
		fi; \
	done

infra-validate:
	@for stack in $(STACKS); do \
		echo "== $$stack"; \
		$(TOFU) -chdir=$(INFRA)/$$stack fmt -check -recursive && \
		$(TOFU) -chdir=$(INFRA)/$$stack init -backend=false -input=false >/dev/null && \
		$(TOFU) -chdir=$(INFRA)/$$stack validate || exit 1; \
	done

infra-deploy: deploy-sftp deploy-network deploy-eks deploy-workload-iam deploy-transfer

infra-destroy: destroy-transfer destroy-workload-iam destroy-eks destroy-network destroy-sftp

deploy-sftp:
	$(require_region)
	$(call require_tfvars,sftp)
	$(require_backend)
	$(call INIT,sftp)
	$(call TF,sftp) apply -var 'region=$(REGION)'

deploy-network:
	$(require_region)
	$(call require_tfvars,network)
	$(require_backend)
	$(call INIT,network)
	$(call TF,network) apply -var 'region=$(REGION)'

deploy-eks: deploy-network
	$(require_region)
	$(call require_tfvars,eks)
	$(require_backend)
	$(call INIT,eks)
	$(call TF,eks) apply -var 'region=$(REGION)' $(EKS_VARS)
	$(MAKE) --no-print-directory kubeconfig

# Point kubectl at the current cluster. Each new cluster gets a new API endpoint,
# so this must run after every (re)creation.
kubeconfig:
	$(require_region)
	aws eks update-kubeconfig --region $(REGION) --name "$$($(call TF,eks) output -raw cluster_name)"

# --- Kubernetes manifests -----------------------------------------------------
#
# The AWS overlay reads two git-ignored files generated from OpenTofu outputs:
#   config.env  application settings (ConfigMap)
#   deploy.env  IRSA role ARN and container image

WORKER_OVERLAY := k8s/pgp-worker/overlays/aws
WORKER_TAG     ?= latest

k8s-config:
	$(require_region)
	@set -e; \
	out() { $(TOFU) -chdir=$(INFRA)/$$1 output -raw $$2; }; \
	account=$$(aws sts get-caller-identity --query Account --output text); \
	{ \
		echo "# Generated by make k8s-config. Do not edit."; \
		echo "AWS_REGION=$(REGION)"; \
		echo "QUEUE_URL=$$(out sftp queue_url)"; \
		echo "CATALOG_TABLE=$$(out sftp catalog_table)"; \
		echo "ALLOWED_BUCKETS=$$(out sftp allowed_buckets)"; \
		echo "OUTPUT_BUCKET=$$(out sftp output_bucket)"; \
	} > $(WORKER_OVERLAY)/config.env; \
	{ \
		echo "# Generated by make k8s-config. Do not edit."; \
		echo "WORKER_ROLE_ARN=$$(out workload-iam pgp_worker_role_arn)"; \
		echo "WORKER_IMAGE=$$account.dkr.ecr.$(REGION).amazonaws.com/pgp-worker:$(WORKER_TAG)"; \
	} > $(WORKER_OVERLAY)/deploy.env; \
	echo "wrote $(WORKER_OVERLAY)/config.env and deploy.env"

# Print the final manifests without touching the cluster.
k8s-render:
	@test -f $(WORKER_OVERLAY)/config.env || (echo "Missing config.env. Run: make k8s-config"; exit 1)
	kubectl kustomize $(WORKER_OVERLAY)

deploy-workload-iam: deploy-sftp deploy-eks
	$(require_region)
	$(call require_tfvars,workload-iam)
	$(require_backend)
	$(call INIT,workload-iam)
	$(call TF,workload-iam) apply -var 'region=$(REGION)' $(WORKLOAD_IAM_VARS)

deploy-transfer: deploy-sftp
	$(require_region)
	$(call require_tfvars,transfer)
	$(require_backend)
	$(call INIT,transfer)
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
