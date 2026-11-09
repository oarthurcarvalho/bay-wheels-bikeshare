import io
from datetime import date, datetime, timedelta, timezone

import pytest

import cobertura
from gbfs import montar_linha

MEIA_NOITE = datetime(2026, 11, 1, tzinfo=timezone.utc)
FIM_DO_DIA = MEIA_NOITE + timedelta(days=1)


def gerar_coletas(primeira, quantidade):
    return [
        {
            "horario": primeira + i * timedelta(minutes=5),
            "bytes": 20_000,
            "last_updated": 1_793_000_000 + i * 300,
        }
        for i in range(quantidade)
    ]


def coletas_do_dia_31():
    # Mesmo padrão do S3: a 1ª execução às 21:35:59 e depois sempre em x0:36 / x5:36
    primeira = datetime(2026, 10, 31, 21, 35, 59, tzinfo=timezone.utc)
    resto = gerar_coletas(datetime(2026, 10, 31, 21, 40, 36, tzinfo=timezone.utc), 28)
    return [{"horario": primeira, "bytes": 20_000, "last_updated": 1}, *resto]


class S3Falso:
    """
    Faz o papel do cliente boto3: lista em 2 páginas e devolve linhas JSONL em gzip.
    """

    def __init__(self, objetos):
        self.objetos = objetos  # {chave: (tamanho, last_updated)}
        self.listagens = []
        self.lidos = []

    def get_paginator(self, nome):
        assert nome == "list_objects_v2"
        return self

    def paginate(self, **kwargs):
        self.listagens.append(kwargs)
        conteudo = [
            {"Key": chave, "Size": tamanho}
            for chave, (tamanho, _) in self.objetos.items()
            if chave.startswith(kwargs["Prefix"])
        ]
        if not conteudo:
            return [{"KeyCount": 0}]
        return [{"Contents": conteudo[:2]}, {"Contents": conteudo[2:]}]

    def get_object(self, Bucket, Key):
        self.lidos.append(Key)
        _, last_updated = self.objetos[Key]
        resposta = {"last_updated": last_updated, "data": {"stations": []}}
        return {"Body": io.BytesIO(montar_linha("station_status", resposta, MEIA_NOITE))}


@pytest.fixture
def usar_s3(monkeypatch):
    monkeypatch.setenv("BUCKET", "bucket-teste")

    def instalar(objetos):
        falso = S3Falso(objetos)
        monkeypatch.setattr(cobertura, "cliente", falso)
        return falso

    return instalar


def test_dia_completo():
    coletas = gerar_coletas(MEIA_NOITE + timedelta(seconds=36), 288)

    resultado = cobertura.calcular_cobertura(coletas, MEIA_NOITE, FIM_DO_DIA)

    assert resultado["esperadas"] == 288
    assert resultado["encontradas"] == 288
    assert resultado["cobertura"] == 1.0
    assert resultado["maior_intervalo_min"] == 5.0
    assert resultado["kb_medio"] == 19.5
    assert resultado["mb_dia"] == 5.49
    assert resultado["last_updated_repetido"] == 0


def test_buraco_de_20_minutos():
    coletas = gerar_coletas(MEIA_NOITE + timedelta(seconds=36), 288)
    del coletas[100:103]

    resultado = cobertura.calcular_cobertura(coletas, MEIA_NOITE, FIM_DO_DIA)

    assert resultado["encontradas"] == 285
    assert resultado["cobertura"] == 0.9896
    assert resultado["maior_intervalo_min"] == 20.0


def test_coleta_parou_no_meio_do_dia():
    coletas = gerar_coletas(MEIA_NOITE + timedelta(seconds=36), 144)

    resultado = cobertura.calcular_cobertura(coletas, MEIA_NOITE, FIM_DO_DIA)

    assert resultado["cobertura"] == 0.5
    # Da última coleta (11:55:36) até a meia-noite seguinte
    assert resultado["maior_intervalo_min"] == 724.4


def test_last_updated_repetido():
    coletas = gerar_coletas(MEIA_NOITE + timedelta(seconds=36), 288)
    coletas[10]["last_updated"] = coletas[9]["last_updated"]

    resultado = cobertura.calcular_cobertura(coletas, MEIA_NOITE, FIM_DO_DIA)

    assert resultado["last_updated_repetido"] == 1


