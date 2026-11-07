import datetime
import json
import os
import re

import boto3
from botocore import UNSIGNED
from botocore.config import Config
from botocore.exceptions import ClientError

BUCKET_LYFT = "baywheels-data"
PRIMEIRO_MES = "202201"
CHAVE_MANIFESTO = "manifestos/viagens.json"
MAX_COPIAS = 10
# O nome muda com o tempo (-baywheels-, -baywheeels-, -lyftbikes-, com e sem .csv)
PADRAO_VIAGEM = re.compile(r"^(\d{6})-.*tripdata.*\.zip$")

cliente = boto3.client("s3")
# O bucket da Lyft é público: listar e baixar sem assinatura não depende de permissão nenhuma
cliente_publico = boto3.client(
    "s3", region_name="us-east-1", config=Config(signature_version=UNSIGNED)
)


def listar_viagens():

    por_mes = {}
    paginador = cliente_publico.get_paginator("list_objects_v2")

    for pagina in paginador.paginate(Bucket=BUCKET_LYFT):
        for objeto in pagina.get("Contents", []):
            combina = PADRAO_VIAGEM.match(objeto["Key"])
            if not combina or combina.group(1) < PRIMEIRO_MES:
                continue

            viagem = {
                "mes": combina.group(1),
                "chave": objeto["Key"],
                "etag": objeto["ETag"],
                "bytes": objeto["Size"],
                "modificado": objeto["LastModified"],
            }
            # Se a Lyft deixar dois arquivos do mesmo mês, vale o mais recente
            atual = por_mes.get(viagem["mes"])
            if atual is None or viagem["modificado"] > atual["modificado"]:
                por_mes[viagem["mes"]] = viagem

    return [por_mes[mes] for mes in sorted(por_mes)]


def pendentes(lista, manifesto):

    return [
        viagem
        for viagem in lista
        if manifesto.get(viagem["mes"], {}).get("etag") != viagem["etag"]
    ]


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


def copiar_viagem(chave):

    bucket = os.environ["BUCKET"]
    mes = PADRAO_VIAGEM.match(chave).group(1)
    destino = f"raw/trips/{mes}/{chave}"

    try:
        cliente.copy_object(
            Bucket=bucket,
            Key=destino,
            CopySource={"Bucket": BUCKET_LYFT, "Key": chave},
        )
    except ClientError as e:
        if e.response["Error"]["Code"] != "AccessDenied":
            raise
        # Plano B: baixar e reenviar em streaming, sem carregar o zip inteiro na memória
        print(f"copy_object negado para {chave}; copiando por download + upload")
        origem = cliente_publico.get_object(Bucket=BUCKET_LYFT, Key=chave)
        cliente.upload_fileobj(origem["Body"], bucket, destino)

    return destino


def lambda_handler(event, context):

    manifesto = ler_manifesto()
    fila = pendentes(listar_viagens(), manifesto)

    copiadas = []
    for viagem in fila[:MAX_COPIAS]:
        destino = copiar_viagem(viagem["chave"])
        manifesto[viagem["mes"]] = {
            "origem": viagem["chave"],
            "etag": viagem["etag"],
            "bytes": viagem["bytes"],
            "destino": destino,
            "copiado_em": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        # Salvar a cada cópia: se a execução cair no meio, a próxima continua daqui
        salvar_manifesto(manifesto)
        copiadas.append(destino)

    return {
        "pendentes": len(fila),
        "copiadas": copiadas,
        "restantes": len(fila) - len(copiadas),
    }


if __name__ == "__main__":

    lista = listar_viagens()
    fila = pendentes(lista, ler_manifesto())
    print(f"{len(lista)} meses na Lyft desde {PRIMEIRO_MES}; {len(fila)} pendentes")
    for viagem in fila:
        print(f"  {viagem['mes']}  {viagem['chave']}  {viagem['bytes'] / 1024 / 1024:.1f} MB")
