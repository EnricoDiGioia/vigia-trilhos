"""
Camada de persistencia: SQLite puro, sem ORM.

Guarda quatro coisas:
  estado_atual  o estado vigente de cada linha (uma linha por linha da rede)
  eventos       cada periodo continuo em que uma linha ficou fora do normal
  inscricoes    quem quer receber e-mail sobre quais linhas
  coletas       o pulso das fontes, para saber se o coletor ficou cego

O arquivo do banco vem de VIGIA_DB (padrao: vigia.db ao lado do codigo).
"""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Iterator

from fontes import LINHAS, ROTULO_STATUS, SEVERIDADE, agora_utc

CAMINHO_DB = os.environ.get("VIGIA_DB") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "vigia.db"
)

_lock = threading.Lock()

ESQUEMA = """
CREATE TABLE IF NOT EXISTS estado_atual (
    linha       INTEGER PRIMARY KEY,
    status      TEXT NOT NULL,
    rotulo      TEXT NOT NULL,
    descricao   TEXT NOT NULL DEFAULT '',
    fonte       TEXT NOT NULL,
    desde       TEXT NOT NULL,
    visto_em    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS eventos (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    linha           INTEGER NOT NULL,
    status          TEXT NOT NULL,
    rotulo          TEXT NOT NULL,
    status_anterior TEXT,
    descricao       TEXT NOT NULL DEFAULT '',
    fonte           TEXT NOT NULL,
    severidade      INTEGER NOT NULL DEFAULT 0,
    inicio          TEXT NOT NULL,
    fim             TEXT,
    duracao_min     REAL,
    -- guardados na ingestão para o baseline estatístico que vem depois:
    -- duração contra o p95 da mesma linha, no mesmo dia da semana e faixa horária
    weekday         INTEGER,
    is_peak         INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_eventos_inicio ON eventos (inicio DESC);
CREATE INDEX IF NOT EXISTS idx_eventos_linha  ON eventos (linha, inicio DESC);
CREATE INDEX IF NOT EXISTS idx_eventos_aberto ON eventos (linha, fim);

CREATE TABLE IF NOT EXISTS inscricoes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    email       TEXT NOT NULL,
    linhas      TEXT NOT NULL,
    severidade_min INTEGER NOT NULL DEFAULT 1,
    token       TEXT NOT NULL UNIQUE,
    ativo       INTEGER NOT NULL DEFAULT 1,
    criado_em   TEXT NOT NULL,
    ultimo_envio TEXT
);
CREATE INDEX IF NOT EXISTS idx_inscricoes_email ON inscricoes (email);

CREATE TABLE IF NOT EXISTS envios (
    inscricao_id INTEGER NOT NULL,
    evento_id    INTEGER NOT NULL,
    enviado_em   TEXT NOT NULL,
    PRIMARY KEY (inscricao_id, evento_id)
);

CREATE TABLE IF NOT EXISTS coletas (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    momento   TEXT NOT NULL,
    fonte     TEXT NOT NULL,
    ok        INTEGER NOT NULL,
    detalhe   TEXT NOT NULL DEFAULT '',
    linhas    INTEGER NOT NULL DEFAULT 0,
    ms        INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_coletas_momento ON coletas (momento DESC);
"""


@contextmanager
def conexao() -> Iterator[sqlite3.Connection]:
    con = sqlite3.connect(CAMINHO_DB, timeout=15)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=8000")
    try:
        yield con
        con.commit()
    finally:
        con.close()


def iniciar() -> None:
    os.makedirs(os.path.dirname(CAMINHO_DB) or ".", exist_ok=True)
    with conexao() as con:
        con.executescript(ESQUEMA)


# --------------------------------------------------------------------------- #
# ciclo de coleta
# --------------------------------------------------------------------------- #

