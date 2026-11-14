# T1 — Universo, auditoria de dados e manifesto

Data: 14/11/2026 · Ramo: `feature/multi-asset-trend` · Desenho: v2 (ADR-0040)

## Resumo em poucas linhas

- **A auditoria passou.** Os dados gratuitos chegam: há **17,1 anos** de histórico avaliável em
  comum (o mínimo exigido era 15).
- **Universo: 11 ativos**, todos negociáveis na Xetra, em euros, na IBKR Irlanda.
- **Divisão fixa** (gravada no manifesto antes de qualquer resultado):

| Período | De | Até | Duração aproximada |
|---|---|---|---|
| Desenvolvimento (60%) | 08/10/2009 | 31/12/2019 | 10,2 anos |
| Validação (20%) | 02/01/2020 | 31/05/2023 | 3,4 anos |
| Teste selado (20%) | 01/06/2023 | 13/11/2026 | 3,5 anos |

- **Nenhum sinal, média móvel ou retorno da estratégia foi calculado.** Só foram feitas
  verificações de qualidade dos dados.
- Manifesto congelado: `app/trend/registry/manifest.json`, hash original
  `93d4e4097b534405c1e0940807c59d8d06cbee46a24d118f2b3ff01400474055`. Uma segunda tentativa de
  congelar é recusada, e o comando `verify` recalcula o hash de todos os arquivos.
- Depois das suas decisões, o manifesto foi **emendado**, não refeito. O novo hash é
  `5f50f2aec2622ecb0323538268c83266f8c620c38fe322a6167b7e72bb6e82cc`. Veja a seção "Decisões
  do dono".

## O universo e por que cada ativo está lá

A escolha seguiu só critérios econômicos e práticos (região, tipo de risco, liquidez, histórico
gratuito), nunca o desempenho passado. Antes de fixar a lista, vi apenas as datas de início e o
número de linhas de cada série.

| Ativo | O que representa | Por que entra | Produto real (Xetra, EUR) | TER |
|---|---|---|---|---|
| US_EQ | Ações dos EUA | Maior mercado do mundo; motor próprio (lucros dos EUA, dólar) | SXR8 · IE00B5BMR087 | 0,07% |
| EUROPE_EQ | Ações europeias | Região do investidor; mistura de setores diferente dos EUA | EXSA · DE0002635307 | 0,20% |
| JAPAN_EQ | Ações do Japão | Economia, banco central e moeda (iene) próprios | EUNN · IE00B4L5YX21 | 0,12% |
| PACXJ_EQ | Pacífico sem Japão | Austrália, Hong Kong, Singapura: ligados a matérias-primas e à China | SXR1 · IE00B52MJY50 | 0,20% |
| EM_EQ | Mercados emergentes | Crescimento, política e moedas diferentes dos desenvolvidos | IS3N · IE00BKM4GZ66 | 0,18% |
| EZ_GOV | Dívida pública da zona euro | Ativo defensivo principal em euros, sem risco cambial | XGLE · LU0290355717 | 0,07% |
| US_TSY | Títulos do Tesouro dos EUA, 7 a 10 anos | Ciclo de juros do Fed e dólar; costuma proteger em crises de ações | IUSM · IE00B1FZS798 | 0,07% |
| EUR_IG | Dívida de empresas em euros (grau de investimento) | Risco de crédito, diferente do risco de juros | IBCS · IE0032523478 | 0,09% |
| GOLD | Ouro físico | Não paga rendimento; reage a juros reais, ao dólar e a crises | 8PSG · IE00B579F325 (ETC) | 0,12% |
| COMMOD | Matérias-primas diversificadas | Energia, metais e agricultura: inflação e choques de oferta | SXRS · IE00BDFL4P12 | 0,19% |
| GLOBAL_RE | Imobiliário cotado, países desenvolvidos | Rendas e sensibilidade a juros, com ciclo próprio | IQQ6 · IE00B1FZS350 | 0,59% |

Observações:
- **Ouro:** o produto é um ETC (título com lastro em ouro físico), e não um fundo UCITS. A lei
  não permite um UCITS de um único metal.
