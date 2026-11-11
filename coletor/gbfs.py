import datetime
import gzip
import json
import os
import pathlib
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

# Trocar GBFS_URL na Lambda é como se provoca a falha da terça, sem novo deploy
URL_PADRAO = "https://gbfs.lyft.com/gbfs/2.3/bay/en"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36"
)
# Espera antes da 2ª e da 3ª tentativa
ESPERAS = (1, 4)
# Pior caso por feed: 3 × 5 s + 1 s + 4 s = 20 s. Na virada da hora são 3 feeds,
# mas se o status cair a execução para ali; os 2 horários somam 40 s de 60 s
TIMEOUT = 5
# Separado para o teste não esperar de verdade
esperar = time.sleep


def vale_tentar_de_novo(erro):

    # HTTPError é subclasse de URLError: tem que vir primeiro
    if isinstance(erro, HTTPError):
        # 429 = pediu para ir mais devagar; 5xx = problema do lado deles
        return erro.code == 429 or erro.code >= 500
    # Timeout, conexão recusada ou DNS: falhas de rede que costumam passar
    return isinstance(erro, (URLError, TimeoutError))


def buscar_feed(feed):

    url = f"{os.environ.get('GBFS_URL', URL_PADRAO)}/{feed}.json"
    pedido = Request(url, headers={"User-Agent": USER_AGENT})

    for espera in (*ESPERAS, None):
        try:
            with urlopen(pedido, timeout=TIMEOUT) as resposta:
                return json.loads(resposta.read())
        except (URLError, TimeoutError) as erro:
            # 404, 403...: tentar de novo não muda nada, falha na hora
            if espera is None or not vale_tentar_de_novo(erro):
                raise
            print(f"{feed}: {erro!r}; nova tentativa em {espera} s")
            esperar(espera)


def montar_linha(feed, resposta, agora):

    dict_linha = {
        "feed": feed,
        "coletado_em": agora.isoformat(),
        "resposta": resposta
    }
    linha = json.dumps(dict_linha) + "\n"
    return gzip.compress(linha.encode("utf-8"))


if __name__ == "__main__":

    feed = "station_status"
    agora = datetime.datetime.now(datetime.timezone.utc)
    resposta = buscar_feed(feed)
    conteudo_gz = montar_linha(feed, resposta, agora)

    pasta_raw = pathlib.Path(__file__).parent.parent / "data" / "raw"
    pasta_raw.mkdir(parents=True, exist_ok=True)

    arquivo = pasta_raw / f"{feed}_{agora.strftime('%H%M%S')}.jsonl.gz"
    arquivo.write_bytes(conteudo_gz)

    estacoes = len(resposta["data"]["stations"])
    print(f"{arquivo}: {len(conteudo_gz) / 1024:.1f} KB, {estacoes} estações")
