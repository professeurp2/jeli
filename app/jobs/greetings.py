"""The two messages Jeli sends of its own accord in the cohort group: hello, and goodbye.

Everywhere else Jeli only ever answers. These two are the exception, and the reason is simple: a
bot that appears in a 240-member group without a word is a stranger reading over their shoulder,
and one that vanishes after the judging without a word is a service that broke. Both are said once,
by a person on the team pressing a button, and never again — the group's own conversation is not
Jeli's to fill.

Each is written once and kept, so the team can read it, change a word and see exactly what will be
posted before anyone does.
"""

import logging

log = logging.getLogger(__name__)

# Sent once each. The key is remembered in the settings, so a second press does nothing.
HELLO = "greeting.hello"
GOODBYE = "greeting.goodbye"

HELLO_TEXT = """\
Bonsoir à toutes et à tous 👋

Je suis *Jeli*, le griot de ce groupe. Comme les griots d'autrefois gardaient la mémoire de leur \
communauté, je garde la vôtre : les annonces, les décisions, les sessions enregistrées, les \
échéances. Rien ne se perd.

*Ce que vous pouvez me demander*
• _« C'est quand la prochaine session ? »_ — je réponds, et je dis qui l'a annoncé
• _« Qu'est-ce que j'ai manqué depuis lundi ? »_ — je vous fais le résumé
• _« Résume-moi la session d'hier »_ — j'écoute les enregistrements
• _« Rappelle-moi avant la réunion »_ — je vous préviens à temps
• _/deadlines_ — tout ce qui arrive dans les deux semaines

Écrivez-moi en français, in English, kwa Kiswahili — je réponds dans votre langue. Par écrit ou en \
vocal, comme vous préférez.

*Deux choses importantes.* Je ne parle que si on m'adresse la parole : écrivez « Jeli » au début \
de votre message, ou répondez à l'un des miens. Et je ne dis jamais rien que quelqu'un n'ait dit \
ici — quand je ne sais pas, je le dis.

Bonne chance à toutes les équipes 🙏
"""

GOODBYE_TEXT = """\
Un dernier mot, et je vous laisse 🙏

Les tests sont terminés. Merci à chacun de vous : vous m'avez posé des questions auxquelles je ne \
savais pas répondre, et c'est comme ça que j'ai appris. Chaque « tu t'es trompé » de ces derniers \
jours a corrigé quelque chose.

Ce que vous avez construit ensemble reste : les sessions, les décisions, les échéances — tout est \
gardé.

Je me mets en veille maintenant. Si l'équipe me rallume un jour, je me souviendrai de tout.

Bon vent à toutes les équipes. Ce fut un honneur de garder votre mémoire ✨
"""


async def send_greeting(adapter, store, which: str, chat_id: str, actor: str = "the team") -> str:
    """Post the hello or the goodbye in one group, once. Returns what happened, for the team."""
    text = HELLO_TEXT if which == HELLO else GOODBYE_TEXT
    already = await store.load_settings() if store else {}
    if already.get(which):
        return "already sent"
    if not chat_id:
        return "no group"
    await adapter.send_text(chat_id, text.strip())
    if store:
        await store.save_settings({which: chat_id}, actor)
        await store.add_audit(actor, f"Sent the {'hello' if which == HELLO else 'goodbye'} message to the group")
    log.info("Sent the %s message to %s", which, chat_id)
    return "sent"
