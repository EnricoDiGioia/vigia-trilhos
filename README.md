# Vigia dos Trilhos

Monitor de status das 15 linhas de metrô e trem da Região Metropolitana de São Paulo.
Coleta o estado de cada linha em intervalos regulares, deriva as transições, guarda o
histórico das ocorrências e avisa — por navegador e por e-mail — quando uma linha
acompanhada muda de estado.

Flask e SQLite, sem nenhuma dependência além do Flask.

**Demo:** [vigia-trilhos.onrender.com](https://vigia-trilhos.onrender.com)

![Painel](docs/painel.png)

---

## Índice

- [Contexto](#contexto)
- [Fontes de dados](#fontes-de-dados)
- [Arquitetura](#arquitetura)
- [Modelo de dados](#modelo-de-dados)
- [Detecção de estado](#detecção-de-estado)
- [Interface](#interface)
- [Notificações](#notificações)
- [API](#api)
- [Variáveis de ambiente](#variáveis-de-ambiente)
- [Estrutura](#estrutura)
- [Limitações conhecidas](#limitações-conhecidas)
- [Próximos passos](#próximos-passos)
- [Aviso legal](#aviso-legal)
- [Licença](#licença)

---

## Contexto

Não existe uma fonte única e oficial com o status das quinze linhas da rede. O portal
CCM da ARTESP publica uma API documentada, mas desde setembro de 2026 cobre apenas as
concessões privadas — linhas 4, 5, 6, 7, 8 e 9, ou 40% da rede. Metrô e CPTM ficam de
fora. O aplicativo oficial de trem e metrô, por outro lado, consome um endpoint que
devolve as quinze linhas de uma vez, mas esse endpoint não é documentado nem tem
contrato público.

O projeto resolve isso combinando três fontes com papéis distintos, e trata a
divergência entre elas como sinal, não como ruído.

---

## Fontes de dados

| Fonte | Cobertura | Autenticação | Papel |
|---|---|---|---|
| **Próximo Trem** — backend do app oficial | 15 linhas | nenhuma | primária |
| **API Trilhos** — ARTESP CCM | linhas 4 a 9 | `Api-Key`, 12 req/h | conferência e histórico oficial |
| **viamobilidade.com.br** — raspagem do HTML | 14 linhas | nenhuma | fallback |

A primária é consultada a cada ciclo. O fallback só entra quando ela falha ou devolve
menos que a rede inteira. A ARTESP tem ritmo próprio, limitado por
`ARTESP_INTERVALO_MIN`, porque o teto de 12 requisições por hora da credencial não
sustenta poll contínuo — ela vale menos como sensor e mais como memória: é a única com
histórico, até 365 dias.

Quando duas fontes reportam a mesma linha, prevalece a de maior prioridade
(`PRIORIDADE_FONTE` em `fontes.py`). A discordância fica registrada como divergência no
resultado do ciclo, e costuma ser o primeiro sintoma de uma fonte travada.

Cada fonte devolve rótulos livres em português — "Operação Normal", "Velocidade
Reduzida", "Circulação Paralisada", grafias que variam entre operadoras. A normalização
em `fontes.normalizar_status()` reduz tudo a seis estados canônicos antes de qualquer
comparação.

---

## Arquitetura

Um ciclo de coleta consulta as fontes, consolida uma leitura por linha, grava o estado,
deriva as transições e dispara as notificações. O ciclo roda de duas maneiras, que podem
conviver:

- **Thread interna**, controlada por `POLL_SEGUNDOS`.
- **`GET /api/cron/poll`**, para um agendador externo.

O segundo modo existe por causa da hospedagem gratuita, que derruba o processo ocioso.
Em vez de lutar contra isso, um cron externo bate na URL e, na mesma requisição, acorda
o serviço e executa a coleta. Um piso de `INTERVALO_MINIMO` segundos entre ciclos impede
que os dois modos se atropelem. O `render.yaml` na raiz descreve o serviço completo e já
vem configurado para o modo dirigido por cron.

```
cron externo ──► /api/cron/poll
                      │
                      ▼
              coletar_tudo()                fontes.py
              ├─ Próximo Trem  (primária)
              ├─ ARTESP        (se o intervalo dela venceu)
              └─ ViaMobilidade (se a primária falhou)
                      │
                      ▼
               consolidar()  ──► uma leitura por linha + divergências
                      │
                      ▼
            aplicar_leituras()               dados.py
              ├─ estado igual   → atualiza carimbo e descrição
              └─ estado mudou   → fecha evento anterior com duração,
                                   abre novo se for anômalo
                      │
                      ▼
             eventos novos ──► notificador de e-mail   alertas.py
```

---

## Modelo de dados

SQLite, quatro tabelas.

**`estado_atual`** — uma linha por linha da rede. O estado vigente, a fonte que o
reportou, `desde` (quando entrou neste estado) e `visto_em` (última confirmação).

**`eventos`** — um registro por período contínuo fora do normal. `inicio`, `fim` nulo
enquanto em curso, `duracao_min`, a severidade, o estado anterior e a descrição da
ocorrência. Grava também `weekday` e `is_peak` no momento da ingestão, para o baseline
estatístico descrito em [Próximos passos](#próximos-passos).

**`inscricoes`** — e-mail, linhas acompanhadas, severidade mínima e um token de
cancelamento. Um e-mail tem uma inscrição; reinscrever sobrescreve as preferências.

**`envios`** — par (inscrição, evento) já notificado. É o que garante que o mesmo evento
nunca seja enviado duas vezes, mesmo que o ciclo rode de novo.

Há ainda `coletas`, um log curto do pulso de cada fonte, podado para três dias.

---

## Detecção de estado

Os seis estados canônicos, com a severidade entre parênteses:

`normal` (0) · `encerrada` (0) · `desconhecido` (1) · `reduzida` (1) · `parcial` (2) ·
`paralisada` (3)

Cada ciclo compara a leitura nova com o estado guardado. Igual, apenas refresca o
carimbo e a descrição. Diferente, fecha o evento anterior com a duração calculada e abre
um novo se o estado de destino for anômalo.

Duas decisões que valem registro:

**Linha ausente do payload vira `desconhecido`, nunca `normal`.** A API às vezes para de
listar uma linha em vez de reportar o problema dela. Tratar ausência como normalidade
seria a forma mais silenciosa de errar.

**O coletor parado é a anomalia mais importante.** `/api/saude` marca `coletor_ok` como
falso quando passam 15 minutos sem coleta, e o indicador no topo da página fica vermelho.
Um monitor que fica cego em silêncio é pior que monitor nenhum.

O poll lê estado, não eventos. Qualquer ocorrência ainda em curso no instante da leitura
é capturada, independentemente de quando começou; uma que comece e termine inteira entre
dois ciclos passa despercebida. O `inicio` gravado é a hora do poll, então as durações
ficam na grade do intervalo de coleta.

Filtros por período usam sobreposição com a janela, não data de início: uma ocorrência
que começou antes do recorte e continua em curso pertence ao período tanto quanto uma
que começou dentro dele.

---

## Interface

O princípio de layout é que **o que está fora do normal sobe e cresce, o que está normal
encolhe**. Quinze cartões idênticos dariam a uma linha paralisada o mesmo peso visual de
uma linha tranquila.

**Painel.** Abre com uma manchete — *"3 linhas com ocorrência"* ou *"Rede operando
normalmente"* — seguida de quais linhas e desde quando. Abaixo, **A rede agora** exibe as
quinze linhas em peças compactas, cada uma na sua cor oficial, tingidas pela gravidade do
estado. É o único componente que mostra a rede inteira de uma vez, e funciona como
navegação: um clique abre a gaveta de detalhe com o histórico daquela linha. Só viram
cartão as linhas acompanhadas e as que estão com ocorrência; as demais ficam numa lista
recolhida no rodapé.

**Ocorrências.** O gráfico de faixas mostra *quando* cada linha esteve fora do normal na
janela escolhida, uma raia por linha, com a espessura do bloco codificando a gravidade
além da cor. Abaixo, a contagem por dia e a lista detalhada, todas respondendo aos mesmos
filtros de período, gravidade e linha.

![Aba de ocorrências, tema escuro](docs/ocorrencias.png)

---

## Notificações

**Navegador.** Notification API. Dispara quando uma linha acompanhada muda de estado, e
funciona apenas com a aba aberta — limitação da API sem service worker. As linhas
acompanhadas ficam no `localStorage`, portanto são por navegador, não por conta.

**E-mail.** SMTP. Todos os eventos de um mesmo ciclo viram uma única mensagem por
inscrito, filtrada pelas linhas e pela severidade mínima que ele escolheu. Cada mensagem
carrega um link de cancelamento. Sem SMTP configurado, a interface esconde o formulário
de inscrição e o resto segue funcionando.

---

## API

| Rota | O que devolve |
|---|---|
| `GET /api/estado` | estado corrente das 15 linhas, última coleta e saúde das fontes |
| `GET /api/ocorrencias` | histórico local. `linhas=3,9` · `dias=7` · `severidade=1..3` · `abertas=1` · `limite` · `offset` |
| `GET /api/ocorrencias/artesp` | histórico oficial. Consome 1 das 12 requisições/hora |
| `GET /api/linha-do-tempo?dias=7` | blocos de ocorrência por linha, recortados na janela |
| `GET /api/serie?dias=14` | contagem de ocorrências por dia |
| `GET /api/saude` | diagnóstico: atraso da coleta, estado de cada fonte, contagens |
| `GET /api/linhas` | catálogo das linhas, estados possíveis e nomes das fontes |
| `POST /api/inscricoes` | cria ou atualiza uma inscrição de e-mail |
| `GET\|POST /api/cron/poll` | executa um ciclo. Exige `?token=` se `CRON_TOKEN` estiver definido |

---

## Variáveis de ambiente

Todas opcionais. Sem nenhuma delas o app sobe com os padrões abaixo; `.env.example`
traz a lista comentada.

| Variável | Padrão | Efeito |
|---|---|---|
| `POLL_SEGUNDOS` | `60` | Intervalo da thread interna. `0` desliga, deixando o cron externo no comando. |
| `INTERVALO_MINIMO` | `25` | Piso em segundos entre dois ciclos. |
| `VIGIA_DB` | `vigia.db` | Caminho do arquivo SQLite. |
| `CRON_TOKEN` | vazio | Se definido, `/api/cron/poll` passa a exigir o token. |
| `ARTESP_API_KEY` | vazio | Credencial do Portal CCM. Vazia, a fonte é ignorada. |
| `ARTESP_INTERVALO_MIN` | `10` | Minutos entre consultas à ARTESP. |
| `SMTP_HOST` `SMTP_PORT` `SMTP_USUARIO` `SMTP_SENHA` `SMTP_DE` | vazio | Envio de e-mail. |
| `SITE_URL` | vazio | URL pública, usada no link de cancelamento. |
| `SEMEAR_DEMO` | `0` | `1` popula ocorrências fictícias num banco vazio. |

---

## Estrutura

```
app.py            servidor Flask, ciclo de coleta, rotas
fontes.py         os três coletores, catálogo das linhas, normalização de status
dados.py          SQLite: estado, eventos, inscrições, saúde
alertas.py        envio de e-mail por SMTP
teste_local.py    teste de fumaça com fonte simulada, sem acesso à rede
templates/        index.html e a página de cancelamento
static/           estilo.css e app.js
docs/             capturas de tela usadas neste README
render.yaml       Blueprint da Render: serviço, comandos e variáveis
Procfile          comando de start para plataformas que leem Procfile
.python-version   versão do Python usada no build
.env.example      todas as variáveis, comentadas
```

`teste_local.py` substitui os coletores por uma fonte simulada e exercita normalização
de rótulos, detecção de transição, fechamento de evento com duração, linha ausente do
payload, o parser de raspagem, todos os endpoints, os filtros de janela e o fluxo de
inscrição por e-mail — tudo sem tocar a rede.

---

## Limitações conhecidas

**Persistência.** O SQLite mora no disco da aplicação. Em hospedagem com disco efêmero o
banco é zerado a cada deploy ou reinício, e com ele o histórico local e as inscrições.

**Notificação do navegador.** Só com a aba aberta, e por navegador em vez de por conta.

**Granularidade.** Ocorrências mais curtas que o intervalo de coleta podem passar
despercebidas, e o aviso chega com latência de até um intervalo.

**Estabilidade da fonte primária.** O endpoint do Próximo Trem não é documentado: pode
mudar de formato, passar a exigir chave ou sumir sem aviso. O fallback de raspagem existe
por isso.

---

## Próximos passos

A tabela `eventos` já grava `weekday` e `is_peak` em cada registro, que é o que falta
para o passo seguinte: comparar a duração corrente contra o p95 histórico da mesma linha,
por faixa horária e dia da semana, e contar eventos numa janela móvel contra o baseline.
Daí sai a diferença entre "mudou de estado" e "está anormal".

Outras direções: correlacionar linhas para detectar evento sistêmico — duas ou mais
degradando em poucos minutos costuma ser chuva ou falha de energia — e cruzar com a API
Olho Vivo da SPTrans para medir o transbordo de demanda para os ônibus.

---

## Aviso legal

Projeto independente, sem qualquer vínculo com Metrô, CPTM, ARTESP ou concessionárias.

Os termos de uso do Portal CCM falam em cópia temporária para uso pessoal e não
comercial, e não tratam explicitamente de coleta automatizada nem de armazenamento de
histórico. Imagens do portal exigem marca d'água intacta e o crédito "Fonte: Portal CCM –
ARTESP (ccm.artesp.sp.gov.br)".

## Licença

[MIT](LICENSE)
