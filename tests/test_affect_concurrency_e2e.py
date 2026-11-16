from __future__ import annotations

import asyncio

import httpx
import pytest

from dusha import api

CALLS = 6


async def test_concurrent_user_messages_keep_every_increment(config, embedding_stub):
    config.embedding.base_url = embedding_stub.url
    config.embedding.model = "stub"
    config.plugin("affect").increment = 0.1
    config.plugins["proactive"].disabled = True
    for index in range(CALLS):
        embedding_stub.says[f"I am scared {index}"] = "fear_general"

    app = api.create_app(config)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            responses = await asyncio.gather(
                *(
                    client.post(
                        "/state/v1/messages",
                        json={
                            "harness": "e2e",
                            "conversation_id": "affect",
                            "role": "user",
                            "content": f"I am scared {index}",
                        },
                    )
                    for index in range(CALLS)
                )
            )
            with app.state.service.database.connect() as db:
                revision = int(db.execute("SELECT revision FROM affect_state WHERE id=1").fetchone()[0])
            state = (await client.get("/state/v1/affect")).json()

    assert all(
        response.status_code == 200 and response.json()["affect"]["emotion"] == "fear"
        for response in responses
    )
    assert state["base"]["fear"] == pytest.approx(0.1 * CALLS, abs=0.01)
    assert state["unanswered_proactive"] == 0
    assert revision == CALLS
