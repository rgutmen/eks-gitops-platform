# EKS cluster built with the community module terraform-aws-modules/eks.
#
# - Control plane managed by AWS, API private by default.
# - One small managed node group for platform components (CoreDNS, KEDA,
#   Karpenter, Argo CD). Workload nodes are provisioned later by Karpenter.
# - IRSA enabled: pods get short-lived IAM credentials per ServiceAccount.
# - Access entries (not the legacy aws-auth ConfigMap) grant cluster access.

module "eks" {
  source  = "terraform-aws-modules/eks/aws"
  version = "~> 21.0"

  name               = var.cluster_name
  kubernetes_version = var.kubernetes_version

  vpc_id     = var.vpc_id
  subnet_ids = var.private_subnet_ids

  # API endpoint: always reachable inside the VPC; from the internet only for listed CIDRs.
  endpoint_private_access      = true
  endpoint_public_access       = length(var.public_endpoint_cidrs) > 0
  endpoint_public_access_cidrs = var.public_endpoint_cidrs

  # IAM Roles for Service Accounts.
  enable_irsa = true

  # Cluster access through EKS access entries only.
  authentication_mode                      = "API"
  enable_cluster_creator_admin_permissions = false
  access_entries = {
    for i, arn in var.admin_principal_arns : "admin-${i}" => {
      principal_arn = arn
      policy_associations = {
        admin = {
          policy_arn   = "arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy"
          access_scope = { type = "cluster" }
        }
      }
    }
  }

  # AWS-managed add-ons. The VPC CNI must exist before nodes join.
  addons = {
    vpc-cni = {
      before_compute = true
    }
    coredns                = {}
    kube-proxy             = {}
    eks-pod-identity-agent = { before_compute = true }
    aws-ebs-csi-driver     = { service_account_role_arn = module.ebs_csi_irsa.arn }
  }

  eks_managed_node_groups = {
    system = {
      ami_type       = "AL2023_x86_64_STANDARD"
      instance_types = var.system_instance_types
      capacity_type  = "ON_DEMAND"

      min_size     = var.system_node_count
      max_size     = var.system_node_count + 1
      desired_size = var.system_node_count

      labels = { "node-role" = "system" }
    }
  }

  # Lets Karpenter find the security group for the nodes it creates.
  node_security_group_tags = {
    "karpenter.sh/discovery" = var.cluster_name
  }
}

# IRSA role for the EBS CSI driver (persistent volumes for PostgreSQL, Prometheus...).
module "ebs_csi_irsa" {
  source  = "terraform-aws-modules/iam/aws//modules/iam-role-for-service-accounts"
  version = "~> 6.0"

  name                  = "${var.cluster_name}-ebs-csi"
  attach_ebs_csi_policy = true

  oidc_providers = {
    main = {
      provider_arn               = module.eks.oidc_provider_arn
      namespace_service_accounts = ["kube-system:ebs-csi-controller-sa"]
    }
  }
}
