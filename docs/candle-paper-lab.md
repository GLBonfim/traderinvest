# Analista e três especialistas — laboratório prospectivo paper

Pedido autorizado: colocar candidatos de velas em prática na conta demo e comparar resultados. O resultado histórico anterior continua sendo **nenhum aprovado**. O laboratório é uma estratégia nova, experimental; não altera os registros históricos nem o carregador padrão.

O Analista de Mercado calcula contexto em H1 com velas fechadas: tendência, eficiência, volatilidade, sessão e padrões geométricos. Registra o contexto e o repassa a três especialistas: reversão, continuação e impulso. Eles exigem padrões da sua família e respeitam os vetos do analista. Componentes Ridge, ExtraTrees e logística previamente treinados ajudam a ordenar as propostas; não são reclassificados como aprovados. O coordenador evita conflitos e o gestor de risco autoriza cada entrada.

## Restrição da conta e opção escolhida

A sessão paper DU…14 tem saldo em EUR. A prévia WHAT-IF de BUY EURUSD foi rejeitada com erro 201, `FX trade would expose account to currency leverage`. As prévias de SELL EURUSD, EURGBP e EURJPY passaram sem erros. WHAT-IF é apenas uma prévia, não uma execução. A experiência utiliza `--funded-eur-only`: entradas SELL nesses três pares, cobertas por EUR disponível; BUY apenas nos stops, alvos e fechamentos. Um padrão bullish é vetado, nunca transformado em venda. Nenhuma configuração da conta real foi alterada.

A execução exige confirmação explícita de experimento, porta paper, conta DU, prévias recentes dos pares admitidos e saldo EUR identificado. Cada entrada consome no máximo 10% do saldo EUR observado, além dos limites do gestor. Faltando saldo, cotação LIVE recente, padrão ou contexto compatível, não há entrada.

Antes de habilitar os especialistas, o sistema prepara uma única vez reservas de caixa: converte EUR 1.000 virtuais para cada uma das moedas USD, GBP e JPY. São três conversões de preparação, com custos, separadas do desempenho dos agentes. Exige mercado aberto, cotações LIVE recentes e verificação dos saldos e das posições virtuais CASH. Se a corretora não reportar essas posições, o processo pausa sem liberar estratégias. As posições de preparação confirmadas são descontadas apenas na reconciliação do laboratório; não se ocultam posições externas.

Cada entrada é limitada a EUR 5.000 e exige reserva na moeda de retorno suficiente para a distância do stop, mais 2% de margem para gaps e EUR 20 equivalentes para custos. Essa reserva reduz o risco de recusa do fechamento por falta de moeda; não garante preço de execução nem ausência de gaps maiores. O saldo é conferido novamente a cada entrada.

## Risco e acompanhamento

- Capital virtual de referência: EUR 10.000; risco inicial por entrada: 0,1%, antes de gaps e custos.
- Máximo de cinco entradas totais por dia e três posições simultâneas; perda diária de 1% e drawdown de 5% bloqueiam novas entradas.
- Bracket com stop de 2 ATR, alvo de 3 ATR e prazo máximo de oito velas; proteções ficam na corretora.
- Deterioração retira um candidato e utiliza uma reserva da mesma especialidade. Há três ativos e três reservas. Se elas acabarem, o papel correspondente fica sem operar.
- Os componentes preditivos ficam congelados nesta rodada. A demo coleta evidência prospectiva para comparação e posterior treinamento; não há alteração contínua de pesos a cada tick.
- O experimento admite novas entradas por até 30 dias. Depois disso, apenas saídas e reconciliação de posições/pendências.

Estado e diário: `data/fx/candle-lab-funded-paper/status.json` e `pilot.sqlite`. O processo deve permanecer ativo, com computador e Gateway ligados. As primeiras oportunidades dependem da abertura do mercado e da sessão líquida permitida (Londres/Nova York). Não é garantida entrada de todos os agentes no mesmo dia.

O relatório separa propostas, intenções enviadas, preenchimentos e operações fechadas. Resultados líquidos do piloto são estimativas; nenhuma classificação de “melhor” é válida sem mostrar amostra e custos. O limite inicial de observação é de pelo menos 20 operações fechadas por candidato antes de conclusões de desempenho.

Para parar novas ações, criar `STOP` na pasta desse experimento. Isso desarma o processo; as proteções existentes permanecem na corretora. Não apagar estado para reiniciar nem liberar kill switch automaticamente. Conferir posições e pendências antes de qualquer retomada.

## Reconciliação por caixa e execuções confirmadas (19/10/2026)

Depois de reconectar o Gateway, a API deixou de reportar as posições virtuais FX de
preparação, embora os quatro saldos por moeda coincidissem exatamente com as conversões,
o teste fechado e suas comissões. Posições virtuais e caixa por moeda são informações
distintas na IBKR; ausência da primeira não demonstra que o caixa foi zerado.

O modo explícito `--cash-ledger`, apenas com `--funded-eur-only`, utiliza uma âncora
auditada em `cash-anchor.json`, vinculada à conta DU única e ao hash de funding.json.
Ela só pode ser estabelecida sem exposição de estratégia ou ordens abertas, mediante
reconstrução independente de todo o caixa a partir dos preenchimentos conhecidos.
Não envia nem repete conversões e não modifica o baseline histórico da preparação.

Cada execução posterior deve corresponder a uma intenção previamente registrada no
diário. Quantidade, preço e comissão confirmada atualizam atomicamente o caixa esperado
e as quantidades por par no diário encadeado. O caixa da corretora deve coincidir a cada
ciclo e antes de uma entrada; uma execução desconhecida, comissão ausente, alteração de
caixa, ordem externa, outro ativo, conta diferente ou mudança na âncora pausa o processo.
Preenchimentos repetidos não são contabilizados novamente. Correções exigem revisão.
A ausência de execuções antigas após reconexão não permite inventá-las: se a persistência
não as contiver e o caixa não fechar, o sistema para. Esse modo não é aprovação dos modelos.