def aplicar_leituras(leituras, momento: str) -> list[dict]:
    """
    Compara as leituras novas com o estado guardado, atualiza o estado e abre ou
    fecha eventos conforme as transicoes. Devolve os eventos ABERTOS neste ciclo —
    e essa lista que vira notificacao.
    """
    novos: list[dict] = []
    if not leituras:
        return novos

    with _lock, conexao() as con:
        atual = {
            r["linha"]: dict(r) for r in con.execute("SELECT * FROM estado_atual")
        }

        for leitura in leituras:
            anterior = atual.get(leitura.linha)
            severidade = SEVERIDADE.get(leitura.status, 1)

            if anterior and anterior["status"] == leitura.status:
                # mesmo estado: so refresca a descricao e o carimbo de visto
                con.execute(
                    "UPDATE estado_atual SET descricao=?, rotulo=?, fonte=?, visto_em=? "
                    "WHERE linha=?",
                    (leitura.descricao, leitura.rotulo, leitura.fonte, momento, leitura.linha),
                )
                if leitura.descricao:
                    con.execute(
                        "UPDATE eventos SET descricao=? WHERE linha=? AND fim IS NULL",
                        (leitura.descricao, leitura.linha),
                    )
                continue

            # houve transicao — fecha o que estava aberto
            if anterior:
                _fechar_evento(con, leitura.linha, momento)

            con.execute(
                "INSERT INTO estado_atual (linha, status, rotulo, descricao, fonte, desde, visto_em) "
                "VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT(linha) DO UPDATE SET "
                "status=excluded.status, rotulo=excluded.rotulo, descricao=excluded.descricao, "
                "fonte=excluded.fonte, desde=excluded.desde, visto_em=excluded.visto_em",
                (
                    leitura.linha,
                    leitura.status,
                    leitura.rotulo,
                    leitura.descricao,
                    leitura.fonte,
                    momento,
                    momento,
                ),
            )

            # so vira ocorrencia o que esta fora do normal
            if severidade > 0:
                dia_semana, pico = _contexto_temporal(momento)
                cur = con.execute(
                    "INSERT INTO eventos "
                    "(linha, status, rotulo, status_anterior, descricao, fonte, severidade, "
                    " inicio, weekday, is_peak) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        leitura.linha,
                        leitura.status,
                        leitura.rotulo,
                        anterior["status"] if anterior else None,
                        leitura.descricao,
                        leitura.fonte,
                        severidade,
                        momento,
                        dia_semana,
                        pico,
                    ),
                )
                novos.append(
                    {
                        "id": cur.lastrowid,
                        "linha": leitura.linha,
                        "status": leitura.status,
                        "rotulo": leitura.rotulo,
                        "descricao": leitura.descricao,
                        "fonte": leitura.fonte,
                        "severidade": severidade,
                        "inicio": momento,
                        "status_anterior": anterior["status"] if anterior else None,
                    }
                )

        # linhas que sumiram do payload viram estado desconhecido, nunca "normal"
        vistas = {l.linha for l in leituras}
        for numero in set(atual) - vistas:
            if atual[numero]["status"] != "desconhecido":
                con.execute(
                    "UPDATE estado_atual SET status='desconhecido', rotulo='Sem informação', "
                    "visto_em=? WHERE linha=?",
                    (momento, numero),
                )

    return novos


def _fechar_evento(con: sqlite3.Connection, linha: int, momento: str) -> None:
    aberto = con.execute(
        "SELECT id, inicio FROM eventos WHERE linha=? AND fim IS NULL "
        "ORDER BY inicio DESC LIMIT 1",
        (linha,),
    ).fetchone()
    if not aberto:
        return
    minutos = _minutos_entre(aberto["inicio"], momento)
    con.execute(
        "UPDATE eventos SET fim=?, duracao_min=? WHERE id=?",
        (momento, minutos, aberto["id"]),
    )


def _contexto_temporal(momento: str) -> tuple[int, int]:
    """(dia_da_semana, e_horario_de_pico) no fuso de São Paulo, para o baseline futuro."""
    try:
        instante = datetime.fromisoformat(momento.replace("Z", "+00:00")).astimezone(
            timezone(timedelta(hours=-3))
        )
    except (ValueError, AttributeError):
        return 0, 0
    dia = instante.weekday()  # 0 = segunda
    pico = dia < 5 and (6 <= instante.hour < 9 or 17 <= instante.hour < 20)
    return dia, int(pico)


def _minutos_entre(inicio: str, fim: str) -> float:
    try:
        a = datetime.fromisoformat(inicio.replace("Z", "+00:00"))
        b = datetime.fromisoformat(fim.replace("Z", "+00:00"))
        return round((b - a).total_seconds() / 60, 1)
    except (ValueError, AttributeError):
        return 0.0


