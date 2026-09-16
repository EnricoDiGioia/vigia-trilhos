"""
Coletores das fontes de status das linhas metroferroviarias da RMSP.

Tres fontes, papeis diferentes:

  proximo_trem   backend do app oficial. Sem autenticacao, cobre as 15 linhas.
                 E a fonte primaria.
  artesp         API Trilhos do Portal CCM. Documentada, exige Api-Key e limita
                 12 requisicoes/hora. Cobre so as concessoes privadas (4,5,6,7,8,9),
                 mas e a unica com historico de ocorrencias (ate 365 dias).
  viamobilidade  raspagem do HTML da home. Fallback quando a primaria falha.

Nenhuma dependencia externa: so a biblioteca padrao.
"""

from __future__ import annotations

import json
import re
import ssl
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

TIMEOUT = 20
UA = "vigia-trilhos/1.0 (monitor de status metroferroviario)"

URL_PROXIMO_TREM = "https://apim-proximotrem-prd-brazilsouth-001.azure-api.net/api/v1"
URL_ARTESP = "https://ccm.artesp.sp.gov.br/metroferroviario/api"
URL_VIAMOBILIDADE = "https://www.viamobilidade.com.br/"

# --------------------------------------------------------------------------- #
# catalogo da rede
# --------------------------------------------------------------------------- #

LINHAS: dict[int, dict[str, str]] = {
    1:  {"nome": "Azul",      "operador": "Metrô SP",      "cor": "#0455A1"},
    2:  {"nome": "Verde",     "operador": "Metrô SP",      "cor": "#007E5E"},
    3:  {"nome": "Vermelha",  "operador": "Metrô SP",      "cor": "#EE3124"},
    4:  {"nome": "Amarela",   "operador": "ViaQuatro",     "cor": "#E8B900"},
    5:  {"nome": "Lilás",     "operador": "ViaMobilidade", "cor": "#9B3F96"},
    6:  {"nome": "Laranja",   "operador": "Linha Uni",     "cor": "#F07D00"},
    7:  {"nome": "Rubi",      "operador": "TIC Trens",     "cor": "#CA016B"},
    8:  {"nome": "Diamante",  "operador": "ViaMobilidade", "cor": "#9AA0A2"},
    9:  {"nome": "Esmeralda", "operador": "ViaMobilidade", "cor": "#01A9A7"},
    10: {"nome": "Turquesa",  "operador": "CPTM",          "cor": "#049FC3"},
    11: {"nome": "Coral",     "operador": "Trivia Trens",  "cor": "#F58220"},
    12: {"nome": "Safira",    "operador": "Trivia Trens",  "cor": "#01529C"},
    13: {"nome": "Jade",      "operador": "Trivia Trens",  "cor": "#00AB4F"},
    15: {"nome": "Prata",     "operador": "Metrô SP",      "cor": "#82898B"},
    17: {"nome": "Ouro",      "operador": "ViaMobilidade", "cor": "#B8930F"},
}

# severidade: 0 nada a reportar, 3 o pior caso
SEVERIDADE: dict[str, int] = {
    "normal": 0,
    "encerrada": 0,
    "desconhecido": 1,
    "reduzida": 1,
    "parcial": 2,
    "paralisada": 3,
}

ROTULO_STATUS: dict[str, str] = {
    "normal": "Operação normal",
    "encerrada": "Operação encerrada",
    "desconhecido": "Sem informação",
    "reduzida": "Velocidade reduzida",
    "parcial": "Operação parcial",
    "paralisada": "Paralisada",
}

# ordem de confianca quando mais de uma fonte reporta a mesma linha
PRIORIDADE_FONTE = ["proximo_trem", "artesp", "viamobilidade"]

NOME_FONTE = {
    "proximo_trem": "Próximo Trem",
    "artesp": "ARTESP CCM",
    "viamobilidade": "ViaMobilidade",
}


