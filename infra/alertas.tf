# Para onde vão os avisos (SNS → e-mail) e o que dispara cada aviso (alarmes do CloudWatch).

import {
  to = aws_sns_topic.alertas
  id = "arn:aws:sns:us-east-1:398449337512:baywheels-alertas"
}

resource "aws_sns_topic" "alertas" {
  name = "baywheels-alertas"
}

import {
  to = aws_sns_topic_subscription.email
  id = "arn:aws:sns:us-east-1:398449337512:baywheels-alertas:f62fbb93-8637-4faf-b900-2f81a7298d74"
}

resource "aws_sns_topic_subscription" "email" {
  topic_arn = aws_sns_topic.alertas.arn
  protocol  = "email"
  # Vem do terraform.tfvars (fora do git): o repositório é público
  endpoint = var.email_alertas
}

# ---------- Alarmes ----------

locals {
  alarmes = {
    # 3 falhas seguidas (15 min): uma falha solta da Lyft não acorda ninguém
    "baywheels-status-errors" = {
      funcao     = "status"
      descricao  = "Alarme para errors da função Lambda"
      metrica    = "Errors"
      periodo    = 300
      avaliacoes = 3
      limite     = 1
      comparacao = "GreaterThanOrEqualToThreshold"
      sem_dado   = "notBreaching"
    }
    # Menos de 10 das 12 execuções numa hora; sem dado nenhum = a agenda parou = alarme
    "baywheels-status-sem-execucao" = {
      funcao     = "status"
      descricao  = "Alarme para quando o Lambda não rodar"
      metrica    = "Invocations"
      periodo    = 3600
      avaliacoes = 1
      limite     = 10
      comparacao = "LessThanThreshold"
      sem_dado   = "breaching"
    }
    "baywheels-viagens-erros" = {
      funcao     = "viagens"
      descricao  = null
      metrica    = "Errors"
      periodo    = 3600
      avaliacoes = 1
      limite     = 1
      comparacao = "GreaterThanOrEqualToThreshold"
      sem_dado   = "notBreaching"
    }
    "baywheels-envio-erros" = {
      funcao     = "envio"
      descricao  = null
      metrica    = "Errors"
      periodo    = 3600
      avaliacoes = 1
      limite     = 1
      comparacao = "GreaterThanOrEqualToThreshold"
      sem_dado   = "notBreaching"
    }
  }
}

import {
  for_each = local.alarmes
  to       = aws_cloudwatch_metric_alarm.coletor[each.key]
  id       = each.key
}

resource "aws_cloudwatch_metric_alarm" "coletor" {
  for_each = local.alarmes

  alarm_name        = each.key
  alarm_description = each.value.descricao

  namespace   = "AWS/Lambda"
  metric_name = each.value.metrica
  statistic   = "Sum"
  dimensions = {
    FunctionName = aws_lambda_function.coletor[each.value.funcao].function_name
  }

  period              = each.value.periodo
  evaluation_periods  = each.value.avaliacoes
  datapoints_to_alarm = each.value.avaliacoes
  threshold           = each.value.limite
  comparison_operator = each.value.comparacao
  treat_missing_data  = each.value.sem_dado

  alarm_actions = [aws_sns_topic.alertas.arn]
}
