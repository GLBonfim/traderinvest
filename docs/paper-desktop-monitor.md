# Acompanhar os agentes no TraderInvest FX

A tela inicial **Conta demo • agentes** acompanha o laboratório IBKR existente. A leitura atualiza a cada dois segundos; o laboratório registra ciclos aproximadamente a cada minuto e analisa velas H1 fechadas.

- **Visão geral:** resultado líquido estimado em euros somente dos agentes, contagem de operações abertas/encerradas, função e atividade dos três especialistas. As tabelas separam posições em andamento do histórico encerrado, com entrada, saída e motivo.
- **Por que operar ou esperar?:** padrões observados, contexto e motivos para aguardar ou vetar uma entrada. Uma forma de vela não é uma ordem executada.
- **Detalhes e testes:** execuções confirmadas, testes de integração separados dos agentes, conversões de preparação de caixa e diário técnico.
- **Banca e valor da ordem:** orçamento do experimento e tamanho automático ou fixo da ordem. Consulte [Banca e valor da ordem](paper-budget-settings.md). A interface envia um pedido local; somente o laboratório pode confirmar sua aplicação, sem posições/pendências e após reconciliar a conta.

Sem dados recentes ou sem confirmar a integridade do diário, a tela não mostra os agentes como ativos. Valores antigos são identificados como históricos. Totais em euros vêm de todo o diário verificado; a tabela detalhada é limitada às últimas 500 execuções/encerramentos. Testes e preparação de caixa não entram no resultado dos agentes. O horário do histórico é o do registro local, que pode ser posterior à execução durante uma desconexão.

O programa não inicia outro laboratório e a interface não envia ordens à corretora. Fechar a janela não para os agentes: o computador e o Gateway precisam continuar ligados. A aba **Simulação local • sem corretora** usa um ambiente fictício separado e só inicia seu motor ao ser selecionada.

Defina a pasta de dados em `paper-monitor.json` ao lado do executável, com a chave `lab_dir`, ou use **Configurar fonte…** e selecione a pasta com `status.json` e `pilot.sqlite`. O monitor lê cópias privadas do diário e WAL; não modifica o diário de origem. As configurações de banca são pedidos locais separados e auditados pelo laboratório.

Mantenha o executável junto de `_internal/` e dos demais arquivos do pacote. Não copie apenas o `.exe`. Atalhos de versões anteriores precisam ser atualizados para apontar ao novo pacote.
