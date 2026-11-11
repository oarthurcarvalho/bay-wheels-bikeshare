import io
import json
from urllib.error import HTTPError, URLError

import pytest

import gbfs


def erro_http(codigo):
    return HTTPError("https://gbfs", codigo, f"HTTP {codigo}", {}, io.BytesIO())


class RespostaFalsa:
    def __init__(self, corpo):
        self.corpo = corpo

    def __enter__(self):
        return self

    def __exit__(self, *erro):
        return False

    def read(self):
        return json.dumps(self.corpo).encode()


@pytest.fixture
def servidor(monkeypatch):
    """
    Troca o urlopen por uma fila de respostas (exceção = falha) e o esperar
    por um que só anota. Devolve as URLs pedidas e as esperas.
    """
    registro = {"urls": [], "esperas": []}

    def instalar(*respostas):
        fila = list(respostas)

        def urlopen_falso(pedido, timeout):
            registro["urls"].append(pedido.full_url)
            resposta = fila.pop(0)
            if isinstance(resposta, Exception):
                raise resposta
            return RespostaFalsa(resposta)

        monkeypatch.setattr(gbfs, "urlopen", urlopen_falso)
        monkeypatch.setattr(gbfs, "esperar", registro["esperas"].append)
        return registro

    return instalar


def test_sucesso_de_primeira_nao_espera(servidor):
    registro = servidor({"last_updated": 1})

    assert gbfs.buscar_feed("station_status") == {"last_updated": 1}
    assert registro["esperas"] == []
    assert registro["urls"] == ["https://gbfs.lyft.com/gbfs/2.3/bay/en/station_status.json"]


def test_503_e_429_esperam_1_e_4_segundos(servidor):
    registro = servidor(erro_http(503), erro_http(429), {"last_updated": 1})

    assert gbfs.buscar_feed("station_status") == {"last_updated": 1}
    assert registro["esperas"] == [1, 4]


def test_tres_falhas_seguidas_sobem_o_erro(servidor):
    registro = servidor(erro_http(500), erro_http(502), erro_http(503))

    with pytest.raises(HTTPError) as erro:
        gbfs.buscar_feed("station_status")

    assert erro.value.code == 503
    assert registro["esperas"] == [1, 4]


def test_404_falha_na_hora(servidor):
    registro = servidor(erro_http(404), {"nunca": "chega"})

    with pytest.raises(HTTPError):
        gbfs.buscar_feed("station_status")

    assert len(registro["urls"]) == 1
    assert registro["esperas"] == []


def test_timeout_e_erro_de_rede_tentam_de_novo(servidor):
    registro = servidor(URLError(TimeoutError()), TimeoutError(), {"last_updated": 1})

    assert gbfs.buscar_feed("station_status") == {"last_updated": 1}
    assert registro["esperas"] == [1, 4]


def test_gbfs_url_troca_o_endereco(servidor, monkeypatch):
    # É assim que a falha da terça é provocada na Lambda
    monkeypatch.setenv("GBFS_URL", "https://gbfs.lyft.com/gbfs/2.3/bay/quebrado")
    registro = servidor(erro_http(404))

    with pytest.raises(HTTPError):
        gbfs.buscar_feed("station_status")

    assert registro["urls"] == ["https://gbfs.lyft.com/gbfs/2.3/bay/quebrado/station_status.json"]