@dataclass
class Leitura:
    """Uma observacao do estado de uma linha, ja normalizada."""

    linha: int
    status: str
    rotulo: str
    descricao: str
    fonte: str
    atualizado_em: str | None = None  # carimbo da origem, quando ela informa


@dataclass
class Coleta:
    """Resultado de uma chamada a uma fonte."""

    fonte: str
    ok: bool
    leituras: list[Leitura]
    detalhe: str = ""
    ms: int = 0


# --------------------------------------------------------------------------- #
# normalizacao
# --------------------------------------------------------------------------- #

def _sem_acento(txt: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", txt) if unicodedata.category(c) != "Mn"
    )


def normalizar_status(rotulo: str) -> str:
    """Reduz o rotulo livre de cada fonte a um dos seis estados canonicos."""
    t = _sem_acento(rotulo or "").lower().strip()
    if not t:
        return "desconhecido"
    if any(p in t for p in ("paralisad", "interromp", "suspens", "sem circulac")):
        return "paralisada"
    if "parcial" in t:
        return "parcial"
    if any(p in t for p in ("reduzid", "maior tempo", "lentid", "diferenciad", "restric")):
        return "reduzida"
    if any(p in t for p in ("encerrad", "fora de operac", "fechad", "nao opera")):
        return "encerrada"
    if "normal" in t:
        return "normal"
    return "desconhecido"


def _limpar_html(txt: str) -> str:
    txt = re.sub(r"<[^>]+>", " ", txt or "")
    txt = (
        txt.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
    )
    return re.sub(r"\s+", " ", txt).strip()


def _numero_linha(texto: object) -> int | None:
    m = re.search(r"\d+", str(texto or ""))
    if not m:
        return None
    n = int(m.group())
    return n if n in LINHAS else None


def agora_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #

def _requisitar(url: str, cabecalhos: dict[str, str] | None = None) -> tuple[int, bytes, dict]:
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(cabecalhos or {})})
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=ctx) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


def _json(url: str, cabecalhos: dict[str, str] | None = None) -> tuple[object, str]:
    """Devolve (dados, erro). Em caso de falha, dados e None e erro descreve o motivo."""
    try:
        codigo, corpo, cab = _requisitar(url, cabecalhos)
    except Exception as e:  # noqa: BLE001 - qualquer falha de rede vira detalhe legivel
        return None, f"{type(e).__name__}: {e}"

    if codigo != 200:
        extra = ""
        if codigo == 429 and cab.get("Retry-After"):
            extra = f", tente de novo em {cab['Retry-After']}s"
        if codigo == 403:
            extra = ", IP possivelmente fora da allowlist"
        return None, f"HTTP {codigo}{extra}"

    try:
        return json.loads(corpo), ""
    except json.JSONDecodeError:
        return None, "resposta não é JSON"


# --------------------------------------------------------------------------- #
# fonte 1: Proximo Trem
# --------------------------------------------------------------------------- #

def coletar_proximo_trem() -> Coleta:
    inicio = datetime.now()
    dados, erro = _json(f"{URL_PROXIMO_TREM}/lines")
    ms = int((datetime.now() - inicio).total_seconds() * 1000)

    if erro:
        return Coleta("proximo_trem", False, [], erro, ms)
    if not isinstance(dados, dict) or not isinstance(dados.get("Data"), list):
        return Coleta("proximo_trem", False, [], "formato inesperado: sem a chave Data", ms)

    leituras: list[Leitura] = []
    for item in dados["Data"]:
        if not isinstance(item, dict):
            continue
        codigo = item.get("Code")
        if not isinstance(codigo, int) or codigo not in LINHAS:
            continue
        rotulo = str(item.get("StatusLabel") or "").strip()
        leituras.append(
            Leitura(
                linha=codigo,
                status=normalizar_status(rotulo),
                rotulo=rotulo or "—",
                descricao=str(item.get("Description") or "").strip(),
                fonte="proximo_trem",
            )
        )

    if not leituras:
        return Coleta("proximo_trem", False, [], "nenhuma linha reconhecida na resposta", ms)
    return Coleta("proximo_trem", True, leituras, f"{len(leituras)} linhas", ms)


