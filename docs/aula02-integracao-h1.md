# Integração da Aula02 no experimento H1

Versão `aula02-h1-veto/1.0.0`, ativação explícita `--lesson-h1`.

A pedido humano em 22/10/2026, a aula foi adaptada ao laboratório H1 existente como **veto adicional**, não como novo emissor de ordens. Não é o modelo original H4/M5; M5 continua desabilitado. A aprovação histórica permanece falsa, os pesos são fixos e a avaliação paper é prospectiva.

## Convenções congeladas antes da implantação

- Entrada: mesmas velas H1 fechadas e verificadas pelo piloto. Não fazer upsample de H1 para M5.
- Contexto: últimas 16 horas contínuas obrigatórias. Para cada sweep candidato nas oito barras anteriores, referência = H4 UTC imediatamente anterior, completo, com fechamento abaixo da abertura.
- Sweep vendedor: máxima excede CRH, fechamento retorna entre CRL e CRH, sem varrer também CRL na mesma barra. A variante bullish não gera SELL.
- MSS local: numa barra posterior ao sweep, corpo vendedor e fechamento abaixo da mínima das três barras anteriores ao sweep. É uma convenção computável de quebra estrutural; não reivindica leitura de fluxo institucional.
- Expansão: maior máxima e menor mínima entre sweep e primeira quebra estrutural; âncoras ficam fixas. Novo extremo superior invalida; novo fundo intermediário também invalida em vez de recalcular a OTE com futuro.
- Reteste: barra atual toca a faixa de 62–79% de retração, não excede 79%, fecha abaixo de 62%, com corpo vendedor e dentro do range de referência.
- Padrão da família do especialista e previsão direcional concordante continuam necessários. O filtro da aula nunca fabrica um padrão ou uma previsão.
- Filtros existentes de sessão, spread, atraso do sinal, cotação LIVE, risco, quantidade, caixa e execução permanecem obrigatórios.
- Stop 2 ATR, alvo 3 ATR e prazo de oito barras são os controles existentes do laboratório, não uma alegação de parâmetros ensinados no PDF. As saídas de operações anteriores não são recalculadas pela aula.

## Registro

Cada contexto contém `lesson_h1` com versão, fonte/hash, motivo e timestamps de sweep/MSS/reteste. Cada especialista registra `pilot_lesson_evaluation`: decisão de proposta ou NO TRADE. Proposta não é ordem, ordem não é fill e resultado aberto não é lucro realizado.

O painel existente mostra os contextos detalhados; não foi reconstruído o executável nesta implantação. A confirmação autoritativa da versão ativa é `status.json` com `lesson_h1_enabled=true` e versão acima.

## Evidência anterior à implantação

- 66 testes conjuntos de sequência, piloto e banca; 63 testes de execução/funding/cash-ledger; mais três casos de consumo do filtro pelos especialistas: 132 testes distintos aprovados.
- Ruff e mypy limpos.
- Replay descritivo: 17.007 janelas H1 de desenvolvimento 2023; sequências completas EURUSD=1, EURGBP=0, EURJPY=2. Não são trades nem avaliação de retorno; sem holdout, sem treino e sem aprovação estatística.
- Probe paper somente leitura: LIVE confirmado nos três pares, stop e alvo BUY de 5.000 EURUSD ativos. Nenhuma ordem real de teste enviada.

## Implantação

Pausa por STOP próprio `LESSON-H1-DEPLOY-20261022`; não remover STOP de outra origem. Retomada exige conta DU única, integridade de ambos os diários, caixa/execuções reconciliados, quantidade de estratégia exatamente igual ao estado persistido, somente as proteções conhecidas, novas cotações LIVE e prévias WHAT-IF sem erros. Não repetir conversões ou testes manuais encerrados. Em qualquer divergência, manter pausado e registrar evidência.


## Confirmação de implantação — 2026-10-22T00:39:51.399365+00:00

Laboratório original retomado com --lesson-h1, PID 18904, sessão EXEC32813, state=observing, armed=true. Os três contextos carregaram aula02-h1-veto/1.0.0; nenhum apresentou sequência completa no primeiro ciclo. A posição EURUSD anterior e as duas proteções foram preservadas; nenhuma nova ordem foi enviada na implantação. LIVE reconfirmado após retomada nos três pares. Caixas reconciliados, diários 220/7 íntegros na auditoria.132 testes distintos aprovados; Ruff/mypy limpos. Não houve treino de pesos nem aprovação histórica.

Para retomar futuramente, preservar --lesson-h1 junto de --arm --funded-eur-only --cash-ledger e consentimento EXPERIMENTO-SOMENTE-DEMO, porta4002/client62, somente após reconciliação. Nunca iniciar processo concorrente ou remover STOP/kill de outra origem.
