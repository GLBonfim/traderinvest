# Validação dos agentes FX v2 (IBKR, histórico real)

> Gerado por `python -m app.fx.research.report`. Investigação apenas: nenhuma ordem foi enviada. Resultados passados não garantem resultados futuros.

## Dados e método

- Fonte: IBKR (TWS API, sessão paper, só leitura), Forex IDEALPRO, velas M15 BID e ASK, 9 pares, ~6 anos; spread mínimo conservador por par aplicado a todas as velas (as velas BID/ASK da IBKR fecham muitas vezes com spread 0).
- Períodos (hora de entrada, UTC): desenvolvimento 2020-10-17 → 2024-07-01; validação 2024-07-01 → 2025-10-01; teste final 2025-10-01 → 2026-10-17 (avaliado uma única vez).
- Execução: entrada na abertura da vela seguinte ao preço real (ask/bid); stop e alvo no lado que fecha a posição; gap fecha à abertura; stop e alvo na mesma vela = stop; saída por tempo e antes do fim de semana; uma posição por par.
- Custos em R (múltiplos do risco inicial, 0,5 % de 10 000 €): spread real, comissão max(2 USD, 0,2 bps) por lado, financiamento 0,5 bps por noite.
- Horizonte escolhido para o sistema (só com dados de desenvolvimento): **M15** (pontuações: {'M15': -39.71661292466352, 'H1': -41.90660454279542}).
- Regra de aprovação pré-registada (sha256 `c2e853491dc8cbf3…`, criada 2026-10-18T11:45:56Z antes do teste final): {'min_trades': 100, 'alpha_holm': 0.1, 'min_profit_factor': 1.05, 'validation_mean_r_positive': True, 'random_baseline_percentile': 90}.

## Calibração (desenvolvimento) e validação

### Tendência

Parâmetros escolhidos: `{'stop_atr': 2.5, 'target_atr': 4.0, 'min_slope_atr': 0.6}` (de 14 combinações; seleção pela estatística t suavizada pelos vizinhos da grelha).

| | Operações | Média R líquida | Acerto | Profit factor | Total R | DD máx (R) | Custo médio (R) |
|---|---|---|---|---|---|---|---|
| Desenvolvimento | 11742 | -0.198 | 36% | 0.68 | -2321.4 | 2321.4 | 0.080 |
| Validação | 3976 | -0.211 | 36% | 0.68 | -839.2 | 843.0 | 0.081 |
| Validação sem filtro de regime | 4194 | -0.205 | 37% | 0.68 | -859.5 | 863.7 | 0.081 |

### Momentum

Parâmetros escolhidos: `{'channel': 40, 'stop_atr': 2.0, 'target_atr': 3.0}` (de 10 combinações; seleção pela estatística t suavizada pelos vizinhos da grelha).

| | Operações | Média R líquida | Acerto | Profit factor | Total R | DD máx (R) | Custo médio (R) |
|---|---|---|---|---|---|---|---|
| Desenvolvimento | 12678 | -0.169 | 38% | 0.72 | -2137.4 | 2175.5 | 0.075 |
| Validação | 3858 | -0.211 | 37% | 0.67 | -815.3 | 817.2 | 0.076 |
| Validação sem filtro de regime | 4384 | -0.204 | 37% | 0.67 | -896.0 | 901.5 | 0.076 |

### Reversão à média

Parâmetros escolhidos: `{'band_k': 2.5, 'max_er': 0.2, 'stop_buffer_atr': 1.0}` (de 8 combinações; seleção pela estatística t suavizada pelos vizinhos da grelha).

| | Operações | Média R líquida | Acerto | Profit factor | Total R | DD máx (R) | Custo médio (R) |
|---|---|---|---|---|---|---|---|
| Desenvolvimento | 208 | -0.148 | 40% | 0.75 | -30.8 | 38.2 | 0.075 |
| Validação | 54 | -0.073 | 44% | 0.87 | -4.0 | 13.3 | 0.075 |
| Validação sem filtro de regime | 67 | +0.016 | 48% | 1.03 | +1.1 | 12.8 | 0.075 |

## Teste final (uma única avaliação)

