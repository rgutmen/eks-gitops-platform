.DEFAULT_GOAL := setup
.PHONY: setup unit test up down \
	infra-tfvars infra-validate infra-deploy infra-destroy kubeconfig k8s-config k8s-render k8s-keda \
	k8s-console-secret k8s-apply k8s-delete image console \
	deploy-ci deploy-sftp deploy-network deploy-eks deploy-workload-iam deploy-transfer \
	destroy-ci destroy-sftp destroy-network destroy-eks destroy-workload-iam destroy-transfer
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
#   ci            ECR and GitHub Actions push role       (no dependencies)
#   sftp          buckets, SQS, EventBridge, DynamoDB   (no dependencies)
#   network       EKS VPC                                (no dependencies)
#   eks           cluster                                (needs network)
#   transfer      SFTP server and its VPC                (needs sftp)
#   workload-iam  IRSA roles: worker, KEDA, console      (needs eks, sftp, transfer)
#
# Deploy order:  ci -> sftp -> network -> eks -> transfer -> workload-iam
# Destroy order: workload-iam -> transfer -> eks -> network -> sftp -> ci
#
# destroy-transfer alone stops the expensive SFTP endpoint. The console role
# keeps its policy; re-apply workload-iam after redeploying transfer.
#
# Every apply/destroy shows the plan and asks for confirmation, unless
# AUTO_APPROVE=1 is set (used by `make up` / `make down` when requested).
# Per-stack settings live in infra/<stack>/terraform.tfvars (git-ignored),
# created from terraform.tfvars.example with: make infra-tfvars
# Usage: make infra-deploy [REGION=eu-west-1]   (defaults to $AWS_REGION)

INFRA     := infra
STACKS    := ci sftp network eks workload-iam transfer
TOFU      ?= tofu
TF         = $(TOFU) -chdir=$(INFRA)/$(1)
REGION    ?= $(AWS_REGION)
APPROVE    = $(if $(AUTO_APPROVE),-auto-approve,)

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
	-var 'decrypted_bucket_arn=$(shell $(call TF,sftp) output -raw decrypted_bucket_arn 2>/dev/null)' \
	-var 'transfer_server_arn=$(shell $(call TF,transfer) output -raw server_arn 2>/dev/null)' \
	-var 'transfer_user_role_arn=$(shell $(call TF,transfer) output -raw user_access_role_arn 2>/dev/null)' \
	-var 'transfer_vpc_id=$(shell $(call TF,transfer) output -raw vpc_id 2>/dev/null)' \
	-var 'transfer_vpc_endpoint_id=$(shell $(call TF,transfer) output -raw vpc_endpoint_id 2>/dev/null)'

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
	@mkdir -p tmp
	@for stack in $(STACKS); do \
		echo "== $$stack"; \
		$(TOFU) -chdir=$(INFRA)/$$stack fmt -check -recursive && \
		$(TOFU) -chdir=$(INFRA)/$$stack init -backend=false -input=false >/dev/null && \
		$(TOFU) -chdir=$(INFRA)/$$stack validate || exit 1; \
	done

infra-deploy: deploy-ci deploy-sftp deploy-network deploy-eks deploy-transfer deploy-workload-iam

infra-destroy: destroy-workload-iam destroy-transfer destroy-eks destroy-network destroy-sftp destroy-ci

deploy-ci:
	$(require_region)
	$(call require_tfvars,ci)
	$(require_backend)
	$(call INIT,ci)
	$(call TF,ci) apply $(APPROVE) -var 'region=$(REGION)'

deploy-sftp:
	$(require_region)
	$(call require_tfvars,sftp)
	$(require_backend)
	$(call INIT,sftp)
	$(call TF,sftp) apply $(APPROVE) -var 'region=$(REGION)'

deploy-network:
	$(require_region)
	$(call require_tfvars,network)
	$(require_backend)
	$(call INIT,network)
	$(call TF,network) apply $(APPROVE) -var 'region=$(REGION)'

deploy-eks: deploy-network
	$(require_region)
	$(call require_tfvars,eks)
	$(require_backend)
	$(call INIT,eks)
	$(call TF,eks) apply $(APPROVE) -var 'region=$(REGION)' $(EKS_VARS)
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

WORKER_OVERLAY  := k8s/pgp-worker/overlays/aws
CONSOLE_OVERLAY := k8s/sftp-console/overlays/aws
# Images are immutable and tagged with the commit SHA that built them.
WORKER_TAG     ?= $(shell git rev-parse --short=12 HEAD)

