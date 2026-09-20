import asyncio
import math
from types import SimpleNamespace

import httpx
import pytest
from google.genai import errors

from app.kb import embeddings
from app.kb.embeddings import DIMENSIONS, MODEL, Embedder


class FakeModels:
    def __init__(self, fail_first=0):
        self.calls = []
        self.fail_first = fail_first

    async def embed_content(self, model, contents, config):
        self.calls.append((model, list(contents), config))
        if self.fail_first:
            self.fail_first -= 1
            raise errors.ClientError(429, {"error": {"code": 429, "message": "quota", "status": "RESOURCE_EXHAUSTED"}})
        # Not unit length on purpose: the embedder must normalise.
        return SimpleNamespace(embeddings=[SimpleNamespace(values=[3.0, 4.0] + [0.0] * (DIMENSIONS - 2)) for _ in contents])


def make_embedder(models):
    return Embedder(api_keys="unused", client=SimpleNamespace(aio=SimpleNamespace(models=models)))


def test_documents_are_embedded_in_batches_and_normalised():
    models = FakeModels()
    vectors = asyncio.run(make_embedder(models).embed_documents([f"text {i}" for i in range(250)]))
    assert [len(contents) for _, contents, _ in models.calls] == [100, 100, 50]
    model, _, config = models.calls[0]
    assert model == MODEL
    assert config.task_type == "RETRIEVAL_DOCUMENT" and config.output_dimensionality == DIMENSIONS
    assert len(vectors) == 250
    assert math.isclose(math.sqrt(sum(v * v for v in vectors[0])), 1.0)
    assert vectors[0][:2] == [0.6, 0.8]


def test_long_texts_are_split_to_stay_within_the_token_budget_per_request():
    models = FakeModels()
    chunk = "x" * 1500  # ~500 tokens: a full conversation chunk
    asyncio.run(make_embedder(models).embed_documents([chunk] * 40))
    sizes = [len(contents) for _, contents, _ in models.calls]
    assert sum(sizes) == 40
    assert all(size * embeddings.estimate_tokens(chunk) <= embeddings.BATCH_TOKENS for size in sizes)


def test_token_budget_waits_for_the_minute_to_free_up():
    clock = SimpleNamespace(now=0.0)
    waits = []

    async def sleep(seconds):
        waits.append(seconds)
        clock.now += seconds

    budget = embeddings.TokenBudget(1000, clock=lambda: clock.now, sleep=sleep)

    async def scenario():
        await budget.spend(600)
        await budget.spend(300)  # 900: fits
        assert waits == []
        await budget.spend(300)  # 1200 would exceed: wait until the first spend is a minute old
        assert waits == [60.0]

    asyncio.run(scenario())


def test_queries_use_the_query_task_type():
    models = FakeModels()
    vector = asyncio.run(make_embedder(models).embed_query("when is the bootcamp?"))
    assert models.calls[0][2].task_type == "RETRIEVAL_QUERY"
    assert len(vector) == DIMENSIONS


def test_rate_limits_are_retried(monkeypatch):
    async def no_wait(seconds):
        pass

    monkeypatch.setattr(embeddings.asyncio, "sleep", no_wait)
    models = FakeModels(fail_first=2)
    assert len(asyncio.run(make_embedder(models).embed_documents(["a"]))) == 1
    assert len(models.calls) == 3


def test_network_drops_are_retried(monkeypatch):
    async def no_wait(seconds):
        pass

    class Flaky(FakeModels):
        async def embed_content(self, model, contents, config):
            if not self.calls:
                self.calls.append("dropped")
                raise httpx.ConnectError("[Errno 11001] getaddrinfo failed")
            return await super().embed_content(model, contents, config)

    monkeypatch.setattr(embeddings.asyncio, "sleep", no_wait)
    models = Flaky()
    assert len(asyncio.run(make_embedder(models).embed_documents(["a"]))) == 1


def test_other_errors_are_not_retried():
    class Broken(FakeModels):
        async def embed_content(self, model, contents, config):
            self.calls.append(model)
            raise errors.ClientError(400, {"error": {"code": 400, "message": "bad", "status": "INVALID_ARGUMENT"}})

    models = Broken()
    with pytest.raises(errors.ClientError):
        asyncio.run(make_embedder(models).embed_documents(["a"]))
    assert len(models.calls) == 1