# --------------------------------------------------------------------------- #
# fonte 2: ARTESP CCM
# --------------------------------------------------------------------------- #

def coletar_artesp(chave: str) -> Coleta:
    if not chave:
        return Coleta("artesp", False, [], "sem ARTESP_API_KEY configurada", 0)

    inicio = datetime.now()
    dados, erro = _json(
        f"{URL_ARTESP}/status/",
        {"Authorization": f"Api-Key {chave}", "Accept": "application/json"},
    )
    ms = int((datetime.now() - inicio).total_seconds() * 1000)

    if erro:
        return Coleta("artesp", False, [], erro, ms)
    if not isinstance(dados, dict):
        return Coleta("artesp", False, [], "formato inesperado", ms)

    leituras: list[Leitura] = []
    for empresa in dados.get("empresas", []) or []:
        for linha in empresa.get("linhas", []) or []:
            numero = _numero_linha(linha.get("codigo") or linha.get("nome"))
            if numero is None:
                continue
            status_bruto = linha.get("status") or {}
            rotulo = str(
                status_bruto.get("situacao") or status_bruto.get("classificacao") or ""
            ).strip()
            # a API traz um booleano explicito; ele manda mais que o texto livre
            if status_bruto.get("operacao_normal") is True and not rotulo:
                rotulo = "Operação Normal"
            leituras.append(
                Leitura(
                    linha=numero,
                    status=normalizar_status(rotulo),
                    rotulo=rotulo or "—",
                    descricao="",
                    fonte="artesp",
                    atualizado_em=status_bruto.get("atualizado_em"),
                )
            )

    if not leituras:
        return Coleta("artesp", False, [], "nenhuma linha na resposta", ms)
    return Coleta("artesp", True, leituras, f"{len(leituras)} linhas", ms)


def ocorrencias_artesp(
    chave: str,
    dias: int = 30,
    linha: int | None = None,
) -> tuple[list[dict], str]:
    """
    Historico oficial de ocorrencias. Cobre so as concessoes privadas, mas e o unico
    lugar com passado — e cada chamada consome 1 das 12 requisicoes/hora da chave.
    """
    if not chave:
        return [], "sem ARTESP_API_KEY configurada"

    dias = max(1, min(dias, 365))
    parametros = {
        "data_inicio": (date.today() - timedelta(days=dias)).isoformat(),
        "data_fim": date.today().isoformat(),
    }
    if linha:
        parametros["linha"] = str(linha)

    url = f"{URL_ARTESP}/ocorrencias/?" + urllib.parse.urlencode(parametros)
    dados, erro = _json(url, {"Authorization": f"Api-Key {chave}", "Accept": "application/json"})
    if erro:
        return [], erro
    if not isinstance(dados, dict):
        return [], "formato inesperado"

    saida: list[dict] = []
    for oc in dados.get("ocorrencias", []) or []:
        info_linha = oc.get("linha") or {}
        numero = _numero_linha(info_linha.get("codigo") or info_linha.get("nome"))
        classificacao = oc.get("classificacao") or {}
        situacao = str(oc.get("situacao") or classificacao.get("label") or "").strip()
        saida.append(
            {
                "id": f"artesp-{oc.get('id')}",
                "linha": numero,
                "status": normalizar_status(situacao),
                "rotulo": situacao or "—",
                "descricao": str(oc.get("descricao") or "").strip(),
                "inicio": oc.get("data_hora"),
                "fim": None,
                "duracao_min": None,
                "fonte": "artesp",
                "empresa": (oc.get("empresa") or {}).get("nome"),
            }
        )
    return saida, ""


# --------------------------------------------------------------------------- #
# fonte 3: raspagem da ViaMobilidade
# --------------------------------------------------------------------------- #

