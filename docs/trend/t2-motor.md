# T2 — O motor de simulação

Data: 14/11/2026 · Ramo: `feature/multi-asset-trend` · Desenho: v2, seções 6 a 13 e 19

## Resumo em poucas linhas

- **O motor está construído e testado.** Calcula o sinal, decide os pesos, simula as ordens, os
  custos e os juros em euros, e calcula as métricas, as seis regras de aprovação e o veredicto.
- **A estratégia ainda não correu sobre dados reais.** Não calculei nenhum sinal, peso, ordem,
  retorno ou resultado. Isso é a T3. Os comandos que tocaram no histórico real estão listados,
  um a um, na seção "Que dados foram usados na T2".
- **68 testes passam**, e o ruff e o mypy estão limpos no pacote e nos testes. Entre os testes
  estão o caso calculado à mão, a causalidade e a reprodução exata.
- O manifesto foi emendado mais uma vez, com os **preços âncora** dos produtos (ver abaixo).
- Há **8 escolhas de implementação** que precisam da sua aprovação (seção 19 do desenho).

## Antes da T2: o merge com o main

- **Merge:** fundi `origin/main` no ramo (merge, não rebase). Os conflitos em `CHANGELOG.md`,
  `docs/decisions.md` e no desenho de 4 de novembro ficaram com as duas versões.
- **Verificação:** os testes da tendência passaram e o `verify` terminou sem erro. Depois fiz o
  commit e o push.
- **Testes do resto do projeto:** 1 teste do FX falha (`test_m1_config_is_simulation_only`).
  Também falha no próprio `origin/main`, sem as minhas mudanças, por isso não vem do merge.
  Como `app/fx` está congelado, não lhe toquei.

## Como o motor funciona, num dia de mercado

Em cada dia de mercado (um dia em que pelo menos um ativo negocia), por esta ordem:

1. **Juros:** o caixa recebe os juros desde o dia anterior, à taxa conhecida nesse dia.
2. **Preços:** cada ativo que negociou hoje passa para o fecho de hoje. Um ativo em feriado fica
   com o último fecho.
3. **Execuções:** as ordens decididas antes executam-se ao **fecho de hoje** do ativo. As vendas
   vêm antes das compras, e uma compra é cortada para unidades inteiras se o caixa não chegar.
4. **Contas:** o valor da carteira é o caixa mais as posições. O código confirma todos os dias
   que a variação bate certo com ganhos, juros e custos.
5. **Decisão às 23:00 de Lisboa:** com os fechos até hoje, o motor calcula o estado de cada
   ativo e o peso-alvo. Decide uma ordem se o estado mudou, ou se o peso se afastou mais de 25%
   do alvo. A ordem fica à espera do próximo fecho desse ativo.

## Preços âncora: porque o manifesto mudou outra vez

O desenho (6.4) pede que as quantidades sejam unidades inteiras do **produto real**, ao preço
real. A 500 € isso conta muito: uma unidade de SXR8 custava cerca de 93 € em 2010.

- **Ativos com série americana (6):** não tinha o preço do produto europeu, por isso descarreguei
  as séries da Xetra desses produtos. Só se usa um preço de cada, o do **dia âncora**.
- **Dia âncora:** o primeiro fecho do produto a partir de 08/10/2009. Para as séries próprias é o
  último dia até 08/10/2009.
- **Onde caem:** todos os dias âncora estão no período de desenvolvimento, entre 2009 e 2018. Os
  dados da validação e do teste selado não entram nesta conta.
- **Manifesto:** emendado de `7276157c…` para `d945583a…`. Nenhum arquivo antigo mudou, e a
  divisão 60/20/20 é a mesma.

| Ativo | Produto | Dia âncora | Preço (EUR) |
|---|---|---|---|
| US_EQ | SXR8 | 19/05/2010 | 92,67 |
| PACXJ_EQ | SXR1 | 04/05/2010 | 91,58 |
| US_TSY | IUSM | 08/10/2009 | 116,87 |
| GOLD | 8PSG | 08/10/2009 | 104,41 |
| COMMOD | SXRS | 03/05/2018 | 4,53 |
| GLOBAL_EQ (60/40) | IUSQ | 21/10/2011 | 21,37 |
| Séries próprias | EXSA, IQQJ, IQQE, XGLE, IBCS, IQQ6 | 08/10/2009 | do próprio arquivo |

