"""Run the fixed question set against the real knowledge base and answer models.

    python -m scripts.evaluate                 # evals/questions.json
    python -m scripts.evaluate --show-answers

Each question either expects a grounded answer (with sources, containing one of `expect_any`,
and citing a call recording when `expect_recording` is true) or expects Jeli to say it doesn't
know (`"expect": "dont_know"`). Also reports the latency.

Cases with `"mode": "already_answered"` are questions posted in the group, not to Jeli (R7):
`"expect": "steps_in"` (the group answered it, Jeli points to it) or `"expect": "silent"`.
"""

import argparse
import asyncio
import json
import time
from pathlib import Path

from app.answer.language import TEXTS, detect_language
from app.config import get_settings
from app.kb.store import Store
from scripts.ask import build_answerer
from scripts.common import require, run

LATENCY_TARGET_SECONDS = 10


def check(case: dict, reply: str | None) -> bool:
    if case.get("mode") == "already_answered":
        return (reply is not None) == (case["expect"] == "steps_in")
    dont_know = TEXTS[detect_language(case["question"])]["dont_know"]
    if case.get("expect") == "dont_know":
        return reply == dont_know
    answer, _, sources = reply.partition("📌")
    grounded = bool(sources) and any(expected.lower() in answer.lower() for expected in case["expect_any"])
    return grounded and ("🎥" in sources if case.get("expect_recording") else True)


async def main(path: Path, show_answers: bool, pause: float) -> None:
    cases = json.loads(path.read_text(encoding="utf-8"))
    settings = get_settings()
    store = Store(require(settings.database_url, "DATABASE_URL"))
    await store.open()
    passed, latencies = 0, []
    try:
        answerer = build_answerer(settings, store)
        for case in cases:
            started = time.monotonic()
            if case.get("mode") == "already_answered":
                reply = await answerer.already_answered(case["question"], settings.duplicate_min_similarity)
            else:
                reply = await answerer.answer(case["question"], asker="evaluator")
            latencies.append(time.monotonic() - started)
            ok = check(case, reply)
            passed += ok
            label = f"[in the group, expect {case['expect']}] " if case.get("mode") == "already_answered" else ""
            print(f"{'PASS' if ok else 'FAIL'}  {latencies[-1]:4.1f} s  {label}{case['question']}")
            if (show_answers or not ok) and reply:
                print("      " + reply.replace("\n", "\n      "))
            # Real questions are spread out; back-to-back ones would hit the free tier's per-minute cap.
            await asyncio.sleep(pause)
    finally:
        await store.close()
    slow = sum(latency > LATENCY_TARGET_SECONDS for latency in latencies)
    print(f"\n{passed}/{len(cases)} passed · median {sorted(latencies)[len(latencies) // 2]:.1f} s · "
          f"max {max(latencies):.1f} s · {slow} over {LATENCY_TARGET_SECONDS} s")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("path", nargs="?", type=Path, default=Path("evals/questions.json"))
    parser.add_argument("--show-answers", action="store_true")
    parser.add_argument("--pause", type=float, default=6, help="seconds between questions (free-tier rate limits)")
    args = parser.parse_args()
    run(main(args.path, args.show_answers, args.pause))
