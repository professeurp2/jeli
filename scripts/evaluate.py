"""Run the fixed question set against the real knowledge base and answer models.

    python -m scripts.evaluate                 # evals/questions.json
    python -m scripts.evaluate --show-answers
    python -m scripts.evaluate --only understand   # one kind of case: answer, understand, already_answered

Kinds of cases:
- answer (default): a grounded answer, containing one of `expect_any`, with at least one verified
  source (`"expect": "grounded"`, the default) — citing a call recording when `expect_recording`
  is true; or an answer without a source needed (`"expect": "answered"`, e.g. general questions
  the community brief covers); or Jeli saying it doesn't know (`"expect": "dont_know"`).
- understand: what the understanding step makes of a message, with the conversation given in
  `history` ([[member, jeli], …]): its kind must be one of `expect_kind`, and the standalone
  request must contain one of `expect_any` when given.
- already_answered: questions posted in the group, not to Jeli (R7): `"expect": "steps_in"` (the
  group answered it, Jeli points to it) or `"expect": "silent"`.

Also reports the latency. Measured on 21 Sep: the previous version looked for a "📌" marker that
answers no longer contain, so every grounded case failed and nothing was measured for a day.
"""

import argparse
import asyncio
import json
import time
from pathlib import Path

from app.answer.brief import Brief
from app.answer.citations import ignored_keys
from app.answer.conversation import Turn
from app.answer.language import TEXTS, detect_language
from app.answer.llm import LLM
from app.answer.understand import Understander
from app.config import get_settings
from app.kb.store import Store
from scripts.ask import build_answerer
from scripts.common import require, run

LATENCY_TARGET_SECONDS = 10


def check(case: dict, reply) -> tuple[bool, str]:
    """(passed, what was seen)."""
    mode = case.get("mode", "answer")
    if mode == "already_answered":
        return (reply is not None) == (case["expect"] == "steps_in"), "stepped in" if reply else "silent"
    if mode == "understand":
        kind_ok = reply.kind in case["expect_kind"]
        text_ok = not case.get("expect_any") or any(e.lower() in reply.standalone.lower() for e in case["expect_any"])
        return kind_ok and text_ok, f"{reply.kind} · {reply.standalone[:80]}"
    dont_know = TEXTS[detect_language(case["question"])]["dont_know"]
    unanswered = getattr(reply, "unanswered", False) or reply == dont_know or reply.startswith(TEXTS[detect_language(case["question"])]["dont_know_near"])
    if case.get("expect") == "dont_know":
        return unanswered, "don't know" if unanswered else "answered"
    answer = str(reply)
    cited = list(getattr(reply, "cited", []))
    mentions = any(expected.lower() in answer.lower() for expected in case.get("expect_any", []))
    if case.get("expect") == "answered":
        return (not unanswered) and mentions, f"{'answered' if not unanswered else 'unanswered'}, {len(cited)} source(s)"
    recording = ("🎥" in " ".join(cited)) if case.get("expect_recording") else True
    return (not unanswered) and mentions and bool(cited) and recording, f"{len(cited)} source(s){', from a recording' if '🎥' in ' '.join(cited) else ''}"


async def main(path: Path, show_answers: bool, pause: float, only: str | None) -> None:
    cases = json.loads(path.read_text(encoding="utf-8"))
    if only:
        cases = [c for c in cases if c.get("mode", "answer") == only]
    settings = get_settings()
    store = Store(require(settings.database_url, "DATABASE_URL"))
    await store.open()
    passed, latencies, failed = 0, [], []
    try:
        llm = LLM(settings.api_key_list, settings.answer_models)
        answerer = build_answerer(settings, store, llm)
        # As in production: the team's settings from the dashboard win over the environment.
        saved = await store.load_settings()
        answerer.ignored = ignored_keys(saved.get("ignored_authors") or settings.ignored_author_list) | ignored_keys(saved.get("muted_members") or [])
        answerer.organisers = ignored_keys(saved.get("organisers") or settings.organiser_list)
        answerer.chat_labels = saved.get("chat_labels") or settings.chat_label_map
        brief = Brief(store, llm)
        answerer.brief = await brief.load()
        understander = Understander(llm)
        understander.brief = answerer.brief
        for case in cases:
            mode = case.get("mode", "answer")
            started = time.monotonic()
            if mode == "already_answered":
                reply = await answerer.already_answered(case["question"], settings.duplicate_min_similarity)
            elif mode == "understand":
                turns = [Turn(started, m, j) for m, j in case.get("history", [])]
                reply = await understander.understand(case["question"], detect_language(case["question"]), turns)
            else:
                reply = await answerer.answer(case["question"], asker="evaluator")
            latencies.append(time.monotonic() - started)
            ok, seen = check(case, reply)
            passed += ok
            if not ok:
                failed.append(case["question"])
            label = f"[{mode}] " if mode != "answer" else ""
            print(f"{'PASS' if ok else 'FAIL'}  {latencies[-1]:4.1f} s  {label}{case['question']}  →  {seen}")
            if (show_answers or not ok) and reply and mode != "understand":
                print("      " + str(reply).replace("\n", "\n      "))
            # Real questions are spread out; back-to-back ones would hit the free tier's per-minute cap.
            await asyncio.sleep(pause)
    finally:
        await store.close()
    slow = sum(latency > LATENCY_TARGET_SECONDS for latency in latencies)
    print(f"\n{passed}/{len(cases)} passed · median {sorted(latencies)[len(latencies) // 2]:.1f} s · "
          f"max {max(latencies):.1f} s · {slow} over {LATENCY_TARGET_SECONDS} s")
    if failed:
        print("failed: " + " | ".join(failed))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("path", nargs="?", type=Path, default=Path("evals/questions.json"))
    parser.add_argument("--show-answers", action="store_true")
    parser.add_argument("--only", choices=["answer", "understand", "already_answered"])
    parser.add_argument("--pause", type=float, default=3, help="seconds between questions (free-tier rate limits)")
    args = parser.parse_args()
    run(main(args.path, args.show_answers, args.pause, args.only))
