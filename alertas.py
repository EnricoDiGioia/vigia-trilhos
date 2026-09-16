"""
Envio de e-mail por SMTP.

Configuracao por variaveis de ambiente:

  SMTP_HOST      ex.: smtp.gmail.com
  SMTP_PORT      587 (STARTTLS, padrao) ou 465 (SSL direto)
  SMTP_USUARIO   ex.: seu-email@gmail.com
  SMTP_SENHA     senha de app, nunca a senha da conta
  SMTP_DE        remetente; se vazio, usa SMTP_USUARIO
  SITE_URL       usada no link de cancelamento dentro do e-mail

Sem SMTP_HOST, o modulo fica inerte e a interface esconde a inscricao por e-mail.
"""

from __future__ import annotations

import html
import os
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr

from fontes import LINHAS, NOME_FONTE, ROTULO_STATUS

SMTP_HOST = os.environ.get("SMTP_HOST", "").strip()
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USUARIO = os.environ.get("SMTP_USUARIO", "").strip()
SMTP_SENHA = os.environ.get("SMTP_SENHA", "")
SMTP_DE = os.environ.get("SMTP_DE", "").strip() or SMTP_USUARIO
SITE_URL = os.environ.get("SITE_URL", "").rstrip("/")

CORES_SEVERIDADE = {1: "#8A5800", 2: "#B36A00", 3: "#A82119"}


def configurado() -> bool:
    return bool(SMTP_HOST and SMTP_DE)


def _assunto(eventos: list[dict]) -> str:
    if len(eventos) == 1:
        e = eventos[0]
        info = LINHAS.get(e["linha"], {})
        return f"Linha {e['linha']}-{info.get('nome', '?')}: {ROTULO_STATUS.get(e['status'], e['rotulo'])}"
    numeros = sorted({e["linha"] for e in eventos})
    return f"{len(eventos)} ocorrências nas linhas {', '.join(str(n) for n in numeros)}"


def _texto(eventos: list[dict], url_cancelar: str) -> str:
    partes = ["Vigia dos Trilhos — mudança de status\n"]
    for e in eventos:
        info = LINHAS.get(e["linha"], {})
        partes.append(
            f"Linha {e['linha']}-{info.get('nome', '?')} ({info.get('operador', '')})\n"
            f"  {ROTULO_STATUS.get(e['status'], e['rotulo'])}"
            f"{' — ' + e['descricao'] if e.get('descricao') else ''}\n"
            f"  fonte: {NOME_FONTE.get(e['fonte'], e['fonte'])}\n"
        )
    if url_cancelar:
        partes.append(f"\nPara parar de receber: {url_cancelar}")
    return "\n".join(partes)


def _html(eventos: list[dict], url_cancelar: str) -> str:
    blocos = []
    for e in eventos:
        info = LINHAS.get(e["linha"], {})
        cor = info.get("cor", "#888")
        sev = CORES_SEVERIDADE.get(e.get("severidade", 1), "#8A5800")
        descricao = (
            f'<p style="margin:6px 0 0;color:#444f54;font-size:14px;line-height:1.5">'
            f"{html.escape(e['descricao'])}</p>"
            if e.get("descricao")
            else ""
        )
        blocos.append(
            f"""
    <tr><td style="padding:14px 0;border-bottom:1px solid #d2dad9">
      <table role="presentation" cellpadding="0" cellspacing="0" style="width:100%">
        <tr>
          <td style="width:6px;background:{cor};border-radius:3px"></td>
          <td style="padding-left:14px">
            <div style="font:600 16px/1.3 Arial,Helvetica,sans-serif;color:#10161a">
              Linha {e['linha']} &middot; {html.escape(info.get('nome', '?'))}
            </div>
            <div style="font:400 13px/1.4 Arial,Helvetica,sans-serif;color:#6e7a80;margin-top:2px">
              {html.escape(info.get('operador', ''))}
            </div>
            <div style="margin-top:8px">
              <span style="display:inline-block;padding:3px 10px;border-radius:2px;
                     background:{sev};color:#fff;font:600 12px/1.4 Arial,Helvetica,sans-serif;
                     letter-spacing:.04em;text-transform:uppercase">
                {html.escape(ROTULO_STATUS.get(e['status'], e['rotulo']))}
              </span>
            </div>
            {descricao}
          </td>
        </tr>
      </table>
    </td></tr>"""
        )

    rodape = (
        f'<p style="font:400 12px/1.5 Arial,Helvetica,sans-serif;color:#6e7a80;margin:22px 0 0">'
        f'Você recebe isto porque pediu alertas destas linhas. '
        f'<a href="{html.escape(url_cancelar)}" style="color:#0d6e63">Parar de receber</a>.</p>'
        if url_cancelar
        else ""
    )

    return f"""<!doctype html>
<html><body style="margin:0;padding:24px;background:#f0f2f1">
  <table role="presentation" cellpadding="0" cellspacing="0"
         style="max-width:560px;margin:0 auto;background:#fff;border:1px solid #d2dad9;
                border-radius:4px;padding:24px">
    <tr><td>
      <div style="font:700 13px/1 Arial,Helvetica,sans-serif;color:#0d6e63;
                  letter-spacing:.14em;text-transform:uppercase">Vigia dos Trilhos</div>
      <h1 style="font:700 22px/1.2 Arial,Helvetica,sans-serif;color:#10161a;margin:10px 0 4px">
        Mudança de status
      </h1>
    </td></tr>
    {"".join(blocos)}
    <tr><td>{rodape}</td></tr>
  </table>
</body></html>"""


def enviar_alerta(email: str, eventos: list[dict], token: str = "") -> tuple[bool, str]:
    """Manda um unico e-mail com todos os eventos do ciclo. Devolve (ok, detalhe)."""
    if not configurado():
        return False, "SMTP não configurado"
    if not eventos:
        return True, "nada a enviar"

    url_cancelar = f"{SITE_URL}/cancelar/{token}" if (SITE_URL and token) else ""

    msg = EmailMessage()
    msg["Subject"] = _assunto(eventos)
    msg["From"] = formataddr(("Vigia dos Trilhos", SMTP_DE))
    msg["To"] = email
    msg.set_content(_texto(eventos, url_cancelar))
    msg.add_alternative(_html(eventos, url_cancelar), subtype="html")

    contexto = ssl.create_default_context()
    try:
        if SMTP_PORT == 465:
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=20, context=contexto) as s:
                if SMTP_USUARIO:
                    s.login(SMTP_USUARIO, SMTP_SENHA)
                s.send_message(msg)
        else:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as s:
                s.starttls(context=contexto)
                if SMTP_USUARIO:
                    s.login(SMTP_USUARIO, SMTP_SENHA)
                s.send_message(msg)
        return True, f"enviado para {email}"
    except Exception as e:  # noqa: BLE001 - qualquer falha vira detalhe no log
        return False, f"{type(e).__name__}: {e}"
