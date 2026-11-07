import io
import json
from datetime import datetime, timezone

import pytest
from botocore.exceptions import ClientError

import viagens


def erro_s3(codigo):
    return ClientError({"Error": {"Code": codigo, "Message": codigo}}, "Operacao")


def objeto_lyft(chave, etag="e1", dia=1):
    return {
        "Key": chave,
        "ETag": etag,
        "Size": 20_000_000,
        "LastModified": datetime(2026, 1, dia, tzinfo=timezone.utc),
    }


class LyftFalsa:
    """
    Faz o papel do cliente sem assinatura: lista o bucket da Lyft em 2 páginas.
    """

    def __init__(self, objetos):
        self.objetos = objetos

    def get_paginator(self, nome):
        assert nome == "list_objects_v2"
        return self

    def paginate(self, Bucket):
        assert Bucket == "baywheels-data"
        metade = len(self.objetos) // 2
        return [{"Contents": self.objetos[:metade]}, {"Contents": self.objetos[metade:]}]

    def get_object(self, Bucket, Key):
        return {"Body": io.BytesIO(f"zip de {Key}".encode())}


class BucketFalso:
    """
    Faz o papel do cliente assinado no bucket raw: guarda tudo num dicionário.
    """

    def __init__(self, negar_copia=False):
        self.objetos = {}
        self.negar_copia = negar_copia
        self.manifestos_salvos = []

    def get_object(self, Bucket, Key):
        if Key not in self.objetos:
            raise erro_s3("NoSuchKey")
        return {"Body": io.BytesIO(self.objetos[Key])}

    def put_object(self, Bucket, Key, Body, ContentType):
        self.objetos[Key] = Body
        if Key == viagens.CHAVE_MANIFESTO:
            self.manifestos_salvos.append(json.loads(Body))

    def copy_object(self, Bucket, Key, CopySource):
        if self.negar_copia:
            raise erro_s3("AccessDenied")
        self.objetos[Key] = f"zip de {CopySource['Key']}".encode()

    def upload_fileobj(self, corpo, bucket, chave):
        self.objetos[chave] = corpo.read()


@pytest.fixture
def s3(monkeypatch):
    monkeypatch.setenv("BUCKET", "bucket-teste")

    def instalar(objetos_lyft, negar_copia=False):
        bucket = BucketFalso(negar_copia)
        monkeypatch.setattr(viagens, "cliente_publico", LyftFalsa(objetos_lyft))
        monkeypatch.setattr(viagens, "cliente", bucket)
        return bucket

    return instalar


def meses(lista):
    return [viagem["mes"] for viagem in lista]


def test_listar_filtra_nome_e_mes(s3):
    s3(
        [
            objeto_lyft("index.html"),
            objeto_lyft("2017-fordgobike-tripdata.csv.zip"),
            objeto_lyft("202112-baywheels-tripdata.csv.zip"),
            objeto_lyft("202609-lyftbikes-tripdata.zip"),
            objeto_lyft("202201-baywheels-tripdata.csv.zip"),
            objeto_lyft("202505-baywheeels-tripdata.csv.zip"),
        ]
    )

    assert meses(viagens.listar_viagens()) == ["202201", "202505", "202609"]


def test_listar_mes_repetido_fica_com_o_mais_recente(s3):
    s3(
        [
            objeto_lyft("202603-baywheels-tripdata.csv.zip", etag="velho", dia=1),
            objeto_lyft("202603-lyftbikes-tripdata.zip", etag="novo", dia=9),
        ]
    )

    lista = viagens.listar_viagens()

    assert len(lista) == 1
    assert lista[0]["chave"] == "202603-lyftbikes-tripdata.zip"


def test_pendentes_sem_manifesto_e_tudo():
    lista = [{"mes": "202201", "etag": "a"}, {"mes": "202202", "etag": "b"}]

    assert viagens.pendentes(lista, {}) == lista


def test_pendentes_com_manifesto_igual_e_nada():
    lista = [{"mes": "202201", "etag": "a"}, {"mes": "202202", "etag": "b"}]
    manifesto = {"202201": {"etag": "a"}, "202202": {"etag": "b"}}

    assert viagens.pendentes(lista, manifesto) == []


def test_pendentes_pega_arquivo_republicado():
    lista = [{"mes": "202201", "etag": "a"}, {"mes": "202202", "etag": "b-novo"}]
    manifesto = {"202201": {"etag": "a"}, "202202": {"etag": "b"}}

    assert meses(viagens.pendentes(lista, manifesto)) == ["202202"]


def test_pendentes_renomeado_com_mesmo_conteudo_nao_copia():
    lista = [{"mes": "202201", "chave": "202201-lyftbikes-tripdata.zip", "etag": "a"}]
    manifesto = {"202201": {"origem": "202201-baywheels-tripdata.csv.zip", "etag": "a"}}

    assert viagens.pendentes(lista, manifesto) == []


def test_ler_manifesto_inexistente_e_vazio(s3):
    s3([])

    assert viagens.ler_manifesto() == {}


def test_ler_manifesto_sem_permissao_falha(s3, monkeypatch):
    bucket = s3([])

    def negar(Bucket, Key):
        raise erro_s3("AccessDenied")

    monkeypatch.setattr(bucket, "get_object", negar)

    # Tratar AccessDenied como "sem manifesto" recopiaria tudo
    with pytest.raises(ClientError):
        viagens.ler_manifesto()


def test_copiar_viagem_usa_copy_object(s3):
    bucket = s3([])

    destino = viagens.copiar_viagem("202609-lyftbikes-tripdata.zip")

    assert destino == "raw/trips/202609/202609-lyftbikes-tripdata.zip"
    assert bucket.objetos[destino] == b"zip de 202609-lyftbikes-tripdata.zip"


def test_copiar_viagem_negada_cai_no_download_e_upload(s3):
    bucket = s3([], negar_copia=True)

    destino = viagens.copiar_viagem("202609-lyftbikes-tripdata.zip")

    assert bucket.objetos[destino] == b"zip de 202609-lyftbikes-tripdata.zip"


def test_carga_inicial_retomavel(s3):
    lyft = [objeto_lyft(f"2022{mes:02d}-baywheels-tripdata.csv.zip") for mes in range(1, 13)]
    bucket = s3(lyft)

    primeira = viagens.lambda_handler({}, None)
    segunda = viagens.lambda_handler({}, None)
    terceira = viagens.lambda_handler({}, None)

    assert (primeira["pendentes"], len(primeira["copiadas"]), primeira["restantes"]) == (12, 10, 2)
    assert (segunda["pendentes"], len(segunda["copiadas"])) == (2, 2)
    assert terceira == {"pendentes": 0, "copiadas": [], "restantes": 0}
    # Um manifesto salvo por cópia, cada um com um mês a mais
    assert [len(salvo) for salvo in bucket.manifestos_salvos] == list(range(1, 13))
    assert len([chave for chave in bucket.objetos if chave.startswith("raw/trips/")]) == 12


def test_republicacao_copia_so_o_mes_que_mudou(s3):
    lyft = [objeto_lyft(f"2022{mes:02d}-baywheels-tripdata.csv.zip") for mes in range(1, 4)]
    s3(lyft)
    viagens.lambda_handler({}, None)

    lyft[1]["ETag"] = "e2"
    resultado = viagens.lambda_handler({}, None)

    assert resultado["copiadas"] == ["raw/trips/202202/202202-baywheels-tripdata.csv.zip"]
