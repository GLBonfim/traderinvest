# TraderInvest FX — preparação para venda / subscrição

> Documento de planeamento para o proprietário. **Não é aconselhamento jurídico, fiscal ou
> regulatório.** Os pontos marcados ⚖ precisam de validação por um advogado especializado em
> mercado de capitais (e, quando aplicável, de contacto com a CMVM) antes de qualquer venda.

## 1. Estado atual (versão 0.1.0)

| Área | Estado |
|---|---|
| Aplicação desktop nativa (janela própria, ecrã inteiro, ícone, `.exe`) | ✅ pronta (PySide6) |
| Sala de operações isométrica, 5 agentes, orquestrador, risco central | ✅ pronta |
| Regras diárias (5 entradas, −5 %, +15 %), kill switch, persistência e recuperação | ✅ prontas e testadas |
| Ambiente SIMULAÇÃO (mercado sintético + corretor simulado) | ✅ |
| Ambiente DEMO (conta de prática de um corretor) | ❌ por fazer (precisa de escolher corretor) |
| Ambiente REAL | 🔒 bloqueado no código (ADR-0006) |
| Instalador, assinatura de código, atualizações automáticas | ❌ por fazer |
| Licenciamento / subscrição | ❌ por fazer (proposta abaixo) |
| Validação estatística dos agentes em dados reais | ❌ por fazer — **obrigatória antes de qualquer alegação de desempenho** |

## 2. Regulação (UE / Portugal) ⚖

- **Recomendações personalizadas** de investimento são *consultoria para investimento*
  (MiFID II), atividade que exige autorização (CMVM). **Gerir contas de clientes** ou executar
  ordens em nome deles é *gestão de carteiras* / *receção e transmissão de ordens*, também
  reguladas.
- O modelo de menor risco regulatório é vender **software** genérico: o cliente instala-o no seu
  computador, liga **a sua própria conta** num corretor autorizado, configura as regras e é
  responsável pelas decisões. O vendedor não vê nem opera contas de clientes, não dá sinais
  personalizados e não promete rendimentos.
- Mesmo assim, a execução automática por agentes pode ser vista como serviço de investimento
  consoante o desenho e o marketing. **Validar com advogado antes do lançamento.**
- **Marketing**: proibido prometer lucros; resultados simulados ou passados têm de vir com
  avisos claros; avisos de risco de produtos alavancados (Forex/CFD) em todo o material.
- **Consumidores**: direito de livre resolução de 14 dias em conteúdos digitais (com as
  exceções legais), termos e condições, política de privacidade (RGPD) se houver contas online,
  telemetria ou licenciamento com dados pessoais.
- **IVA**: vendas digitais B2C na UE seguem a regra do país do cliente (regime OSS). Um
  *merchant of record* (ver §5) trata disto pelo vendedor.

## 3. Dados de mercado

- **yfinance / Yahoo** não pode ser usado comercialmente (termos de uso). O produto FX não o usa.
- Modelo recomendado: **"traga o seu corretor"** — preços e execução vêm da conta do próprio
  cliente, ao abrigo do contrato dele com o corretor. Evita redistribuir dados de terceiros.
- Fornecedores de dados pagos (ex.: feeds FX institucionais) exigem licença de
  **redistribuição** para mostrar dados a clientes; confirmar sempre por escrito.
- Candidatos para o ambiente DEMO (a avaliar: disponibilidade para residentes na UE/Portugal,
  termos da API para uso por software de terceiros, conta de prática gratuita): OANDA (API v20
  practice), IG (REST/Streaming, demo), Interactive Brokers (TWS/Client Portal, paper),
  cTrader Open API (corretores Spotware). **Nenhuma conta foi criada e nenhuma credencial usada.**

## 4. Licenças de software

- **Qt / PySide6 — LGPL v3**: uso comercial permitido com ligação dinâmica (as DLLs ficam
  ficheiros separados na pasta da aplicação), aviso de licença e acesso ao código-fonte do Qt.
  Já incluído: `packaging/THIRD_PARTY_NOTICES.md`. Não modificar o Qt (ou publicar as
  modificações). Alternativa: licença comercial do Qt.
- numpy / pandas (BSD), pydantic (MIT), tzdata (Apache) e o bootloader do PyInstaller (GPL
  com exceção) permitem distribuição comercial.
- **Arte**: toda gerada por código próprio; a imagem de referência do pedido original **não**
  foi copiada. Se no futuro se usar arte de terceiros, guardar as licenças.
- Código Python empacotado com PyInstaller é fácil de descompilar. Para proteger a lógica:
  compilar os módulos centrais com **Nuitka**/Cython e manter valor no serviço (ver §5).

## 5. Modelo de subscrição (proposta técnica)

1. **Merchant of record** (Paddle, Lemon Squeezy ou FastSpring): checkout, faturação, IVA OSS,
   reembolsos, subscrições mensais/anuais.
2. **Licenças**: chave assinada (Ed25519) com plano, validade e identificador do cliente,
   verificada offline na aplicação com a chave pública embutida; renovação online periódica
   (ex.: a cada 7 dias, com período de tolerância). Serviços como Keygen podem gerir isto.
   Nunca guardar segredos de assinatura na aplicação.
3. **Edições** (exemplo): *Simulação* (gratuita, só mercado sintético); *Pro* (conta DEMO do
   corretor do cliente, histórico, relatórios); execução real só depois de enquadramento
   legal e de um ADR próprio.
4. **Atualizações**: canal de atualizações com manifesto assinado; o instalador verifica a
   assinatura antes de aplicar.
5. **Telemetria / relatórios de erro**: apenas com consentimento explícito (opt-in), sem dados
   de contas.

## 6. Distribuição no Windows

- **Assinatura de código**: sem ela o Windows SmartScreen avisa "aplicação desconhecida".
  Opções: certificado OV/EV de uma autoridade certificadora, ou Azure Trusted Signing.
- **Instalador**: Inno Setup ou MSIX a partir de `dist/TraderInvestFX` (atalho no menu
  Iniciar, desinstalação limpa, dados do utilizador em `%LOCALAPPDATA%\TraderInvestFX`).
- Testar numa VM Windows limpa (sem Python instalado) antes de cada versão.

## 7. Próximos passos recomendados (por ordem)

1. Escolher o corretor para o ambiente DEMO e implementar o adaptador (com testes sobre
   respostas gravadas, sem credenciais no repositório).
2. Recolher dados FX reais (do corretor) e validar os agentes com as ferramentas estatísticas
   já existentes no projeto; ajustar ou retirar agentes sem evidência.
3. Consulta jurídica (§2) e decisão do modelo de negócio.
4. Licenciamento + merchant of record + assinatura de código + instalador.
5. Versão inglesa da interface (os textos já estão centralizados em `app/desktop/strings.py`).
