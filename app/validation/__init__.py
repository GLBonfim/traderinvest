"""Phase 9 statistical validation: block-bootstrap uncertainty, paired comparisons with
multiple-testing control, predefined cost sensitivity, redundancy and concentration.

Exploratory research output. Statistical significance is not economic significance, and
nothing here is evidence of future profitability.
"""

from app.validation.config import (
    COST_SCENARIOS,
    VALIDATION_VERSION,
    CostScenario,
    SliceSpec,
    ValidationConfig,
)
from app.validation.engine import Validator, report_summary
from app.validation.models import ValidationReport

__all__ = [
    "COST_SCENARIOS",
    "VALIDATION_VERSION",
    "CostScenario",
    "SliceSpec",
    "ValidationConfig",
    "ValidationReport",
    "Validator",
    "report_summary",
]
