"""
Vigia dos Trilhos — servidor.

Um ciclo de coleta consulta as fontes, grava o estado, deriva as transicoes e
dispara os e-mails. O ciclo roda de dois jeitos, e os dois podem conviver:

  thread interna   ligada por POLL_SEGUNDOS (padrao 60). Boa para rodar local.
  GET /api/cron/poll   para hospedagem gratuita que derruba o processo ocioso:
                       um cron externo bate nessa URL e, na mesma requisicao,
                       acorda o servico e executa a coleta.

Variaveis de ambiente relevantes estao no .env.example.
"""

from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timezone

from flask import Flask, jsonify, render_template, request

import alertas
import dados
import fontes

# --------------------------------------------------------------------------- #
# configuracao
# --------------------------------------------------------------------------- #

POLL_SEGUNDOS = int(os.environ.get("POLL_SEGUNDOS", "60"))
INTERVALO_MINIMO = int(os.environ.get("INTERVALO_MINIMO", "25"))  # anti-martelada
ARTESP_API_KEY = os.environ.get("ARTESP_API_KEY", "").strip()
# 12 requisicoes/hora e o teto da credencial; 10 minutos deixa folga para retry
ARTESP_INTERVALO_MIN = int(os.environ.get("ARTESP_INTERVALO_MIN", "10"))
CRON_TOKEN = os.environ.get("CRON_TOKEN", "").strip()
SEMEAR_DEMO = os.environ.get("SEMEAR_DEMO", "").lower() in ("1", "true", "sim")

app = Flask(__name__)

_lock_ciclo = threading.Lock()
_ultimo_ciclo = 0.0
_ultimo_artesp = 0.0
_ultimo_resultado: dict = {}


# --------------------------------------------------------------------------- #
# ciclo de coleta
# --------------------------------------------------------------------------- #

def executar_ciclo(forcar: bool = False) -> dict:
    """Uma rodada completa: coleta, persiste, detecta transicoes, notifica."""
    global _ultimo_ciclo, _ultimo_artesp, _ultimo_resultado

    with _lock_ciclo:
        relogio = time.monotonic()
        if not forcar and _ultimo_ciclo and relogio - _ultimo_ciclo < INTERVALO_MINIMO:
            return {
                **_ultimo_resultado,
                "pulado": True,
                "motivo": f"último ciclo há {int(relogio - _ultimo_ciclo)}s",
            }
        _ultimo_ciclo = relogio

        usar_artesp = bool(ARTESP_API_KEY) and (
            not _ultimo_artesp or relogio - _ultimo_artesp >= ARTESP_INTERVALO_MIN * 60
        )

        bruto = fontes.coletar_tudo(ARTESP_API_KEY, usar_artesp)
        if usar_artesp:
            _ultimo_artesp = relogio

        dados.registrar_coletas(bruto["momento"], bruto["coletas"])
        novos = dados.aplicar_leituras(bruto["leituras"], bruto["momento"])
        notificados = _notificar(novos)

        _ultimo_resultado = {
            "pulado": False,
            "momento": bruto["momento"],
            "linhas_lidas": len(bruto["leituras"]),
            "fontes": [
                {"fonte": c.fonte, "ok": c.ok, "detalhe": c.detalhe, "ms": c.ms}
                for c in bruto["coletas"]
            ],
            "divergencias": bruto["divergencias"],
            "eventos_novos": len(novos),
            "emails_enviados": notificados,
        }
        return _ultimo_resultado


def _notificar(eventos: list[dict]) -> int:
    if not eventos or not alertas.configurado():
        return 0
    enviados = 0
    for inscricao, pertinentes in dados.destinatarios_para(eventos):
        ok, detalhe = alertas.enviar_alerta(
            inscricao["email"], pertinentes, inscricao["token"]
        )
        if ok:
            dados.marcar_enviados(inscricao["id"], pertinentes)
            enviados += 1
        else:
            app.logger.warning("falha ao enviar para %s: %s", inscricao["email"], detalhe)
    return enviados


def _laco_de_fundo() -> None:
    time.sleep(3)  # deixa o servidor subir antes da primeira coleta
    while True:
        try:
            executar_ciclo()
        except Exception:  # noqa: BLE001 - o laco nunca pode morrer
            app.logger.exception("falha no ciclo de coleta")
        time.sleep(POLL_SEGUNDOS)


# --------------------------------------------------------------------------- #
# paginas
# --------------------------------------------------------------------------- #

@app.get("/")
def pagina_inicial():
    return render_template(
        "index.html",
        email_ativo=alertas.configurado(),
        artesp_ativo=bool(ARTESP_API_KEY),
    )


@app.get("/cancelar/<token>")
def pagina_cancelar(token: str):
    ok = dados.cancelar_inscricao(token)
    return render_template("cancelar.html", ok=ok)


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #

@app.get("/api/linhas")
def api_linhas():
    return jsonify(
        {
            "linhas": [
                {"linha": n, **info} for n, info in sorted(fontes.LINHAS.items())
            ],
            "status": fontes.ROTULO_STATUS,
            "severidade": fontes.SEVERIDADE,
            "fontes": fontes.NOME_FONTE,
        }
    )


@app.get("/api/estado")
def api_estado():
    return jsonify(dados.estado())


