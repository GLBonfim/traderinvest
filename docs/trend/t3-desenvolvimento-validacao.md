# T3 — Desenvolvimento e validação: condição de paragem atingida

Data: 14/11/2026 · Ramo: `feature/multi-asset-trend` · Desenho v2, seções 8.3, 11, 12 e 17

## Conclusão primeiro

**A regra que aprovou na T0 manda parar o trabalho aqui.** Nenhum valor da grelha de capital
(500, 2 000, 5 000, 10 000 e 25 000 €) fica abaixo de 0,50% de custo de negociação por ano.
Mesmo o maior, 25 000 €, custa 1,84% ao ano.

A sua decisão 3 da T0 diz: "Se nenhum valor da grelha ficar abaixo de 0,5%, para e reporta."
O desenho (8.3) acrescenta que não há capital de recurso.

Por isso:
- **Não há capital de referência.** As seis regras de aprovação não podem ser avaliadas como
  foram pré-registadas.
- **Não há T4.** O teste selado **não foi reivindicado nem aberto** e fica fechado.
- **Não criei nenhuma variante** da estratégia, como manda o protocolo. Qualquer ideia nova
  precisaria de um desenho novo, decidido por si.

## Custo de negociação por ano (a regra que decide)

Estratégia, custos realistas, em % do capital médio por ano:

| Capital | Desenvolvimento (2009–2019) | Validação (2020–2023) | **Os dois juntos** |
|---|---|---|---|
| 500 € | 24,48% | 10,53% | **18,43%** |
| 2 000 € | 15,07% | 10,19% | **13,46%** |
| 5 000 € | 7,89% | 6,66% | **7,56%** |
| 10 000 € | 3,94% | 3,89% | **3,93%** |
| 25 000 € | 1,77% | 2,09% | **1,84%** |
| Limite aprovado | | | **0,50%** |

"Os dois juntos" soma os custos dos dois períodos e divide pelo capital médio vezes os anos de
cada período.

## Porque os custos são tão altos

Confirmei que é o comportamento real da regra pré-registada, e não um erro do código:

- **Muitas ordens:** com 25 000 €, a estratégia fez 1 117 ordens em 10,2 anos de
  desenvolvimento (cerca de 109 por ano) e 442 em 3,4 anos de validação (cerca de 130 por ano).
- **De onde vêm:** quase todas são entradas e saídas quando o preço cruza a média de 200 dias.
  No desenvolvimento houve 500 entradas, 489 saídas e só 128 ajustes de peso. Muitas vezes o
  preço cruza a média para cima e para baixo em poucos dias. Um exemplo, com a dívida pública da
  zona euro em 2011: entrada a 1/12, saída a 2/12 e nova entrada a 5/12.
- **Ordens pequenas:** cada ativo pesa no máximo 1/11 da carteira. Com 25 000 €, a ordem
  mediana vale cerca de 1 250 a 1 600 €.
- **Comissão mínima:** a 0,05%, uma ordem de 1 500 € pagaria 0,75 €. A comissão mínima é 3 €,
  por isso **todas as ordens pagaram os 3 € mínimos**. Isso dá cerca de 0,2% por ordem, a que se
  somam o spread e a derrapagem.
- **Capitais pequenos:** o problema é muito maior. Com 2 000 € no desenvolvimento, as comissões
  levaram o capital de 2 000 € para 351 €.

O registo de todas as ordens, com o motivo, o estado, a volatilidade, os pesos e os custos,
está em `data/trend/results/t3/trades_<período>_<capital>.csv` (fora do git).

## Resultados completos, só para informação

A regra de paragem já decide. Estes números não mudam isso nem servem para escolher nada. Mostro
os dois capitais grandes; os outros estão no arquivo de resultados.

**Desenvolvimento, 08/10/2009 a 31/12/2019:**

| Carteira (capital) | Cenário | Retorno anual | Sharpe | Queda máxima | Ordens | Custo por ano |
|---|---|---|---|---|---|---|
| Estratégia (25 000 €) | sem custos | 2,99% | 0,82 | 9,5% | 1 120 | 0,00% |
| Estratégia (25 000 €) | **realista** | **1,05%** | **0,35** | 16,1% | 1 117 | 1,77% |
| Estratégia (25 000 €) | custos a dobrar | −1,04% | −0,16 | 24,2% | 1 124 | 3,78% |
| Estratégia (5 000 €) | realista | −5,56% | −1,41 | 48,7% | 1 037 | 7,89% |
| Pesos iguais (25 000 €) | realista | 6,29% | 0,79 | 18,6% | 874 | 0,75% |
| 60/40 (25 000 €) | realista | 8,85% | 0,93 | 15,3% | 21 | 0,03% |
| Caixa em EUR | — | −0,31% | — | 3,8% | 0 | — |

