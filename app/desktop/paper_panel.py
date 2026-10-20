"""PAPER monitor and bounded budget requests; the UI never sends broker orders."""

import json
import uuid
from pathlib import Path
from typing import Any

from PySide6.QtCore import QSettings, QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from app.desktop import APP_ID, ORG_NAME
from app.desktop.panels import fill, money, price, table
from app.desktop.paper_monitor import discover_lab, pair_reason, read_monitor
from app.desktop.theme import P, mono_font


def cell(value: Any) -> tuple[str, str | None]:
    return str(value), None


class PaperLabPanel(QWidget):
    def __init__(self, root: Path | None = None) -> None:
        super().__init__()
        saved = QSettings(ORG_NAME, APP_ID).value("paper_lab_dir")
        self.root = root or (Path(str(saved)) if saved else discover_lab())
        self.snapshot: dict[str, Any] = {}
        self.budget_dirty = False
        self.budget_initialized = False
        self.budget_request_id: str | None = None
        layout = QVBoxLayout(self)
        top = QHBoxLayout()
        title = QLabel("Seus agentes na conta demo")
        title.setFont(mono_font(13, bold=True))
        title.setStyleSheet(f"color: {P.cyan}")
        top.addWidget(title, 1)
        refresh = QPushButton("Atualizar agora")
        refresh.clicked.connect(self.refresh)
        choose = QPushButton("Configurar fonte…")
        choose.clicked.connect(self.choose_root)
        top.addWidget(refresh)
        self.choose_button = choose
        choose.setToolTip("Selecionar a pasta de dados. Não conecta nem altera a corretora.")
        top.addWidget(choose)
        layout.addLayout(top)
        self.state = QLabel()
        self.state.setFont(mono_font(11, bold=True))
        layout.addWidget(self.state)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        cards = QHBoxLayout()
        self.kpis = {}
        for key, heading in (
            ("pnl", "Resultado dos agentes"),
            ("open", "Operações abertas"),
            ("closed", "Operações encerradas"),
        ):
            frame = QFrame()
            frame.setStyleSheet(f"QFrame {{background: {P.panel}; border-radius: 8px;}}")
            box = QVBoxLayout(frame)
            box.addWidget(QLabel(heading))
            value = QLabel("—")
            value.setFont(mono_font(22, bold=True))
            value.setStyleSheet("font-size: 24px; font-weight: bold;")
            box.addWidget(value)
            self.kpis[key] = value
            cards.addWidget(frame)
        layout.addLayout(cards)
        self.result_note = QLabel(
            "Resultado líquido estimado de operações encerradas. "
            "Testes e preparação de caixa ficam separados."
        )
        self.result_note.setWordWrap(True)
        layout.addWidget(self.result_note)
        self.agents = table(
            ["Agente", "O que está fazendo", "O que faz", "Encerradas", "Resultado líquido EUR"]
        )
        self.agents.setMinimumHeight(140)
        self.agents.setMaximumHeight(155)
        layout.addWidget(self.agents)
        self.tabs = QTabWidget()
        overview = QWidget()
        overview_layout = QVBoxLayout(overview)
        self.next_step = QLabel()
        self.next_step.setWordWrap(True)
        overview_layout.addWidget(self.next_step)
        overview_layout.addWidget(QLabel("Operações em andamento"))
        self.positions = table(
            [
                "Par",
                "Operação",
                "Quantidade EUR",
                "Preço de entrada",
                "Limite de perda (stop)",
                "Objetivo (alvo)",
                "Agente",
            ]
        )
        overview_layout.addWidget(self.positions, 1)
        overview_layout.addWidget(QLabel("Operações encerradas • somente agentes"))
        self.closed = table(
            [
                "Par",
                "Entrada",
                "Saída",
                "Motivo da saída",
                "Resultado líquido EUR",
                "Registro (hora local)",
            ]
        )
        overview_layout.addWidget(self.closed, 1)
        self.tabs.addTab(overview, "Visão geral")
        self.contexts = table(
            [
                "Par",
                "Última vela fechada (UTC)",
                "Regime",
                "Padrões observados",
                "Decisão / motivo",
            ]
        )
        for column in (3, 4):
            self.contexts.horizontalHeader().setSectionResizeMode(
                column, QHeaderView.ResizeMode.Stretch
            )
        self.tabs.addTab(self.contexts, "Por que operar ou esperar?")
        self.details = QTabWidget()
        self.orders = table(
            ["Origem", "Referência", "Par", "Lado", "Unidades", "Preço", "Confirmação"]
        )
        self.details.addTab(self.orders, "Execuções confirmadas")
        self.tests = table(["Teste", "Par", "Entrada", "Saída", "Líquido estimado EUR", "Estado"])
        self.details.addTab(self.tests, "Testes de integração")
        self.funding = table(["Par", "Unidades EUR", "Preço", "Estado", "Comissão EUR"])
        self.details.addTab(self.funding, "Preparação de caixa")
        self.events = QTextEdit()
        self.events.setReadOnly(True)
        self.details.addTab(self.events, "Diário e explicações")
        self.tabs.addTab(self.details, "Detalhes e testes")
        budget_page = QWidget()
        budget_layout = QVBoxLayout(budget_page)
        explanation = QLabel(
            "Defina o orçamento dos agentes, separado do saldo fictício da corretora. "
            "Os resultados acumulados continuam sendo somados à banca de referência. "
            "A alteração só é aplicada com a conta reconciliada e nenhuma operação ou ordem aberta."
        )
        explanation.setWordWrap(True)
        budget_layout.addWidget(explanation)
        form = QFormLayout()
        self.capital_input = QDoubleSpinBox()
        self.capital_input.setRange(100, 10000)
        self.capital_input.setDecimals(2)
        self.capital_input.setSingleStep(100)
        self.capital_input.setSuffix(" EUR")
        self.fixed_input = QCheckBox("Usar valor fixo por entrada")
        self.order_input = QDoubleSpinBox()
        self.order_input.setRange(1000, 5000)
        self.order_input.setDecimals(0)
        self.order_input.setSingleStep(1000)
        self.order_input.setSuffix(" EUR")
        self.order_input.setEnabled(False)
        self.fixed_input.toggled.connect(self.order_input.setEnabled)
        for signal in (
            self.capital_input.valueChanged,
            self.order_input.valueChanged,
            self.fixed_input.toggled,
        ):
            signal.connect(self.mark_budget_dirty)
        form.addRow("Banca de referência", self.capital_input)
        form.addRow("Modo da ordem", self.fixed_input)
        form.addRow("Valor da ordem", self.order_input)
        budget_layout.addLayout(form)
        help_text = QLabel(
            "Modo automático: o risco calcula o tamanho, limitado a EUR 5.000. "
            "Modo fixo: usa o valor escolhido ou recusa a entrada se não couber no risco. "
            "Ordens atuais: mínimo EUR 1.000, passos de EUR 1.000. Uma banca pequena pode "
            "não permitir entradas nesse mínimo. Risco planejado: 0,1% por operação; custos, "
            "diferenças de execução e gaps podem aumentar a perda. Limites: 5 entradas por dia, "
            "3 posições, perda diária de 1% e drawdown de 5%. Só vale para a demo."
        )
        help_text.setWordWrap(True)
        budget_layout.addWidget(help_text)
        self.budget_current = QLabel()
        self.budget_current.setWordWrap(True)
        budget_layout.addWidget(self.budget_current)
        self.budget_message = QLabel("Valores carregados da configuração atual.")
        self.budget_message.setWordWrap(True)
        budget_layout.addWidget(self.budget_message)
        apply_button = QPushButton("Aplicar na conta demo")
        apply_button.clicked.connect(self.save_budget)
        budget_layout.addWidget(apply_button)
        budget_layout.addStretch(1)
        self.tabs.addTab(budget_page, "Banca e valor da ordem")
        layout.addWidget(self.tabs, 1)
        self.note = QLabel(
            "Conta demo: dinheiro fictício. Agentes em avaliação; "
            "resultados ainda não comprovam consistência. "
            "Esta tela acompanha o laboratório; fechar o programa não interrompe os agentes."
        )
        self.note.setWordWrap(True)
        layout.addWidget(self.note)
        self.source = QLabel()
        self.source.setObjectName("Muted")
        self.source.setWordWrap(True)
        layout.addWidget(self.source)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(2000)
        self.refresh()

    def mark_budget_dirty(self, *_: Any) -> None:
        self.budget_dirty = True

    def save_budget(self) -> None:
        from app.fx.ibkr.budget_settings import validate

        try:
            value = validate(
                {
                    "capital_eur": self.capital_input.value(),
                    "order_eur": self.order_input.value(),
                    "fixed_order": self.fixed_input.isChecked(),
                }
            )
            if (
                not (self.root / "status.json").is_file()
                or not (self.root / "pilot.sqlite").is_file()
            ):
                raise ValueError("Localize o laboratório antes de configurar.")
            rid = uuid.uuid4().hex
            temp = self.root / ("budget-request-" + rid + ".tmp")
            temp.write_text(json.dumps({**value, "request_id": rid}), encoding="utf-8")
            temp.replace(self.root / "budget-request.json")
            self.budget_request_id = rid
            self.budget_message.setText(
                "Pedido enviado. Ainda não aplicado: aguarde a confirmação do laboratório. "
                "Se desconectado ou com operações abertas, os valores atuais continuam valendo."
            )
        except (ValueError, OSError) as exc:
            self.budget_message.setText(f"Não foi aplicado: {exc}")

    def choose_root(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, "Pasta com status.json e pilot.sqlite", str(self.root)
        )
        if folder:
            self.root = Path(folder)
            self.budget_initialized = False
            self.budget_dirty = False
            self.budget_request_id = None
            QSettings(ORG_NAME, APP_ID).setValue("paper_lab_dir", folder)
            self.refresh()

    def refresh(self) -> None:
        self.apply_snapshot(read_monitor(self.root))

    def apply_snapshot(self, snapshot: dict[str, Any]) -> None:
        self.snapshot = snapshot
        s = snapshot.get("status", {})
        budget = s.get(
            "budget_settings", {"capital_eur": 10000, "order_eur": 5000, "fixed_order": False}
        )
        if not self.budget_initialized:
            self.capital_input.setValue(budget["capital_eur"])
            self.order_input.setValue(budget["order_eur"])
            self.fixed_input.setChecked(budget["fixed_order"])
            self.budget_initialized = True
            self.budget_dirty = False
        mode = (
            f"fixo EUR {budget['order_eur']:.0f}"
            if budget["fixed_order"]
            else "automático (máx. EUR 5.000)"
        )
        self.budget_current.setText(
            f"Última configuração registrada: banca EUR {budget['capital_eur']:.2f}; ordem {mode}."
        )
        response = s.get("budget_status", {})
        if self.budget_request_id and response.get("request_id") == self.budget_request_id:
            self.budget_message.setText(response.get("message", "Aguardando confirmação."))
        elif self.budget_request_id and response.get("state") == "rejected":
            self.budget_message.setText(
                "Não aplicado: " + response.get("message", "Pedido recusado.")
            )
        fresh = bool(snapshot.get("fresh"))
        armed = bool(s.get("armed")) and fresh and snapshot.get("journal_ok") is True
        age = snapshot.get("age")
        age_text = f"{max(0, int(age))} s" if age is not None else "indisponível"
        label = (
            "CONECTADO À DEMO • agentes habilitados"
            if armed
            else "PAUSADO OU SEM CONEXÃO CONFIRMADA"
        )
        account = s.get("broker_preflight", {}).get("paper_account", "paper a confirmar")
        self.state.setText(f"{label}  |  Última atualização: {age_text}  |  {account}")
        self.state.setStyleSheet(f"color: {P.green if armed else P.amber}")
        if not s:
            self.summary.setText(
                snapshot.get("error", "Laboratório não localizado. Use Localizar laboratório.")
            )
        else:
            risk = s.get("risk", {})
            self.summary.setText(
                f"Velas de 1 hora (H1) • {account} • "
                f"Entradas no dia operacional: {risk.get('entries_used', '—')}/"
                f"{risk.get('entries_max', '—')} (inclui testes). "
                "O analista identifica padrões; cada agente decide se há oportunidade."
            )
        if snapshot.get("journal_ok") is not True:
            self.summary.setText(
                self.summary.text() + " Entradas de agentes: — (diário não confirmado)."
            )
        verified = snapshot.get("journal_ok") is True
        closed = snapshot.get("closed_trades", []) if verified else []
        totals = snapshot.get("agent_results", {}) if verified else {}
        pnl = sum(totals.values())
        results_ok = verified and snapshot.get("results_complete", True)
        self.kpis["pnl"].setText(f"{pnl:+.2f} €" if results_ok else "—")
        self.kpis["open"].setText(str(len(s.get("open", {}))) if s else "—")
        self.kpis["closed"].setText(
            str(snapshot.get("counts", {}).get("pilot_close", 0)) if verified else "—"
        )
        self.result_note.setText(
            "Resultado líquido estimado das operações encerradas, com comissões. "
            "Exclui testes e preparação de caixa."
            + (" Dados históricos: conexão atual não confirmada." if not armed else "")
            + (
                " Tabela: últimas 500 execuções/encerramentos. Total: todo o diário."
                if snapshot.get("counts", {}).get("pilot_order_filled", 0)
                + snapshot.get("counts", {}).get("pilot_close", 0)
                > 500
                else ""
            )
        )
        self.next_step.setText(
            "Os agentes acompanham as operações abaixo e procuram novos sinais."
            if armed and s.get("open")
            else "Aguardando uma oportunidade que atenda aos critérios. "
            "Nenhuma operação aberta. As velas H1 representam períodos de 1 hora."
            if armed
            else "Não há confirmação atual de atividade. "
            "Os dados abaixo são o último registro; não confirmam a situação atual na corretora."
        )
        rows: list[list[tuple[str, str | None]]] = []
        roles = {"reversao": "Reversão", "continuacao": "Continuação", "impulso": "Impulso"}
        purposes = {
            "reversao": "Procura mudança de direção",
            "continuacao": "Procura continuação da tendência",
            "impulso": "Procura movimento com força",
        }
        for agent in s.get("agents", []):
            if not agent.get("active"):
                continue
            mid = agent.get("id")
            positions = [p for p in s.get("open", {}).values() if p.get("model_id") == mid]
            activity = (
                "Acompanhando " + ", ".join(p["symbol"] for p in positions)
                if positions
                else "Analisando / aguardando sinal"
            )
            total = totals.get(mid, 0.0)
            rows.append(
                [
                    cell(roles.get(agent.get("role"), "Agente")),
                    cell(activity if armed else "Sem confirmação atual"),
                    cell(purposes.get(agent.get("role"), "")),
                    cell(agent.get("closed_trades", 0)),
                    cell(f"{total:+.2f}" if results_ok else "—"),
                ]
            )
        fill(self.agents, rows)
        rows = []
        for symbol, ctx in s.get("market_context", {}).items():
            observations = ctx.get("candle_observations", ctx.get("patterns", []))
            names = [
                x.get("knowledge", {}).get("label_pt", x.get("name", ""))
                + (" (sem contexto)" if x.get("context_requirements_met") is False else "")
                for x in observations
            ]
            rows.append(
                [
                    (symbol, None),
                    (str(ctx.get("bar_close", "—")), None),
                    (str(ctx.get("regime", "—")), None),
                    (", ".join(names) or "Nenhum", None),
                    (pair_reason(ctx, bool(s.get("funded_eur_only"))), None),
                ]
            )
        fill(self.contexts, rows)
        self.contexts.resizeRowsToContents()
        fill(
            self.orders,
            [
                [
                    (
                        {"buy": "COMPRA", "sell": "VENDA"}.get(str(o.get(k)), str(o.get(k, "—"))),
                        None,
                    )
                    for k in ("origin", "ref", "symbol", "side", "units", "price", "state")
                ]
                for o in snapshot.get("orders", [])
            ],
        )

        def role_name(mid: str) -> str:
            return next(
                (
                    roles.get(a.get("role"), "Agente")
                    for a in s.get("agents", [])
                    if a.get("id") == mid
                ),
                "Agente",
            )

        fill(
            self.positions,
            [
                [
                    cell(p.get("symbol", "—")),
                    cell("Venda" if p.get("units", 0) < 0 else "Compra"),
                    cell(abs(p["units"])),
                    cell(price(p.get("entry_price"), p.get("symbol"))),
                    cell(price(p.get("stop"), p.get("symbol"))),
                    cell(price(p.get("target"), p.get("symbol"))),
                    cell(role_name(p.get("model_id", ""))),
                ]
                for p in s.get("open", {}).values()
            ],
        )
        from datetime import datetime

        fill(
            self.closed,
            [
                [
                    cell(t.get("symbol")),
                    cell(price(t.get("entry"), t.get("symbol"))),
                    cell(price(t.get("exit"), t.get("symbol"))),
                    cell(
                        {
                            "alvo": "Objetivo atingido",
                            "stop": "Limite de perda",
                            "tempo": "Prazo da operação encerrado",
                        }.get(t.get("reason"), t.get("reason", "—"))
                    ),
                    cell(money(t.get("pnl"))),
                    cell(datetime.fromisoformat(t["time"]).astimezone().strftime("%d/%m %H:%M")),
                ]
                for t in closed
            ],
        )
        fill(
            self.tests,
            [
                [
                    (t["ref"], None),
                    (str(t.get("symbol", "—")), None),
                    (price(t.get("entry")), None),
                    (price(t.get("exit")), None),
                    (money(t.get("pnl")), None),
                    (t["state"], None),
                ]
                for t in snapshot.get("tests", [])
            ],
        )
        funding = s.get("funding", {}).get("orders", {})
        fill(
            self.funding,
            [
                [
                    (sym, None),
                    (str(p.get("units", "—")), None),
                    (price(p.get("price"), sym), None),
                    (str(p.get("status", "—")), None),
                    (money(p.get("commission_eur")), None),
                ]
                for sym, p in funding.items()
            ],
        )
        lines = []
        for event in reversed(snapshot.get("events", [])):
            payload = event["payload"]
            lines.append(f"{event['ts']}  {event['kind']}\n{payload}")
        self.events.setPlainText(snapshot.get("journal_error", "") + "\n\n".join(lines))
        self.source.setText(
            f"Tela atualizada a cada 2 s • análise com velas de 1 hora • fonte: {self.root}"
        )
