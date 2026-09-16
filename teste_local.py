"""
Teste de fumaca: exercita o app inteiro com uma fonte simulada, sem tocar a rede.

    python3 teste_local.py

Verifica: criacao do banco, deteccao de transicao (normal -> reduzida -> paralisada
-> normal), fechamento de evento com duracao, todos os endpoints da API e o fluxo
de inscricao por e-mail.
"""

from __future__ import annotations

import os
import sys
import tempfile

os.environ["VIGIA_DB"] = os.path.join(tempfile.mkdtemp(), "teste.db")
os.environ["POLL_SEGUNDOS"] = "0"      # sem thread de fundo durante o teste
os.environ["INTERVALO_MINIMO"] = "0"   # sem trava de tempo entre ciclos
os.environ.pop("ARTESP_API_KEY", None)
os.environ.pop("SMTP_HOST", None)

import app as servidor  # noqa: E402
import dados  # noqa: E402
import fontes  # noqa: E402

FALHAS: list[str] = []


def checar(condicao: bool, descricao: str) -> None:
    marca = "ok   " if condicao else "FALHA"
    print(f"  {marca} {descricao}")
    if not condicao:
        FALHAS.append(descricao)


def fonte_simulada(estados: dict[int, tuple[str, str]]):
    """Substitui a coleta real por um conjunto fixo de leituras."""
    def _coletar():
        leituras = [
            fontes.Leitura(
                linha=numero,
                status=fontes.normalizar_status(rotulo),
                rotulo=rotulo,
                descricao=descricao,
                fonte="proximo_trem",
            )
            for numero, (rotulo, descricao) in estados.items()
        ]
        return fontes.Coleta("proximo_trem", True, leituras, f"{len(leituras)} linhas", 5)
    return _coletar


def rede_normal() -> dict[int, tuple[str, str]]:
    return {n: ("Operação Normal", "") for n in fontes.LINHAS}


