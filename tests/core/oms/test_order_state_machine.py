"""Tests for order state machine transitions, safety violations, and order table invariants."""

import itertools

import pytest

from algodesk.api.errors import SafetyViolation
from algodesk.api.trading import ORDER_TRANSITIONS, OrderState, check_transition

ALL_STATES = list(OrderState)
ALL_PAIRS = list(itertools.product(ALL_STATES, ALL_STATES))


@pytest.mark.parametrize("from_state,to_state", ALL_PAIRS)
def test_all_state_transitions(from_state: OrderState, to_state: OrderState) -> None:
    """Exhaustively verify all legal and illegal transitions across the order state machine."""
    allowed = ORDER_TRANSITIONS.get(from_state, set())
    if to_state in allowed:
        assert check_transition(from_state, to_state) is True
    else:
        with pytest.raises(SafetyViolation) as exc_info:
            check_transition(from_state, to_state)
        assert f"Illegal order transition from {from_state.value} to {to_state.value}" in str(
            exc_info.value
        )


@pytest.mark.parametrize(
    "term_state", [OrderState.FILLED, OrderState.CANCELLED, OrderState.REJECTED]
)
def test_terminal_states_have_no_transitions(term_state: OrderState) -> None:
    """Terminal states must have an empty transition set."""
    assert ORDER_TRANSITIONS[term_state] == set()
    for next_state in ALL_STATES:
        with pytest.raises(SafetyViolation):
            check_transition(term_state, next_state)


def test_transition_matrix_completeness() -> None:
    """Verify state machine covers all declared enum members."""
    assert set(ORDER_TRANSITIONS.keys()) == set(ALL_STATES)
