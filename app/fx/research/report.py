"""`python -m app.fx.research.report` — writes docs/fx-agent-validation.md from the study outputs
(data/fx/research/{calibration,preregistration,holdout,system}.json). Numbers only; the raw data
stays under data/ (not versioned)."""

import json
from pathlib import Path
from typing import Any

from app.fx.research.study import OUT_DIR, PERIODS, RULE

DOC = Path("docs/fx-agent-validation.md")
NAMES = {"trend": "Tendência", "momentum": "Momentum", "mean_reversion": "Reversão à média"}


def _load(name: str) -> dict[str, Any] | None:
    p = OUT_DIR / f"{name}.json"
    return json.loads(p.read_text()) if p.exists() else None


def _row(label: str, s: dict[str, Any]) -> str:
    pf = s.get("profit_factor", 0.0)
    pf_txt = "∞" if pf == float("inf") else f"{pf:.2f}"
    return (
        f"| {label} | {s.get('trades', 0):.0f} | {s.get('mean_r', 0):+.3f} | "
        f"{s.get('win_rate', 0):.0%} | {pf_txt} | {s.get('total_r', 0):+.1f} | "
        f"{s.get('max_dd_r', 0):.1f} | {s.get('mean_cost_r', 0):.3f} |"
    )


HEADER = (
    "| | Operações | Média R líquida | Acerto | Profit factor | Total R | DD máx (R) "
    "| Custo médio (R) |\n|---|---|---|---|---|---|---|---|"
)


