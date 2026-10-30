from gbfs import buscar_feed, montar_linha

FEEDS_HORARIOS = ["station_information", "system_regions"]


def chave_bruto(feed, agora):

    if agora.tzinfo is None:
        raise ValueError("agora precisa ter fuso horário (UTC)")

    return f"raw/{feed}/date={agora.strftime('%Y-%m-%d')}/{agora.strftime('%H%M%S')}.jsonl.gz"


def coletar_feed(feed, agora):

    resposta = buscar_feed(feed)
    return montar_linha(feed, resposta, agora)


def coletar_status(agora):

    arquivos = {}

    arquivos[chave_bruto("station_status", agora)] = coletar_feed("station_status", agora)

    if agora.minute < 5:
        for feed in FEEDS_HORARIOS:
            try:
                arquivos[chave_bruto(feed, agora)] = coletar_feed(feed, agora)
            except Exception as e:
                print(f"Falha ao coletar {feed}: {e!r}")

    return arquivos
