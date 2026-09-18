import io
import zipfile
from datetime import datetime, timezone

from app.ingest.whatsapp_export import message_ids, parse_export, read_export

LRM = "‎"
NNBSP = " "  # narrow no-break space, used before AM/PM by recent phones

ANDROID_FR = """\
12/09/2026 09:58 - Les messages et les appels sont chiffrés de bout en bout. Aucun tiers, pas même WhatsApp, ne peut les lire ou les écouter.
12/09/2026 10:02 - Awa Traoré a ajouté Moussa Diallo
12/09/2026 14:05 - Awa Traoré: Bonjour à tous, le bootcamp est déplacé au 25 septembre.
12/09/2026 14:07 - Moussa Diallo: Merci ! Et le lieu ?
Toujours à Bamako ?
13/09/2026 08:30 - Awa Traoré: <Médias omis>
13/09/2026 08:31 - Awa Traoré: Oui, même lieu <Ce message a été modifié>
13/09/2026 08:32 - Moussa Diallo: Ce message a été supprimé
"""

ANDROID_EN_12H = f"""\
9/12/26, 2:05{NNBSP}PM - Awa Traoré: The bootcamp moves to September 25.
9/12/26, 2:07{NNBSP}PM - +223 70 00 00 00: Where?
9/13/26, 9:15{NNBSP}AM - Awa Traoré: Same place.
"""

IPHONE_EN = f"""\
[12/09/2026, 09:58:01] Cohort 1: {LRM}Messages and calls are end-to-end encrypted. No one outside of this chat, not even WhatsApp, can read or listen to them.
[12/09/2026, 10:02:11] Cohort 1: {LRM}Awa Traoré added Moussa Diallo
[12/09/2026, 14:05:33] Awa Traoré: Deadline for the pitch deck: Friday 6 pm.
[12/09/2026, 14:06:02] {LRM}Moussa Diallo: {LRM}image omitted
[12/09/2026, 14:06:40] Moussa Diallo: Noted, thanks
"""


def test_android_french_export():
    messages = parse_export(ANDROID_FR)
    assert [(m.author, m.text) for m in messages] == [
        ("Awa Traoré", "Bonjour à tous, le bootcamp est déplacé au 25 septembre."),
        ("Moussa Diallo", "Merci ! Et le lieu ?\nToujours à Bamako ?"),
        ("Awa Traoré", "Oui, même lieu"),
    ]
    assert messages[0].sent_at == datetime(2026, 9, 12, 14, 5, tzinfo=timezone.utc)


def test_android_english_month_first_and_12_hour_clock():
    messages = parse_export(ANDROID_EN_12H)
    assert len(messages) == 3
    assert messages[0].sent_at == datetime(2026, 9, 12, 14, 5, tzinfo=timezone.utc)
    assert messages[2].sent_at == datetime(2026, 9, 13, 9, 15, tzinfo=timezone.utc)
    assert messages[1].author == "+223 70 00 00 00"


def test_iphone_export_skips_system_notices_and_media():
    messages = parse_export(IPHONE_EN)
    assert [(m.author, m.text) for m in messages] == [
        ("Awa Traoré", "Deadline for the pitch deck: Friday 6 pm."),
        ("Moussa Diallo", "Noted, thanks"),
    ]
    assert messages[0].sent_at.second == 33


def test_ambiguous_dates_default_to_day_first():
    [message] = parse_export("05/09/2026 10:00 - Awa: hi\n")
    assert (message.sent_at.month, message.sent_at.day) == (9, 5)
    [message] = parse_export("05/09/2026 10:00 - Awa: hi\n", day_first=False)
    assert (message.sent_at.month, message.sent_at.day) == (5, 9)


def test_local_times_are_converted_with_the_export_timezone():
    [message] = parse_export("12/09/2026 14:05 - Awa: hi\n", timezone="Africa/Lagos")  # UTC+1
    assert message.sent_at.astimezone(timezone.utc).hour == 13


def test_ids_are_stable_and_tell_identical_messages_apart():
    text = "12/09/2026 14:05 - Awa: ok\n12/09/2026 14:05 - Awa: ok\n12/09/2026 14:06 - Moussa: ok\n"
    first = message_ids("group@g.us", parse_export(text))
    again = message_ids("group@g.us", parse_export(text))
    assert first == again
    assert len(set(first)) == 3
    assert message_ids("other@g.us", parse_export(text)) != first


def test_zip_exports_are_read(tmp_path):
    path = tmp_path / "WhatsApp Chat - Cohort 1.zip"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("_chat.txt", IPHONE_EN)
        archive.writestr("00000012-PHOTO-2026-09-12.jpg", b"...")
    path.write_bytes(buffer.getvalue())
    assert len(parse_export(read_export(path))) == 2
