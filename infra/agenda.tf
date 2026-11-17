# Quem dispara cada Lambda: EventBridge Scheduler, uma agenda por função.
# Importadas como estavam no console; a role de cada agenda está em iam.tf.

locals {
  horarios = {
    status = {
      nome      = "baywheels-status-5min"
      expressao = "rate(5 minutes)"
      # Com rate() o fuso não muda nada; ficou o que o console gravou
      fuso      = "America/Sao_Paulo"
      descricao = "Dispara a coleta do GBFS a cada 5 minutos"
      entrada   = "{}"
    }
    viagens = {
      nome      = "baywheels-viagens-diario"
      expressao = "cron(0 12 * * ? *)"
      fuso      = "UTC"
      descricao = null
      entrada   = "{}"
    }
    # 01h UTC: o dia UTC anterior já fechou e a última coleta (23:55) já está no S3
    envio = {
      nome      = "baywheels-envio-diario"
      expressao = "cron(0 1 * * ? *)"
      fuso      = "UTC"
      descricao = null
      entrada   = null
    }
  }
}

import {
  for_each = local.horarios
  to       = aws_scheduler_schedule.coletor[each.key]
  id       = "default/${each.value.nome}"
}

resource "aws_scheduler_schedule" "coletor" {
  for_each = local.horarios

  name                         = each.value.nome
  description                  = each.value.descricao
  schedule_expression          = each.value.expressao
  schedule_expression_timezone = each.value.fuso

  # Sem janela flexível: a coleta tem que cair sempre no mesmo segundo de cada 5 min
  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_lambda_function.coletor[each.key].arn
    role_arn = aws_iam_role.agenda[each.key].arn
    input    = each.value.entrada

    # O Scheduler não repete: quem decide repetir é a Lambda (event_invoke_config em lambda.tf)
    retry_policy {
      maximum_retry_attempts       = 0
      maximum_event_age_in_seconds = 86400
    }
  }
}