def registrar_coletas(momento: str, coletas) -> None:
    with conexao() as con:
        for c in coletas:
            con.execute(
                "INSERT INTO coletas (momento, fonte, ok, detalhe, linhas, ms) VALUES (?,?,?,?,?,?)",
                (momento, c.fonte, 1 if c.ok else 0, c.detalhe, len(c.leituras), c.ms),
            )
        # mantem o log curto: so os ultimos 3 dias interessam
        corte = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
        con.execute("DELETE FROM coletas WHERE momento < ?", (corte.replace("+00:00", "Z"),))


# --------------------------------------------------------------------------- #
# leitura
# --------------------------------------------------------------------------- #

def estado() -> dict:
    with conexao() as con:
        guardado = {r["linha"]: dict(r) for r in con.execute("SELECT * FROM estado_atual")}
        ultima = con.execute(
            "SELECT momento FROM coletas ORDER BY id DESC LIMIT 1"
        ).fetchone()
        fontes = con.execute(
            "SELECT fonte, ok, detalhe, momento, ms FROM coletas "
            "WHERE id IN (SELECT MAX(id) FROM coletas GROUP BY fonte)"
        ).fetchall()

    linhas = []
    for numero, info in LINHAS.items():
        atual = guardado.get(numero)
        status = atual["status"] if atual else "desconhecido"
        linhas.append(
            {
                "linha": numero,
                "nome": info["nome"],
                "operador": info["operador"],
                "cor": info["cor"],
                "status": status,
                "status_rotulo": ROTULO_STATUS.get(status, "Sem informação"),
                "rotulo_origem": atual["rotulo"] if atual else "",
                "descricao": atual["descricao"] if atual else "",
                "severidade": SEVERIDADE.get(status, 1),
                "fonte": atual["fonte"] if atual else None,
                "desde": atual["desde"] if atual else None,
                "visto_em": atual["visto_em"] if atual else None,
            }
        )

    return {
        "linhas": linhas,
        "ultima_coleta": ultima["momento"] if ultima else None,
        "fontes": [dict(f) for f in fontes],
        "agora": agora_utc(),
    }


def ocorrencias(
    linhas: list[int] | None = None,
    dias: int = 7,
    severidade_min: int = 1,
    apenas_abertas: bool = False,
    limite: int = 200,
    deslocamento: int = 0,
) -> dict:
    condicoes = ["severidade >= ?"]
    valores: list = [severidade_min]

    if dias:
        corte = (datetime.now(timezone.utc) - timedelta(days=dias)).isoformat()
        condicoes.append("inicio >= ?")
        valores.append(corte.replace("+00:00", "Z"))
    if linhas:
        marcadores = ",".join("?" * len(linhas))
        condicoes.append(f"linha IN ({marcadores})")
        valores.extend(linhas)
    if apenas_abertas:
        condicoes.append("fim IS NULL")

    onde = " AND ".join(condicoes)

    with conexao() as con:
        total = con.execute(f"SELECT COUNT(*) c FROM eventos WHERE {onde}", valores).fetchone()["c"]
        registros = con.execute(
            f"SELECT * FROM eventos WHERE {onde} ORDER BY inicio DESC LIMIT ? OFFSET ?",
            [*valores, limite, deslocamento],
        ).fetchall()

    agora = datetime.now(timezone.utc)
    itens = []
    for r in registros:
        d = dict(r)
        info = LINHAS.get(d["linha"], {})
        d["nome"] = info.get("nome", "?")
        d["operador"] = info.get("operador", "")
        d["cor"] = info.get("cor", "#888888")
        d["status_rotulo"] = ROTULO_STATUS.get(d["status"], d["rotulo"])
        d["em_curso"] = d["fim"] is None
        if d["fim"] is None:
            d["duracao_min"] = _minutos_entre(
                d["inicio"], agora.isoformat().replace("+00:00", "Z")
            )
        itens.append(d)

    return {"total": total, "itens": itens}


