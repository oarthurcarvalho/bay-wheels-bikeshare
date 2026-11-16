# Tudo o que o coletor pode fazer na AWS está neste arquivo.
# Importado do console como estava: primeiro o código igual ao que roda, depois as melhorias.

locals {
  conta  = "398449337512"
  regiao = "us-east-1"
  raw    = aws_s3_bucket.raw.arn

  # Uma role por Lambda; a política inline é o que cada uma pode tocar além dos logs
  lambdas = {
    status = {
      descricao = "Allows Lambda functions to call AWS services on your behalf."
      politica  = "baywheels-status-s3-put"
    }
    viagens = {
      descricao = "Allows Lambda functions to call AWS services on your behalf."
      politica  = "baywheels-viagens-s3"
    }
    envio = {
      descricao = "Lambda baywheels-envio: le raw/ no S3 e o token no SSM"
      politica  = "baywheels-envio-s3-ssm"
    }
  }

  documento_lambda = {
    status  = data.aws_iam_policy_document.status.json
    viagens = data.aws_iam_policy_document.viagens.json
    envio   = data.aws_iam_policy_document.envio.json
  }

  # Uma role por agenda, com os nomes que o console gerou (renomear recria a role)
  agendas = {
    status = {
      role     = "baywheels-scheduler-role"
      politica = "Amazon-EventBridge-Scheduler-Execution-Policy-82ba11f8-c504-49ec-83e9-eac085733770"
    }
    viagens = {
      role     = "baywheels-viagens-scheduler-role"
      politica = "Amazon-EventBridge-Scheduler-Execution-Policy-4d0f7b79-1e12-425d-af68-a5d0336b556c"
    }
    envio = {
      role     = "Amazon_EventBridge_Scheduler_LAMBDA_1347cab8b0"
      politica = "Amazon-EventBridge-Scheduler-Execution-Policy-4f8199f9-fb90-4793-a31a-2d947990a9c5"
    }
  }
}

# ---------- Quem pode assumir cada role ----------

data "aws_iam_policy_document" "assumir_lambda" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

data "aws_iam_policy_document" "assumir_scheduler" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["scheduler.amazonaws.com"]
    }
    # Só agendas desta conta: evita outra conta usar o Scheduler para assumir a role
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.conta]
    }
  }
}

# ---------- Lambdas ----------

import {
  for_each = local.lambdas
  to       = aws_iam_role.lambda[each.key]
  id       = "baywheels-${each.key}-role"
}

resource "aws_iam_role" "lambda" {
  for_each = local.lambdas

  name               = "baywheels-${each.key}-role"
  description        = each.value.descricao
  assume_role_policy = data.aws_iam_policy_document.assumir_lambda.json
}

