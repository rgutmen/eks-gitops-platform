# SFTP user catalog. Stores user configuration and secret references only;
# PGP private keys live in Secrets Manager.
resource "aws_dynamodb_table" "catalog" {
  name                        = "${var.name}-catalog"
  billing_mode                = "PAY_PER_REQUEST"
  hash_key                    = "pk"
  deletion_protection_enabled = var.catalog_deletion_protection

  attribute {
    name = "pk"
    type = "S"
  }

  server_side_encryption {
    enabled = true
  }

  point_in_time_recovery {
    enabled = true
  }
}