**Validação, 02/01/2020 a 31/05/2023:**

| Carteira (capital) | Cenário | Retorno anual | Sharpe | Queda máxima | Ordens | Custo por ano |
|---|---|---|---|---|---|---|
| Estratégia (25 000 €) | sem custos | 1,32% | 0,44 | 6,5% | 440 | 0,00% |
| Estratégia (25 000 €) | **realista** | **−0,82%** | **−0,12** | 8,2% | 442 | 2,09% |
| Estratégia (25 000 €) | custos a dobrar | −3,05% | −0,71 | 12,0% | 444 | 4,30% |
| Estratégia (5 000 €) | realista | −5,67% | −1,63 | 19,3% | 323 | 6,66% |
| Pesos iguais (25 000 €) | realista | 1,75% | 0,23 | 22,3% | 242 | 0,87% |
| 60/40 (25 000 €) | realista | 2,49% | 0,26 | 21,5% | 7 | 0,07% |
| Caixa em EUR | — | −0,28% | — | 2,1% | 0 | — |

**Notas:**
- **Caixa negativo:** o retorno do caixa é negativo porque o €STR (e a EONIA) estiveram abaixo de
  zero durante anos, e o protocolo tira mais 0,25 p.p. A regra real da IBKR, registada na T1, é
  outra: não paga juros sobre os primeiros 10 000 €. Não verifiquei se cobrava juros negativos
  nesses anos.
- **Sharpe do caixa:** não tem significado (é o caixa comparado consigo mesmo), por isso aparece
  "—".
- **Exposição média da estratégia:** com 25 000 €, esteve investida em cerca de 52% no
  desenvolvimento e 38% na validação. O resto ficou em caixa.

**Incerteza, só para informação.** Bootstrap por blocos, estratégia realista com 25 000 €:
- **Limite inferior unilateral a 95% do Sharpe:** −0,18 no desenvolvimento e −1,08 na validação.
  Os dois estão abaixo de zero.
- **Diferença de Sharpe para os pesos iguais:** de −0,81 a −0,09 no desenvolvimento (a 95%); de
  −1,17 a +0,34 na validação.

**As seis regras, só para informação** (não há capital de referência). Com 25 000 €, a
estratégia cumpre:
- **no desenvolvimento**, a 1 (bate o caixa), a 3 (queda máxima) e a 6 (concentração do lucro);
  falha a 2 (Sharpe), a 4 (custos a dobrar) e a 5 (subperíodos);
- **na validação**, só a 3.

## Rastreabilidade

- **Manifesto:** `d945583a…`. A fronteira do teste selado foi gravada no commit `214d892` antes
  da primeira simulação.
- **Código:** HEAD `214d892`, mais alterações ainda sem commit. São dois arquivos novos
  (`app/trend/research.py` e o seu teste) e três modificados (`cli.py`, com o comando `t3`;
  `metrics.py`, que passa a guardar o capital médio; e `guard.py`, que grava os arquivos com fim
  de linha LF). O hash dessas alterações está no próprio resultado: `4253c105…`.
- **Resultados:** `data/trend/results/t3/results.json` (fora do git) e o resumo
  `docs/trend/t3/summary.json`. Cada simulação tem o seu hash de reprodução.
- **Avisos de dados:** nenhum ativo ficou sem volatilidade utilizável em nenhuma simulação.

## O que acontece agora

Pelas regras fixadas antes de qualquer resultado:
- **O trabalho para.** O teste selado fica fechado e não é gasto.
- **Proibido mexer:** nenhuma regra, parâmetro ou ativo muda depois destes resultados, como
  pediu.
- **Variantes:** o protocolo proíbe criar variantes para salvar o resultado.
- **A decisão seguinte é sua.** Pode encerrar esta linha de investigação, ou abrir um desenho
  novo do zero, com o seu próprio registo prévio.