import {
  for_each = local.lambdas
  to       = aws_iam_role_policy_attachment.logs[each.key]
  id       = "baywheels-${each.key}-role/arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# Política da AWS para gravar no CloudWatch Logs: o único Resource "*" do projeto
resource "aws_iam_role_policy_attachment" "logs" {
  for_each = local.lambdas

  role       = aws_iam_role.lambda[each.key].name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

import {
  for_each = local.lambdas
  to       = aws_iam_role_policy.lambda[each.key]
  id       = "baywheels-${each.key}-role:${each.value.politica}"
}

resource "aws_iam_role_policy" "lambda" {
  for_each = local.lambdas

  name   = each.value.politica
  role   = aws_iam_role.lambda[each.key].id
  policy = local.documento_lambda[each.key]
}

# status: a cada 5 min grava o retrato do GBFS em raw/
data "aws_iam_policy_document" "status" {
  statement {
    sid       = "GravarBrutoNoRaw"
    actions   = ["s3:PutObject"]
    resources = ["${local.raw}/raw/*"]
  }
}

# viagens: copia o zip do mês do bucket público da Lyft para raw/trips/
data "aws_iam_policy_document" "viagens" {
  statement {
    sid       = "LerViagensDaLyft"
    actions   = ["s3:GetObject"]
    resources = ["arn:aws:s3:::baywheels-data/*"]
  }
  statement {
    sid       = "GravarViagens"
    actions   = ["s3:PutObject"]
    resources = ["${local.raw}/raw/trips/*"]
  }
  statement {
    sid       = "LerEGravarManifesto"
    actions   = ["s3:GetObject", "s3:PutObject"]
    resources = ["${local.raw}/manifestos/viagens.json"]
  }
  # Sem ListBucket, objeto inexistente volta 403 em vez de NoSuchKey
  statement {
    sid       = "DistinguirManifestoInexistente"
    actions   = ["s3:ListBucket"]
    resources = [local.raw]
  }
}

# envio: junta o dia, manda para o Volume do Databricks e publica o relatório
data "aws_iam_policy_document" "envio" {
  statement {
    sid       = "Listar"
    actions   = ["s3:ListBucket"]
    resources = [local.raw]
  }
  statement {
    sid       = "LerBruto"
    actions   = ["s3:GetObject"]
    resources = ["${local.raw}/raw/*"]
  }
  statement {
    sid       = "LerManifestoViagens"
    actions   = ["s3:GetObject"]
    resources = ["${local.raw}/manifestos/viagens.json"]
  }
  statement {
    sid     = "Manifestos"
    actions = ["s3:GetObject", "s3:PutObject"]
    resources = [
      "${local.raw}/manifestos/envio.json",
      "${local.raw}/manifestos/envio_viagens.json",
    ]
  }
  # Pelo nome: o valor do token nunca passa pelo Terraform nem entra no state
  statement {
    sid       = "LerSsm"
    actions   = ["ssm:GetParameter"]
    resources = ["arn:aws:ssm:${local.regiao}:${local.conta}:parameter/baywheels/*"]
  }
  statement {
    sid       = "GravarRelatorios"
    actions   = ["s3:PutObject"]
    resources = ["${local.raw}/relatorios/cobertura/*"]
  }
  statement {
    sid       = "Alertas"
    actions   = ["sns:Publish"]
    resources = [local.topico_alertas]
  }
}

# ---------- Agendas (EventBridge Scheduler) ----------

import {
  for_each = local.agendas
  to       = aws_iam_role.agenda[each.key]
  id       = each.value.role
}

resource "aws_iam_role" "agenda" {
  for_each = local.agendas

  name = each.value.role
  # O console cria em /service-role/; mudar o path recria a role
  path               = "/service-role/"
  assume_role_policy = data.aws_iam_policy_document.assumir_scheduler.json
}

# Cada agenda só invoca a sua Lambda (o ":*" cobre versões e aliases)
data "aws_iam_policy_document" "invocar" {
  for_each = local.agendas

  statement {
    actions = ["lambda:InvokeFunction"]
    resources = [
      "arn:aws:lambda:${local.regiao}:${local.conta}:function:baywheels-${each.key}:*",
      "arn:aws:lambda:${local.regiao}:${local.conta}:function:baywheels-${each.key}",
    ]
  }
}

import {
  for_each = local.agendas
  to       = aws_iam_policy.agenda[each.key]
  id       = "arn:aws:iam::${local.conta}:policy/service-role/${each.value.politica}"
}

resource "aws_iam_policy" "agenda" {
  for_each = local.agendas

  name   = each.value.politica
  path   = "/service-role/"
  policy = data.aws_iam_policy_document.invocar[each.key].json
}

import {
  for_each = local.agendas
  to       = aws_iam_role_policy_attachment.agenda[each.key]
  id       = "${each.value.role}/arn:aws:iam::${local.conta}:policy/service-role/${each.value.politica}"
}

resource "aws_iam_role_policy_attachment" "agenda" {
  for_each = local.agendas

  role       = aws_iam_role.agenda[each.key].name
  policy_arn = aws_iam_policy.agenda[each.key].arn
}
