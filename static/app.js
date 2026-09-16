/* Vigia dos Trilhos — interface.
   Sem framework: o app é pequeno o bastante para não precisar de um.

   Princípio do layout: o que está fora do normal sobe e cresce; o que está
   normal encolhe. A faixa "A rede agora" é o único lugar que mostra as 15
   linhas de uma vez, e serve de navegação para a gaveta de detalhe. */

(() => {
  "use strict";

  const CHAVE_SEGUIDAS = "vigia.linhasSeguidas";
  const CHAVE_EMAIL = "vigia.email";
  const INTERVALO_ESTADO = 30000;

  const corpo = document.body;
  const EMAIL_ATIVO = corpo.dataset.emailAtivo === "1";
  const ARTESP_ATIVO = corpo.dataset.artespAtivo === "1";

  const app = {
    linhas: [],
    porNumero: {},
    nomesFonte: {},
    anterior: null,
    seguidas: carregarSeguidas(),
    aba: "painel",
    gaveta: null,
    ultimaColeta: null,
    oc: { dias: 7, sev: 1, soSeguidas: false, offset: 0, total: 0, itens: [], fonte: "local" },
  };

  const $ = (s) => document.querySelector(s);
  const $$ = (s) => Array.from(document.querySelectorAll(s));

  // ═══════════════════ armazenamento local ═══════════════════ //

  function carregarSeguidas() {
    try {
      const lista = JSON.parse(localStorage.getItem(CHAVE_SEGUIDAS) || "[]");
      return new Set(Array.isArray(lista) ? lista.map(Number) : []);
    } catch { return new Set(); }
  }
  function salvarSeguidas() {
    try { localStorage.setItem(CHAVE_SEGUIDAS, JSON.stringify([...app.seguidas])); } catch {}
  }
  function lembrar(c, v) { try { localStorage.setItem(c, v); } catch {} }
  function lembrado(c) { try { return localStorage.getItem(c) || ""; } catch { return ""; } }

  // ═══════════════════ utilidades ═══════════════════ //

  async function buscar(url) {
    const r = await fetch(url, { headers: { Accept: "application/json" } });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.erro || `HTTP ${r.status}`);
    return d;
  }

  function escapar(t) {
    const d = document.createElement("div");
    d.textContent = t == null ? "" : String(t);
    return d.innerHTML;
  }

  function haQuantoTempo(iso) {
    if (!iso) return "—";
    const min = (Date.now() - new Date(iso).getTime()) / 60000;
    if (min < 1) return "agora";
    if (min < 60) return `há ${Math.floor(min)} min`;
    if (min < 1440) return `há ${Math.floor(min / 60)} h`;
    return `há ${Math.floor(min / 1440)} d`;
  }
  function relogio(iso) {
    if (!iso) return "—";
    return new Date(iso).toLocaleString("pt-BR",
      { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
  }
  function duracao(min) {
    if (min == null) return "—";
    if (min < 60) return `${Math.round(min)} min`;
    const h = Math.floor(min / 60), m = Math.round(min % 60);
    return m ? `${h} h ${m} min` : `${h} h`;
  }
  function plural(n, um, muitos) { return n === 1 ? um : muitos; }

  function brinde(txt) {
    const el = $("#brinde");
    el.textContent = txt;
    el.hidden = false;
    clearTimeout(brinde._t);
    brinde._t = setTimeout(() => { el.hidden = true; }, 3800);
  }

  function classe(l) {
    return l.status === "desconhecido" ? "sx" : `s${Math.min(l.severidade || 0, 3)}`;
  }
  function temOcorrencia(l) { return l.severidade > 0 && l.status !== "desconhecido"; }

  const SINO = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"
    stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
    <path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.7 21a2 2 0 0 1-3.4 0"/></svg>`;

  // ═══════════════════ manchete ═══════════════════ //

  function desenharManchete() {
    const problemas = app.linhas.filter(temOcorrencia);
    const semInfo = app.linhas.filter((l) => l.status === "desconhecido");
    const sobre = $("#sobretitulo");
    const manchete = $("#manchete");
    const sub = $("#submanchete");

    sobre.classList.toggle("viva", problemas.length > 0);

    if (problemas.length) {
      sobre.textContent = "agora";
      manchete.textContent =
        `${problemas.length} ${plural(problemas.length, "linha", "linhas")} ` +
        `${plural(problemas.length, "com ocorrência", "com ocorrência")}`;
      const trechos = problemas
        .slice(0, 4)
        .map((l) => `<b>${l.linha}-${escapar(l.nome)}</b> ${escapar(l.status_rotulo.toLowerCase())}`);
      const resto = problemas.length > 4 ? ` e mais ${problemas.length - 4}` : "";
      sub.innerHTML = `${trechos.join(" · ")}${resto}. Atualizado ${haQuantoTempo(app.ultimaColeta)}.`;
    } else if (semInfo.length === app.linhas.length) {
      sobre.textContent = "coletor";
      manchete.textContent = "Ainda sem leitura das linhas";
      sub.textContent = "O coletor subiu há pouco ou as fontes não responderam. O diagnóstico está no rodapé.";
    } else {
      sobre.textContent = "agora";
      manchete.textContent = "Rede operando normalmente";
      const aviso = semInfo.length
        ? ` Sem informação de ${semInfo.length} ${plural(semInfo.length, "linha", "linhas")}.`
        : "";
      sub.innerHTML = `Nenhuma ocorrência nas linhas monitoradas.${aviso} Atualizado ${haQuantoTempo(app.ultimaColeta)}.`;
    }
  }

  // ═══════════════════ a rede agora ═══════════════════ //

  function desenharRede() {
    $("#rede").innerHTML = app.linhas.map((l) => `
      <button class="peca ${classe(l)}" style="--cor:${l.cor}" data-linha="${l.linha}" role="listitem"
        aria-label="Linha ${l.linha} ${escapar(l.nome)}, ${escapar(l.status_rotulo)}">
        ${app.seguidas.has(l.linha) ? '<span class="peca-marca" title="você segue"></span>' : ""}
        <span class="peca-num">${l.linha}</span>
        <span class="peca-nome">${escapar(l.nome)}</span>
        <span class="peca-ponto"></span>
      </button>`).join("");

    $$("#rede .peca").forEach((p) =>
      p.addEventListener("click", () => abrirGaveta(Number(p.dataset.linha))));
  }

  // ═══════════════════ cartões ═══════════════════ //

  function cartao(l) {
    const segue = app.seguidas.has(l.linha);
    return `
      <article class="cartao" style="--cor:${l.cor}">
        <div class="cartao-trilho"></div>
        <div class="cartao-corpo">
          <div class="cartao-topo">
            <button class="cartao-id" data-detalhe="${l.linha}" style="text-align:left">
              <span class="cartao-nome">Linha ${l.linha} · ${escapar(l.nome)}</span>
              <span class="cartao-op">${escapar(l.operador)}</span>
            </button>
            <button class="sino" data-seguir="${l.linha}" aria-pressed="${segue}"
              aria-label="${segue ? "Parar de seguir" : "Seguir"} a linha ${l.linha}"
              title="${segue ? "Parar de seguir" : "Seguir esta linha"}">${SINO}</button>
          </div>
          <span class="selo ${classe(l)}">${escapar(l.status_rotulo)}</span>
          ${l.descricao ? `<p class="cartao-desc">${escapar(l.descricao)}</p>` : ""}
          <div class="cartao-pe">
            <span class="cartao-tempo">${l.desde ? `neste estado ${haQuantoTempo(l.desde)}` : "sem histórico"}</span>
            <button class="elo" data-detalhe="${l.linha}">detalhe</button>
          </div>
        </div>
      </article>`;
  }

  function desenharCartoes() {
    const seguidas = app.linhas.filter((l) => app.seguidas.has(l.linha));
    const problemas = app.linhas.filter((l) => temOcorrencia(l) && !app.seguidas.has(l.linha));

    // Minhas linhas fica sempre visível: quando vazia, é o convite de primeira visita
    $("#cartoes-minhas").innerHTML = seguidas.length
      ? seguidas.map(cartao).join("")
      : `<div class="vazio" style="grid-column:1/-1">
           <strong>Escolha suas linhas</strong>
           Toque numa linha na faixa acima e ative o sino. Elas passam a aparecer aqui,
           e o aviso chega quando o estado muda.
         </div>`;

    const blocoAtencao = $("#bloco-atencao");
    if (problemas.length) {
      blocoAtencao.hidden = false;
      $("#titulo-atencao").textContent = seguidas.length
        ? `Outras linhas com ocorrência (${problemas.length})`
        : `Precisa de atenção (${problemas.length})`;
      $("#cartoes-atencao").innerHTML = problemas.map(cartao).join("");
    } else {
      blocoAtencao.hidden = true;
      $("#cartoes-atencao").innerHTML = "";
    }

    const semNada = !app.linhas.some(temOcorrencia) && app.linhas.length > 0;
    $("#bloco-calmaria").hidden = !semNada;
    if (semNada) {
      const semInfo = app.linhas.filter((l) => l.status === "desconhecido").length;
      $("#calmaria-txt").textContent = semInfo
        ? `${app.linhas.length - semInfo} linhas em operação normal, ${semInfo} sem informação.`
        : "As 15 linhas estão em operação normal.";
    }

    $("#compacta").innerHTML = app.linhas.map((l) => `
      <button class="compacta-item" data-detalhe="${l.linha}" style="--cor:${l.cor}">
        <span class="compacta-barra"></span>
        <span class="compacta-num">${l.linha}</span>
        <span class="compacta-nome">${escapar(l.nome)}<small>${escapar(l.operador)}</small></span>
        <span class="selo ${classe(l)}">${escapar(l.status_rotulo)}</span>
      </button>`).join("");

    ligarBotoes();
    atualizarNotaLinhas();
  }

  function ligarBotoes() {
    $$("[data-seguir]").forEach((b) =>
      b.addEventListener("click", (ev) => {
        ev.stopPropagation();
        alternarSeguir(Number(b.dataset.seguir));
      }));
    $$("[data-detalhe]").forEach((b) =>
      b.addEventListener("click", () => abrirGaveta(Number(b.dataset.detalhe))));
  }

  function alternarSeguir(numero) {
    const novo = !app.seguidas.has(numero);
    if (novo) app.seguidas.add(numero); else app.seguidas.delete(numero);
    salvarSeguidas();
    desenharRede();
    desenharCartoes();
    if (app.gaveta === numero) abrirGaveta(numero, true);
    if (app.aba === "ocorrencias" && app.oc.soSeguidas) carregarOcorrencias(true);

    if (novo && "Notification" in window && Notification.permission === "default") {
      brinde("Linha adicionada. Ative as notificações para ser avisado.");
    }
  }

  function atualizarNotaLinhas() {
    const nota = $("#nota-linhas");
    if (!nota) return;
    const n = app.seguidas.size;
    nota.textContent = n === 0
      ? "nenhuma linha escolhida"
      : `${n} ${plural(n, "linha", "linhas")}: ${[...app.seguidas].sort((a, b) => a - b).join(", ")}`;
  }

  // ═══════════════════ gaveta de detalhe ═══════════════════ //

  async function abrirGaveta(numero, manter = false) {
    const l = app.porNumero[numero];
    if (!l) return;
    app.gaveta = numero;
    const segue = app.seguidas.has(numero);
    const caixa = $("#gaveta");
    const painel = $("#gaveta-painel");

    painel.innerHTML = `
      <div class="g-topo" style="--cor:${l.cor}">
        <button class="g-fechar" id="g-fechar" aria-label="Fechar">&times;</button>
        <div class="g-num">Linha ${l.linha} · ${escapar(l.nome)}</div>
        <div class="g-op">${escapar(l.operador)}</div>
        <span class="selo ${classe(l)}">${escapar(l.status_rotulo)}</span>
        ${l.descricao ? `<p class="g-desc">${escapar(l.descricao)}</p>` : ""}
      </div>
      <div class="g-corpo">
        <div class="g-acao">
          <button class="botao ${segue ? "" : "primario"}" id="g-seguir">
            ${segue ? "Parar de seguir" : "Seguir esta linha"}
          </button>
          ${segue && "Notification" in window && Notification.permission !== "granted"
            ? '<button class="botao" id="g-notificar">Ativar notificações</button>' : ""}
        </div>
        <dl class="g-dados">
          <div class="g-dado"><dt>neste estado</dt><dd>${l.desde ? haQuantoTempo(l.desde).replace("há ", "") : "—"}</dd></div>
          <div class="g-dado"><dt>fonte</dt><dd>${escapar(app.nomesFonte[l.fonte] || l.fonte || "—")}</dd></div>
        </dl>
        <div class="g-sec">
          <h3>Últimos 7 dias</h3>
          <div id="g-historico"><p class="g-vazio">carregando…</p></div>
        </div>
      </div>`;

    caixa.hidden = false;
    if (!manter) document.addEventListener("keydown", escFecha);
    $("#g-fechar").addEventListener("click", fecharGaveta);
    $("#g-seguir").addEventListener("click", () => alternarSeguir(numero));
    const btnNot = $("#g-notificar");
    if (btnNot) btnNot.addEventListener("click", pedirPermissao);

    try {
      const h = await buscar(`/api/ocorrencias?linhas=${numero}&dias=7&limite=8`);
      $("#g-historico").innerHTML = h.itens.length
        ? `<div class="g-mini">${h.itens.map((o) => `
             <div class="g-mini-item">
               <span class="g-mini-quando">${relogio(o.inicio)}</span>
               <span class="g-mini-txt">${escapar(o.status_rotulo)}${o.em_curso ? " · em curso" : ` · ${duracao(o.duracao_min)}`}</span>
             </div>`).join("")}</div>
           ${h.total > h.itens.length ? `<p class="g-vazio" style="margin-top:8px">e mais ${h.total - h.itens.length} no período</p>` : ""}`
        : '<p class="g-vazio">Nenhuma ocorrência registrada nos últimos 7 dias.</p>';
    } catch {
      $("#g-historico").innerHTML = '<p class="g-vazio">Não consegui carregar o histórico.</p>';
    }
  }

  function fecharGaveta() {
    $("#gaveta").hidden = true;
    app.gaveta = null;
    document.removeEventListener("keydown", escFecha);
  }
  function escFecha(ev) { if (ev.key === "Escape") fecharGaveta(); }

  // ═══════════════════ notificações ═══════════════════ //

  function podeNotificar() {
    return "Notification" in window && Notification.permission === "granted";
  }

  function detectarMudancas(novas) {
    const mapa = {};
    novas.forEach((l) => { mapa[l.linha] = l; });

    // a primeira leitura só estabelece a linha de base — avisar aqui seria ruído
    if (app.anterior === null) { app.anterior = mapa; return; }
    if (!podeNotificar()) { app.anterior = mapa; return; }

    const mudou = [];
    for (const n of app.seguidas) {
      const antes = app.anterior[n], agora = mapa[n];
      if (!antes || !agora || antes.status === agora.status) continue;
      if (antes.severidade === 0 && agora.severidade === 0) continue; // normal ↔ encerrada
      mudou.push(agora);
    }

    mudou.forEach((l) => {
      try {
        const n = new Notification(`Linha ${l.linha} · ${l.nome}`, {
          body: l.descricao || l.status_rotulo,
          tag: `vigia-linha-${l.linha}`,
          renotify: true,
        });
        n.onclick = () => { window.focus(); n.close(); };
      } catch {}
    });
    if (mudou.length) brinde(`${mudou.length} ${plural(mudou.length, "linha mudou", "linhas mudaram")} de estado`);
    app.anterior = mapa;
  }

  async function pedirPermissao() {
    if (!("Notification" in window)) { brinde("Este navegador não suporta notificações."); return; }
    if (Notification.permission === "denied") {
      brinde("As notificações estão bloqueadas nas permissões do site."); return;
    }
    if (Notification.permission === "granted") { ajustarBotaoNotificacao(); return; }
    const r = await Notification.requestPermission();
    ajustarBotaoNotificacao();
    if (app.gaveta) abrirGaveta(app.gaveta, true);
    if (r === "granted") brinde("Pronto. Avisos ligados enquanto esta aba estiver aberta.");
  }

  function ajustarBotaoNotificacao() {
    const b = $("#btn-notificacao");
    if (!("Notification" in window)) { b.hidden = true; return; }
    const p = Notification.permission;
    b.textContent = p === "granted" ? "Notificações ativas"
      : p === "denied" ? "Notificações bloqueadas" : "Ativar notificações";
    b.disabled = p !== "default";
  }

  // ═══════════════════ ocorrências: lista ═══════════════════ //

  async function carregarOcorrencias(reiniciar = false) {
    if (reiniciar) { app.oc.offset = 0; app.oc.itens = []; app.oc.fonte = "local"; }

    const p = new URLSearchParams({
      dias: String(app.oc.dias), severidade: String(app.oc.sev),
      limite: "50", offset: String(app.oc.offset),
    });
    if (app.oc.soSeguidas && app.seguidas.size) p.set("linhas", [...app.seguidas].join(","));

    try {
      const d = await buscar(`/api/ocorrencias?${p}`);
      app.oc.total = d.total;
      app.oc.itens = app.oc.offset ? app.oc.itens.concat(d.itens) : d.itens;
      desenharLista();
    } catch (e) {
      $("#lista-ocorrencias").innerHTML =
        `<div class="vazio"><strong>Não consegui carregar</strong>${escapar(e.message)}</div>`;
    }
  }

  async function carregarArtesp() {
    const b = $("#btn-artesp");
    b.disabled = true; b.textContent = "Consultando…";
    try {
      const d = await buscar(`/api/ocorrencias/artesp?dias=${app.oc.dias}`);
      app.oc.itens = d.itens; app.oc.total = d.total; app.oc.fonte = "artesp";
      desenharLista();
      brinde(`${d.total} ocorrências oficiais (linhas 4 a 9).`);
    } catch (e) {
      brinde(`ARTESP: ${e.message}`);
    } finally {
      b.disabled = false; b.textContent = "Histórico oficial ARTESP";
    }
  }

  function desenharLista() {
    const lista = $("#lista-ocorrencias");
    const itens = app.oc.itens;
    $("#contagem").textContent = app.oc.total
      ? `${app.oc.total} ${plural(app.oc.total, "ocorrência", "ocorrências")}`
      : "nenhuma ocorrência";

    if (!itens.length) {
      lista.innerHTML = `<div class="vazio">
        <strong>Nada registrado no período</strong>
        ${app.oc.fonte === "artesp"
          ? "A ARTESP não retornou ocorrências para esse intervalo."
          : "O histórico começa a ser construído quando o coletor sobe. Se ele acabou de subir, volte mais tarde."}
      </div>`;
      $("#btn-mais").hidden = true;
      return;
    }

    lista.innerHTML = itens.map((o) => `
      <article class="oc" style="--cor:${o.cor}">
        <div class="oc-trilho"></div>
        <div class="oc-corpo">
          <div class="oc-cabeca">
            <span class="oc-linha">Linha ${o.linha} · ${escapar(o.nome)}</span>
            <span class="selo s${Math.min(o.severidade, 3)}">${escapar(o.status_rotulo)}</span>
            ${o.em_curso ? '<span class="oc-curso">em curso</span>' : ""}
          </div>
          ${o.descricao ? `<p class="oc-desc">${escapar(o.descricao)}</p>` : ""}
          <div class="oc-meta">
            <span>${relogio(o.inicio)}</span><span>${duracao(o.duracao_min)}</span>
            <span>${escapar(app.nomesFonte[o.fonte] || o.fonte)}</span>
          </div>
        </div>
      </article>`).join("");

    $("#btn-mais").hidden = app.oc.fonte === "artesp" || itens.length >= app.oc.total;
  }

  // ═══════════════════ gráfico de faixas (uma linha por linha) ═══════════════════ //

  function posicionarBalao(balao, svg, area, xUnid, yUnid, html) {
    const cSvg = svg.getBoundingClientRect();
    const cArea = area.getBoundingClientRect();
    const escala = cSvg.width / svg.viewBox.baseVal.width;
    balao.innerHTML = html;
    balao.style.left = `${xUnid * escala}px`;
    balao.style.top = `${yUnid * escala + (cSvg.top - cArea.top)}px`;
    balao.classList.add("visivel");
  }

  async function desenharFaixas() {
    const area = $("#faixas-area");
    let dados;
    try { dados = await buscar(`/api/linha-do-tempo?dias=${app.oc.dias}`); }
    catch { area.innerHTML = ""; return; }

    const W = 1000, ROT = 132, DIR = 14, TOPO = 22, ALT_LIN = 23;
    const linhas = app.oc.soSeguidas && app.seguidas.size
      ? dados.linhas.filter((l) => app.seguidas.has(l.linha))
      : dados.linhas;
    const A = TOPO + linhas.length * ALT_LIN + 10;
    const largura = W - ROT - DIR;
    const t0 = new Date(dados.inicio).getTime();
    const t1 = new Date(dados.fim).getTime();
    const x = (iso) => ROT + ((new Date(iso).getTime() - t0) / (t1 - t0)) * largura;
    // a altura do bloco também codifica a gravidade: quem só vê a cor de um jeito
    // ainda lê a diferença pela espessura
    const ALTURA_SEV = { 1: 7, 2: 11, 3: 16 };

    // marcas de tempo: hora quando a janela é de um dia, dia quando é maior
    const marcas = [];
    const passo = dados.dias <= 1 ? 3 * 3600e3 : dados.dias <= 7 ? 864e5 : 5 * 864e5;
    const primeiro = new Date(t0);
    if (dados.dias <= 1) primeiro.setMinutes(0, 0, 0);
    else primeiro.setHours(0, 0, 0, 0);
    for (let t = primeiro.getTime(); t <= t1; t += passo) {
      if (t < t0) continue;
      const d = new Date(t);
      marcas.push({
        px: ROT + ((t - t0) / (t1 - t0)) * largura,
        txt: dados.dias <= 1
          ? d.toLocaleTimeString("pt-BR", { hour: "2-digit", minute: "2-digit" })
          : d.toLocaleDateString("pt-BR", { day: "2-digit", month: "2-digit" }),
      });
    }

    const grade = marcas.map((m) =>
      `<line class="eixo-linha" x1="${m.px}" y1="${TOPO - 6}" x2="${m.px}" y2="${TOPO + linhas.length * ALT_LIN}"></line>
       <text class="eixo-txt" text-anchor="middle" x="${m.px}" y="${TOPO - 11}">${m.txt}</text>`).join("");

    const corpoSvg = linhas.map((l, i) => {
      const yTopo = TOPO + i * ALT_LIN;
      const meio = yTopo + ALT_LIN / 2;
      const blocos = l.blocos.map((b, j) => {
        const bx = Math.max(ROT, x(b.inicio));
        // piso de largura: numa janela de 7 dias uma ocorrência de 20 min some
        const bw = Math.max(4.5, x(b.fim) - bx);
        const bh = ALTURA_SEV[Math.min(b.severidade, 3)] || 6;
        return `<rect class="bloco-oc s${Math.min(b.severidade, 3)}" data-l="${i}" data-b="${j}"
          x="${bx}" y="${meio - bh / 2}" width="${bw}" height="${bh}" rx="2"></rect>`;
      }).join("");
      return `
        <g>
          <line class="faixa-trilho" x1="${ROT}" y1="${meio}" x2="${W - DIR}" y2="${meio}"></line>
          <rect x="6" y="${meio - 7}" width="3" height="14" rx="1.5" fill="${l.cor}"></rect>
          <text class="faixa-num" x="16" y="${meio + 4}">${l.linha}</text>
          <text class="faixa-rotulo" x="36" y="${meio + 4}">${escapar(l.nome)}</text>
          ${blocos}
        </g>`;
    }).join("");

    area.innerHTML = `
      <svg class="faixas-svg" viewBox="0 0 ${W} ${A}" role="img"
           aria-label="Períodos fora do normal por linha">
        ${grade}${corpoSvg}
      </svg>
      <div class="balao" id="balao-faixas"></div>`;

    const svg = area.querySelector("svg");
    const balao = $("#balao-faixas");
    area.querySelectorAll(".bloco-oc").forEach((r) => {
      const l = linhas[Number(r.dataset.l)];
      const b = l.blocos[Number(r.dataset.b)];
      r.addEventListener("mouseenter", () => {
        r.classList.add("destaque");
        posicionarBalao(balao, svg, area,
          Number(r.getAttribute("x")) + Number(r.getAttribute("width")) / 2,
          Number(r.getAttribute("y")),
          `<b>Linha ${l.linha} · ${escapar(l.nome)}</b>${escapar(b.rotulo)}<br>` +
          `${relogio(b.inicio)} · ${b.em_curso ? "em curso" : duracao(b.duracao_min)}`);
      });
      r.addEventListener("mouseleave", () => {
        r.classList.remove("destaque");
        balao.classList.remove("visivel");
      });
    });

    const afetadas = linhas.filter((l) => l.blocos.length).length;
    const total = linhas.reduce((s, l) => s + l.blocos.length, 0);
    $("#faixas-nota").textContent = total
      ? `${total} ${plural(total, "ocorrência", "ocorrências")} em ${afetadas} ${plural(afetadas, "linha", "linhas")}`
      : "nenhuma ocorrência no período";
  }

  // ═══════════════════ gráfico por dia ═══════════════════ //

  async function desenharSerie() {
    const area = $("#serie-area");
    let serie;
    try { serie = (await buscar("/api/serie?dias=14")).serie; }
    catch { area.innerHTML = ""; return; }

    const L = 40, A = 92, ESQ = 28, DIR = 6, TOPO = 9, BASE = 19;
    const larguraTotal = serie.length * L;
    const plot = A - TOPO - BASE;
    const maximo = Math.max(1, ...serie.map((d) => d.n));
    const lb = L - 11;

    const barras = serie.map((d, i) => {
      const h = d.n ? Math.max(3, (d.n / maximo) * plot) : 2;
      return `<rect class="barra${d.n ? "" : " vazia"}" data-i="${i}"
        x="${ESQ + i * L + (L - lb) / 2}" y="${TOPO + plot - h}" width="${lb}" height="${h}" rx="3"></rect>`;
    }).join("");

    const rotulos = serie.map((d, i) => {
      if (i % 3 !== 0 && i !== serie.length - 1) return "";
      const dia = new Date(`${d.dia}T12:00:00`);
      return `<text class="eixo-txt" text-anchor="middle" x="${ESQ + i * L + L / 2}" y="${A - 5}">
        ${dia.toLocaleDateString("pt-BR", { day: "2-digit", month: "2-digit" })}</text>`;
    }).join("");

    const alvos = serie.map((d, i) =>
      `<rect class="alvo" data-i="${i}" x="${ESQ + i * L}" y="${TOPO}" width="${L}" height="${plot}" fill="transparent"></rect>`).join("");

    area.innerHTML = `
      <svg class="serie-svg" viewBox="0 0 ${ESQ + larguraTotal + DIR} ${A}" role="img"
           aria-label="Ocorrências por dia nos últimos 14 dias">
        <text class="eixo-txt" text-anchor="end" x="${ESQ - 7}" y="${TOPO + 7}">${maximo}</text>
        <text class="eixo-txt" text-anchor="end" x="${ESQ - 7}" y="${TOPO + plot}">0</text>
        <line class="base" x1="${ESQ}" y1="${TOPO + plot}" x2="${ESQ + larguraTotal}" y2="${TOPO + plot}"></line>
        ${barras}${rotulos}${alvos}
      </svg>
      <div class="balao" id="balao-serie"></div>`;

    const svg = area.querySelector("svg");
    const balao = $("#balao-serie");
    area.querySelectorAll(".alvo").forEach((alvo) => {
      const i = Number(alvo.dataset.i);
      const barra = area.querySelector(`.barra[data-i="${i}"]`);
      alvo.addEventListener("mouseenter", () => {
        barra.classList.add("destaque");
        const d = serie[i];
        const dia = new Date(`${d.dia}T12:00:00`);
        posicionarBalao(balao, svg, area,
          ESQ + i * L + L / 2, Number(barra.getAttribute("y")),
          `${dia.toLocaleDateString("pt-BR", { day: "2-digit", month: "short" })} · ${d.n}`);
      });
      alvo.addEventListener("mouseleave", () => {
        barra.classList.remove("destaque");
        balao.classList.remove("visivel");
      });
    });

    const total = serie.reduce((s, d) => s + d.n, 0);
    $("#serie-nota").textContent = `últimos 14 dias · ${total} no total`;
  }

  // ═══════════════════ e-mail ═══════════════════ //

  function prepararEmail() {
    if (!EMAIL_ATIVO) { $("#aviso-email").hidden = false; return; }
    $("#cartao-email").hidden = false;
    $("#email").value = lembrado(CHAVE_EMAIL);

    $("#form-email").addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const retorno = $("#retorno-email");
      const mostrar = (txt, erro) => {
        retorno.textContent = txt;
        retorno.classList.toggle("erro", erro);
        retorno.hidden = false;
      };

      const linhas = [...app.seguidas];
      if (!linhas.length) { mostrar("Escolha pelo menos uma linha antes de se inscrever.", true); return; }

      const botao = ev.target.querySelector("button[type=submit]");
      botao.disabled = true;
      try {
        const r = await fetch("/api/inscricoes", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            email: $("#email").value.trim(), linhas, severidade: Number($("#severidade").value),
          }),
        });
        const d = await r.json();
        if (!r.ok) throw new Error(d.erro || "falha na inscrição");
        lembrar(CHAVE_EMAIL, $("#email").value.trim());
        mostrar(`${d.mensagem} Avisaremos sobre as linhas ${d.linhas.join(", ")}.`, false);
      } catch (e) {
        mostrar(e.message, true);
      } finally {
        botao.disabled = false;
      }
    });
  }

  // ═══════════════════ abas e filtros ═══════════════════ //

  function prepararControles() {
    const mapa = { "aba-painel": "painel", "aba-ocorrencias": "ocorrencias" };
    $$(".aba").forEach((aba) => aba.addEventListener("click", () => {
      $$(".aba").forEach((a) => {
        a.classList.toggle("ativa", a === aba);
        a.setAttribute("aria-selected", String(a === aba));
      });
      app.aba = mapa[aba.id];
      $("#painel").hidden = app.aba !== "painel";
      $("#ocorrencias").hidden = app.aba !== "ocorrencias";
      if (app.aba === "ocorrencias") atualizarAbaOcorrencias();
    }));

    $$("[data-dias]").forEach((b) => b.addEventListener("click", () => {
      $$("[data-dias]").forEach((o) => o.classList.toggle("ativa", o === b));
      app.oc.dias = Number(b.dataset.dias);
      atualizarAbaOcorrencias();
    }));

    $$("[data-sev]").forEach((b) => b.addEventListener("click", () => {
      $$("[data-sev]").forEach((o) => o.classList.toggle("ativa", o === b));
      app.oc.sev = Number(b.dataset.sev);
      carregarOcorrencias(true);
    }));

    $("#so-seguidas").addEventListener("change", (ev) => {
      app.oc.soSeguidas = ev.target.checked;
      atualizarAbaOcorrencias();
    });

    $("#btn-mais").addEventListener("click", () => {
      app.oc.offset = app.oc.itens.length;
      carregarOcorrencias(false);
    });

    $("#btn-notificacao").addEventListener("click", pedirPermissao);
    $("#gaveta-fundo").addEventListener("click", fecharGaveta);

    if (ARTESP_ATIVO) {
      $("#btn-artesp").hidden = false;
      $("#btn-artesp").addEventListener("click", carregarArtesp);
    }
  }

  function atualizarAbaOcorrencias() {
    carregarOcorrencias(true);
    desenharFaixas();
    desenharSerie();
  }

  // ═══════════════════ ciclo ═══════════════════ //

  async function atualizarEstado() {
    try {
      const d = await buscar("/api/estado");
      app.linhas = d.linhas;
      app.porNumero = Object.fromEntries(d.linhas.map((l) => [l.linha, l]));
      app.ultimaColeta = d.ultima_coleta;
      detectarMudancas(d.linhas);
      desenharManchete();
      desenharRede();
      desenharCartoes();
    } catch {
      $("#manchete").textContent = "Sem conexão com o servidor";
    }
  }

  async function atualizarSaude() {
    try {
      const s = await buscar("/api/saude");
      const luz = $("#pulso-luz"), txt = $("#pulso-txt");
      luz.className = `pulso-luz ${s.coletor_ok ? "viva" : "morta"}`;
      txt.textContent = s.coletor_ok
        ? `coletando · ${haQuantoTempo(s.ultima_coleta)}`
        : s.ultima_coleta ? `parado ${haQuantoTempo(s.ultima_coleta)}` : "sem coleta";
      $("#diagnostico").textContent = (s.fontes || [])
        .map((f) => `${app.nomesFonte[f.fonte] || f.fonte}: ${f.ok ? "ok" : f.detalhe}`)
        .join("  ·  ");
    } catch {
      $("#pulso-txt").textContent = "servidor fora do ar";
      $("#pulso-luz").className = "pulso-luz morta";
    }
  }

  async function iniciar() {
    ajustarBotaoNotificacao();
    prepararControles();
    prepararEmail();

    try {
      const c = await buscar("/api/linhas");
      app.nomesFonte = c.fontes;
    } catch {}

    await atualizarEstado();
    await atualizarSaude();

    setInterval(atualizarEstado, INTERVALO_ESTADO);
    setInterval(atualizarSaude, INTERVALO_ESTADO * 2);
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) { atualizarEstado(); atualizarSaude(); }
    });
  }

  iniciar();
})();