def linha_do_tempo(dias: int = 7) -> dict:
    """
    Blocos de ocorrência por linha, recortados na janela pedida — a matéria-prima do
    gráfico de faixas. Evento em curso é recortado no instante atual; evento que
    começou antes da janela entra recortado no início dela.
    """
    fim_janela = datetime.now(timezone.utc)
    inicio_janela = fim_janela - timedelta(days=dias)
    corte = inicio_janela.isoformat(timespec="seconds").replace("+00:00", "Z")
    agora = fim_janela.isoformat(timespec="seconds").replace("+00:00", "Z")

    with conexao() as con:
        registros = con.execute(
            "SELECT linha, status, rotulo, descricao, severidade, inicio, fim, duracao_min "
            "FROM eventos WHERE severidade >= 1 AND (fim IS NULL OR fim >= ?) "
            "ORDER BY inicio",
            (corte,),
        ).fetchall()

    por_linha: dict[int, list[dict]] = {n: [] for n in LINHAS}
    for r in registros:
        if r["linha"] not in por_linha:
            continue
        comeco = max(r["inicio"], corte)
        termino = r["fim"] or agora
        if termino <= corte:
            continue
        por_linha[r["linha"]].append(
            {
                "inicio": comeco,
                "fim": termino,
                "status": r["status"],
                "rotulo": ROTULO_STATUS.get(r["status"], r["rotulo"]),
                "descricao": r["descricao"],
                "severidade": r["severidade"],
                "em_curso": r["fim"] is None,
                "duracao_min": r["duracao_min"]
                if r["fim"]
                else _minutos_entre(r["inicio"], agora),
            }
        )

    return {
        "inicio": corte,
        "fim": agora,
        "dias": dias,
        "linhas": [
            {
                "linha": n,
                "nome": LINHAS[n]["nome"],
                "operador": LINHAS[n]["operador"],
                "cor": LINHAS[n]["cor"],
                "blocos": por_linha[n],
                "minutos_afetados": round(sum(b["duracao_min"] or 0 for b in por_linha[n]), 1),
            }
            for n in sorted(LINHAS)
        ],
    }


def serie_diaria(dias: int = 14) -> list[dict]:
    """Contagem de ocorrencias por dia, no fuso de Sao Paulo, para o grafico."""
    corte = (datetime.now(timezone.utc) - timedelta(days=dias)).isoformat().replace("+00:00", "Z")
    with conexao() as con:
        registros = con.execute(
            "SELECT DATE(REPLACE(inicio, 'Z', ''), '-3 hours') AS dia, COUNT(*) AS n "
            "FROM eventos WHERE inicio >= ? AND severidade >= 1 "
            "GROUP BY dia ORDER BY dia",
            (corte,),
        ).fetchall()

    contagem = {r["dia"]: r["n"] for r in registros}
    hoje = datetime.now(timezone(timedelta(hours=-3))).date()

    saida = []
    for n in range(dias - 1, -1, -1):
        dia = (hoje - timedelta(days=n)).isoformat()
        saida.append({"dia": dia, "n": contagem.get(dia, 0)})
    return saida


def saude() -> dict:
    with conexao() as con:
        fontes = con.execute(
            "SELECT fonte, ok, detalhe, momento, ms FROM coletas "
            "WHERE id IN (SELECT MAX(id) FROM coletas GROUP BY fonte)"
        ).fetchall()
        ultima = con.execute("SELECT MAX(momento) m FROM coletas").fetchone()["m"]
        total_eventos = con.execute("SELECT COUNT(*) c FROM eventos").fetchone()["c"]
        abertas = con.execute(
            "SELECT COUNT(*) c FROM eventos WHERE fim IS NULL"
        ).fetchone()["c"]
        inscritos = con.execute(
            "SELECT COUNT(*) c FROM inscricoes WHERE ativo=1"
        ).fetchone()["c"]

    atraso = None
    if ultima:
        atraso = _minutos_entre(ultima, agora_utc())

    return {
        "ultima_coleta": ultima,
        "atraso_min": atraso,
        # o coletor parado e a anomalia que mais importa: sem ele, tudo parece normal
        "coletor_ok": atraso is not None and atraso < 15,
        "fontes": [dict(f) for f in fontes],
        "eventos_registrados": total_eventos,
        "ocorrencias_abertas": abertas,
        "inscricoes_ativas": inscritos,
        "banco": CAMINHO_DB,
    }


# --------------------------------------------------------------------------- #
# inscricoes de e-mail
# --------------------------------------------------------------------------- #

