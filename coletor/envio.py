import datetime
import gzip
import io
import json
import os
import pathlib
import sys
import tempfile
import zipfile
from urllib.request import Request, urlopen

import boto3
from botocore.exceptions import ClientError

import cobertura

# Primeiro dia com coleta do Scheduler; antes disso não há o que enviar
PRIMEIRO_DIA = datetime.date(2026, 10, 31)
CHAVE_MANIFESTO_DIAS = "manifestos/envio.json"
# Escrito pela baywheels-viagens: o que já foi copiado da Lyft para raw/trips/
CHAVE_MANIFESTO_VIAGENS = "manifestos/viagens.json"
CHAVE_MANIFESTO_ENVIO_VIAGENS = "manifestos/envio_viagens.json"
# Feed no S3 → nome do arquivo do dia no Volume
ARQUIVOS_DO_DIA = {
    "station_status": "status.jsonl.gz",
    "station_information": "info.jsonl.gz",
    "system_regions": "regions.jsonl.gz",
}
# Na rotina é 1 dia; o limite só pesa quando há atraso acumulado (cada dia leva ~20 s)
MAX_DIAS = 5
COBERTURA_MINIMA = 0.95
MAX_MESES = 10
# Nível 6: no zip de set/26, 4,3 s e 22 MB; o nível 9 leva 11,5 s para ganhar 4%
NIVEL_GZIP = 6
BLOCO = 1024 * 1024
# Na Lambda é o /tmp; separado para o teste usar uma pasta própria
PASTA_TMP = pathlib.Path(tempfile.gettempdir())

cliente = boto3.client("s3")
ssm = boto3.client("ssm", region_name="us-east-1")
sns = boto3.client("sns", region_name="us-east-1")


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

    # Contar em blocos: descomprimir o dia inteiro de uma vez quase estourou 256 MB na sexta
    linhas = 0
    with gzip.GzipFile(fileobj=io.BytesIO(juntado)) as arquivo:
        for bloco in iter(lambda: arquivo.read(BLOCO), b""):
            linhas += bloco.count(b"\n")
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
        # O dia só está completo com o relatório; dias enviados antes dele voltam à fila
        if "cobertura" not in manifesto.get(dia.isoformat(), {}):
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


def ler_manifesto(chave):

    try:
        objeto = cliente.get_object(Bucket=os.environ["BUCKET"], Key=chave)
    except ClientError as e:
        if e.response["Error"]["Code"] == "NoSuchKey":
            return {}
        raise

    return json.loads(objeto["Body"].read())


