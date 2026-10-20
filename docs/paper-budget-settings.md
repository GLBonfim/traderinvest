# Banca e valor da ordem — somente demo

Abra **Conta demo • agentes → Banca e valor da ordem**.

1. Escolha a **banca de referência**. Esse é o orçamento do experimento, separado do saldo fictício da corretora. Faixa atual: EUR 100 a EUR 10.000.
2. Para escolher o tamanho de cada ordem, marque **Usar valor fixo por entrada** e selecione o valor. Desmarcado, o sistema mantém o cálculo automático por risco.
3. Clique **Aplicar na conta demo**. Aguarde a mensagem **Aplicado** e confira a última configuração registrada. Pedido enviado não significa aplicado.

As ordens suportadas nesta configuração são de EUR 1.000 a EUR 5.000, em passos de EUR 1.000. Uma banca pequena pode não comportar nem o mínimo: nesse caso a entrada é recusada, sem aumentar o risco para forçá-la. Não há promessa de que toda oportunidade resulte em ordem.

No modo fixo, o tamanho escolhido só é usado se couber no tamanho autorizado pelo gestor de risco. Caso contrário, a ordem é recusada. O sistema não aumenta a ordem nem afasta o stop para acomodar o valor.

A configuração só muda com estratégia sem posições nem pendências, conta reconciliada e nenhuma ordem aberta. Enquanto um pedido aguarda o encerramento de operações, novas entradas ficam suspensas; saídas continuam funcionando. Desconectado ou parado, o laboratório não confirma aplicação.

Os limites anteriores continuam: risco planejado de 0,1% por operação, máximo de 5 entradas por dia, 3 posições simultâneas, perda diária de 1% e drawdown de 5%. Custos, gaps e diferenças de execução podem aumentar a perda além do risco planejado.

Mudar a banca preserva os resultados acumulados e o histórico. A alteração do orçamento é registrada separadamente, sem ser contada como lucro. Os referenciais monetários de risco são deslocados pelo mesmo valor, preservando perdas em euros, contadores e bloqueios. Uma redução de banca pode acionar um limite percentual no ciclo seguinte. A configuração não libera bloqueios já acionados.

Esta atualização não executa ordens de teste, não move dinheiro e não habilita conta real.
