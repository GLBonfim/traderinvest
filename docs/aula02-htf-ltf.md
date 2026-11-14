# Aula 02 — conhecimento, estratégias e preparação dos agentes

Fonte: **Aula 02 – Método Operacional II / Especialização Técnica HTF/LTF**, Alves Trader (@alves.trader_), 38 páginas. SHA256: `4e53c8c39e0944751180398927e13ea31e4dcd2f1eb7143fc3a960971804fa1c`.

Leitura: texto das 38 páginas extraído; todas as páginas renderizadas e examinadas; figuras OTE e horários ampliadas. As páginas referidas são a numeração física do PDF. Material educacional é fonte de hipóteses, não prova de desempenho, observação de ordens institucionais ou autorização para operar. Conteúdo promocional e QR codes não foram seguidos.

## O que o material ensina

| Páginas | Conteúdo e interpretação fiel |
|---|---|
| 1–3 | Identidade, título e agenda da formação; não contém regra de execução. |
| 4–5 | Comparação Forex/índices: descentralização, sessões, liquidez, gaps e sensibilidade macro. Preferências do autor são afirmações didáticas, não estatísticas demonstradas. A autorização atual continua restrita a EURUSD/EURGBP/EURJPY. |
| 6–8 | Price Action descreve estruturas; SMC interpreta liquidez e deslocamentos; ICT adiciona tempo e narrativa. Alegações de intenção institucional não são verificáveis apenas com OHLC. |
| 9 | Hierarquia: D1 contexto/viés/extremos; H4 direção/liquidez/oferta-demanda; H1 tendência/rompimento/reversão/OB/FVG; M15/M5/M1 confirmação. Não tratar um sinal M5 isolado como viés macro. |
| 10–13 | Diagramas de continuidade e reversão: extremos, ruptura da estrutura e transição direcional. Os exemplos não estabelecem algoritmo de pivôs, tolerâncias ou fechamento versus pavio. |
| 14 | OTE em gráfico: níveis de Fibonacci 0, 0,62, 0,705, 0,79 e 1; a faixa 62–79% é uma leitura dos níveis ilustrados. O gráfico mostra sweep, MSS, expansão e retração; não informa stop universal ou desempenho. |
| 15–21 | Diferenciar comportamento de Ásia, Londres e NY; três sequências de range/liquidez → sweep → mudança de estrutura → OTE. |
| 22–28 | CRT: range da vela maior, máxima CRH, mínima CRL, equilíbrio 50%; varrer um extremo, voltar ao range, confirmar MSS e buscar o outro extremo. Preferência por alinhamento com fluxo HTF. |
| 29–34 | Liquidez compradora/vendedora, zonas de interesse H4, refinamento H1, liquidez interna/externa, extremos fortes/fracos, OB e FVG. Exemplos visuais de trajetórias não são uma amostra auditada de trades. |
| 35 | Mapa de horários e relação liquidez/tempo/preço. Há divergência de NY em relação à p20. |
| 36–38 | Convite à prática e promoção de mentoria; sem regras adicionais suficientes para automação. |

## Vocabulário para analista e especialistas

- **HTF/LTF**: contexto em período maior e confirmação no menor. Só usar barras já encerradas na hora da decisão.
- **BSL/SSL**: regiões acima de máximas/abaixo de mínimas interpretadas como potenciais pools de liquidez. Não são volume de stops observado.
- **Sweep**: ultrapassagem de extremo seguida de retorno. Para o protótipo, exige fechamento dentro do range; essa exigência numérica é uma convenção de pesquisa explícita.
- **BOS**: rompimento na direção estrutural; **CHoCH/MSS**: mudança do comportamento/estrutura. Uma ruptura de pivô sem tendência prévia conhecida não basta para classificá-la como MSS.
- **Premium/discount**: acima/abaixo do equilíbrio de um range previamente definido. Não significam preço caro/barato fundamentalmente.
- **OTE**: retração de uma expansão previamente fixada. Para expansão [L,H], bullish: [H−0,79(H−L), H−0,62(H−L)]; bearish: [L+0,62(H−L), L+0,79(H−L)]. Não recalcular âncoras usando o futuro.
- **CRT**: sequência sobre o range de uma vela de referência; o alvo oposto é objetivo do modelo, não promessa de que será atingido.
- **FVG**: desequilíbrio visual de três velas; o protótipo apenas registra gap entre extremos da primeira e terceira, sem afirmar que será preenchido. Essa geometria não é definida numericamente na aula.
- **OB/POI**: bloco/zona de interesse; seleção, invalidação e validade precisam de regras congeladas antes de backtest.
- **IRL/ERL**: liquidez interna/externa ao range relevante.
- **Strong/weak high/low**: qualificação contextual dos extremos; não inferir força a partir de um único pavio.
- **SMT, PO3 e draw on liquidity**: termos citados como confluência/narrativa; aula não fornece definição operacional suficiente. Não implementados como confirmação.