## As 8 escolhas que precisam da sua aprovação

O desenho não as fixava ao pormenor. Escolhi a opção mais simples e mais prudente, sem ver
nenhum resultado. O texto técnico está na seção 19 do desenho.

1. **As mesmas decisões em todos os cenários de custo.** O sinal e o peso vêm sempre da série
   realista. Os cenários "custo zero" e "custo a dobrar" mudam só os custos e o TER. Assim a
   diferença entre cenários mostra só o efeito dos custos.
2. **Preços âncora,** como descritos acima.
3. **Histórico carregado:** cada simulação lê os 201 dias antes do início avaliável, para
   aquecer a média e a volatilidade. Os resultados não dependem disso.
4. **Dias de reequilíbrio dos benchmarks:** o "último dia do mês" (ou do ano) vem do calendário
   oficial das bolsas, publicado com antecedência. Se faltar esse dia nos dados, o reequilíbrio
   passa para o primeiro dia do período seguinte.
5. **Compras sem caixa suficiente:** são cortadas para as unidades que o caixa paga, e o resto
   não fica pendente. Uma compra que não chega a uma unidade é anulada e fica registada.
6. **Ativo com dados parados:** se a volatilidade não puder ser calculada, por exemplo com mais
   de 5 dias seguidos sem variação, esse ativo não negocia nesse dia e a simulação fica
   assinalada.
7. **Anualização do Sharpe:** usa o número real de dias de mercado por ano em cada período.
8. **Arquivos do guarda do teste selado:** ficam em `data/trend/guard/`, fora do git. O recibo
   da reivindicação vai no relatório final da T4.

## O que os testes cobrem (seção 13 do desenho)

| Tema | O que se testa |
|---|---|
| **Causalidade** | Alterar, apagar ou acrescentar dados depois de um dia T não muda nada até T: contas, decisões, ordens e execuções. Testado em 3 datas e 4 variantes |
| **Próximo fecho** | Fins de semana, feriados de um só ativo, calendários diferentes, decisão sem fecho futuro (fica pendente), ordem pendente não duplicada |
| **Sinal** | Com 199 fechos o estado é "dados insuficientes"; com 200 já há sinal. Preço igual à média dá CAIXA. "Dados insuficientes" nunca negoceia |
| **Pesos e quantidades** | Limite de volatilidade; soma dos pesos ≤ 1; desvio de 25% exatos não negoceia; unidades inteiras; ordens de quantidade zero não saem; vendas antes de compras; caixa nunca negativo |
| **Contas** | As identidades contabilísticas em todos os dias. Um caso de 2 ativos e 5 dias calculado à mão, com entrada, reequilíbrio, saída num feriado, comissão mínima de 3 € e juros |
| **Câmbio e juros** | Câmbio do BCE com mais de 4 dias úteis de atraso faz parar. Passagem EONIA → €STR com o ajuste certo. Juros ACT/360 com a taxa do dia anterior |
| **Horários (a sua condição b)** | Para todos os dias de 2008 a 2026, os fechos dos EUA e da Xetra e o câmbio do BCE acontecem antes das 23:00 de Lisboa |
| **Reprodução** | Duas simulações iguais dão saídas idênticas byte a byte |
| **Guarda do teste selado** | Recusa sem fronteira, recusa uma segunda reivindicação, recusa um manifesto diferente, e uma falha a meio conta como teste gasto. O leitor de dados recusa as linhas seladas sem reivindicação |
| **Regras e veredicto** | Cada uma das 6 regras falha sozinha quando deve. O bootstrap usa sempre a mesma semente |

## Que dados foram usados na T2 (confirmação por escrito)

**Testes: só dados sintéticos.** Os 68 testes usam séries inventadas dentro do próprio teste.
Nenhum teste lê um arquivo de preços, câmbio ou juros: a pasta de dados nos testes é sempre uma
pasta temporária vazia. Três testes leem os registros versionados no git, sem dados de mercado:
- `universe.json`, para os custos e as classes;
- `manifest.json`, só para testar que o leitor **recusa** as linhas seladas antes de abrir
  qualquer arquivo.

**Comandos que correram sobre o histórico real durante a T2.** Nenhum calculou sinal, média de
200 dias, volatilidade, peso, ordem ou retorno da estratégia ou dos benchmarks.