| | Operações | Média R líquida | Acerto | Profit factor | Total R | DD máx (R) | Custo médio (R) |
|---|---|---|---|---|---|---|---|
| Tendência | 3259 | -0.292 | 34% | 0.58 | -950.9 | 951.4 | 0.083 |
| Momentum | 3203 | -0.246 | 36% | 0.63 | -788.0 | 789.7 | 0.077 |
| Reversão à média | 69 | +0.086 | 55% | 1.16 | +5.9 | 12.2 | 0.080 |

| Agente | p (bootstrap) | p Holm | Aleatório p90 (R) | Custos agravados (R) | Aprovado |
|---|---|---|---|---|---|
| Tendência | 1.000 | 1.000 | -0.252 | -0.345 | não (holm_p, profit_factor, validation_positive, beats_random_p90) |
| Momentum | 1.000 | 1.000 | -0.277 | -0.303 | não (holm_p, profit_factor, validation_positive) |
| Reversão à média | 0.319 | 0.958 | -0.097 | +0.021 | não (min_trades, holm_p, validation_positive) |

Por par (média R líquida no teste final):

| Par | Tendência | Momentum | Reversão à média |
|---|---|---|---|
| AUDUSD | -0.258 (360) | -0.263 (336) | +0.212 (7) |
| EURGBP | -0.474 (322) | -0.375 (361) | +0.613 (8) |
| EURJPY | -0.259 (348) | -0.208 (287) | -1.718 (5) |
| EURUSD | -0.202 (391) | -0.152 (419) | +0.809 (7) |
| GBPUSD | -0.251 (371) | -0.135 (413) | +0.074 (12) |
| NZDUSD | -0.288 (335) | -0.302 (309) | +0.275 (5) |
| USDCAD | -0.324 (400) | -0.321 (425) | -0.088 (5) |
| USDCHF | -0.367 (366) | -0.258 (388) | -0.314 (10) |
| USDJPY | -0.226 (366) | -0.209 (265) | +0.378 (10) |

## Sistema completo no teste final (orquestrador, coordenador, risco)

Corridas com o código usado no teste final (antes do travão de drawdown):

| Corrida | Retorno | DD máx | Entradas | Máx. entradas/dia | Bloqueios diários | Kill switch | Comissões | Cadeia íntegra |
|---|---|---|---|---|---|---|---|---|
| Só agentes aprovados (produto) | +0.00 % | 0.00 % | 0 | 0 | 0 | 0 | 0.00 € | True |
| Diagnóstico: todos os agentes, incluindo reprovados | -76.10 % | 76.74 % | 1318 | 5 | 1 | 0 | 1969.32 € | True |

O diagnóstico mostra que os limites diários (-5 % / +15 %, 5 entradas) não travam uma perda lenta e contínua: um só bloqueio diário num ano. Por isso foi acrescentado depois um travão de drawdown máximo (20 % abaixo do pico, ativa o kill switch). O travão bloqueia NOVAS entradas; não fecha posições abertas nem garante que a perda pare nesse nível (gaps, slippage).

### Exploratório pós-observação (com o travão de drawdown)

Corrida feita DEPOIS de conhecer o resultado acima, só para verificar o mecanismo; não é uma validação independente e não repete o teste final dos agentes.

| Corrida | Retorno | DD máx | Entradas | Máx. entradas/dia | Bloqueios diários | Kill switch | Comissões | Cadeia íntegra |
|---|---|---|---|---|---|---|---|---|
| Diagnóstico com travão: todos os agentes | -18.11 % | 20.06 % | 159 | 5 | 0 | 1 | 425.23 € | True |

## Conclusão

**Nenhum agente passou a regra pré-registada.** Os agentes continuam a analisar o mercado e a mostrar propostas ("em observação"), mas o produto não abre posições com eles. Em SIMULAÇÃO, o modo de demonstração (Ficheiro → Configuração) permite ver agentes não aprovados a operar preços sintéticos, sempre identificados como tal.

Estes resultados dizem respeito a estes padrões técnicos em M15/H1 com os custos de uma conta de 10 000 €; não dizem nada sobre outras estratégias. Qualquer nova estratégia tem de passar o mesmo protocolo antes de ser aprovada.

