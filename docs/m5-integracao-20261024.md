# Integração M5 — 24/10/2026

Dados BID/ASK nativos de cinco minutos, somente velas fechadas e contínuas. Sem interpolação nem conversão de M15 para M5. Analista, padrões e previsão precisam concordar; tamanho autorizado pelo RiskManager antes de verificar custo/risco. Noventa e seis barras correspondem a oito horas. Duplicação de sinais é bloqueada pelo estado persistente.

Os seis candidatos originais continuam reprovados. Uma nova rodada separada, com filtro causal Aula02 CRT/MSS/OTE e protocolo congelado 10a8a5950e98, produziu zero operações na validação: todos reprovados por falta de amostra, sem avaliação do holdout. Isso não comprova lucro nem inviabilidade permanente. Nenhum candidato M5 foi promovido. M1 não foi implementado/validado.

O teste com corretora foi exclusivamente readonly; nenhum novo teste manual de ordem, conversão ou fill foi enviado. O teste de entrada/stop/alvo e prazo M5 usou corretora falsa isolada. A carteira conjunta precisa de validação adicional caso apareçam candidatos elegíveis: joint-paper-gate.json é obrigatório antes de habilitar M5.

O supervisor único preserva H1 com --lesson-h1. Mercado fechado: espera e não arma. Ao reabrir, exige conta DU única, flat, nenhuma ordem, caixa/execuções e cadeias íntegros, LIVE nos três pares e WHATIF aceitos antes de iniciar o laboratório existente. Nunca remove STOP/kill, limpa reservas ou repete ordem ambígua. O processo depende de computador e Gateway ativos. Os PIDs/sessões registrados não constituem garantia futura.

Limites preservados: 5 entradas/dia, 3 posições, risco 0,1%, perda diária 1%, drawdown 5%, SELL financiado em EUR, máximo 5000 por entrada, BUY somente fechamento. Funding, âncora e custos históricos não foram reiniciados.
