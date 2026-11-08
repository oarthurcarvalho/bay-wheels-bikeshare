import gzip
import io
import json
from datetime import date, datetime, timezone

import pytest
from botocore.exceptions import ClientError

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


# --- Sexta 6: dias pendentes, manifesto e lambda_handler ---


def manifesto_com(*dias):
    return {dia: {"linhas": {}} for dia in dias}


def test_dias_pendentes_sem_manifesto_e_tudo_ate_ontem():
    pendentes = envio.dias_pendentes({}, date(2026, 11, 3))

    assert pendentes == [date(2026, 10, 31), date(2026, 11, 1), date(2026, 11, 2)]


def test_dias_pendentes_so_o_que_falta():
    manifesto = manifesto_com("2026-10-31", "2026-11-01")

    assert envio.dias_pendentes(manifesto, date(2026, 11, 3)) == [date(2026, 11, 2)]


def test_dias_pendentes_pega_buraco_no_meio():
    # 01/11 falhou e 02/11 foi: o 01/11 continua pendente
    manifesto = manifesto_com("2026-10-31", "2026-11-02")

    assert envio.dias_pendentes(manifesto, date(2026, 11, 3)) == [date(2026, 11, 1)]


def test_dias_pendentes_em_dia_e_vazio():
    manifesto = manifesto_com("2026-10-31", "2026-11-01", "2026-11-02")

    assert envio.dias_pendentes(manifesto, date(2026, 11, 3)) == []


def erro_s3(codigo):
    return ClientError({"Error": {"Code": codigo, "Message": codigo}}, "GetObject")


class BucketFalso(S3Falso):
    """
    O S3Falso de antes, agora também com o manifesto: get_object de chave
    inexistente dá NoSuchKey e put_object guarda o que foi salvo.
    """

    def __init__(self, objetos):
        super().__init__(objetos)
        self.manifestos_salvos = []

    def get_object(self, Bucket, Key):
        if Key not in self.objetos:
            raise erro_s3("NoSuchKey")
        return super().get_object(Bucket, Key)

    def put_object(self, Bucket, Key, Body, ContentType):
        self.objetos[Key] = Body
        self.manifestos_salvos.append(json.loads(Body))


class SsmFalso:
    def get_parameter(self, Name, WithDecryption=False):
        valor = {"/baywheels/databricks/host": "https://host", "/baywheels/databricks/token": "tok"}
        return {"Parameter": {"Value": valor[Name]}}


def coletas_do_dia(dia, feeds=tuple(envio.ARQUIVOS_DO_DIA)):
    # 2 arquivos por feed bastam para provar que o dia foi juntado
    objetos = {}
    for feed in feeds:
        for hhmmss in ("000036", "010036"):
            agora = datetime.fromisoformat(f"{dia}T{hhmmss[:2]}:00:36+00:00")
            objetos[f"raw/{feed}/date={dia}/{hhmmss}.jsonl.gz"] = montar_linha(feed, {}, agora)
    return objetos


@pytest.fixture
def lambda_falsa(monkeypatch):
    """
    Liga a Lambda em S3, SSM e Databricks falsos e congela "hoje" em 03/11.
    Devolve o bucket e a lista de caminhos que receberam PUT.
    """
    monkeypatch.setenv("BUCKET", "bucket-teste")
    monkeypatch.setattr(envio, "ssm", SsmFalso())

    enviados = []

    def urlopen_falso(pedido, timeout):
        enviados.append(pedido.full_url.split("/raw/")[1].removesuffix("?overwrite=true"))
        return RespostaFalsa()

    monkeypatch.setattr(envio, "urlopen", urlopen_falso)

    monkeypatch.setattr(envio, "hoje_utc", lambda: date(2026, 11, 3))

    def instalar(objetos):
        bucket = BucketFalso(objetos)
        monkeypatch.setattr(envio, "cliente", bucket)
        return bucket

    return instalar, enviados


def test_lambda_envia_dias_pendentes_e_segunda_execucao_nao_envia_nada(lambda_falsa):
    instalar, enviados = lambda_falsa
    bucket = instalar(
        {**coletas_do_dia("2026-10-31"), **coletas_do_dia("2026-11-01"), **coletas_do_dia("2026-11-02")}
    )

    primeira = envio.lambda_handler({}, None)
    segunda = envio.lambda_handler({}, None)

    assert primeira["enviados"] == ["2026-10-31", "2026-11-01", "2026-11-02"]
    assert len(enviados) == 9  # 3 dias × 3 arquivos
    assert "station_information/date=2026-11-01/info.jsonl.gz" in enviados
    assert segunda == {"pendentes": 0, "enviados": [], "falhas": [], "restantes": 0}
    # Um manifesto salvo por dia, e o último tem as linhas de cada feed
    assert [len(salvo) for salvo in bucket.manifestos_salvos] == [1, 2, 3]
    assert bucket.manifestos_salvos[-1]["2026-11-02"]["linhas"]["station_status"] == 2


def test_lambda_dia_com_falha_nao_segura_os_outros_e_termina_com_erro(lambda_falsa):
    instalar, _ = lambda_falsa
    # Em 01/11 faltou o system_regions inteiro
    sem_regions = coletas_do_dia("2026-11-01", feeds=("station_status", "station_information"))
    bucket = instalar({**coletas_do_dia("2026-10-31"), **sem_regions, **coletas_do_dia("2026-11-02")})

    with pytest.raises(RuntimeError, match="2026-11-01"):
        envio.lambda_handler({}, None)

    assert sorted(bucket.manifestos_salvos[-1]) == ["2026-10-31", "2026-11-02"]


def test_lambda_envia_no_maximo_max_dias(lambda_falsa, monkeypatch):
    instalar, _ = lambda_falsa
    monkeypatch.setattr(envio, "MAX_DIAS", 2)
    instalar({**coletas_do_dia("2026-10-31"), **coletas_do_dia("2026-11-01"), **coletas_do_dia("2026-11-02")})

    resultado = envio.lambda_handler({}, None)

    assert resultado["enviados"] == ["2026-10-31", "2026-11-01"]
    assert resultado["restantes"] == 1
