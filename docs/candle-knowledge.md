# Base de conhecimento de velas — TraderInvestFX

30 padrões catalogados: 20 do motor atual e 10 adicionais em pesquisa. Cada registro traz nome, família, geometria, contexto prévio e fonte. Os textos foram sintetizados; não há cópia de livros, imagens ou cursos.

## Como o analista usa o conhecimento

O analista repassa aos especialistas o padrão observado, sua regra e referência. A nova explicação inclui também formas neutras e padrões cujo contexto anterior não foi atendido. Essas observações não viram ordens. O vetor de sinais, a classificação atual por especialista e os limites permanecem congelados. A atualização acrescenta metadados; não retreina Ridge, ExtraTrees ou regressão logística.

Reversão exige examinar a tendência antes da primeira vela do padrão. Continuação exige avaliar a direção vigente. Impulso descreve um deslocamento forte, não sua duração futura. Doji e pião não escolhem compra ou venda. Força geométrica não é taxa de acerto.

## Catálogo

| Identificador | Nome | Velas | Contexto prévio | Uso | Regra e fonte |
|---|---|---:|---|---|---|
| hammer | Martelo | 1 | down | detector atual | Pavio inferior longo, corpo no topo; queda prévia. [Fonte](https://www.ig.com/uk/ig-academy/the-basics-of-technical-analysis/candlestick-patterns) |
| hanging_man | Enforcado | 1 | up | detector atual | Forma do martelo após alta; contexto muda a identidade. [Fonte](https://www.ig.com/uk/ig-academy/the-basics-of-technical-analysis/candlestick-patterns) |
| inverted_hammer | Martelo invertido | 1 | down | detector atual | Pavio superior longo após queda. [Fonte](https://www.ig.com/uk/ig-academy/the-basics-of-technical-analysis/candlestick-patterns) |
| shooting_star | Estrela cadente | 1 | up | detector atual | Pavio superior longo após alta. [Fonte](https://www.ig.com/uk/ig-academy/the-basics-of-technical-analysis/candlestick-patterns) |
| bullish_engulfing | Engolfo de alta | 2 | down | detector atual | Corpo comprador maior envolve corpo vendedor, não necessariamente pavios. [Fonte](https://www.forex.com/en-us/trading-academy/courses/technical-analysis/key-candlestick-patterns/) |
| bearish_engulfing | Engolfo de baixa | 2 | up | detector atual | Corpo vendedor maior envolve corpo comprador; contexto de alta. [Fonte](https://www.forex.com/en-us/trading-academy/courses/technical-analysis/key-candlestick-patterns/) |
| morning_star | Estrela da manhã | 3 | down | detector atual | Três velas: queda, corpo pequeno, recuperação de metade do primeiro corpo. [Fonte](https://www.babypips.com/learn/forex/triple-candlestick-patterns) |
| evening_star | Estrela da tarde | 3 | up | detector atual | Três velas: alta, corpo pequeno, recuo de metade do primeiro corpo. [Fonte](https://www.babypips.com/learn/forex/triple-candlestick-patterns) |
| tweezer_bottom | Pinça de fundo | 2 | down | detector atual | Mínimas próximas com cores opostas após queda; tolerância local. [Fonte](https://www.forex.com/en-us/trading-academy/courses/technical-analysis/key-candlestick-patterns/) |
| tweezer_top | Pinça de topo | 2 | up | detector atual | Máximas próximas com cores opostas após alta; tolerância local. [Fonte](https://www.forex.com/en-us/trading-academy/courses/technical-analysis/key-candlestick-patterns/) |
| three_white_soldiers | Três soldados brancos | 3 | sem exigência direcional | detector atual | Três corpos compradores progressivos, abrindo dentro do anterior. [Fonte](https://ta-lib.org/functions/cdl3whitesoldiers) |
| three_black_crows | Três corvos negros | 3 | sem exigência direcional | detector atual | Três corpos vendedores progressivos, abrindo dentro do anterior. [Fonte](https://ta-lib.org/functions/cdl3blackcrows) |
| rising_three_methods | Três métodos de alta | 5 | sem exigência direcional | detector atual | Impulso, três velas menores contidas, retomada compradora. [Fonte](https://ta-lib.org/functions/) |
| falling_three_methods | Três métodos de baixa | 5 | sem exigência direcional | detector atual | Impulso, três velas menores contidas, retomada vendedora. [Fonte](https://ta-lib.org/functions/) |
| inside_bar | Barra interna | 2 | sem exigência direcional | detector atual | Amplitude totalmente contida na anterior; sem direção automática. [Fonte](https://www.forex.com/en-us/trading-academy/courses/technical-analysis/key-candlestick-patterns/) |
| outside_bar | Barra externa | 2 | sem exigência direcional | detector atual | Amplitude envolve a anterior; não equivale a engolfo de corpos. [Fonte](https://www.forex.com/en-us/trading-academy/courses/technical-analysis/key-candlestick-patterns/) |
| doji | Doji | 1 | sem exigência direcional | detector atual | Corpo muito pequeno; indecisão, sem direção automática. [Fonte](https://www.ig.com/uk/ig-academy/the-basics-of-technical-analysis/candlestick-patterns) |
| spinning_top | Pião | 1 | sem exigência direcional | detector atual | Corpo pequeno e dois pavios relevantes; indecisão. [Fonte](https://www.ig.com/uk/ig-academy/the-basics-of-technical-analysis/candlestick-patterns) |
| marubozu | Marubozu | 1 | sem exigência direcional | detector atual | Corpo ocupa quase toda a amplitude; impulso, não lucro garantido. [Fonte](https://ta-lib.org/functions/) |
| long_body | Corpo longo | 1 | sem exigência direcional | detector atual | Corpo grande na própria amplitude e ante os corpos anteriores. [Fonte](https://ta-lib.org/functions/) |
| bullish_harami | Harami de alta | 2 | down | pesquisa, sem ordens | Corpo longo seguido de corpo estritamente contido: pequeno, de cor oposta. [Fonte](https://ta-lib.org/functions/cdlharami) |
| bullish_harami_cross | Harami cruz de alta | 2 | down | pesquisa, sem ordens | Corpo longo seguido de corpo estritamente contido: doji. [Fonte](https://ta-lib.org/functions/cdlharamicross) |
| bearish_harami | Harami de baixa | 2 | up | pesquisa, sem ordens | Corpo longo seguido de corpo estritamente contido: pequeno, de cor oposta. [Fonte](https://ta-lib.org/functions/cdlharami) |
| bearish_harami_cross | Harami cruz de baixa | 2 | up | pesquisa, sem ordens | Corpo longo seguido de corpo estritamente contido: doji. [Fonte](https://ta-lib.org/functions/cdlharamicross) |
| piercing_line | Linha penetrante | 2 | down | pesquisa, sem ordens | Gap abaixo da mínima anterior e recuperação além do meio do corpo, sem engolfo. [Fonte](https://ta-lib.org/functions/cdlpiercing) |
| dark_cloud_cover | Nuvem negra | 2 | up | pesquisa, sem ordens | Gap acima da máxima anterior e recuo além do meio do corpo, sem engolfo. [Fonte](https://ta-lib.org/functions/cdldarkcloudcover) |
| dragonfly_doji | Doji libélula | 1 | sem exigência direcional | pesquisa, sem ordens | Doji com pavio inferior longo e superior curto; não autoriza direção. [Fonte](https://ta-lib.org/functions/cdldragonflydoji) |
| gravestone_doji | Doji lápide | 1 | sem exigência direcional | pesquisa, sem ordens | Doji com pavio superior longo e inferior curto; não autoriza direção. [Fonte](https://ta-lib.org/functions/cdlgravestonedoji) |
| morning_doji_star | Estrela da manhã doji | 3 | down | pesquisa, sem ordens | Variante local da estrela da manhã com doji central; admite contato de corpos. [Fonte](https://ta-lib.org/functions/cdlmorningdojistar) |
| evening_doji_star | Estrela da tarde doji | 3 | up | pesquisa, sem ordens | Variante local da estrela da tarde com doji central; admite contato de corpos. [Fonte](https://ta-lib.org/functions/cdleveningdojistar) |

## Regras locais verificáveis

Os limiares explícitos não são resultados de otimização nem reprodução numérica da TA-Lib. A própria biblioteca utiliza configurações de corpo e sombra relativas a médias; não se deve pressupor equivalência entre detectores. [TA-Lib: configurações](https://ta-lib.org/api/candle-settings/).

No motor atual: doji tem corpo até 10% da amplitude; pião até 30% e pavios de ao menos 25%; martelo tem pavio longo de ao menos 60%, curto até 10%, corpo até 30% e pavio longo de pelo menos dois corpos. Marubozu exige corpo de pelo menos 90% e pavios até 5%. Corpo longo exige pelo menos 60% da amplitude e 1,5 vezes a média dos 20 corpos anteriores. Engolfo envolve os corpos e exige corpo atual maior; barra externa envolve máximas e mínimas.

Na extensão de pesquisa: harami usa primeiro corpo >=50% da amplitude, segundo <=30%, contenção estrita e cor oposta. Harami cruz troca o segundo corpo por doji <=10%. Linha penetrante e nuvem negra mantêm gaps reais além da mínima/máxima anterior; não se afrouxam requisitos para gerar ocorrências em Forex. Estrelas doji usam a variante local de contato de corpos já existente nas estrelas comuns. Dojis libélula/lápide usam corpo <=10%, pavio longo >=60% e curto <=10%, permanecendo neutros.

Há divergências documentadas entre convenções: soldados e corvos são frequentemente apresentados como reversão após uma tendência; o laboratório os roteia na família continuação. Esse roteamento não foi alterado, nem se reclassificaram resultados históricos. A TA-Lib descreve ainda uma vela anterior adicional em corvos; o detector local usa as três velas da formação. [Soldados](https://ta-lib.org/functions/cdl3whitesoldiers), [corvos](https://ta-lib.org/functions/cdl3blackcrows).

## Tempo, contexto e invalidação

A pesquisa considera somente velas fechadas. Padrões com várias velas não podem atravessar intervalos ausentes. Suporte e resistência são a mínima e máxima de 20 velas anteriores ao início do padrão, nunca pivôs calculados com futuro. Esses níveis são observações, não filtros novos de execução. Volume Forex desconhecido fica zero como ausência de informação, sem ser usado para confirmar.

A confirmação é uma convenção nova de pesquisa: na vela seguinte fechada, o preço deve fechar além do envelope do padrão na direção indicada. Romper o extremo contrário invalida; cruzar ambos os extremos e fechar confirmando fica ambíguo, porque OHLC não revela a sequência intravela. Sem direção, o resultado é não direcional. Sem próxima vela fechada, fica pendente. O campo confirmation_known_at registra quando essa informação passou a existir. Essa confirmação não foi adicionada como novo filtro das ordens atuais.

## Auditoria descritiva em dados reais

Foram lidos 156 arquivos BID M5 genuínos da IBKR, com hashes conferidos, agregados em H1 apenas quando a hora continha as 12 barras M5. Dados de desenvolvimento de 2023; nenhum retorno futuro, otimização ou holdout foi avaliado. Amplitudes e corpos usam preços BID, sem reconstrução de máximas sintéticas de mid.

| Par | Horas completas | Observações de padrões, sobrepostas |
|---|---:|---:|
| EURUSD | 5928 | 5654 |
| EURGBP | 5928 | 5587 |
| EURJPY | 5928 | 5795 |

Uma mesma vela pode ter várias descrições; o total de observações não é total de operações. As contagens e confirmações geométricas não medem precisão, lucro ou aprovação estatística. Não foram escolhidos vencedores pela frequência.

A evidência empírica não autoriza presumir vantagem: um estudo de Marshall, Young e Rose não encontrou valor nas estratégias de velas testadas em ações do DJIA. Isso não é um teste do nosso Forex H1. Aqui serve para manter separadas descrição e validação de mercado. [Resumo do artigo](https://www.sciencedirect.com/science/article/abs/pii/S0378426605002116).

## Uso e validação

O catálogo JSON é consumível por software; as observações CSV trazem os padrões e explicações. O código de pesquisa não importa executores nem envia ordens. Os dez detectores novos não integram o registro de execução; exigem protocolo próprio, custos, dados temporais separados e avaliação prospectiva antes de serem candidatos a operar. Os pesos atuais ficam fixos, e os candidatos M5 reprovados continuam desativados.

A regressão em 36 janelas reais compara todos os campos de decisão atuais e as propostas dos três especialistas antes/depois da adição de explicações. O resultado está em regressao-decisoes.json. Testes sintéticos verificam formas, exemplos contrários, velas incompletas, contexto, gaps, suporte/resistência e causalidade; eles não constituem validação financeira.

Fontes adicionais revisadas: [CME](https://www.cmegroup.com/education/courses/trading-and-analysis/technical-analysis), [OANDA](https://www.oanda.com/us-en/skills-and-insights/education/technical-analysis/price-charts-and-candlesticks/chart-patterns-in-technical-analysis-a-comprehensive-guide/). A lista de URLs, consultadas em 19/10/2026, está em fontes.json.