1. **Verificar que os 6 produtos existem no Yahoo** (um script `yfinance` de uma linha), antes
   de qualquer download.
   - **Mostrado:** para SXR8.DE, SXR1.DE, IUSM.DE, 8PSG.DE, SXRS.DE e IUSQ.DE, a moeda, o nome,
     a data do primeiro dia e o número de linhas.
   - **Não mostrado:** preços.
2. **`uv run python -m app.trend.cli fetch --snapshot 2026-11-14 --add-missing`**
   - **O que fez:** descarregou as 6 séries para arquivos novos. Nenhum arquivo antigo foi
     tocado.
   - **Mostrado:** os nomes dos 6 arquivos acrescentados.
3. **`uv run python -m app.trend.cli amend --date 2026-11-14 --reaudit-snapshot 2026-11-14 --reason "..."`**,
   seguido de `verify` e de um pequeno script que imprimiu a emenda.
   - **Correu:** a auditoria de qualidade da T1 sobre todo o histórico, até 13/11/2026. São as
     mesmas verificações já aprovadas: duplicados, falhas, ajuste de dividendos, câmbio e saltos
     acima de 15%.
   - **Também construiu:** os níveis de retorno total em EUR de cada ativo **só até
     31/12/2019** (desenvolvimento), para calcular os preços âncora.
   - **Mostrado:** os hashes do manifesto, "hashes OK", os arquivos acrescentados e, por ativo,
     o símbolo, o dia âncora e o **preço de fecho do produto nesse dia** (a tabela acima). Também
     a divisão 60/20/20, igual à anterior.
   - **Nota honesta:** o manifesto guarda o `unit_scale` de cada ativo. É o preço âncora a
     dividir pelo nível de retorno total no dia âncora. Desse número pode deduzir-se o retorno
     acumulado do ativo entre 08/10/2009 e o dia âncora, e esses dias são todos de 2009 a 2018,
     dentro do desenvolvimento. Não olhei para esse retorno nem o mostrei.
4. **Confirmação de carga** (um script que chamou `build_market` até 31/05/2023 e depois tentou
   o teste selado).
   - **O que fez:** construiu na memória os níveis em EUR de todos os ativos até ao fim da
     validação.
   - **Mostrado:** só contagens e datas: 3728 dias de mercado de 22/12/2008 a 31/05/2023, o
     número de dias por ativo, 6252 taxas de juro, e a recusa `GuardError: sealed rows need a
     sealed claim` na tentativa de ler o teste selado.
   - **Não mostrado nem calculado:** níveis, retornos, sinais.

**Antes da T2, ainda na segunda ronda da T1,** correram mais dois comandos sobre dados reais.
Registo-os para ficar completo:
- **A auditoria dos novos IQQJ e IQQE.** Mostrou as linhas, as diferenças de calendário, a
  verificação dos dividendos, a ausência de saltos acima de 15% e o maior preço parado.
- **Uma contagem de dias sem volume.** Para IQQJ, IQQE e EXSA, mostrou o número de dias por ano.
  Para o IQQJ em 2026 mostrou também que o preço mudou em todos os 18 dias sem volume e que o
  volume mediano é de 17 451 unidades.
- **Período selado:** esta contagem usou linhas de 2026, que pertencem ao período selado. Mostrou
  volumes e se o preço mudou ou não, mas nunca a direção nem o tamanho de qualquer movimento.

**Estado da emenda dos preços âncora:** está no `manifest.json` deste commit **como proposta**,
pendente da sua revisão. Se a rejeitar, uma nova emenda desfaz a mudança, e a cadeia de hashes
guarda as duas.

## Próximo passo (T3), depois da sua aprovação

- **O que corre:** desenvolvimento e validação, nos 3 cenários de custo e nos 5 capitais, e os 3
  benchmarks.
- **O relatório:** em português, com métricas, custos, a grelha de capitais e a explicação ordem
  a ordem.
- **O teste selado:** fica fechado. O guarda cria a fronteira antes da primeira simulação.

## Estado

Este relatório, o código, os testes, a seção 19 do desenho e a emenda dos preços âncora estão
no git como **proposta**. As 8 escolhas e a emenda ficam pendentes da sua revisão. A T3 não
começou.