@app.get("/api/ocorrencias")
def api_ocorrencias():
    linhas = _inteiros(request.args.get("linhas", ""))
    return jsonify(
        dados.ocorrencias(
            linhas=linhas or None,
            dias=_inteiro(request.args.get("dias"), 7, 1, 365),
            severidade_min=_inteiro(request.args.get("severidade"), 1, 0, 3),
            apenas_abertas=request.args.get("abertas") == "1",
            limite=_inteiro(request.args.get("limite"), 100, 1, 500),
            deslocamento=_inteiro(request.args.get("offset"), 0, 0, 100000),
        )
    )


@app.get("/api/linha-do-tempo")
def api_linha_do_tempo():
    return jsonify(dados.linha_do_tempo(_inteiro(request.args.get("dias"), 7, 1, 90)))


@app.get("/api/serie")
def api_serie():
    return jsonify({"serie": dados.serie_diaria(_inteiro(request.args.get("dias"), 14, 7, 90))})


@app.get("/api/saude")
def api_saude():
    info = dados.saude()
    info["email_configurado"] = alertas.configurado()
    info["artesp_configurada"] = bool(ARTESP_API_KEY)
    info["poll_segundos"] = POLL_SEGUNDOS
    info["ultimo_ciclo"] = _ultimo_resultado or None
    return jsonify(info)


@app.get("/api/ocorrencias/artesp")
def api_ocorrencias_artesp():
    """
    Historico oficial. Consome 1 das 12 requisicoes/hora da chave, entao e um
    botao explicito na interface, nunca uma chamada automatica.
    """
    if not ARTESP_API_KEY:
        return jsonify({"erro": "ARTESP_API_KEY não configurada", "itens": []}), 400
    itens, erro = fontes.ocorrencias_artesp(
        ARTESP_API_KEY, dias=_inteiro(request.args.get("dias"), 30, 1, 365)
    )
    if erro:
        return jsonify({"erro": erro, "itens": []}), 502
    for item in itens:
        info = fontes.LINHAS.get(item["linha"] or 0, {})
        item["nome"] = info.get("nome", "?")
        item["cor"] = info.get("cor", "#888888")
        item["operador"] = info.get("operador", "")
        item["status_rotulo"] = fontes.ROTULO_STATUS.get(item["status"], item["rotulo"])
        item["severidade"] = fontes.SEVERIDADE.get(item["status"], 1)
        item["em_curso"] = False
    return jsonify({"total": len(itens), "itens": itens})


@app.post("/api/inscricoes")
def api_inscrever():
    corpo = request.get_json(silent=True) or {}
    email = str(corpo.get("email", "")).strip().lower()
    linhas = [int(n) for n in corpo.get("linhas", []) if str(n).isdigit()]
    severidade = _inteiro(corpo.get("severidade"), 1, 0, 3)

    if "@" not in email or "." not in email.split("@")[-1]:
        return jsonify({"erro": "e-mail inválido"}), 400
    if not linhas:
        return jsonify({"erro": "escolha pelo menos uma linha"}), 400
    if not alertas.configurado():
        return jsonify({"erro": "envio de e-mail não está configurado neste servidor"}), 503

    registro = dados.criar_inscricao(email, linhas, severidade)
    return jsonify(
        {
            "ok": True,
            "novo": registro["novo"],
            "linhas": sorted(set(linhas)),
            "mensagem": "Inscrição criada." if registro["novo"] else "Preferências atualizadas.",
        }
    )


@app.post("/api/inscricoes/cancelar")
def api_cancelar():
    corpo = request.get_json(silent=True) or {}
    email = str(corpo.get("email", "")).strip().lower()
    registro = dados.inscricao_por_email(email)
    if not registro:
        return jsonify({"erro": "nenhuma inscrição ativa para este e-mail"}), 404
    dados.cancelar_inscricao(registro["token"])
    return jsonify({"ok": True})


@app.route("/api/cron/poll", methods=["GET", "POST"])
def api_cron():
    """Alvo do cron externo. Acorda o servico e executa uma coleta na mesma chamada."""
    if CRON_TOKEN:
        recebido = request.args.get("token") or request.headers.get("X-Cron-Token", "")
        if recebido != CRON_TOKEN:
            return jsonify({"erro": "token inválido"}), 403
    return jsonify(executar_ciclo(forcar=request.args.get("forcar") == "1"))


# --------------------------------------------------------------------------- #
# utilidades
# --------------------------------------------------------------------------- #

def _inteiro(valor, padrao: int, minimo: int, maximo: int) -> int:
    try:
        return max(minimo, min(int(valor), maximo))
    except (TypeError, ValueError):
        return padrao


def _inteiros(texto: str) -> list[int]:
    return [int(p) for p in texto.split(",") if p.strip().isdigit()]


@app.after_request
def _sem_cache(resposta):
    if request.path.startswith("/api/"):
        resposta.headers["Cache-Control"] = "no-store"
    return resposta


# --------------------------------------------------------------------------- #
# inicializacao
# --------------------------------------------------------------------------- #

def preparar() -> None:
    dados.iniciar()
    if SEMEAR_DEMO:
        criados = dados.semear_demonstracao()
        if criados:
            app.logger.info("semeados %s eventos de demonstração", criados)
    if POLL_SEGUNDOS > 0:
        threading.Thread(target=_laco_de_fundo, daemon=True, name="coletor").start()


preparar()


if __name__ == "__main__":
    porta = int(os.environ.get("PORT", "8000"))
    app.run(host="0.0.0.0", port=porta, debug=os.environ.get("DEBUG") == "1")
