# Pesquisa M5: meta de dois especialistas e INDEX

Pedido humano de 26/10/2026: manter pesquisa M5, buscar pelo menos dois especialistas qualificados e incluir todos os indices com dados disponiveis.

A meta nao altera gates de aprovacao. Resultados negativos, rodadas encerradas e fontes congeladas sao preservados. Treinos nao se repetem em dados identicos para perseguir um resultado positivo. Holdout2026 continua reservado.

## Forex: event-uniqueness-v1

Hipotese: alvos de 96 barras sobrepostos redundam o mesmo movimento. Cada evento de treino purgado recebe a media da inversa da concorrencia dos horizontes elegiveis do mesmo par; normaliza-se a media em1 e mantem-se peso da especialidade5/1. Avaliacao/custos/limites/51features/thresholds nao mudam. Seis candidatos depth3/5;15modelos previstos incluindo finais apenas de estudo. Protocolo94cb3c6394a072cfbac58222b873776f680219c0a4a3bd512cd2c81e36bb620a gravado antes dos dados.

## INDEX

Pesquisa separada dos pares Forex. Descoberta de contratos IND com catalogo multi-regiao e consultas de simbolos no provedor. Disponivel significa resposta de barras M5 nativas validas; contrato encontrado nao garante permissao de dados. O universo descoberto nao e promessa de todo indice existente. Ausencias e erros permanecem no manifesto.

IND e referencia de mercado, nao ativo executavel. Estudos direcionais em IND nao medem lucro negociavel. ETF, CFD ou futuro nao substituem silenciosamente IND. Para aprovacao de um produto negociavel ainda sao necessarios contrato/tick/multiplicador/calendario/custos e bid/ask apropriados, validacao temporal e carteira protegida. Nao reutilizar tamanho EUR/pip/comissoes de Forex em indices.

Coleta e estudo sao somente leitura, sem ordens, sem promover modelo e sem alterar o H1. O executor H1 existente, caixa, anchor, limites e testes manuais encerrados permanecem preservados.

Referencia do provedor para barras historicas: https://www.interactivebrokers.com/docs/tws-api/doc/md/historical-bars