def build() -> str:
    cal, reg = _load("calibration"), _load("preregistration")
    ho = _load("holdout")
    sysr = _load("system_pre_breaker") or _load("system")
    explo = _load("system_post_breaker_exploratory")
    if cal is None or reg is None:
        raise SystemExit("run `python -m app.fx.research.study calibrate` first")
    tf = reg["timeframe"]
    lines = [
        "# Validação dos agentes FX v2 (IBKR, histórico real)",
        "",
        "> Gerado por `python -m app.fx.research.report`. Investigação apenas: nenhuma ordem foi "
        "enviada. Resultados passados não garantem resultados futuros.",
        "",
        "## Dados e método",
        "",
        "- Fonte: IBKR (TWS API, sessão paper, só leitura), Forex IDEALPRO, velas M15 BID e ASK, "
        "9 pares, ~6 anos; spread mínimo conservador por par aplicado a todas as velas "
        "(as velas BID/ASK da IBKR fecham muitas vezes com spread 0).",
        f"- Períodos (hora de entrada, UTC): desenvolvimento {PERIODS['development'][0]} → "
        f"{PERIODS['development'][1]}; validação {PERIODS['validation'][0]} → "
        f"{PERIODS['validation'][1]}; teste final {PERIODS['holdout'][0]} → "
        f"{PERIODS['holdout'][1]} (avaliado uma única vez).",
        "- Execução: entrada na abertura da vela seguinte ao preço real (ask/bid); stop e alvo "
        "no lado que fecha a posição; gap fecha à abertura; stop e alvo na mesma vela = stop; "
        "saída por tempo e antes do fim de semana; uma posição por par.",
        "- Custos em R (múltiplos do risco inicial, 0,5 % de 10 000 €): spread real, comissão "
        "max(2 USD, 0,2 bps) por lado, financiamento 0,5 bps por noite.",
        f"- Horizonte escolhido para o sistema (só com dados de desenvolvimento): **{tf}** "
        f"(pontuações: {cal['dev_score_by_timeframe']}).",
        f"- Regra de aprovação pré-registada (sha256 `{reg['sha256'][:16]}…`, criada "
        f"{reg['created_at'][:19]}Z antes do teste final): {RULE}.",
        "",
    ]
    agents = cal["by_timeframe"][tf]["agents"]
    lines += ["## Calibração (desenvolvimento) e validação", ""]
    for a, v in agents.items():
        lines += [
            f"### {NAMES[a]}",
            "",
            f"Parâmetros escolhidos: `{v['chosen']}` (de {len(v['grid'])} combinações; "
            "seleção pela estatística t suavizada pelos vizinhos da grelha).",
            "",
            HEADER,
            _row("Desenvolvimento", v["dev"]),
            _row("Validação", v["validation"]),
            _row("Validação sem filtro de regime", v["validation_without_regime_filter"]),
            "",
        ]
    if ho:
        lines += ["## Teste final (uma única avaliação)", "", HEADER]
        for a, v in ho["agents"].items():
            lines.append(_row(NAMES[a], v["holdout"]))
        lines += [
            "",
            "| Agente | p (bootstrap) | p Holm | Aleatório p90 (R) | Custos agravados (R) | "
            "Aprovado |",
            "|---|---|---|---|---|---|",
        ]
        for a, v in ho["agents"].items():
            failed = [k for k, ok in v["checks"].items() if not ok]
            verdict = "**SIM**" if v["approved"] else f"não ({', '.join(failed)})"
            lines.append(
                f"| {NAMES[a]} | {v['p_one_sided']:.3f} | {v['p_holm']:.3f} | "
                f"{v['random_baseline_mean_r_p90']:+.3f} | "
                f"{v['holdout_stressed_costs']['mean_r']:+.3f} | {verdict} |"
            )
        lines += ["", "Por par (média R líquida no teste final):", ""]
        pairs = sorted({p for v in ho["agents"].values() for p in v["by_pair"]})
        lines.append("| Par | " + " | ".join(NAMES[a] for a in ho["agents"]) + " |")
        lines.append("|---" * (len(ho["agents"]) + 1) + "|")
        for p in pairs:
            cells = []
            for v in ho["agents"].values():
                s = v["by_pair"].get(p)
                cells.append(f"{s['mean_r']:+.3f} ({s['trades']:.0f})" if s else "—")
            lines.append(f"| {p} | " + " | ".join(cells) + " |")
        lines.append("")
    table_head = [
        "| Corrida | Retorno | DD máx | Entradas | Máx. entradas/dia | Bloqueios diários "
        "| Kill switch | Comissões | Cadeia íntegra |",
        "|---|---|---|---|---|---|---|---|---|",
    ]

    def sys_row(label: str, r: dict[str, Any]) -> str:
        kills = r.get("journal_events", {}).get("kill_switch", 0)
        return (
            f"| {label} | {r['return_pct']:+.2f} % | {r['max_drawdown_pct']:.2f} % | "
            f"{r['entries_filled']} | {r['max_entries_one_day']} | {len(r['risk_locks'])} | "
            f"{kills} | {r['commissions_total']:.2f} € | {r['journal_chain_ok']} |"
        )

    if sysr:
        lines += [
            "## Sistema completo no teste final (orquestrador, coordenador, risco)",
            "",
            "Corridas com o código usado no teste final (antes do travão de drawdown):",
            "",
            *table_head,
            sys_row("Só agentes aprovados (produto)", sysr["approved_only"]),
            sys_row(
                "Diagnóstico: todos os agentes, incluindo reprovados",
                sysr["diagnostic_all_agents"],
            ),
            "",
            "O diagnóstico mostra que os limites diários (-5 % / +15 %, 5 entradas) não travam "
            "uma perda lenta e contínua: um só bloqueio diário num ano. Por isso foi acrescentado "
            "depois um travão de drawdown máximo (20 % abaixo do pico, ativa o kill switch). "
            "O travão bloqueia NOVAS entradas; não fecha posições abertas nem garante que a perda "
            "pare nesse nível (gaps, slippage).",
            "",
        ]
    if explo:
        lines += [
            "### Exploratório pós-observação (com o travão de drawdown)",
            "",
            "Corrida feita DEPOIS de conhecer o resultado acima, só para verificar o mecanismo; "
            "não é uma validação independente e não repete o teste final dos agentes.",
            "",
            *table_head,
            sys_row("Diagnóstico com travão: todos os agentes", explo["diagnostic_all_agents"]),
            "",
        ]
    if ho:
        approved = [NAMES[a] for a, v in ho["agents"].items() if v["approved"]]
        verdict = (
            "Agentes aprovados: " + ", ".join(approved)
            if approved
            else "**Nenhum agente passou a regra pré-registada.** Os agentes continuam a analisar "
            'o mercado e a mostrar propostas ("em observação"), mas o produto não abre '
            "posições com eles. Em SIMULAÇÃO, o modo de demonstração (Ficheiro → Configuração) "
            "permite ver agentes não aprovados a operar preços sintéticos, sempre identificados "
            "como tal."
        )
        lines += [
            "## Conclusão",
            "",
            verdict,
            "",
            "Estes resultados dizem respeito a estes padrões técnicos em M15/H1 com os custos de "
            "uma conta de 10 000 €; não dizem nada sobre outras estratégias. Qualquer nova "
            "estratégia tem de passar o mesmo protocolo antes de ser aprovada.",
            "",
        ]
    return "\n".join(lines) + "\n"


def main() -> int:
    DOC.write_text(build(), encoding="utf-8")
    print(f"written {DOC}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
