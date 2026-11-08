import datetime
import gzip
import os
import boto3

from urllib.request import Request, urlopen
from urllib.error import HTTPError

cliente = boto3.client("s3")

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



if __name__ == "__main__":

    ssm = boto3.client("ssm", region_name="us-east-1")
    host = ssm.get_parameter(Name="/baywheels/databricks/host")["Parameter"]["Value"]
    token = ssm.get_parameter(Name="/baywheels/databricks/token", WithDecryption=True)["Parameter"]["Value"]

    data = datetime.date(2026, 11, 1)
    juntado, linhas = juntar_dia("station_status", data)
    caminho = f"station_status/date={data.isoformat()}/status.jsonl.gz"
    status = enviar_volume(caminho, juntado, host, token)
    print(f"{linhas} linhas, {len(juntado) / 1024 / 1024:.2f} MB → {caminho} ({status})")
