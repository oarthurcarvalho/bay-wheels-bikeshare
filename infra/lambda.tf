# As 3 Lambdas do coletor: código, configuração e logs.
# O zip sai de coletor/ a cada plan: mudou um .py, o apply publica; não mudou, nada acontece.

locals {
  # Vira o aws_sns_topic.alertas.arn quando o SNS entrar no Terraform (agenda e alertas)
  topico_alertas = "arn:aws:sns:${local.regiao}:${local.conta}:baywheels-alertas"

  funcoes = {
    # A cada 5 min: retrato do GBFS
    status = {
      arquivos = ["gbfs.py", "status.py"]
      memoria  = 128
      timeout  = 60
      ambiente = tomap({ BUCKET = aws_s3_bucket.raw.bucket })
    }
    # Todo dia: copia o mês novo da Lyft
    viagens = {
      arquivos = ["viagens.py"]
      memoria  = 256
      timeout  = 300
      ambiente = tomap({ BUCKET = aws_s3_bucket.raw.bucket })
    }
    # Todo dia: junta o dia, manda para o Volume e publica a cobertura.
    # 1 GB porque juntar o dia chegou a 251 de 256 MB; a CPU também cresce com a memória
    envio = {
      arquivos = ["envio.py", "cobertura.py"]
      memoria  = 1024
      timeout  = 300
      ambiente = tomap({
        BUCKET         = aws_s3_bucket.raw.bucket
        TOPICO_ALERTAS = local.topico_alertas
      })
    }
  }

  # A viagens fica no padrão da AWS (2 novas tentativas): copiar o mês é idempotente
  sem_nova_tentativa = toset(["status", "envio"])
}

# ---------- Código ----------

data "archive_file" "lambda" {
  for_each = local.funcoes

  type        = "zip"
  output_path = "${path.module}/../build/baywheels-${each.key}.zip"

  # Só os arquivos que a função importa, na raiz do zip
  dynamic "source" {
    for_each = each.value.arquivos
    content {
      content  = file("${path.module}/../coletor/${source.value}")
      filename = source.value
    }
  }
}

# ---------- Funções ----------

import {
  for_each = local.funcoes
  to       = aws_lambda_function.coletor[each.key]
  id       = "baywheels-${each.key}"
}

resource "aws_lambda_function" "coletor" {
  for_each = local.funcoes

  function_name = "baywheels-${each.key}"
  role          = aws_iam_role.lambda[each.key].arn
  runtime       = "python3.12"
  architectures = ["arm64"]
  handler       = "${each.key}.lambda_handler"
  memory_size   = each.value.memoria
  timeout       = each.value.timeout

  filename = data.archive_file.lambda[each.key].output_path
  # É o hash que decide se há código novo: mesmo conteúdo, mesmo hash, nada a publicar
  source_code_hash = data.archive_file.lambda[each.key].output_base64sha256

  environment {
    variables = each.value.ambiente
  }
}

# ---------- Execução assíncrona (o Scheduler chama assim) ----------

import {
  for_each = local.sem_nova_tentativa
  to       = aws_lambda_function_event_invoke_config.coletor[each.key]
  id       = "baywheels-${each.key}"
}

# Sem nova tentativa: a falha vira Errors na hora e o alarme avisa.
# A coleta seguinte vem em 5 min e o envio de amanhã repega o dia que ficou fora do manifesto
resource "aws_lambda_function_event_invoke_config" "coletor" {
  for_each = local.sem_nova_tentativa

  function_name                = aws_lambda_function.coletor[each.key].function_name
  maximum_retry_attempts       = 0
  maximum_event_age_in_seconds = 300
}

# ---------- Logs ----------

import {
  for_each = local.funcoes
  to       = aws_cloudwatch_log_group.lambda[each.key]
  id       = "/aws/lambda/baywheels-${each.key}"
}

# Sem retenção, o CloudWatch guarda (e cobra) para sempre
resource "aws_cloudwatch_log_group" "lambda" {
  for_each = local.funcoes

  name              = "/aws/lambda/baywheels-${each.key}"
  retention_in_days = 14
}