k8s-config:
	$(require_region)
	@set -e; \
	out() { $(TOFU) -chdir=$(INFRA)/$$1 output -raw $$2; }; \
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
		echo "WORKER_IMAGE=$$(out ci pgp_worker_repository_url):$(WORKER_TAG)"; \
	} > $(WORKER_OVERLAY)/deploy.env; \
	{ \
		echo "# Generated by make k8s-config. Do not edit."; \
		echo "AWS_REGION=$(REGION)"; \
		echo "CATALOG_TABLE=$$(out sftp catalog_table)"; \
		echo "ALLOWED_BUCKETS=$$(out sftp allowed_buckets)"; \
		echo "TRANSFER_SERVER_ID=$$(out transfer server_id)"; \
		echo "TRANSFER_USER_ROLE=$$(out transfer user_access_role_arn)"; \
		echo "TRANSFER_VPC_ID=$$(out transfer vpc_id)"; \
		echo "TRANSFER_ENDPOINT_ID=$$(out transfer vpc_endpoint_id)"; \
		echo "SFTP_HOSTNAMES=$$($(TOFU) -chdir=$(INFRA)/transfer output -json sftp_public_ips | tr -d '[]" ')"; \
		echo "COOKIE_SECURE=false"; \
	} > $(CONSOLE_OVERLAY)/config.env; \
	{ \
		echo "# Generated by make k8s-config. Do not edit."; \
		echo "CONSOLE_ROLE_ARN=$$(out workload-iam console_role_arn)"; \
		echo "CONSOLE_IMAGE=$$(out ci pgp_worker_repository_url):$(WORKER_TAG)"; \
	} > $(CONSOLE_OVERLAY)/deploy.env; \
	echo "wrote config.env and deploy.env for pgp-worker and sftp-console"

# Print the final manifests without touching the cluster.
k8s-render:
	@test -f $(WORKER_OVERLAY)/config.env || (echo "Missing config.env. Run: make k8s-config"; exit 1)
	kubectl kustomize $(WORKER_OVERLAY)
	@echo ---
	kubectl kustomize $(CONSOLE_OVERLAY)

# Console logins and cookie key. Asks for one operator password, never echoes
# it, and keeps an existing file unless FORCE=1.
CONSOLE_OPERATOR ?= operator
k8s-console-secret: setup
	@if [ -f $(CONSOLE_OVERLAY)/secret.env ] && [ -z "$(FORCE)" ]; then \
		echo "keep    $(CONSOLE_OVERLAY)/secret.env (FORCE=1 to replace)"; \
	else \
		hash=$$(uv run python -m sftp_platform.web.auth hash) || exit 1; \
		umask 077; { \
			echo "OPERATORS=$(CONSOLE_OPERATOR):$$hash"; \
			echo "SESSION_SECRET=$$(openssl rand -base64 48 | tr -d '\n')"; \
		} > $(CONSOLE_OVERLAY)/secret.env; \
		echo "wrote   $(CONSOLE_OVERLAY)/secret.env"; \
	fi

# Apply both workloads. The image tag is the current commit, built by the
# release workflow (or `make image`).
k8s-apply:
	@test -f $(CONSOLE_OVERLAY)/secret.env || (echo "Missing secret.env. Run: make k8s-console-secret"; exit 1)
	kubectl apply -k $(WORKER_OVERLAY)
	kubectl apply -k $(CONSOLE_OVERLAY)
	kubectl -n sftp-platform rollout status deploy/sftp-console --timeout=5m

k8s-delete:
	-kubectl delete -k $(CONSOLE_OVERLAY) --ignore-not-found --wait=true
	-kubectl delete -k $(WORKER_OVERLAY) --ignore-not-found --wait=true
	-helm uninstall keda -n keda --wait

# Open the console on http://localhost:8080 (Ctrl+C to stop).
console:
	kubectl -n sftp-platform port-forward svc/sftp-console 8080:80

# Build and push the image for the current commit from this machine. CI does
# the same on every push to main; use this only when CI is not available.
image:
	$(require_region)
	@set -e; repo=$$($(call TF,ci) output -raw pgp_worker_repository_url); \
	aws ecr get-login-password --region $(REGION) | docker login --username AWS --password-stdin $${repo%%/*}; \
	docker build -t $$repo:$(WORKER_TAG) . && docker push $$repo:$(WORKER_TAG)

# --- Platform add-ons (Helm) --------------------------------------------------

KEDA_CHART_VERSION ?= 2.21.0

# Install or upgrade KEDA with its IRSA role from the workload-iam outputs.
k8s-keda:
	$(require_region)
	helm repo add kedacore https://kedacore.github.io/charts --force-update >/dev/null
	helm upgrade --install keda kedacore/keda \
		--version $(KEDA_CHART_VERSION) \
		--namespace keda --create-namespace \
		--values platform/keda/values.yaml \
		--set podIdentity.aws.irsa.roleArn="$$($(call TF,workload-iam) output -raw keda_role_arn)" \
		--wait --timeout 5m

deploy-workload-iam: deploy-sftp deploy-eks deploy-transfer
	$(require_region)
	$(call require_tfvars,workload-iam)
	$(require_backend)
	$(call INIT,workload-iam)
	$(call TF,workload-iam) apply $(APPROVE) -var 'region=$(REGION)' $(WORKLOAD_IAM_VARS)

deploy-transfer: deploy-sftp
	$(require_region)
	$(call require_tfvars,transfer)
	$(require_backend)
	$(call INIT,transfer)
	$(call TF,transfer) apply $(APPROVE) -var 'region=$(REGION)' $(TRANSFER_VARS)

# Pool security groups the console added (sftp-clients-1..N) are not in the
# OpenTofu state. Detach and delete them first so the VPC can be removed.
destroy-transfer:
	$(require_region)
	@set -e; vpc=$$($(call TF,transfer) output -raw vpc_id 2>/dev/null || true); \
	ep=$$($(call TF,transfer) output -raw vpc_endpoint_id 2>/dev/null || true); \
	first=$$($(call TF,transfer) output -raw client_security_group_id 2>/dev/null || true); \
	[ -n "$$vpc" ] || exit 0; \
	for sg in $$(aws ec2 describe-security-groups --region $(REGION) \
		--filters Name=vpc-id,Values=$$vpc Name=tag:sftp-platform/pool,Values=sftp-clients \
		--query 'SecurityGroups[].GroupId' --output text); do \
		[ "$$sg" = "$$first" ] && continue; \
		echo "removing console-created pool group $$sg"; \
		[ -n "$$ep" ] && aws ec2 modify-vpc-endpoint --region $(REGION) --vpc-endpoint-id $$ep --remove-security-group-ids $$sg >/dev/null || true; \
		aws ec2 delete-security-group --region $(REGION) --group-id $$sg; \
	done
	$(call TF,transfer) destroy $(APPROVE) -var 'region=$(REGION)' $(TRANSFER_VARS)

destroy-workload-iam:
	$(require_region)
	$(call TF,workload-iam) destroy $(APPROVE) -var 'region=$(REGION)' $(WORKLOAD_IAM_VARS)

destroy-eks: destroy-workload-iam
	$(require_region)
	-$(MAKE) --no-print-directory k8s-delete
	$(call TF,eks) destroy $(APPROVE) -var 'region=$(REGION)' $(EKS_VARS)

destroy-network: destroy-eks
	$(require_region)
	$(call TF,network) destroy $(APPROVE) -var 'region=$(REGION)'

# The catalog table has deletion protection and buckets refuse to delete while
# they hold objects. Both are deliberate: data is never removed by accident.
# PURGE=1 lifts both for this run only (used by `make down`): it first applies
# the relaxed settings, then destroys.
SFTP_PURGE = $(if $(PURGE),-var catalog_deletion_protection=false -var force_destroy_inbound=true,)
destroy-sftp: destroy-transfer destroy-workload-iam
	$(require_region)
	$(if $(PURGE),$(call TF,sftp) apply -auto-approve -var 'region=$(REGION)' $(SFTP_PURGE),)
	$(call TF,sftp) destroy $(APPROVE) -var 'region=$(REGION)' $(SFTP_PURGE)

# Repositories refuse to delete while they hold images. PURGE=1 lifts that.
CI_PURGE = $(if $(PURGE),-var force_delete_repositories=true,)
destroy-ci: destroy-sftp
	$(require_region)
	$(if $(PURGE),$(call TF,ci) apply -auto-approve -var 'region=$(REGION)' $(CI_PURGE),)
	$(call TF,ci) destroy $(APPROVE) -var 'region=$(REGION)' $(CI_PURGE)

# --- Whole platform -----------------------------------------------------------
#
#   make up     every stack, KEDA, image, both workloads
#   make down   everything: workloads, KEDA, IRSA roles, SFTP server and its
#               VPC, EKS cluster and nodes, network, buckets with their files,
#               catalog, queues, ECR with its images and the CI role.
#               Only the OpenTofu state bucket is kept.
#
# Each stack still shows its plan and asks for confirmation. AUTO_APPROVE=1
# skips the prompts.

up: infra-deploy k8s-keda k8s-config k8s-console-secret
	@repo=$$($(call TF,ci) output -raw pgp_worker_repository_url); \
	aws ecr describe-images --region $(REGION) --repository-name $${repo##*/} --image-ids imageTag=$(WORKER_TAG) >/dev/null 2>&1 \
		|| $(MAKE) --no-print-directory image
	$(MAKE) --no-print-directory k8s-apply
	@echo; echo "Platform is up. Open the console with: make console  ->  http://localhost:8080"

down:
	$(require_region)
	@echo "This deletes EVERY resource of the platform, including stored files, the"
	@echo "client catalog and all container images. Only the state bucket is kept."
	@if [ -z "$(AUTO_APPROVE)" ]; then read -p "Type 'destroy' to continue: " a && [ "$$a" = destroy ]; fi
	-$(MAKE) --no-print-directory k8s-delete
	$(MAKE) --no-print-directory PURGE=1 destroy-network destroy-ci
