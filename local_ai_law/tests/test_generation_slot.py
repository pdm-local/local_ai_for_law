"""P0-3 regression: a model switch must not land while a generation is in flight.

There is one local GPU slot. `ollama_generation_slot(model)` performs the model switch inside the
gate, and a switch to a different model must wait until all in-flight generations drain — otherwise
a second caller could unload/swap the model mid-stream (VRAM spike / corrupt output). This test
holds the slot with model A and asserts a concurrent switch to model B blocks until A releases.

Hermetic: the only Ollama touch (the unload call) is stubbed; no GPU/server needed.
"""
from __future__ import annotations

import threading

import pytest

from local_ai_law.services import ollama_client as oc


@pytest.fixture()
def quiet_unload(monkeypatch):
    # The switch path calls `_client.generate(keep_alive=0)` to unload the old model. Stub it so
    # no real Ollama server is needed, and reset the gate to a known idle state.
    monkeypatch.setattr(oc._client, "generate", lambda **kw: None)
    oc._active_model = None
    oc.set_generation_concurrency(2)  # >1 so serialization is driven by the switch rule, not the limit
    try:
        yield
    finally:
        oc.set_generation_concurrency(1)
        oc._active_model = None


def test_switch_waits_for_inflight_generation(quiet_unload):
    a_holding = threading.Event()
    let_a_finish = threading.Event()
    b_switched = threading.Event()

    def hold_model_a():
        with oc.ollama_generation_slot("model-A"):
            a_holding.set()
            let_a_finish.wait(timeout=2)

    def switch_to_b():
        with oc.ollama_generation_slot("model-B"):
            b_switched.set()

    ta = threading.Thread(target=hold_model_a)
    ta.start()
    assert a_holding.wait(timeout=2), "model-A generation never started"

    tb = threading.Thread(target=switch_to_b)
    tb.start()

    # While A is in flight, the switch to B must NOT proceed.
    assert not b_switched.wait(timeout=0.4), \
        "REGRESSION (P0-3): model switch landed while a generation was in flight"

    # Once A drains, the switch is free to complete.
    let_a_finish.set()
    ta.join(timeout=2)
    assert b_switched.wait(timeout=2), "switch to model-B never completed after the drain"
    tb.join(timeout=2)