- **Matérias-primas:** o produto é sintético. Ele replica o índice por meio de um swap, um
  contrato com bancos (Deutsche Bank e J.P. Morgan).
- **Distribuição:** EXSA, IUSM, IBCS e IQQ6 distribuem dividendos. Os outros acumulam (reinvestem
  os dividendos dentro do fundo). A série de retorno total trata os dois casos da mesma forma.

### Que série de preços se usa para cada ativo (regra mecânica)

Quando o histórico do próprio produto europeu começa até 31/12/2008, usa-se esse histórico.
Caso contrário, usa-se o ETF americano que segue o mesmo índice. Pela sua decisão de
14/11/2026, se ele não existir, o ativo sai da lista; nunca se usa um índice parecido.

| Usam a série do próprio produto (em EUR) | Usam um substituto americano (em USD, convertido para EUR) |
|---|---|
| EXSA, XGLE, IBCS, IQQ6 | SPY, EWJ, EPP, EEM, IEF, GLD, DJP |

O ativo que define o início do período é o imobiliário (IQQ6): a série dele começa em 23/12/2008
e precisa de 200 sessões de aquecimento. Por isso, o primeiro dia avaliável é 08/10/2009.

### Benchmarks (carteiras de comparação)

- **Pesos iguais:** 1/11 em cada ativo, sempre investido, reequilibrado todo mês.
- **60/40 global:** 60% em ações mundiais (substituto ACWI; produto IUSQ, IE00B6R52259) e 40% em
  dívida pública da zona euro (o mesmo XGLE). É a carteira equilibrada típica de quem investe em
  euros.
- **Caixa em EUR:** a taxa €STR menos 0,25 ponto percentual.

## Fontes de dados (todas gratuitas)

| Dado | Fonte | Cobertura |
|---|---|---|
| Preços diários e dividendos | Yahoo Finance, via a biblioteca `yfinance` 1.7.0 | Do início de cada série até 13/11/2026 |
| Câmbio USD/EUR | BCE, taxa de referência diária (publicada por volta das 16h de Frankfurt) | Desde 1999 |
| Taxa de caixa | BCE: EONIA até 30/09/2019, €STR a partir de 01/10/2019 | Sem buracos |

- **Onde ficam:** os arquivos brutos estão em `data/trend/raw/2026-11-14/`, fora do git, como
  manda a regra do repositório. O manifesto guarda o hash SHA-256 de cada um.
- **Proteção:** o código recusa sobrescrever um arquivo baixado.
- **Stooq:** testei esta fonte como segunda opção, mas hoje ela bloqueia downloads automáticos.

## O que a auditoria verificou e encontrou

| Verificação | Resultado |
|---|---|
| Datas duplicadas ou fora de ordem | Nenhuma |
| Preços em falta, zero ou negativos | Nenhum |
| Ajuste de dividendos do Yahoo | Coincide com "fecho + dividendos" até 0,0002% em todas as séries |
| Preços parados (mesmo fecho repetido) | No máximo 4 dias seguidos (o limite de alarme é 5) |
| Câmbio para as séries em dólar | Nunca mais de 2 dias úteis de atraso (o limite é 4) |
| Cadeia de taxas de caixa EONIA → €STR | Contínua, com no máximo 3 dias úteis entre publicações |
| Saltos diários acima de 15% | Só em outubro de 2008 (EWJ, EPP, EEM): crise real, fora do período avaliável |
| Calendário da Xetra versus linhas do Yahoo | 3 a 6 dias de diferença por série europeia (ex.: 3 de outubro de 2011 a 2013) |

**Regras fixadas para as diferenças de calendário.** As sessões de cada ativo são as linhas do
fornecedor. Se o calendário oficial diz que houve sessão mas o Yahoo não tem a linha, o movimento
desse dia aparece no dia seguinte, e o nível de retorno total não muda.

**Volume zero.** As séries europeias têm cerca de 255 linhas sem volume em 2008, porque o Yahoo
não registrou volumes nesse ano. Os preços variam, por isso não são dados parados, e 2008 fica
dentro do aquecimento.

