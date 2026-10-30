import gzip
import json
import datetime
import pathlib
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError


def buscar_feed(feed):

    url = f"https://gbfs.lyft.com/gbfs/2.3/bay/en/{feed}.json"
    user_agent = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36"

    config_request = Request(url, headers={"User-Agent": user_agent})

    for tentativa in range(2):

        try:
            with urlopen(config_request, timeout=8) as response:
                content = json.loads(response.read())
                return content

        except HTTPError as e:
            if e.code >= 500 and tentativa < 1:
                print(f"Erro HTTP {e.code}. Tentando novamente...")
                continue
            raise

        except URLError as e:
            if isinstance(e.reason, TimeoutError) and tentativa < 1:
                print("Timeout. Tentando novamente...")
                continue
            raise

        except TimeoutError:
            if tentativa < 1:
                print("Timeout. Tentando novamente...")
                continue
            raise


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
