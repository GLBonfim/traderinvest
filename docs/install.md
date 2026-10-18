# Instalar TraderInvest FX no Windows

## Instalador

Use o ficheiro `TraderInvestFX-<versão>-setup.exe` fornecido pelo responsável
pelo projeto. A instalação é por utilizador e não requer privilégios de administrador.
Leia o aviso inicial, escolha a pasta e, se desejar, o atalho no ambiente de trabalho.
A pasta predefinida é `%LOCALAPPDATA%\Programs\TraderInvestFX`.
Abra **TraderInvest FX** pelo menu Iniciar ou pelo atalho escolhido.
Não é necessário instalar Python para usar a versão empacotada.

Se o Windows bloquear um pacote sem assinatura, contacte o responsável pela
versão para obter um pacote validado. Não desative proteções do Windows.

## Versão portátil

Extraia todo o ZIP `TraderInvestFX-<versão>-win64-portable.zip` para uma pasta
com permissão de escrita. Execute `TraderInvestFX\TraderInvestFX.exe`.
Mantenha a pasta `_internal` junto do executável; não copie apenas o `.exe`.

## Dados, atualização e remoção

Configuração, base de dados e registos ficam em `%LOCALAPPDATA%\TraderInvestFX`,
separados dos ficheiros do programa. Feche a aplicação e faça uma cópia dessa
pasta antes de atualizar. Instale a nova versão na mesma pasta do programa;
na distribuição portátil, extraia a nova versão para uma pasta própria.

Para desinstalar, use Aplicações instaladas do Windows. O instalador preserva
os dados e o diário de auditoria. Na versão portátil, a pasta do programa pode
ser removida após fechar a aplicação; guarde os dados se precisar do histórico.

A aplicação inicia em simulação. Para recolha IBKR paper, consulte
[fx-platform.md](fx-platform.md#ibkr-paper--phase-a-read-only). Credenciais
permanecem no Gateway/TWS. Operações reais estão bloqueadas.

## Demonstrar a interface

Os agentes atuais não passaram os critérios de validação estatística. Por padrão,
analisam o mercado sintético e mostram propostas, sem abrir posições.

Para demonstrar operações simuladas, abra **Ficheiro → Configuração…**, marque
**Demonstrar agentes NÃO APROVADOS na simulação** e guarde. O aviso explica que
essa opção usa apenas preços sintéticos; ela não comprova desempenho real.
Desmarque a opção e guarde para voltar à observação. Isso impede novas entradas
desses agentes; posições simuladas já abertas continuam sob as regras de gestão
existentes. A opção não reinicia a conta e não habilita negociação real.

## Produzir uma distribuição (manutenção)

Na raiz do repositório:

```powershell
uv sync --group build
uv run --group build python packaging/build.py --installer
```

O processo constrói e verifica o executável e gera o ZIP. Se Inno Setup 6 já
estiver instalado (ISCC no PATH ou numa pasta padrão), compila também o
instalador. Se não estiver, informa que esse passo foi omitido; o ZIP continua
disponível. Falhas de compilação de um ISCC encontrado interrompem o processo.
Para compilar só o instalador após um build validado:

```powershell
ISCC packaging\installer.iss
```

A versão vem de `app/desktop/__init__.py` (`APP_VERSION`), tanto para o
executável e ZIP como para o instalador. Os resultados ficam em `dist/`.
O build não assina os ficheiros. Antes de distribuir, validar numa máquina
Windows limpa, confirmar os avisos/licenças e assinar o pacote aprovado.
