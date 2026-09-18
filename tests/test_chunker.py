from datetime import datetime, timedelta, timezone

from app.ingest.chunker import chunk_messages, format_message
from app.models import StoredMessage

START = datetime(2026, 9, 12, 14, 0, tzinfo=timezone.utc)


def message(minutes, author="Awa", text="hello", source="whatsapp_export"):
    return StoredMessage(
        id=f"m{minutes}-{author}", chat_id="g@g.us", source=source, author=author,
        sent_at=START + timedelta(minutes=minutes), text=text,
    )


def test_a_silence_starts_a_new_chunk():
    chunks = chunk_messages([message(0), message(5, "Moussa"), message(50), message(52, "Moussa")])
    assert [c.message_ids for c in chunks] == [("m0-Awa", "m5-Moussa"), ("m50-Awa", "m52-Moussa")]
    assert chunks[0].authors == ("Awa", "Moussa")
    assert chunks[0].started_at == START and chunks[0].ended_at == START + timedelta(minutes=5)


def test_a_full_chunk_is_closed():
    messages = [message(i, text="x" * 200) for i in range(20)]
    chunks = chunk_messages(messages, max_chars=1000)
    assert len(chunks) > 1
    assert all(len(c.content) <= 1000 for c in chunks)
    assert sum(len(c.message_ids) for c in chunks) == 20


def test_content_carries_date_author_and_text():
    [chunk] = chunk_messages([message(5, text="The bootcamp moves to 25 September")])
    assert chunk.content == "[2026-09-12 14:05 UTC] Awa: The bootcamp moves to 25 September"
    assert format_message(message(0)).startswith("[2026-09-12 14:00 UTC]")


def test_messages_are_ordered_and_sources_reported():
    chunks = chunk_messages([message(3, source="whatsapp_live"), message(1)])
    assert chunks[0].message_ids == ("m1-Awa", "m3-Awa")
    assert chunks[0].source == "mixed"