**Convenção de retorno total.** O retorno de cada dia é (fecho + dividendo) ÷ fecho anterior. É a
conta econômica correta para quem detém o título. O método do Yahoo difere no máximo 0,09% num dia
de dividendo grande. A série ajustada do Yahoo serve só para conferir os dados.

## Custos (valores e data: 14/11/2026)

| Item | Valor usado | Grau de certeza |
|---|---|---|
| Comissão IBKR Pro Fixed, Xetra | 0,05% do valor, mínimo de 3 € por ordem, sem máximo | **Confirmada pelo dono** no preçário da IBKR Irlanda (14/11/2026) |
| Alternativa não usada: IBKR Tiered | 0,05%, mínimo de 1,25 €, máximo de 29 €, mais taxas da bolsa | Fontes secundárias |
| Spread (metade, por lado) + derrapagem | Classe A: 0,03% + 0,02% · Classe B: 0,05% + 0,03% · Classe C: 0,10% + 0,05% | **Estimativas**, não medições |
| TER | O de cada produto (tabela acima), sem contagem dupla | Fichas dos produtos (justETF, 14/11/2026) |
| Câmbio na compra | Nenhum: todos os produtos negociam em EUR | — |

- **Classes de liquidez:**
  - A: SXR8, EXSA, IS3N.
  - B: EUNN, SXR1, XGLE, IUSM, IBCS, 8PSG.
  - C: SXRS, IQQ6 e IUSQ (este só no benchmark).
- **Spreads:** não encontrei fonte gratuita legível. A Deutsche Börse mostra os spreads só por
  JavaScript. Os valores usados são conservadores para fundos deste tamanho.
- **Juros reais da IBKR:** segundo um retrato de fonte secundária, a IBKR não paga juros sobre os
  primeiros 10 000 € e paga a taxa de referência menos 0,5 p.p. acima disso. O protocolo usa
  €STR − 0,25 p.p. para todos os capitais, e esta diferença vai aparecer ao lado dos resultados.

## Limitações declaradas desde já

1. **Horário dos substitutos americanos:** negociam no horário dos EUA, e os produtos reais
   negociam no horário europeu. A conversão usa a taxa do BCE, publicada antes do fecho americano.
2. **Dividendos dos substitutos americanos:** são brutos. Os fundos irlandeses pagam 15% de
   retenção sobre dividendos dos EUA. Isso fica sem ajuste, para não criar um parâmetro novo; a
   diferença é de cerca de 0,2% ao ano nas ações dos EUA.
3. **DJP:** é uma nota de dívida (ETN) do banco Barclays e serve só como histórico do índice
   Bloomberg Commodity.
4. **Spreads constantes:** entre 2009 e 2012 os spreads reais foram provavelmente maiores.
5. **Teste selado curto:** cerca de 3,5 anos. A incerteza estatística vai ser grande.
6. **Conhecimento prévio:** o dono e eu conhecemos a história dos mercados. A escolha do universo
   está protegida só pelo procedimento (critérios escritos e registro antes dos dados).

## Decisões do dono (14/11/2026), registradas antes de qualquer resultado

1. **Comissão:** fica o plano Fixed (0,05%, mínimo de 3 €), confirmado por você no preçário da
   IBKR Irlanda. A fonte e a data estão no registro e no manifesto.
2. **Spreads e derrapagem:** estimativas aprovadas. A classe de cada ativo está no manifesto
   (`costs.asset_tiers`). Na fase demo medem-se os valores reais.
3. **60/40:** aprovada, com reequilíbrio **uma vez por ano**, início em caixa e os mesmos custos
   dos outros benchmarks.
4. **Regra das séries:** aprovada com três condições:
   - (a) sem um ETF americano do **mesmo** índice, o ativo sai da lista. O código verifica isso
     sozinho e recusa simular enquanto houver uma violação.
   - (b) sinal e câmbio só usam valores conhecidos às 23:00 de Lisboa. O fecho dos EUA (20:00 ou
     21:00 em Lisboa) é posterior ao europeu, mas ainda anterior às 23:00, mesmo nas semanas em
     que a mudança de hora não coincide. A T2 vai ter um teste para isto.
   - (c) a série americana já desconta o TER do ETF americano. Por isso soma-se de volta esse TER
     e desconta-se o do produto europeu. Os TER americanos estão no registro: SPY 0,0945%, EWJ
     0,49%, EPP 0,47%, EEM 0,70% (as fontes dão de 0,68% a 0,72%), IEF 0,15%, GLD 0,40%, DJP
     0,75% e ACWI 0,32%. Ficam constantes ao longo do histórico, uma limitação declarada.