def salvar_manifesto(manifesto, chave):

    cliente.put_object(
        Bucket=os.environ["BUCKET"],
        Key=chave,
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


def publicar_relatorio(data, host, token):

    relatorio = cobertura.relatorio_cobertura(data)
    corpo = json.dumps(relatorio, indent=2).encode("utf-8")
    chave = f"relatorios/cobertura/date={data.isoformat()}.json"

    # S3 é a fonte da verdade; o Volume recebe a cópia para o Databricks
    cliente.put_object(
        Bucket=os.environ["BUCKET"], Key=chave, Body=corpo, ContentType="application/json"
    )
    enviar_volume(chave, corpo, host, token)

    if relatorio["cobertura"] < COBERTURA_MINIMA:
        # Assunto do SNS: só ASCII e menos de 100 caracteres
        sns.publish(
            TopicArn=os.environ["TOPICO_ALERTAS"],
            Subject=f"Bay Wheels: cobertura de {data} em {relatorio['cobertura']:.1%}",
            Message=corpo.decode("utf-8"),
        )

    return relatorio


def enviar_dias(host, token):

    manifesto = ler_manifesto(CHAVE_MANIFESTO_DIAS)
    fila = dias_pendentes(manifesto, hoje_utc())

    enviados, falhas = [], []
    for data in fila[:MAX_DIAS]:
        try:
            linhas = enviar_dia(data, host, token)
            relatorio = publicar_relatorio(data, host, token)
        except Exception as e:
            # Um dia ruim não segura os outros: fica fora do manifesto e volta amanhã
            print(f"Falha ao enviar {data}: {e!r}")
            falhas.append(data.isoformat())
            continue

        # Só entra no manifesto depois dos 3 envios e do relatório OK
        manifesto[data.isoformat()] = {
            "linhas": linhas,
            "cobertura": relatorio["cobertura"],
            "enviado_em": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        salvar_manifesto(manifesto, CHAVE_MANIFESTO_DIAS)
        enviados.append(data.isoformat())

    return {
        "pendentes": len(fila),
        "enviados": enviados,
        "falhas": falhas,
        "restantes": len(fila) - len(enviados),
    }


def converter_zip(caminho_zip, caminho_gz):

    with zipfile.ZipFile(caminho_zip) as arquivo_zip:
        # O zip da Lyft traz também __MACOSX/._AAAAMM-...csv, que termina em .csv mas não é dado
        csvs = [
            nome
            for nome in arquivo_zip.namelist()
            if nome.endswith(".csv") and not nome.startswith("__MACOSX/")
        ]
        if len(csvs) != 1:
            raise ValueError(f"Esperava 1 CSV em {caminho_zip}, achei {csvs}")

        # Em blocos: os 129 MB do CSV de set/26 nunca ficam inteiros na memória
        linhas = 0
        with arquivo_zip.open(csvs[0]) as csv, gzip.open(
            caminho_gz, "wb", compresslevel=NIVEL_GZIP
        ) as destino:
            for bloco in iter(lambda: csv.read(BLOCO), b""):
                destino.write(bloco)
                linhas += bloco.count(b"\n")

    # A 1ª linha é o cabeçalho; o CSV da Lyft termina com quebra de linha
    return linhas - 1


def caminhos_tmp(mes):

    return PASTA_TMP / f"{mes}.zip", PASTA_TMP / f"{mes}.csv.gz"


def preparar_viagem(mes, chave):

    caminho_zip, caminho_gz = caminhos_tmp(mes)
    # O zipfile precisa pular pelo arquivo, então o zip vai inteiro para o disco antes
    cliente.download_file(os.environ["BUCKET"], chave, str(caminho_zip))
    viagens = converter_zip(caminho_zip, caminho_gz)
    caminho_zip.unlink()
    return caminho_gz, viagens


def meses_pendentes(copiados, enviados):

    # Mesma regra do baywheels-viagens: ETag diferente = a Lyft republicou o mês
    return [
        mes
        for mes in sorted(copiados)
        if enviados.get(mes, {}).get("etag") != copiados[mes]["etag"]
    ]


def enviar_viagens(host, token):

    copiados = ler_manifesto(CHAVE_MANIFESTO_VIAGENS)
    enviados = ler_manifesto(CHAVE_MANIFESTO_ENVIO_VIAGENS)
    fila = meses_pendentes(copiados, enviados)

    feitos, falhas = [], []
    for mes in fila[:MAX_MESES]:
        try:
            caminho_gz, viagens = preparar_viagem(mes, copiados[mes]["destino"])
            # ~22 MB por mês: cabe com folga em 1 GB de memória
            enviar_volume(f"trips/{mes}.csv.gz", caminho_gz.read_bytes(), host, token)
        except Exception as e:
            print(f"Falha ao enviar viagens de {mes}: {e!r}")
            falhas.append(mes)
            continue
        finally:
            # A Lambda reaproveita o /tmp entre execuções: limpar dando certo ou não
            for caminho in caminhos_tmp(mes):
                caminho.unlink(missing_ok=True)

        enviados[mes] = {
            "etag": copiados[mes]["etag"],
            "viagens": viagens,
            "enviado_em": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        salvar_manifesto(enviados, CHAVE_MANIFESTO_ENVIO_VIAGENS)
        feitos.append(mes)

    return {
        "pendentes": len(fila),
        "enviados": feitos,
        "falhas": falhas,
        "restantes": len(fila) - len(feitos),
    }


def lambda_handler(event, context):

    host, token = ler_credenciais()
    # Dias primeiro: são pequenos e é o que o Databricks lê todo dia
    resultado = {"dias": enviar_dias(host, token), "viagens": enviar_viagens(host, token)}
    print(json.dumps(resultado))

    # Terminar com erro é o que faz o alarme de Errors disparar
    falhas = resultado["dias"]["falhas"] + resultado["viagens"]["falhas"]
    if falhas:
        raise RuntimeError(f"Envios com falha: {falhas}")

    return resultado


if __name__ == "__main__":

    if len(sys.argv) > 1:
        # Conferir a conversão com um zip local: python coletor/envio.py caminho.zip
        caminho_zip = pathlib.Path(sys.argv[1])
        caminho_gz = PASTA_TMP / caminho_zip.with_suffix(".csv.gz").name
        viagens = converter_zip(caminho_zip, caminho_gz)
        print(f"{viagens} viagens, {caminho_gz.stat().st_size / 1024 / 1024:.1f} MB → {caminho_gz}")
    else:
        dias = dias_pendentes(ler_manifesto(CHAVE_MANIFESTO_DIAS), hoje_utc())
        meses = meses_pendentes(
            ler_manifesto(CHAVE_MANIFESTO_VIAGENS), ler_manifesto(CHAVE_MANIFESTO_ENVIO_VIAGENS)
        )
        print(f"{len(dias)} dias pendentes: {[dia.isoformat() for dia in dias]}")
        print(f"{len(meses)} meses de viagens pendentes: {meses}")
