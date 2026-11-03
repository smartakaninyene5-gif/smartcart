"""Deterministic Purchase Guard (pure domain)."""

from app.domain.guard.engine import evaluate
from app.domain.guard.policy import GuardPolicy
from app.domain.guard.rules import GuardContext, GuardDecision, RuleResult

__all__ = ["evaluate", "GuardPolicy", "GuardContext", "GuardDecision", "RuleResult"]