def criar_inscricao(email: str, linhas: list[int], severidade_min: int = 1) -> dict:
    token = secrets.token_urlsafe(24)
    csv = ",".join(str(n) for n in sorted(set(linhas)))
    with conexao() as con:
        # um e-mail tem uma inscricao; reinscrever sobrescreve as preferencias
        existente = con.execute(
            "SELECT id, token FROM inscricoes WHERE email=?", (email,)
        ).fetchone()
        if existente:
            con.execute(
                "UPDATE inscricoes SET linhas=?, severidade_min=?, ativo=1 WHERE id=?",
                (csv, severidade_min, existente["id"]),
            )
            return {"id": existente["id"], "token": existente["token"], "novo": False}

        cur = con.execute(
            "INSERT INTO inscricoes (email, linhas, severidade_min, token, ativo, criado_em) "
            "VALUES (?,?,?,?,1,?)",
            (email, csv, severidade_min, token, agora_utc()),
        )
        return {"id": cur.lastrowid, "token": token, "novo": True}


def cancelar_inscricao(token: str) -> bool:
    with conexao() as con:
        cur = con.execute("UPDATE inscricoes SET ativo=0 WHERE token=?", (token,))
        return cur.rowcount > 0


def inscricao_por_email(email: str) -> dict | None:
    with conexao() as con:
        r = con.execute(
            "SELECT * FROM inscricoes WHERE email=? AND ativo=1", (email,)
        ).fetchone()
    return dict(r) if r else None


def destinatarios_para(eventos: list[dict]) -> list[tuple[dict, list[dict]]]:
    """Casa cada inscricao ativa com os eventos que ela ainda nao recebeu."""
    if not eventos:
        return []

    with conexao() as con:
        inscricoes = [dict(r) for r in con.execute("SELECT * FROM inscricoes WHERE ativo=1")]
        ja_enviados = {
            (r["inscricao_id"], r["evento_id"])
            for r in con.execute("SELECT inscricao_id, evento_id FROM envios")
        }

    saida = []
    for inscricao in inscricoes:
        desejadas = {int(n) for n in inscricao["linhas"].split(",") if n.strip().isdigit()}
        pertinentes = [
            e
            for e in eventos
            if e["linha"] in desejadas
            and e["severidade"] >= inscricao["severidade_min"]
            and (inscricao["id"], e["id"]) not in ja_enviados
        ]
        if pertinentes:
            saida.append((inscricao, pertinentes))
    return saida


def marcar_enviados(inscricao_id: int, eventos: list[dict]) -> None:
    momento = agora_utc()
    with conexao() as con:
        con.executemany(
            "INSERT OR IGNORE INTO envios (inscricao_id, evento_id, enviado_em) VALUES (?,?,?)",
            [(inscricao_id, e["id"], momento) for e in eventos],
        )
        con.execute("UPDATE inscricoes SET ultimo_envio=? WHERE id=?", (momento, inscricao_id))


def semear_demonstracao() -> int:
    """
    Popula eventos ficticios para conhecer a interface antes de haver historico real.
    So roda com o banco vazio e marca tudo com a fonte 'demo'.
    """
    import random

    with conexao() as con:
        if con.execute("SELECT COUNT(*) c FROM eventos").fetchone()["c"]:
            return 0

    random.seed(42)
    estados = [("reduzida", "Velocidade Reduzida", 1), ("parcial", "Operação Parcial", 2),
               ("paralisada", "Paralisada", 3)]
    motivos = [
        "Falha em equipamento de via entre estações.",
        "Ocorrência com passageiro em plataforma.",
        "Interferência na rede aérea.",
        "Falha no sistema de sinalização.",
        "Intensidade de chuva no trecho elevado.",
    ]
    numeros = list(LINHAS)
    agora = datetime.now(timezone.utc)
    criados = 0

    with conexao() as con:
        for _ in range(48):
            linha = random.choice(numeros)
            status, rotulo, severidade = random.choices(estados, weights=[6, 2, 1])[0]
            inicio = agora - timedelta(
                days=random.randint(0, 13), hours=random.randint(0, 23),
                minutes=random.randint(0, 59),
            )
            duracao = round(random.uniform(6, 95), 1)
            fim = inicio + timedelta(minutes=duracao)
            con.execute(
                "INSERT INTO eventos (linha, status, rotulo, status_anterior, descricao, fonte, "
                "severidade, inicio, fim, duracao_min) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    linha, status, rotulo, "normal", random.choice(motivos), "demo",
                    severidade,
                    inicio.isoformat(timespec="seconds").replace("+00:00", "Z"),
                    fim.isoformat(timespec="seconds").replace("+00:00", "Z"),
                    duracao,
                ),
            )
            criados += 1
    return criados
