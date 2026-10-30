import os

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
    