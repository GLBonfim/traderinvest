# Pesquisa de velas e modelos — 18/10/2026

Escopo: pesquisa selecionada e rastreável; não representa toda a internet nem prova de rentabilidade. Definições públicas viram atributos mensuráveis. Resultados são retrospectivos, porque o histórico já foi consultado.

## Fontes e aplicação

- TA-Lib, catálogo e implementação: https://ta-lib.org/functions/ ; exemplo Hammer: https://ta-lib.org/functions/cdlhammer . Usado como referência de taxonomia e distinção entre forma e contexto. O Hammer da TA-Lib não verifica a tendência anterior. Nossos limiares são próprios e documentados em candlestick-engine.md; não alegamos equivalência numérica com a TA-Lib.
- Lo, Mamaysky e Wang (2000), Foundations of Technical Analysis: https://www.nber.org/papers/w7613 . Referência para transformar formas gráficas em hipóteses computáveis. O estudo em ações não demonstra lucro em FX neste projeto.
- CMT Program Guide 2026: https://cmtassociation.org/wp-content/uploads/2025/12/CMT-PROGRAM-GUIDE-2026-1.pdf . Referência de famílias de reversão, continuação e contexto; material curricular não substitui validação.
- Dynamic Deep Convolutional Candlestick Learner: https://arxiv.org/abs/2201.08669 . Modelo visual catalogado para pesquisa futura; CNN não implementada nesta rodada. Reconhecer padrões e obter retorno líquido são objetivos diferentes.
- ExtraTreesRegressor: https://scikit-learn.org/stable/modules/generated/sklearn.ensemble.ExtraTreesRegressor.html . Modelo não linear comparado com Ridge e regressão logística. Árvores rasas exportadas como números JSON; sem pickle na execução.

- Lu, Chen e Hsu (2015), versão dos autores: https://www.econ.sinica.edu.tw/~econ/pdfPaper/14-A010.pdf . Em ações do DJIA, diferentes regras de saída produziram conclusões distintas para os mesmos tipos de padrão, considerando custos e múltiplas comparações. Isso fundamenta avaliar a estratégia completa sem transportar taxas de acerto para FX. Nesta rodada, a saída permanece fixa (2 ATR de stop, 3 ATR de alvo, até 8 velas); não se otimiza a saída após observar o resultado.

## Implementado no treinamento

20 detectores nativos: hammer, hanging man, inverted hammer, shooting star, engulfing bullish/bearish, morning/evening star, tweezer bottom/top, three white soldiers/black crows, rising/falling three methods, inside/outside bar, doji, spinning top, marubozu e long body.

Cada padrão entra como qualidade geométrica entre 0 e 1 quando a forma e o contexto exigido estão presentes; zero caso contrário. Qualidade geométrica não é probabilidade de lucro. A tendência é medida antes da primeira vela do padrão. Entram também corpo, pavios, posição do fechamento e tendência anterior, combinados com momentum, médias, RSI, volatilidade e sessão. Total: 35 atributos.

FX não tem volume negociado centralizado no histórico usado. O adaptador fornece zero apenas para satisfazer o formato OHLCV; nenhum atributo de volume é treinado. Não inventamos volume.

## Comparação e substituição

Treino com expansão temporal, separação de rótulos e embargo; avaliação em 2023, 2024 e 2025 com bid/ask, custos e estresse adicional. Busca finita registrada antes da execução. Comparar retorno líquido, amostra, profit factor e consistência temporal com NO TRADE e os agentes antigos. Preservar todas as tentativas anteriores, inclusive negativas. Nenhum resultado retrospectivo altera a aprovação estatística dos agentes.

Somente candidatos que atendem aos critérios podem ser selecionados para piloto paper. Queda realizada de 5 R ou deterioração após 20 operações aciona retirada e substituição por candidato elegível não retirado. Sem substituto: NO TRADE. Conta real continua bloqueada.

## Catálogo ampliado para futuras hipóteses

O catálogo público TA-Lib consultado lista 61 funções CDL. Referências abaixo são nomes de funções, não implementações adicionadas nem modelos aprovados:

CDL2CROWS, CDL3BLACKCROWS, CDL3INSIDE, CDL3LINESTRIKE, CDL3OUTSIDE, CDL3STARSINSOUTH, CDL3WHITESOLDIERS, CDLABANDONEDBABY, CDLADVANCEBLOCK, CDLBELTHOLD, CDLBREAKAWAY, CDLCLOSINGMARUBOZU, CDLCONCEALBABYSWALL, CDLCOUNTERATTACK, CDLDARKCLOUDCOVER, CDLDOJI, CDLDOJISTAR, CDLDRAGONFLYDOJI, CDLENGULFING, CDLEVENINGDOJISTAR, CDLEVENINGSTAR, CDLGAPSIDESIDEWHITE, CDLGRAVESTONEDOJI, CDLHAMMER, CDLHANGINGMAN, CDLHARAMI, CDLHARAMICROSS, CDLHIGHWAVE, CDLHIKKAKE, CDLHIKKAKEMOD, CDLHOMINGPIGEON, CDLIDENTICAL3CROWS, CDLINNECK, CDLINVERTEDHAMMER, CDLKICKING, CDLKICKINGBYLENGTH, CDLLADDERBOTTOM, CDLLONGLEGGEDDOJI, CDLLONGLINE, CDLMARUBOZU, CDLMATCHINGLOW, CDLMATHOLD, CDLMORNINGDOJISTAR, CDLMORNINGSTAR, CDLONNECK, CDLPIERCING, CDLRICKSHAWMAN, CDLRISEFALL3METHODS, CDLSEPARATINGLINES, CDLSHOOTINGSTAR, CDLSHORTLINE, CDLSPINNINGTOP, CDLSTALLEDPATTERN, CDLSTICKSANDWICH, CDLTAKURI, CDLTASUKIGAP, CDLTHRUSTING, CDLTRISTAR, CDLUNIQUE3RIVER, CDLUPSIDEGAP2CROWS, CDLXSIDEGAP3METHODS.

Para incluir outros padrões, declarar critérios e nova rodada antes de avaliar; controlar a quantidade de tentativas e conservar resultados. Padrões que dependem de gaps de abertura de ações precisam de hipótese explícita antes de adaptação ao FX contínuo. Não ampliar a busca indefinidamente até obter um vencedor aparente.
