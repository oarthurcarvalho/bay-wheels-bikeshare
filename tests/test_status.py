import gzip
import json
from datetime import datetime, timedelta, timezone

import pytest

import status

MEIA_NOITE_E_3 = datetime(2026, 10, 29, 0, 3, tzinfo=timezone.utc)
MEIA_NOITE_E_8 = datetime(2026, 10, 29, 0, 8, tzinfo=timezone.utc)


@pytest.fixture
def feeds_pedidos(monkeypatch):
    """
    Troca o buscar_feed por um falso e devolve a lista de feeds pedidos.
    """
    pedidos = []

    def buscar_feed_falso(feed):
        pedidos.append(feed)
        return {"data": {"stations": []}}

    monkeypatch.setattr(status, "buscar_feed", buscar_feed_falso)
    return pedidos


def falhar_em(monkeypatch, feed_com_erro):
    def buscar_feed_falso(feed):
        if feed == feed_com_erro:
            raise TimeoutError("simulado")
        return {"data": {"stations": []}}

    monkeypatch.setattr(status, "buscar_feed", buscar_feed_falso)


def test_chave_bruto():
    chave = status.chave_bruto("station_status", MEIA_NOITE_E_3)
    assert chave == "raw/station_status/date=2026-10-29/000300.jsonl.gz"


def test_chave_bruto_usa_data_utc():
    # 23:30 em São Paulo (UTC-3) já é 02:30 do dia seguinte em UTC
    sao_paulo = timezone(timedelta(hours=-3))
    agora = datetime(2026, 10, 28, 23, 30, tzinfo=sao_paulo).astimezone(timezone.utc)

    chave = status.chave_bruto("station_status", agora)
    assert chave == "raw/station_status/date=2026-10-29/023000.jsonl.gz"


def test_chave_bruto_sem_fuso():
    with pytest.raises(ValueError):
        status.chave_bruto("station_status", datetime(2026, 10, 29, 0, 3))


def test_inicio_da_hora_coleta_3_feeds(feeds_pedidos):
    arquivos = status.coletar_status(MEIA_NOITE_E_3)

    assert sorted(arquivos) == [
        "raw/station_information/date=2026-10-29/000300.jsonl.gz",
        "raw/station_status/date=2026-10-29/000300.jsonl.gz",
        "raw/system_regions/date=2026-10-29/000300.jsonl.gz",
    ]
    assert feeds_pedidos == ["station_status", "station_information", "system_regions"]


def test_meio_da_hora_coleta_so_status(feeds_pedidos):
    arquivos = status.coletar_status(MEIA_NOITE_E_8)

    assert list(arquivos) == ["raw/station_status/date=2026-10-29/000800.jsonl.gz"]
    assert feeds_pedidos == ["station_status"]


def test_conteudo_e_linha_jsonl_do_feed(feeds_pedidos):
    arquivos = status.coletar_status(MEIA_NOITE_E_8)

    texto = gzip.decompress(arquivos["raw/station_status/date=2026-10-29/000800.jsonl.gz"]).decode()
    assert texto.endswith("\n")
    linha = json.loads(texto)
    assert linha["feed"] == "station_status"
    assert linha["coletado_em"] == "2026-10-29T00:08:00+00:00"


def test_falha_em_feed_horario_nao_derruba_status(monkeypatch):
    falhar_em(monkeypatch, "station_information")

    arquivos = status.coletar_status(MEIA_NOITE_E_3)

    assert sorted(arquivos) == [
        "raw/station_status/date=2026-10-29/000300.jsonl.gz",
        "raw/system_regions/date=2026-10-29/000300.jsonl.gz",
    ]


def test_falha_no_status_sobe(monkeypatch):
    falhar_em(monkeypatch, "station_status")

    with pytest.raises(TimeoutError):
        status.coletar_status(MEIA_NOITE_E_3)
