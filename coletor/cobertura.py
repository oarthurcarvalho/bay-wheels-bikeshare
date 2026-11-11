import datetime
import gzip
import json
import math
import os
import sys

import boto3

INTERVALO = datetime.timedelta(minutes=5)
# Estação que não manda sinal há mais que isso está com o dado congelado
LIMITE_SEM_COMUNICACAO = datetime.timedelta(hours=1)
# O GBFS da Bay Wheels não tem is_virtual_station: o que marca a estação virtual é o nome
PREFIXO_VIRTUAL = "VIRTUAL STATION"
# 2: sem_comunicacao_1h passou a contar só estações físicas (antes de 10/11 contava as virtuais)
VERSAO_RELATORIO = 2
# Primeira execução do Scheduler; o que veio antes (o Test manual de 31/10 01:01) não conta
COLETA_LIGADA_EM = datetime.datetime(2026, 10, 31, 21, 35, tzinfo=datetime.timezone.utc)
cliente = boto3.client("s3")


def listar_coletas(data, feed="station_status"):

    prefixo = f"raw/{feed}/date={data.isoformat()}/"
    paginador = cliente.get_paginator("list_objects_v2")

    coletas = []
    for pagina in paginador.paginate(Bucket=os.environ["BUCKET"], Prefix=prefixo):
        for objeto in pagina.get("Contents", []):
            hhmmss = objeto["Key"].removeprefix(prefixo).split(".")[0]
            hora = datetime.datetime.strptime(hhmmss, "%H%M%S").time()
            horario = datetime.datetime.combine(data, hora, tzinfo=datetime.timezone.utc)
            coletas.append({"chave": objeto["Key"], "horario": horario, "bytes": objeto["Size"]})

    return sorted(coletas, key=lambda coleta: coleta["horario"])


def ler_resposta(chave):

    objeto = cliente.get_object(Bucket=os.environ["BUCKET"], Key=chave)
    linha = json.loads(gzip.decompress(objeto["Body"].read()))
    return linha["resposta"]


def ler_last_updated(chave):

    return ler_resposta(chave)["last_updated"]


def estacoes_sem_comunicacao(resposta):

    # Compara com o relógio do próprio feed, não com o da Lambda
    agora = resposta["last_updated"]
    limite = LIMITE_SEM_COMUNICACAO.total_seconds()
    return sorted(
        estacao["station_id"]
        for estacao in resposta["data"]["stations"]
        if agora - estacao.get("last_reported", 0) > limite
    )


def calcular_cobertura(coletas, inicio, fim):

    coletas = [coleta for coleta in coletas if inicio <= coleta["horario"] < fim]
    esperadas = math.ceil((fim - inicio) / INTERVALO)

    # As bordas entram: se a coleta parou às 18h, o buraco até o fim do dia aparece aqui
    pontos = [inicio, *(coleta["horario"] for coleta in coletas), fim]
    maior_intervalo = max(depois - antes for antes, depois in zip(pontos, pontos[1:]))

    total_bytes = sum(coleta["bytes"] for coleta in coletas)
    last_updated = [coleta["last_updated"] for coleta in coletas]
    repetidos = sum(1 for antes, depois in zip(last_updated, last_updated[1:]) if antes == depois)

    return {
        "inicio": inicio.isoformat(),
        "fim": fim.isoformat(),
        "esperadas": esperadas,
        "encontradas": len(coletas),
        "cobertura": round(len(coletas) / esperadas, 4),
        "maior_intervalo_min": round(maior_intervalo.total_seconds() / 60, 1),
        "kb_medio": round(total_bytes / len(coletas) / 1024, 1) if coletas else 0,
        "mb_dia": round(total_bytes / 1024 / 1024, 2),
        "last_updated_repetido": repetidos,
    }


def cobertura_dia(data, agora=None):

    agora = agora or datetime.datetime.now(datetime.timezone.utc)
    comeco_do_dia = datetime.datetime.combine(data, datetime.time(), tzinfo=datetime.timezone.utc)
    inicio = max(comeco_do_dia, COLETA_LIGADA_EM)
    # Para o dia de hoje o fim é agora, e o número esperado é só aproximado
    fim = min(comeco_do_dia + datetime.timedelta(days=1), agora)

    if inicio >= fim:
        raise ValueError(f"Nenhuma coleta esperada em {data}")

    coletas = [coleta for coleta in listar_coletas(data) if inicio <= coleta["horario"] < fim]
    for coleta in coletas:
        coleta["last_updated"] = ler_last_updated(coleta["chave"])

    return {"data": data.isoformat(), **calcular_cobertura(coletas, inicio, fim)}


def estacoes_virtuais(data):

    # Último cadastro do dia; sem cadastro, ninguém é marcado como virtual
    retratos = listar_coletas(data, "station_information")
    if not retratos:
        return set()

    cadastro = ler_resposta(retratos[-1]["chave"])["data"]["stations"]
    return {
        estacao["station_id"]
        for estacao in cadastro
        if estacao.get("name", "").startswith(PREFIXO_VIRTUAL)
    }


def relatorio_cobertura(data, agora=None):

    relatorio = {"versao": VERSAO_RELATORIO, **cobertura_dia(data, agora)}

    # Retrato das estações na última coleta do dia
    coletas = listar_coletas(data)
    if coletas:
        resposta = ler_resposta(coletas[-1]["chave"])
        mudas = estacoes_sem_comunicacao(resposta)
        relatorio["estacoes"] = len(resposta["data"]["stations"])
    else:
        mudas = []
        relatorio["estacoes"] = 0

    # Virtual não tem equipamento: o last_reported fica parado desde a criação, e isso
    # não é falha. Só as físicas mudas indicam estoque congelado
    virtuais = estacoes_virtuais(data)
    fisicas = [station_id for station_id in mudas if station_id not in virtuais]

    relatorio["virtuais_sem_comunicacao"] = len(mudas) - len(fisicas)
    relatorio["sem_comunicacao_1h"] = len(fisicas)
    relatorio["estacoes_sem_comunicacao"] = fisicas
    return relatorio


if __name__ == "__main__":

    data = datetime.date.fromisoformat(sys.argv[1])
    print(json.dumps(relatorio_cobertura(data), indent=2))
