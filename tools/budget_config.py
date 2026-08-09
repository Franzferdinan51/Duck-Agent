"""Configurable budget constants for tool result persistence.

Per-tool resolution: pinned > config overrides > registry > default.
"""
from dataclasses import dataclass, field
from typing import Dict
PINNED_THRESHOLDS: Dict[str, float] = {'read_file': float('inf')}
DEFAULT_RESULT_SIZE_CHARS: int = 100000
DEFAULT_TURN_BUDGET_CHARS: int = 200000
DEFAULT_PREVIEW_SIZE_CHARS: int = 1500

@dataclass(frozen=True)
class BudgetConfig:
    """Immutable budget constants for the 3-layer tool result persistence system.

    Layer 2 (per-result): resolve_threshold(tool_name) -> threshold in chars.
    Layer 3 (per-turn):   turn_budget -> aggregate char budget across all tool
                          results in a single assistant turn.
    Preview:              preview_size -> inline snippet size after persistence.
    """
    default_result_size: int = DEFAULT_RESULT_SIZE_CHARS
    turn_budget: int = DEFAULT_TURN_BUDGET_CHARS
    preview_size: int = DEFAULT_PREVIEW_SIZE_CHARS
    tool_overrides: Dict[str, int] = field(default_factory=dict)

    def resolve_threshold(self, tool_name: str) -> int | float:
        """Resolve the persistence threshold for a tool.

        Priority: pinned -> tool_overrides -> registry per-tool -> default.

        The registry per-tool value is capped at ``default_result_size`` so a
        context-scaled budget (small model) actually constrains tools that
        register a large fixed ``max_result_size_chars`` (web/terminal/x_search
        all register 100K). For the default budget this is a no-op because both
        equal 100K; for a scaled-down budget it prevents a per-tool registry
        value from re-inflating the cap past the model's window (#23767).
        """
        if tool_name in PINNED_THRESHOLDS:
            return PINNED_THRESHOLDS[tool_name]
        if tool_name in self.tool_overrides:
            return self.tool_overrides[tool_name]
        from tools.registry import registry
        registry_value = registry.get_max_result_size(tool_name, default=self.default_result_size)
        if registry_value == float('inf'):
            return registry_value
        return min(registry_value, self.default_result_size)
DEFAULT_BUDGET = BudgetConfig()
_CHARS_PER_TOKEN: int = 4
_PER_RESULT_WINDOW_FRACTION: float = 0.15
_PER_TURN_WINDOW_FRACTION: float = 0.3
_MIN_RESULT_SIZE_CHARS: int = 8000
_MIN_TURN_BUDGET_CHARS: int = 16000

def budget_for_context_window(context_length: int | None) -> BudgetConfig:
    """Return a BudgetConfig scaled to the active model's context window.

    The fixed defaults (100K result / 200K turn chars) are correct for large
    (200K+ token) models but blind to small ones: on a 65K-token model a single
    tool result persisted at the 100K-char threshold, or a 200K-char turn
    budget (~50K tokens), can by itself approach or exceed the whole window and
    force an oversized request (#23767).

    Scaling keeps large models byte-identical to today (the proportional value
    is clamped to the existing defaults as a CAP) while shrinking the budget for
    small models proportionally to their window, floored so a usable preview
    always survives.
    """
    if not context_length or context_length <= 0:
        return DEFAULT_BUDGET
    window_chars = context_length * _CHARS_PER_TOKEN
    per_result = int(window_chars * _PER_RESULT_WINDOW_FRACTION)
    per_turn = int(window_chars * _PER_TURN_WINDOW_FRACTION)
    per_result = max(_MIN_RESULT_SIZE_CHARS, min(per_result, DEFAULT_RESULT_SIZE_CHARS))
    per_turn = max(_MIN_TURN_BUDGET_CHARS, min(per_turn, DEFAULT_TURN_BUDGET_CHARS))
    return BudgetConfig(default_result_size=per_result, turn_budget=per_turn, preview_size=DEFAULT_PREVIEW_SIZE_CHARS)