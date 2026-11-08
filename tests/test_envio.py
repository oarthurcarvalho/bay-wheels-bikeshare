import gzip
import io
import json
import zipfile
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
    O S3Falso de antes, agora também com os manifestos: get_object de chave
    inexistente dá NoSuchKey, put_object guarda cada versão salva (por chave)
    e download_file grava o objeto num arquivo, como o boto3.
    """

    def __init__(self, objetos):
        super().__init__(objetos)
        self.salvos = {}

    def get_object(self, Bucket, Key):
        if Key not in self.objetos:
            raise erro_s3("NoSuchKey")
        return super().get_object(Bucket, Key)

    def put_object(self, Bucket, Key, Body, ContentType):
        self.objetos[Key] = Body
        self.salvos.setdefault(Key, []).append(json.loads(Body))

    def download_file(self, Bucket, Key, Filename):
        with open(Filename, "wb") as arquivo:
            arquivo.write(self.objetos[Key])


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
def lambda_falsa(monkeypatch, tmp_path):
    """
    Liga a Lambda em S3, SSM e Databricks falsos, congela "hoje" em 03/11
    e usa uma pasta própria como /tmp. Devolve o bucket e um dicionário
    caminho no Volume → bytes recebidos no PUT.
    """
    monkeypatch.setenv("BUCKET", "bucket-teste")
    monkeypatch.setattr(envio, "ssm", SsmFalso())
    monkeypatch.setattr(envio, "hoje_utc", lambda: date(2026, 11, 3))
    monkeypatch.setattr(envio, "PASTA_TMP", tmp_path)

    enviados = {}

    def urlopen_falso(pedido, timeout):
        caminho = pedido.full_url.split("/raw/")[1].removesuffix("?overwrite=true")
        enviados[caminho] = pedido.data
        return RespostaFalsa()

    monkeypatch.setattr(envio, "urlopen", urlopen_falso)

    def instalar(objetos):
        bucket = BucketFalso(objetos)
        monkeypatch.setattr(envio, "cliente", bucket)
        return bucket

    return instalar, enviados


def tres_dias():
    return {**coletas_do_dia("2026-10-31"), **coletas_do_dia("2026-11-01"), **coletas_do_dia("2026-11-02")}


def test_lambda_envia_dias_pendentes_e_segunda_execucao_nao_envia_nada(lambda_falsa):
    instalar, enviados = lambda_falsa
    bucket = instalar(tres_dias())

    primeira = envio.lambda_handler({}, None)
    segunda = envio.lambda_handler({}, None)

    assert primeira["dias"]["enviados"] == ["2026-10-31", "2026-11-01", "2026-11-02"]
    assert len(enviados) == 9  # 3 dias × 3 arquivos
    assert "station_information/date=2026-11-01/info.jsonl.gz" in enviados
    assert segunda["dias"] == {"pendentes": 0, "enviados": [], "falhas": [], "restantes": 0}
    # Um manifesto salvo por dia, e o último tem as linhas de cada feed
    salvos = bucket.salvos[envio.CHAVE_MANIFESTO_DIAS]
    assert [len(salvo) for salvo in salvos] == [1, 2, 3]
    assert salvos[-1]["2026-11-02"]["linhas"]["station_status"] == 2


def test_lambda_dia_com_falha_nao_segura_os_outros_e_termina_com_erro(lambda_falsa):
    instalar, _ = lambda_falsa
    # Em 01/11 faltou o system_regions inteiro
    sem_regions = coletas_do_dia("2026-11-01", feeds=("station_status", "station_information"))
    bucket = instalar({**coletas_do_dia("2026-10-31"), **sem_regions, **coletas_do_dia("2026-11-02")})

    with pytest.raises(RuntimeError, match="2026-11-01"):
        envio.lambda_handler({}, None)

    assert sorted(bucket.salvos[envio.CHAVE_MANIFESTO_DIAS][-1]) == ["2026-10-31", "2026-11-02"]


def test_lambda_envia_no_maximo_max_dias(lambda_falsa, monkeypatch):
    instalar, _ = lambda_falsa
    monkeypatch.setattr(envio, "MAX_DIAS", 2)
    instalar(tres_dias())

    resultado = envio.lambda_handler({}, None)

    assert resultado["dias"]["enviados"] == ["2026-10-31", "2026-11-01"]
    assert resultado["dias"]["restantes"] == 1


# --- Sábado 7: viagens da Lyft como CSV gzip no Volume ---

CABECALHO = b"ride_id,rideable_type,started_at\r\n"


def zip_da_lyft(mes, viagens, csvs_extras=()):
    """
    Monta um zip como os da Lyft: o CSV e o lixo do macOS em __MACOSX/.
    """
    linhas = b"".join(f"R{i},classic_bike,{mes}\r\n".encode() for i in range(viagens))
    memoria = io.BytesIO()
    with zipfile.ZipFile(memoria, "w") as arquivo:
        arquivo.writestr(f"{mes}-baywheels-tripdata.csv", CABECALHO + linhas)
        arquivo.writestr(f"__MACOSX/._{mes}-baywheels-tripdata.csv", b"\x00\x05\x16\x07lixo")
        for nome in csvs_extras:
            arquivo.writestr(nome, CABECALHO)
    return memoria.getvalue()


def test_converter_zip_ignora_macosx_e_conta_viagens(tmp_path):
    caminho_zip = tmp_path / "202609.zip"
    caminho_zip.write_bytes(zip_da_lyft("202609", 5))

    viagens = envio.converter_zip(caminho_zip, tmp_path / "202609.csv.gz")

    texto = gzip.decompress((tmp_path / "202609.csv.gz").read_bytes())
    assert viagens == 5
    assert texto.startswith(CABECALHO)
    assert b"lixo" not in texto


def test_converter_zip_com_dois_csvs_falha(tmp_path):
    caminho_zip = tmp_path / "202609.zip"
    caminho_zip.write_bytes(zip_da_lyft("202609", 5, csvs_extras=["outro.csv"]))

    with pytest.raises(ValueError):
        envio.converter_zip(caminho_zip, tmp_path / "202609.csv.gz")


def test_meses_pendentes_novo_e_republicado():
    copiados = {"202201": {"etag": "a"}, "202202": {"etag": "b-novo"}, "202203": {"etag": "c"}}
    enviados = {"202201": {"etag": "a"}, "202202": {"etag": "b"}}

    assert envio.meses_pendentes(copiados, enviados) == ["202202", "202203"]


def bucket_com_viagens(meses, zips=None):
    """
    Objetos do bucket com o manifesto da baywheels-viagens e um zip por mês.
    """
    objetos, manifesto = {}, {}
    for mes in meses:
        destino = f"raw/trips/{mes}/{mes}-baywheels-tripdata.csv.zip"
        manifesto[mes] = {"etag": f"etag-{mes}", "destino": destino}
        objetos[destino] = (zips or {}).get(mes) or zip_da_lyft(mes, 3)
    objetos[envio.CHAVE_MANIFESTO_VIAGENS] = json.dumps(manifesto).encode()
    return objetos


def test_lambda_envia_viagens_limpa_tmp_e_nao_repete(lambda_falsa, tmp_path):
    instalar, enviados = lambda_falsa
    bucket = instalar({**tres_dias(), **bucket_com_viagens(["202201", "202202"])})

    primeira = envio.lambda_handler({}, None)
    segunda = envio.lambda_handler({}, None)

    assert primeira["viagens"]["enviados"] == ["202201", "202202"]
    assert gzip.decompress(enviados["trips/202201.csv.gz"]).count(b"\n") == 4  # cabeçalho + 3
    assert bucket.salvos[envio.CHAVE_MANIFESTO_ENVIO_VIAGENS][-1]["202202"]["viagens"] == 3
    assert segunda["viagens"]["pendentes"] == 0
    # Nada sobrou no "/tmp"
    assert list(tmp_path.iterdir()) == []


def test_lambda_mes_quebrado_nao_segura_os_outros_e_limpa_tmp(lambda_falsa, tmp_path):
    instalar, enviados = lambda_falsa
    quebrado = {"202202": b"isto nao e um zip"}
    bucket = instalar({**tres_dias(), **bucket_com_viagens(["202201", "202202", "202203"], quebrado)})

    with pytest.raises(RuntimeError, match="202202"):
        envio.lambda_handler({}, None)

    assert sorted(bucket.salvos[envio.CHAVE_MANIFESTO_ENVIO_VIAGENS][-1]) == ["202201", "202203"]
    assert "trips/202202.csv.gz" not in enviados
    assert list(tmp_path.iterdir()) == []


def test_lambda_envia_no_maximo_max_meses(lambda_falsa, monkeypatch):
    instalar, _ = lambda_falsa
    monkeypatch.setattr(envio, "MAX_MESES", 2)
    instalar(bucket_com_viagens(["202201", "202202", "202203"]))
    # Sem coletas no S3 os dias falham; aqui só interessa o limite de meses
    monkeypatch.setattr(envio, "enviar_dias", lambda host, token: {"falhas": []})

    resultado = envio.lambda_handler({}, None)

    assert resultado["viagens"]["enviados"] == ["202201", "202202"]
    assert resultado["viagens"]["restantes"] == 1
