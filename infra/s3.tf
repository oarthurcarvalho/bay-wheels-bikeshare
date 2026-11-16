# Importar, não recriar: o bucket já guarda o bruto desde 31/out
import {
  to = aws_s3_bucket.raw
  id = "baywheels-raw-arthur"
}

resource "aws_s3_bucket" "raw" {
  bucket = "baywheels-raw-arthur"

  # Fonte da verdade da coleta: qualquer plan que tente apagar o bucket falha
  lifecycle {
    prevent_destroy = true
  }
}

import {
  to = aws_s3_bucket_public_access_block.raw
  id = "baywheels-raw-arthur"
}

# O padrão do console, agora escrito: se alguém abrir o bucket, o plan acusa
resource "aws_s3_bucket_public_access_block" "raw" {
  bucket = aws_s3_bucket.raw.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}