**Como o manifesto foi atualizado.** Pediu as decisões *no* manifesto, por isso o arquivo
mudou e o hash também. Só foram acrescentadas regras e custos. Os arquivos de dados, a janela, a
divisão 60/20/20, a auditoria e os parâmetros ficaram iguais, e o código recusa uma emenda que
os altere. O novo manifesto guarda o hash do anterior (`93d4e409…` → `5f50f2ae…`). Assim
qualquer pessoa pode conferir o que mudou.

## Revisão da lista: o que a condição (a) encontrou

Ao aplicar a condição (a), dois ativos **não a cumprem**:

| Ativo | Série americana | Produto europeu | Problema |
|---|---|---|---|
| JAPAN_EQ | EWJ: MSCI Japan | EUNN: MSCI Japan **IMI** | O IMI inclui empresas pequenas. São índices diferentes |
| EM_EQ | EEM: MSCI Emerging Markets | IS3N: MSCI Emerging Markets **IMI** | Idem. O ETF americano do índice IMI (IEMG) só existe desde 2012, tarde demais |

Excluir os dois deixaria 9 ativos, abaixo do mínimo de 10 do desenho. Existe uma saída que
cumpre a regra à letra, sem trocar de índice: escolher o produto europeu do índice **sem** IMI.

| Ativo | Produto alternativo (Xetra, EUR) | Índice | TER | Série no Yahoo desde |
|---|---|---|---|---|
| JAPAN_EQ | IQQJ · iShares MSCI Japan (Dist), IE00B02KXH56 | MSCI Japan | 0,12% | 02/01/2008 |
| EM_EQ | IQQE · iShares MSCI EM (Dist), IE00B0M63177 | MSCI Emerging Markets | 0,18% | 02/01/2008 |

Pela regra das séries, os dois passariam a usar o histórico **do próprio produto**, em euros.
Deixariam de precisar do ETF americano, do ajuste de TER e do cuidado com o horário dos EUA.

- **Custo da troca:** acrescentar dois arquivos aos dados, auditar de novo e emendar o
  manifesto.
- **O que pode mudar:** a janela de datas não muda, porque o ativo que define o início continua
  a ser o imobiliário.
- **O que vi destes produtos:** só a data de início e o número de linhas. Não vi preços nem
  retornos.

Um terceiro ponto para decidir:
- **COMMOD:** o único histórico americano longo do índice Bloomberg Commodity TR é o **DJP**, uma
  nota de dívida do Barclays (ETN) e não um ETF. O índice é exatamente o mesmo do produto
  europeu (SXRS). Os ETF americanos deste índice só existem desde 2017–2018. Se "ETF americano"
  for para ler à letra, as matérias-primas saem e ficam 10 ativos.

## O que preciso de você

1. **JAPAN_EQ e EM_EQ:** trocar para IQQJ e IQQE (a minha recomendação), ou excluir os dois?
   Excluir deixa a lista abaixo do mínimo.
2. **COMMOD:** aceitar o DJP (um ETN) como histórico do mesmo índice, ou excluir as
   matérias-primas?
3. **O resto da lista:** aprovar.

## Arquivos desta fase

- `app/trend/registry/universe.json`: o universo pré-registrado.
- `app/trend/registry/audit.json`: o relatório completo da auditoria.
- `app/trend/registry/manifest.json`: o manifesto congelado.
- `app/trend/{universe,sources,loaders,audit,manifest,cli}.py`: o código; 22 testes em
  `tests/unit/trend/`.
- Comandos:
  - `uv run python -m app.trend.cli verify` confere os hashes e lista as violações da condição
    (a). Hoje termina com erro de propósito, por causa de JAPAN_EQ e EM_EQ.
  - `fetch` e `audit` servem para reproduzir os dados.
  - `amend` regista mudanças de regras antes de qualquer resultado.