def coletar_viamobilidade() -> Coleta:
    inicio = datetime.now()
    try:
        codigo, corpo, _ = _requisitar(URL_VIAMOBILIDADE)
    except Exception as e:  # noqa: BLE001
        ms = int((datetime.now() - inicio).total_seconds() * 1000)
        return Coleta("viamobilidade", False, [], f"{type(e).__name__}: {e}", ms)

    ms = int((datetime.now() - inicio).total_seconds() * 1000)
    if codigo != 200:
        return Coleta("viamobilidade", False, [], f"HTTP {codigo}", ms)

    html = corpo.decode("utf-8", "replace")
    leituras: list[Leitura] = []

    for bloco in re.finditer(
        r'<li[^>]*class="[^"]*\bline-(\d+)\b[^"]*"[^>]*>(.*?)</li>', html, re.S | re.I
    ):
        numero = int(bloco.group(1))
        if numero not in LINHAS:
            continue
        interno = bloco.group(2)

        m_status = re.search(
            r'<[^>]*class="[^"]*\bstatus\b[^"]*"[^>]*>(.*?)</', interno, re.S | re.I
        )
        rotulo = _limpar_html(m_status.group(1)) if m_status else ""

        m_motivo = re.search(r"<p[^>]*>(.*?)</p>", interno, re.S | re.I)
        descricao = _limpar_html(m_motivo.group(1)) if m_motivo else ""

        leituras.append(
            Leitura(
                linha=numero,
                status=normalizar_status(rotulo),
                rotulo=rotulo or "—",
                descricao=descricao,
                fonte="viamobilidade",
            )
        )

    if not leituras:
        return Coleta(
            "viamobilidade",
            False,
            [],
            "seletor line-N não encontrado — o HTML mudou, reveja o parser",
            ms,
        )
    return Coleta("viamobilidade", True, leituras, f"{len(leituras)} linhas", ms)


# --------------------------------------------------------------------------- #
# consolidacao
# --------------------------------------------------------------------------- #

def consolidar(coletas: list[Coleta]) -> tuple[list[Leitura], list[dict]]:
    """
    Escolhe uma leitura por linha seguindo PRIORIDADE_FONTE e devolve tambem as
    divergencias — casos em que duas fontes discordam sobre a mesma linha. Divergencia
    nao e erro, mas e sinal: costuma aparecer quando uma fonte travou.
    """
    por_linha: dict[int, dict[str, Leitura]] = {}
    for coleta in coletas:
        if not coleta.ok:
            continue
        for leitura in coleta.leituras:
            por_linha.setdefault(leitura.linha, {})[leitura.fonte] = leitura

    escolhidas: list[Leitura] = []
    divergencias: list[dict] = []

    for numero in sorted(por_linha):
        candidatas = por_linha[numero]
        estados = {l.status for l in candidatas.values()}
        if len(estados) > 1:
            divergencias.append(
                {
                    "linha": numero,
                    "por_fonte": {f: l.status for f, l in candidatas.items()},
                }
            )
        for fonte in PRIORIDADE_FONTE:
            if fonte in candidatas:
                escolhidas.append(candidatas[fonte])
                break

    return escolhidas, divergencias


def coletar_tudo(chave_artesp: str = "", usar_artesp: bool = True) -> dict:
    """Roda todas as fontes disponiveis e devolve o material de um ciclo de coleta."""
    coletas = [coletar_proximo_trem()]

    # a ARTESP so entra se a primaria falhou ou se o chamador pediu explicitamente,
    # porque o teto de 12 requisicoes/hora nao aguenta poll continuo
    if chave_artesp and usar_artesp:
        coletas.append(coletar_artesp(chave_artesp))

    # o fallback so e acionado quando a primaria nao trouxe a rede inteira
    primaria = coletas[0]
    if not primaria.ok or len(primaria.leituras) < len(LINHAS):
        coletas.append(coletar_viamobilidade())

    leituras, divergencias = consolidar(coletas)
    return {
        "momento": agora_utc(),
        "coletas": coletas,
        "leituras": leituras,
        "divergencias": divergencias,
    }