## Quatro modelos descritos

### Asian Range — pp16–17 e35

1. Fixar as máximas/mínimas da janela Sydney antes de Tóquio.
2. Aguardar sweep de uma extremidade na janela Tóquio.
3. Aguardar confirmação de mudança estrutural na direção do retorno.
4. Fixar a expansão e esperar retração OTE em premium para venda/discount para compra.

O material destaca pares JPY/AUD/NZD; a conta autorizada só admite os três pares existentes. Não ampliar instrumentos. Janela asiática também conflita com o filtro atual H1 de sessão líquida: não abrir exceção silenciosa.

### London Killzone — pp18–19 e35

Usar range asiático encerrado; aguardar sweep na abertura de Londres, MSS e OTE. Não confundir breakout sustentado com varredura revertida. O autor atribui à sessão expansão e direcional inicial; não apresenta taxa de acerto.

### NY Killzone — pp20–21 e35

Identificar liquidez anterior/Londres; aguardar sweep no Open/Close NY; MSS e OTE. Pode haver continuação ou reversão de Londres: a direção exige contexto, não uma escolha posterior ao resultado. Horário permanece bloqueio de especificação.

### CRT — pp24–28

- Venda: varrer CRH, falhar em permanecer acima, voltar ao interior, MSS bearish, expansão em direção a CRL.
- Compra: varrer CRL, voltar ao interior, MSS bullish, objetivo CRH.
- Preferência do autor: CRT a favor do fluxo HTF, não apenas caçar reversão.
- A conta atual **não autoriza BUY de entrada**: leitura bullish deve ficar como observação/NO TRADE, nunca ser invertida para SELL.

## Horários preservados da fonte — não converter em calendário de execução automaticamente

| Fonte | Horário apresentado como Brasil |
|---|---|
| p16 | Sydney 19h–05h; Tóquio 21h–05h; range inicial 19h–21h |
| p18 | Londres 05h–14h |
| p20 | NY 10h30–18h; overlap 10h30 |
| p35 | Sydney liquidez 19h–21h; Tóquio 21h–05h; Londres Open 05h–07h, Close 12h–14h; NY Open 11h30–12h30, Close 15h–18h |

A p20 e p35 não coincidem em NY. O PDF não fixa data, fuso IANA de cada janela nem política DST. Não usar o relógio de Lisboa ou um deslocamento UTC fixo por suposição. Antes de ativação, o perfil deve escolher e versionar calendário por sessão com testes de mudanças de hora. O protótipo UTC H4 é convenção experimental, não confirmação do horário do autor.

## O que foi incorporado ao código

`app/candles/htf_ltf_lesson.py` disponibiliza currículos por função com páginas da fonte, lacunas e status de não aprovação; calcula fatos causais em M5 fechado: H4 anterior completo, CRH/CRL/50%, localização premium/discount, sweep com retorno, ruptura de pivô confirmado e geometria FVG. A função OTE calcula a região indicada no gráfico.

Convenções v1: H4 alinhado em UTC, referência imediatamente anterior, M5 real, pivô simétrico de largura 2 confirmado somente após barras à direita. Todas são hipóteses locais registradas, sem otimização. Barras faltantes impedem geometria relevante; não há interpolação. O pesquisador não importa executor, não usa credenciais e não envia ordens.

**Não foi implementada uma estratégia completa por chamar fatos isolados de setup.** Sweep e ruptura separados não comprovam sequência sweep→MSS→OTE, viés D1/H4, sessão, entrada executável ou vantagem após custos. Os currículos são conhecimento consultável; modelos existentes não tiveram pesos treinados e o processo H1 ativo não carregou automaticamente o novo módulo.