def main() -> int:
    print("Vigia dos Trilhos — teste de fumaça\n")
    dados.iniciar()

    # ---------------------------------------------------------------- #
    print("1. Normalização de rótulos")
    casos = [
        ("Operação Normal", "normal"),
        ("Velocidade Reduzida", "reduzida"),
        ("OPERAÇÃO PARCIAL", "parcial"),
        ("Circulação Paralisada", "paralisada"),
        ("Operação Encerrada", "encerrada"),
        ("Circulação com maior tempo de parada", "reduzida"),
        ("", "desconhecido"),
    ]
    for entrada, esperado in casos:
        obtido = fontes.normalizar_status(entrada)
        checar(obtido == esperado, f"{entrada or '(vazio)'!r} → {obtido}")

    # ---------------------------------------------------------------- #
    print("\n2. Ciclo 1 — rede toda normal")
    servidor.fontes.coletar_proximo_trem = fonte_simulada(rede_normal())
    r1 = servidor.executar_ciclo(forcar=True)
    checar(r1["linhas_lidas"] == len(fontes.LINHAS), f"leu {r1['linhas_lidas']} linhas")
    checar(r1["eventos_novos"] == 0, "nenhum evento aberto com tudo normal")

    # ---------------------------------------------------------------- #
    print("\n3. Ciclo 2 — a linha 3 degrada")
    estados = rede_normal()
    estados[3] = ("Velocidade Reduzida", "Falha de sinalização entre Sé e Brás.")
    servidor.fontes.coletar_proximo_trem = fonte_simulada(estados)
    r2 = servidor.executar_ciclo(forcar=True)
    checar(r2["eventos_novos"] == 1, f"abriu {r2['eventos_novos']} evento")

    atual = dados.estado()
    linha3 = next(l for l in atual["linhas"] if l["linha"] == 3)
    checar(linha3["status"] == "reduzida", f"estado da linha 3 = {linha3['status']}")
    checar(linha3["severidade"] == 1, "severidade 1")
    checar("sinalização" in linha3["descricao"], "descrição preservada")

    # ---------------------------------------------------------------- #
    print("\n4. Ciclo 3 — a linha 3 piora")
    estados[3] = ("Circulação Paralisada", "Ocorrência na via.")
    servidor.fontes.coletar_proximo_trem = fonte_simulada(estados)
    r3 = servidor.executar_ciclo(forcar=True)
    checar(r3["eventos_novos"] == 1, "abriu novo evento na transição reduzida → paralisada")

    oc = dados.ocorrencias(dias=1)
    checar(oc["total"] == 2, f"{oc['total']} ocorrências registradas")
    fechadas = [i for i in oc["itens"] if not i["em_curso"]]
    checar(len(fechadas) == 1, "o evento anterior foi fechado")
    checar(
        fechadas and fechadas[0]["duracao_min"] is not None,
        "evento fechado tem duração calculada",
    )

    # ---------------------------------------------------------------- #
    print("\n5. Ciclo 4 — a linha 3 normaliza")
    estados[3] = ("Operação Normal", "")
    servidor.fontes.coletar_proximo_trem = fonte_simulada(estados)
    servidor.executar_ciclo(forcar=True)
    abertas = dados.ocorrencias(dias=1, apenas_abertas=True)
    checar(abertas["total"] == 0, "nenhuma ocorrência em curso após normalizar")

    # ---------------------------------------------------------------- #
    print("\n6. Linha que some do payload")
    estados_parciais = {n: v for n, v in rede_normal().items() if n != 9}
    servidor.fontes.coletar_proximo_trem = fonte_simulada(estados_parciais)
    servidor.executar_ciclo(forcar=True)
    linha9 = next(l for l in dados.estado()["linhas"] if l["linha"] == 9)
    checar(linha9["status"] == "desconhecido", f"linha ausente vira {linha9['status']}, não 'normal'")

    # ---------------------------------------------------------------- #
    print("\n7. Raspagem da ViaMobilidade (parser contra HTML de exemplo)")
    html = """
    <ul class="lines">
      <li class="line line-3"><span title="Linha 3-Vermelha">3</span>
        <div class="status yellow">Velocidade Reduzida</div>
        <p>Falha em equipamento de via.</p></li>
      <li class="line line-10"><span title="Linha 10-Turquesa">10</span>
        <div class="status green">Operação Normal</div></li>
    </ul>
    <div class="lines"><p><strong>15/09/2026 08:31:00</strong></p></div>
    """
    import re
    achados = []
    for bloco in re.finditer(
        r'<li[^>]*class="[^"]*\bline-(\d+)\b[^"]*"[^>]*>(.*?)</li>', html, re.S | re.I
    ):
        numero = int(bloco.group(1))
        m = re.search(r'<[^>]*class="[^"]*\bstatus\b[^"]*"[^>]*>(.*?)</', bloco.group(2), re.S | re.I)
        achados.append((numero, fontes.normalizar_status(m.group(1) if m else "")))
    checar(achados == [(3, "reduzida"), (10, "normal")], f"parser extraiu {achados}")

    # ---------------------------------------------------------------- #
    print("\n8. Endpoints da API")
    servidor.app.config["TESTING"] = True
    cliente = servidor.app.test_client()

    for rota in ("/", "/api/linhas", "/api/estado", "/api/ocorrencias",
                 "/api/serie", "/api/saude", "/api/cron/poll"):
        resposta = cliente.get(rota)
        checar(resposta.status_code == 200, f"GET {rota} → {resposta.status_code}")

    estado_json = cliente.get("/api/estado").get_json()
    checar(len(estado_json["linhas"]) == 15, "estado devolve as 15 linhas")
    checar(
        all(l.get("cor", "").startswith("#") for l in estado_json["linhas"]),
        "toda linha vem com cor",
    )

    serie = cliente.get("/api/serie?dias=14").get_json()["serie"]
    checar(len(serie) == 14, f"série tem {len(serie)} dias")
    checar(sum(d["n"] for d in serie) >= 2, "série contabiliza os eventos do teste")

    filtrada = cliente.get("/api/ocorrencias?linhas=3&dias=1").get_json()
    checar(all(i["linha"] == 3 for i in filtrada["itens"]), "filtro por linha funciona")

    grave = cliente.get("/api/ocorrencias?severidade=3&dias=1").get_json()
    checar(all(i["severidade"] >= 3 for i in grave["itens"]), "filtro por severidade funciona")

    # ---------------------------------------------------------------- #
    print("\n9. Inscrição por e-mail (SMTP desligado)")
    resposta = cliente.post("/api/inscricoes", json={"email": "a@b.com", "linhas": [3, 9]})
    checar(resposta.status_code == 503, "recusa a inscrição quando o SMTP não está configurado")

    resposta = cliente.post("/api/inscricoes", json={"email": "invalido", "linhas": [3]})
    checar(resposta.status_code == 400, "recusa e-mail malformado")

    registro = dados.criar_inscricao("teste@exemplo.com", [3, 9], 1)
    checar(registro["novo"], "inscrição criada direto na camada de dados")

    eventos = [{"id": 999, "linha": 3, "severidade": 2, "status": "parcial",
                "rotulo": "Operação Parcial", "descricao": "", "fonte": "proximo_trem"}]
    alvos = dados.destinatarios_para(eventos)
    checar(len(alvos) == 1, "a inscrição casa com o evento da linha 3")

    dados.marcar_enviados(registro["id"], eventos)
    checar(not dados.destinatarios_para(eventos), "não reenvia o mesmo evento")

    fora = [{"id": 1000, "linha": 1, "severidade": 2, "status": "parcial",
             "rotulo": "x", "descricao": "", "fonte": "proximo_trem"}]
    checar(not dados.destinatarios_para(fora), "não notifica linha fora da inscrição")

    baixa = [{"id": 1001, "linha": 3, "severidade": 0, "status": "normal",
              "rotulo": "x", "descricao": "", "fonte": "proximo_trem"}]
    checar(not dados.destinatarios_para(baixa), "não notifica abaixo da severidade mínima")

    checar(dados.cancelar_inscricao(registro["token"]), "cancelamento por token funciona")
    checar(cliente.get(f"/cancelar/{registro['token']}").status_code == 200, "página de cancelamento responde")

    # ---------------------------------------------------------------- #
    print("\n10. Trava entre ciclos")
    os.environ["INTERVALO_MINIMO"] = "60"
    servidor.INTERVALO_MINIMO = 60
    servidor.executar_ciclo(forcar=True)
    pulado = servidor.executar_ciclo(forcar=False)
    checar(pulado.get("pulado") is True, "segundo ciclo imediato é ignorado")

    # ---------------------------------------------------------------- #
    print(f"\n{'-' * 52}")
    if FALHAS:
        print(f"{len(FALHAS)} falha(s):")
        for f in FALHAS:
            print(f"  - {f}")
        return 1
    print("Tudo passou.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
