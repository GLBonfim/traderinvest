"""User-facing text (Portuguese). Centralised so the UI can be translated later; keys are
stable identifiers, values are the displayed text."""

PT: dict[str, str] = {
    "window_title": "TraderInvest FX — Centro de Operações",
    "env_sim": "SIMULAÇÃO · DADOS SINTÉTICOS · SEM DINHEIRO REAL",
    "env_demo": "DEMO · CONTA DE PRÁTICA · SEM DINHEIRO REAL",
    "engine_starting": "A iniciar o motor…",
    "engine_connected": "Motor ligado",
    "engine_disconnected": "Motor desligado",
    "engine_stopped": "Motor parado",
    "engine_error": "Erro do motor",
    "pause": "❚❚ Pausar",
    "resume": "▶ Retomar",
    "kill": "■ KILL SWITCH",
    "kill_release": "Libertar kill switch",
    "flatten": "Fechar todas as posições",
    "speed": "Velocidade",
    "tab_market": "Mercado",
    "tab_portfolio": "Carteira",
    "tab_risk": "Risco",
    "tab_agents": "Agentes",
    "tab_orders": "Ordens",
    "tab_log": "Registo",
    "sim_clock": "Relógio simulado",
    "trading_day": "Dia FX",
    "sessions": "Sessões",
    "market_closed": "Mercado fechado",
    "score_note": "A pontuação é uma confiança heurística entre 0 e 1. NÃO é uma probabilidade "
    "de lucro.",
    "close_title": "Sair do TraderInvest FX",
    "close_body": "Ao fechar a janela o motor é parado de forma ordenada: o estado é guardado e "
    "as posições abertas mantêm-se na conta simulada com os seus stops, para serem "
    "recuperadas no próximo arranque.\n\nPretende sair?",
    "disclaimer_title": "Aviso importante",
    "disclaimer_body": "O TraderInvest FX é software de simulação e investigação.\n\n"
    "• Nesta versão não há execução com dinheiro real.\n"
    "• Os preços em modo SIMULAÇÃO são gerados localmente e NÃO são cotações de mercado.\n"
    "• Nada nesta aplicação é aconselhamento financeiro ou recomendação de investimento.\n"
    "• Resultados simulados não garantem resultados futuros. A negociação de Forex com "
    "alavancagem pode causar perdas superiores ao capital investido.\n\n"
    "Ao continuar confirma que compreende estes pontos.",
    "disclaimer_accept": "Compreendo e aceito",
    "disclaimer_reject": "Sair",
}


def t(key: str) -> str:
    return PT.get(key, key)