Distribuição de responsabilidades:

| Papel | Aprendizado operacional documentado |
|---|---|
| Analista | Relatar contexto D1/H4/H1, range, sessão, frescor, lacunas e evidências com páginas. Vetar conflito temporal/direcional e dados incompletos. |
| Reversão | Estudar sweep + retorno + MSS; exigir confirmação; CRT não significa vender toda máxima. |
| Continuação | Priorizar fluxo HTF, expansão e retração OTE; distinguir continuação de mudança estrutural. |
| Impulso | Estudar deslocamento e aberturas de sessão após sweep; não perseguir vela de expansão sem entrada definida. |
| Reservas | Mesmo currículo da respectiva função; nenhuma promoção automática. |

## Protocolo antes de aplicar estes modelos à demo

1. Congelar seleção de referência, pivôs, sequência, tolerância de sweep, direção HTF, âncoras OTE, sessões/DST, expiração/invalidação, stop e alvo. Não adaptar critérios após ver o resultado.
2. Usar histórico genuíno BID/ASK com timestamps e barras completas. D1/H4/H1 só entram após fechamento; pivô só após confirmação. Não reutilizar holdout já visto para declarar aprovação.
3. Executar backtest de estratégia completa com entrada posterior à confirmação, spread, comissões, slippage, gaps e tratamento conservador de stop/alvo na mesma barra; contar propostas, intenções simuladas e fills separadamente.
4. Separar desenvolvimento, validação temporal e holdout novo/adequadamente reservado. Relatar amostra por modelo/par/sessão, lucro líquido, expectativa, DD, concentração e carteira conjunta. Contagens de fatos não são acertos ou retorno.
5. Validar integração paper completa com o protocolo M5 existente (piso de custos, 96 barras, padrões, analista/modelo concordantes), reconciliação cash-ledger e proteções, sem repetir conversões ou os testes manuais encerrados.
6. Só coordenar transição com posições/pendências reconciliadas, sem processo concorrente. Manter limites globais 5 entradas/dia, 3 posições, risco 0,1%, perda diária 1%, DD 5%, entrada até €5.000, banca lida do estado, SELL financiado/BUY apenas saída.

**Estado de liberação: NÃO APROVADO para novas ordens destes modelos.** O experimento H1 existente continua independente; M5 anteriormente reprovado permanece desativado. Não há autorização implícita para liberar bloqueios ou alterar âncora/diário. A aula não contém dados suficientes para afirmar que qualquer especialista ficará lucrativo.

## Arquivos de evidência

- `texto-extraido.txt`: extração por página; os gráficos não estão representados integralmente nesse texto.
- `fonte.json`: identificação e hash do PDF original.
- `curriculo-*.json`: quatro currículos legíveis por máquina, com referências.
- `observacoes.jsonl` / `auditoria.json`: leitura descritiva em desenvolvimento 2023, com hashes dos arquivos de mercado, sem retorno ou holdout.
- `testes.txt`: verificação de causalidade e casos contrários.

A auditoria usa amostragem de uma barra a cada 48 observadas, após aquecimento, não todos os sinais e não uma avaliação de trades. Consequentemente não permite estimar frequência total, precisão ou lucro da estratégia.

## Resultado desta execução
Auditoria estritamente descritiva, sem treino de pesos, ordens ou avaliação de lucro.
| Par | Velas M5 reais 2023 | Janelas amostradas | Sweep superior + retorno | Sweep inferior + retorno |
|---|---:|---:|---:|---:|
| EURUSD | 73431 | 1528 | 28 | 29 |
| EURGBP | 73431 | 1528 | 30 | 38 |
| EURJPY | 73431 | 1528 | 28 | 30 |

As contagens não incluem confirmação conjunta MSS/OTE/HTF e não são entradas, fills ou acertos. Cada par teve 287 janelas sem H4 anterior completo e 30 com lacuna no trecho LTF recente. Esses casos ficam explicitamente identificados.

Validação: 43 testes aprovados (14 do módulo novo e 29 do conhecimento anterior); Ruff limpo. Uma primeira execução ampliada teve falha de criação de pasta temporária; execução repetida com diretório gravável passou integralmente.
