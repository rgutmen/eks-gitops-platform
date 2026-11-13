# Annotate each ServiceAccount with its role:
#   eks.amazonaws.com/role-arn: <role arn>

output "pgp_worker_role_arn" {
  value = module.pgp_worker.arn
}

output "keda_role_arn" {
  value = module.keda.arn
}
