import gzip
import io
import json
from datetime import date, datetime, timezone

import pytest

import envio
from gbfs import montar_linha

DIA = date(2026, 11, 1)
PREFIXO = "raw/station_status/date=2026-11-01/"


class S3Falso:
    """
    Faz o papel do cliente boto3: lista em 2 páginas e devolve o gzip guardado.
    """

    def __init__(self, objetos):
        self.objetos = objetos  # {chave: bytes}

    def get_paginator(self, nome):
        return self

    def paginate(self, Bucket, Prefix):
        chaves = [{"Key": chave} for chave in self.objetos if chave.startswith(Prefix)]
        return [{"Contents": chaves[:1]}, {"Contents": chaves[1:]}, {"KeyCount": 0}]

    def get_object(self, Bucket, Key):
        return {"Body": io.BytesIO(self.objetos[Key])}


def coleta(hora, minuto):
    agora = datetime(2026, 11, 1, hora, minuto, tzinfo=timezone.utc)
    return montar_linha("station_status", {"last_updated": hora * 100 + minuto}, agora)


@pytest.fixture
def usar_s3(monkeypatch):
    monkeypatch.setenv("BUCKET", "bucket-teste")

    def instalar(objetos):
        monkeypatch.setattr(envio, "cliente", S3Falso(objetos))

    return instalar


def test_juntar_dia_vira_um_arquivo_em_ordem(usar_s3):
    # Fora de ordem de propósito, e com um arquivo de outro dia que não pode entrar
    usar_s3(
        {
            f"{PREFIXO}001036.jsonl.gz": coleta(0, 10),
            f"{PREFIXO}000036.jsonl.gz": coleta(0, 0),
            f"{PREFIXO}000536.jsonl.gz": coleta(0, 5),
            "raw/station_status/date=2026-10-31/235536.jsonl.gz": coleta(23, 55),
        }
    )

    juntado, linhas = envio.juntar_dia("station_status", DIA)

    texto = gzip.decompress(juntado).decode("utf-8").splitlines()
    assert linhas == 3
    assert [json.loads(linha)["resposta"]["last_updated"] for linha in texto] == [0, 5, 10]


def test_juntar_dia_vazio_falha(usar_s3):
    usar_s3({})

    with pytest.raises(ValueError):
        envio.juntar_dia("station_status", DIA)


def test_juntar_dia_arquivo_sem_quebra_de_linha_falha(usar_s3):
    usar_s3(
        {
            f"{PREFIXO}000036.jsonl.gz": coleta(0, 0),
            f"{PREFIXO}000536.jsonl.gz": gzip.compress(b'{"sem": "quebra"}'),
        }
    )

    with pytest.raises(ValueError):
        envio.juntar_dia("station_status", DIA)


class RespostaFalsa:
    status = 204

    def __enter__(self):
        return self

    def __exit__(self, *erro):
        return False


def test_enviar_volume_faz_put_com_token(monkeypatch):
    pedidos = []

    def urlopen_falso(pedido, timeout):
        pedidos.append(pedido)
        return RespostaFalsa()

    monkeypatch.setattr(envio, "urlopen", urlopen_falso)

    status = envio.enviar_volume("station_status/date=2026-11-01/status.jsonl.gz", b"dados", "https://host", "tok")

    pedido = pedidos[0]
    assert status == 204
    assert pedido.get_method() == "PUT"
    assert pedido.full_url == (
        "https://host/api/2.0/fs/files/Volumes/workspace/baywheels/raw/"
        "station_status/date=2026-11-01/status.jsonl.gz?overwrite=true"
    )
    assert pedido.get_header("Authorization") == "Bearer tok"
    assert pedido.data == b"dados"