def test_dia_parcial_com_primeiro_intervalo_menor_que_5_min():
    resultado = cobertura.calcular_cobertura(
        coletas_do_dia_31(), cobertura.COLETA_LIGADA_EM, MEIA_NOITE
    )

    assert resultado["esperadas"] == 29
    assert resultado["encontradas"] == 29
    assert resultado["cobertura"] == 1.0
    assert resultado["maior_intervalo_min"] == 5.0


def test_dia_sem_coletas():
    resultado = cobertura.calcular_cobertura([], MEIA_NOITE, FIM_DO_DIA)

    assert resultado["cobertura"] == 0
    assert resultado["kb_medio"] == 0
    assert resultado["maior_intervalo_min"] == 1440.0


def test_listar_coletas_le_horario_da_chave_e_ordena(usar_s3):
    prefixo = "raw/station_status/date=2026-11-01/"
    s3 = usar_s3(
        {
            f"{prefixo}001036.jsonl.gz": (20_100, 0),
            f"{prefixo}000036.jsonl.gz": (20_000, 0),
            f"{prefixo}000536.jsonl.gz": (19_900, 0),
            "raw/station_status/date=2026-10-31/235536.jsonl.gz": (20_000, 0),
        }
    )

    coletas = cobertura.listar_coletas(date(2026, 11, 1))

    assert s3.listagens == [{"Bucket": "bucket-teste", "Prefix": prefixo}]
    assert [coleta["horario"] for coleta in coletas] == [
        MEIA_NOITE + timedelta(seconds=36),
        MEIA_NOITE + timedelta(minutes=5, seconds=36),
        MEIA_NOITE + timedelta(minutes=10, seconds=36),
    ]
    assert [coleta["bytes"] for coleta in coletas] == [20_000, 19_900, 20_100]


def test_listar_dia_vazio(usar_s3):
    usar_s3({})

    assert cobertura.listar_coletas(date(2026, 11, 1)) == []


def test_ler_last_updated(usar_s3):
    chave = "raw/station_status/date=2026-11-01/000036.jsonl.gz"
    usar_s3({chave: (20_000, 1_793_240_082)})

    assert cobertura.ler_last_updated(chave) == 1_793_240_082


def test_cobertura_dia_ignora_o_test_manual(usar_s3):
    objetos = {"raw/station_status/date=2026-10-31/010123.jsonl.gz": (20_000, 1)}
    for i, coleta in enumerate(coletas_do_dia_31()):
        chave = f"raw/station_status/date=2026-10-31/{coleta['horario']:%H%M%S}.jsonl.gz"
        objetos[chave] = (20_000, 1_793_000_000 + i * 300)
    s3 = usar_s3(objetos)

    resultado = cobertura.cobertura_dia(date(2026, 10, 31), agora=MEIA_NOITE + timedelta(hours=1))

    assert resultado["data"] == "2026-10-31"
    assert resultado["encontradas"] == 29
    assert resultado["cobertura"] == 1.0
    assert "raw/station_status/date=2026-10-31/010123.jsonl.gz" not in s3.lidos


def test_cobertura_dia_antes_de_ligar_a_coleta(usar_s3):
    usar_s3({})

    with pytest.raises(ValueError):
        cobertura.cobertura_dia(date(2026, 10, 30), agora=MEIA_NOITE)


def test_estacoes_sem_comunicacao_usa_o_relogio_do_feed():
    resposta = {
        "last_updated": 10_000,
        "data": {
            "stations": [
                {"station_id": "b", "last_reported": 10_000 - 7_200},
                {"station_id": "em-dia", "last_reported": 9_990},
                {"station_id": "exato-1h", "last_reported": 10_000 - 3_600},
                {"station_id": "a", "last_reported": 0},
                {"station_id": "sem-campo"},
            ]
        },
    }

    # Ordenadas; 1 h cravada ainda não conta
    assert cobertura.estacoes_sem_comunicacao(resposta) == ["a", "b", "sem-campo"]


def test_relatorio_dia_sem_coletas_tem_zero_estacoes(usar_s3):
    usar_s3({})

    relatorio = cobertura.relatorio_cobertura(date(2026, 11, 1), agora=FIM_DO_DIA)

    assert relatorio["cobertura"] == 0
    assert relatorio["estacoes"] == 0
    assert relatorio["estacoes_sem_comunicacao"] == []
