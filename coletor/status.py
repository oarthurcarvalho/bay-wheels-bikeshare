import datetime
import gzip
import json
import os
import time

import boto3

from gbfs import buscar_feed, montar_linha

FEEDS_HORARIOS = ["station_information", "system_regions"]
cliente = boto3.client("s3")


def chave_bruto(feed, agora):

    if agora.tzinfo is None:
        raise ValueError("agora precisa ter fuso horário (UTC)")

    return f"raw/{feed}/date={agora.strftime('%Y-%m-%d')}/{agora.strftime('%H%M%S')}.jsonl.gz"


def coletar_feed(feed, agora):

    resposta = buscar_feed(feed)
    return montar_linha(feed, resposta, agora)


def coletar_status(agora):

    arquivos = {}

    arquivos[chave_bruto("station_status", agora)] = coletar_feed("station_status", agora)

    if agora.minute < 5:
        for feed in FEEDS_HORARIOS:
            try:
                arquivos[chave_bruto(feed, agora)] = coletar_feed(feed, agora)
            except Exception as e:
                print(f"Falha ao coletar {feed}: {e!r}")

    return arquivos


def gravar_s3(arquivos):

    bucket = os.environ["BUCKET"]

    for chave, conteudo in arquivos.items():
        cliente.put_object(
            Bucket=bucket,
            Key=chave,
            Body=conteudo,
            ContentType="application/gzip",
        )


def registrar_execucao(arquivos, inicio):

    # Uma linha JSON por execução: o CloudWatch Logs Insights filtra e soma por campo
    chave_status = next(chave for chave in arquivos if chave.startswith("raw/station_status/"))
    linha_status = json.loads(gzip.decompress(arquivos[chave_status]))

    registro = {
        "evento": "coleta",
        "feeds": sorted(chave.split("/")[1] for chave in arquivos),
        "arquivos": len(arquivos),
        "bytes": sum(len(conteudo) for conteudo in arquivos.values()),
        "ms": round((time.perf_counter() - inicio) * 1000),
        "last_updated": linha_status["resposta"].get("last_updated"),
    }
    print(json.dumps(registro))
    return registro


def lambda_handler(event, context):

    inicio = time.perf_counter()
    agora = datetime.datetime.now(datetime.timezone.utc)
    arquivos = coletar_status(agora)
    gravar_s3(arquivos)
    registrar_execucao(arquivos, inicio)

    return {
        "coletado_em": agora.isoformat(),
        "arquivos": list(arquivos),
    }
