import datetime
import gzip
import json
import os
from urllib.request import Request, urlopen

import boto3
from botocore.exceptions import ClientError

# Primeiro dia com coleta do Scheduler; antes disso não há o que enviar
PRIMEIRO_DIA = datetime.date(2026, 10, 31)
CHAVE_MANIFESTO = "manifestos/envio.json"
# Feed no S3 → nome do arquivo do dia no Volume
ARQUIVOS_DO_DIA = {
    "station_status": "status.jsonl.gz",
    "station_information": "info.jsonl.gz",
    "system_regions": "regions.jsonl.gz",
}
MAX_DIAS = 10

cliente = boto3.client("s3")
ssm = boto3.client("ssm", region_name="us-east-1")


def juntar_dia(feed, data):

    bucket = os.environ["BUCKET"]
    prefixo = f"raw/{feed}/date={data.isoformat()}/"

    paginador = cliente.get_paginator("list_objects_v2")
    chaves = []

    for pagina in paginador.paginate(Bucket=bucket, Prefix=prefixo):
        for objeto in pagina.get("Contents", []):
            chaves.append(objeto["Key"])

    if not chaves:
        raise ValueError(f"Nenhum arquivo em {prefixo}")

    partes = []
    for chave in sorted(chaves):
        resposta = cliente.get_object(Bucket=bucket, Key=chave)
        partes.append(resposta["Body"].read())

    juntado = b"".join(partes)

    linhas = gzip.decompress(juntado).count(b"\n")
    if linhas != len(chaves):
        raise ValueError(f"{len(chaves)} arquivos, mas {linhas} linhas")

    return juntado, linhas


def enviar_volume(caminho, dados, host, token):

    url = f"{host}/api/2.0/fs/files/Volumes/workspace/baywheels/raw/{caminho}?overwrite=true"
    pedido = Request(
        url,
        data=dados,
        method="PUT",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/octet-stream",
        },
    )

    with urlopen(pedido, timeout=60) as resposta:
        if not 200 <= resposta.status < 300:
            raise RuntimeError(f"PUT {caminho} devolveu {resposta.status}")
        return resposta.status


def dias_pendentes(manifesto, hoje):

    pendentes = []
    dia = PRIMEIRO_DIA
    # "<" e não "<=": o dia de hoje ainda não fechou
    while dia < hoje:
        if dia.isoformat() not in manifesto:
            pendentes.append(dia)
        dia += datetime.timedelta(days=1)

    return pendentes


def hoje_utc():

    # Separado para o teste poder fixar o "hoje"
    return datetime.datetime.now(datetime.timezone.utc).date()


def ler_credenciais():

    # Lido a cada execução: renovar o token no SSM vale já na próxima, sem redeploy
    host = ssm.get_parameter(Name="/baywheels/databricks/host")["Parameter"]["Value"]
    token = ssm.get_parameter(Name="/baywheels/databricks/token", WithDecryption=True)
    return host, token["Parameter"]["Value"]


def ler_manifesto():

    try:
        objeto = cliente.get_object(Bucket=os.environ["BUCKET"], Key=CHAVE_MANIFESTO)
    except ClientError as e:
        if e.response["Error"]["Code"] == "NoSuchKey":
            return {}
        raise

    return json.loads(objeto["Body"].read())


def salvar_manifesto(manifesto):

    cliente.put_object(
        Bucket=os.environ["BUCKET"],
        Key=CHAVE_MANIFESTO,
        Body=json.dumps(manifesto, indent=2, sort_keys=True).encode("utf-8"),
        ContentType="application/json",
    )


def enviar_dia(data, host, token):

    linhas_por_feed = {}
    for feed, nome in ARQUIVOS_DO_DIA.items():
        juntado, linhas = juntar_dia(feed, data)
        enviar_volume(f"{feed}/date={data.isoformat()}/{nome}", juntado, host, token)
        linhas_por_feed[feed] = linhas

    return linhas_por_feed


def lambda_handler(event, context):

    host, token = ler_credenciais()
    manifesto = ler_manifesto()
    fila = dias_pendentes(manifesto, hoje_utc())

    enviados, falhas = [], []
    for data in fila[:MAX_DIAS]:
        try:
            linhas = enviar_dia(data, host, token)
        except Exception as e:
            # Um dia ruim não segura os outros: fica fora do manifesto e volta amanhã
            print(f"Falha ao enviar {data}: {e!r}")
            falhas.append(data.isoformat())
            continue

        # Só entra no manifesto depois dos 3 envios OK
        manifesto[data.isoformat()] = {
            "linhas": linhas,
            "enviado_em": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        salvar_manifesto(manifesto)
        enviados.append(data.isoformat())

    resultado = {
        "pendentes": len(fila),
        "enviados": enviados,
        "falhas": falhas,
        "restantes": len(fila) - len(enviados),
    }
    print(json.dumps(resultado))

    # Terminar com erro é o que faz o alarme de Errors disparar
    if falhas:
        raise RuntimeError(f"Dias com falha: {falhas}")

    return resultado


if __name__ == "__main__":

    fila = dias_pendentes(ler_manifesto(), hoje_utc())
    print(f"{len(fila)} dias pendentes: {[dia.isoformat() for dia in fila]}")
