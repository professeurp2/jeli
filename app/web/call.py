"""Talking to Jeli out loud, from a browser — no phone number, no app, no account.

WhatsApp does not let a program answer a call, and Teams needs an administrator's blessing. A
browser needs neither: the page takes the microphone, the audio goes to Gemini's Live API, and
Jeli answers in its own voice, interrupting and being interrupted like a person.

It answers from the community's memory, not from general knowledge. Two things carry that: what
Jeli already knows without looking (the brief and its own state, the same block every written
answer starts from), and a tool it can call mid-sentence — `search_memory` — which runs the very
search behind /search and the answers in the groups. So a caller can ask "when do the tests close?"
and hear the answer with who announced it, in the second it takes to say it.

The Live API is the one part of Jeli that cannot run out: measured on AI Studio on 23 September,
its requests per day are unlimited — only tokens per minute are capped.
"""

import asyncio
import contextlib
import json
import logging
import time

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from google import genai
from google.genai import types

from app.answer.persona import CAPABILITIES, PERSONA, background
from app.web import ui
from app.web.ui import esc

log = logging.getLogger(__name__)

router = APIRouter()

# The Live API's own models, newest first. Their requests per day are unlimited.
CALL_MODELS = ["gemini-2.5-flash-native-audio-latest", "gemini-3.8-live", "gemini-3.1-flash-live-preview"]
VOICE = "Aoede"
# A call ends for one of three reasons, and none of them is a stopwatch: the caller hangs up, nobody
# has spoken for a long time, or no Live model will take the call any more. Measured 23 Sep: a
# ten-minute wall cut conversations that were still going, and a session ending — which Google does
# routinely, every few minutes — ended the whole call instead of being reopened.
QUIET_SECONDS = 300
GIVE_UP_AFTER = 4
RECONNECT_PAUSE = 1.0
SHORTEST_REAL_SESSION = 3.0
# What the caller hears if nothing can answer.
NO_ENGINE = "Jeli ne peut pas prendre l'appel pour l'instant."
RESUMING = "Un instant — je reprends…"
GONE_QUIET = "Personne ne parlait depuis un moment, alors j'ai raccroché. Rappelez quand vous voulez."

SPOKEN = """
You are on a voice call now, not writing a message. So: short sentences, one idea at a time, the
way a person answers out loud. No lists, no bullet points, no formatting — say "three things:
first… then… and finally…". Never read a URL aloud; say where it is instead. When you do not know,
say so in one sentence and offer the next step.

Before answering anything about what the community said, decided, or scheduled, call search_memory
and answer from what it returns, naming who said it and when. Never answer such a question from
general knowledge, and never invent a date.
"""

SEARCH_TOOL = types.Tool(
    function_declarations=[
        types.FunctionDeclaration(
            name="search_memory",
            description=(
                "Search the community's memory — the groups' messages, the transcribed sessions and "
                "the shared documents — and return the passages that answer a question, with who "
                "said each one and when. Call this for anything about what was said, decided, "
                "announced or scheduled."
            ),
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "question": types.Schema(
                        type=types.Type.STRING,
                        description="The caller's question, written out in full, in their language.",
                    )
                },
                required=["question"],
            ),
        )
    ]
)


async def _instructions(state) -> str:
    """Who Jeli is, what it knows without looking, and how to speak on a call."""
    brief = getattr(getattr(state, "brief", None), "text", "") or ""
    awareness = ""
    if getattr(state, "awareness", None) is not None:
        try:
            awareness = await state.awareness.state()
        except Exception:
            log.warning("Could not read Jeli's state for a call", exc_info=True)
    return PERSONA + CAPABILITIES + SPOKEN + "\n\n" + background(brief, awareness, "")


async def _search(state, question: str) -> str:
    """The same search as the groups' answers, as text the voice can read out."""
    answerer = getattr(state, "answerer", None)
    if answerer is None:
        return "The memory is not available right now."
    try:
        reply = await answerer.answer(question, asker="a caller", member="")
    except Exception:
        log.warning("A call's memory search failed", exc_info=True)
        return "The memory could not be searched right now."
    return str(reply)[:4000] or "Nothing in the community's memory answers that."


# --- The page -------------------------------------------------------------------------------------


@router.get("/jeli/call", response_class=HTMLResponse)
async def call_page(request: Request) -> HTMLResponse:
    body = (
        f'<div class="login-brand"><span class="login-avatar">{ui.LEMUR}</span><h1>Jeli</h1>'
        "<p>Parlez-lui. Il vous répond de vive voix.</p></div>"
        + ui.card(
            "Appeler Jeli",
            """<div class="call">
              <button id="talk" class="btn primary wide">Appeler</button>
              <p id="state" class="hint">Votre micro ne s'ouvre qu'une fois l'appel commencé, et se ferme quand vous raccrochez.</p>
              <div id="said" class="rows"></div>
            </div>""",
            icon_name="mic",
            description="Posez une question sur le programme, les sessions, les échéances — il cherche dans la mémoire du groupe pendant qu'il vous parle.",
        )
        + '<p class="login-foot">Rien n\'est enregistré : ce que vous dites sert à répondre, puis disparaît.</p>'
        + _script()
    )
    return HTMLResponse(
        f"""<!doctype html>
<html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex"><title>Appeler Jeli</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>{ui.CSS}
.call {{ display: grid; gap: 14px; justify-items: center; padding: 8px 0 4px; }}
.call .btn {{ max-width: 280px; }}
.call.live #talk {{ background: #b3261e; border-color: #b3261e; }}
#said {{ width: 100%; max-height: 260px; overflow-y: auto; }}
#said .row span {{ white-space: pre-wrap; }}
</style></head>
<body class="login-body"><main class="public">{body}</main></body></html>"""
    )


def _script() -> str:
    """Microphone in at 16 kHz, Jeli's voice out at 24 kHz, both as raw PCM over one socket."""
    return """<script>
    const talk = document.getElementById('talk'), state = document.getElementById('state');
    const said = document.getElementById('said');
    let socket, mic, context, out, playAt = 0, live = false, searching = null;

    let open = {};  // the row each speaker is still adding to
    function show(who, text, done) {
      // The transcription arrives a few words at a time. One row per turn, not per fragment:
      // "Jeli / demandé", "Jeli / un récap", "Jeli / de la" was the same sentence, cut to pieces.
      let row = open[who];
      if (!row) {
        row = document.createElement('div');
        row.className = 'row';
        row.innerHTML = '<div class="row-text"><b></b><span></span></div>';
        row.querySelector('b').textContent = who;
        said.appendChild(row);
        open[who] = row;
      }
      const line = row.querySelector('span');
      line.textContent = (line.textContent + ' ' + text).replace(/\s+/g, ' ').trim();
      if (done) delete open[who];
      said.scrollTop = said.scrollHeight;
    }
    function note(text) {  // something Jeli is doing, not something it said
      const row = document.createElement('div');
      row.className = 'row';
      row.innerHTML = '<div class="row-text"><span class="muted"></span></div>';
      row.querySelector('span').textContent = text;
      said.appendChild(row); said.scrollTop = said.scrollHeight;
      return row;
    }

    function play(bytes) {
      if (!out) return;
      const pcm = new Int16Array(bytes.buffer);
      const buffer = out.createBuffer(1, pcm.length, 24000);
      const channel = buffer.getChannelData(0);
      for (let i = 0; i < pcm.length; i++) channel[i] = pcm[i] / 32768;
      const source = out.createBufferSource();
      source.buffer = buffer; source.connect(out.destination);
      playAt = Math.max(playAt, out.currentTime);
      source.start(playAt); playAt += buffer.duration;
    }

    async function start() {
      state.textContent = 'Connexion…';
      const stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true } });
      context = new AudioContext({ sampleRate: 16000 });
      out = new AudioContext({ sampleRate: 24000 });
      socket = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/jeli/call/ws');
      socket.binaryType = 'arraybuffer';
      socket.onopen = () => { state.textContent = 'En ligne — parlez.'; document.querySelector('.call').classList.add('live'); talk.textContent = 'Raccrocher'; live = true; };
      socket.onmessage = (event) => {
        if (typeof event.data !== 'string') return play(new Uint8Array(event.data));
        const message = JSON.parse(event.data);
        if (message.said) show('Vous', message.said);
        if (message.jeli) show('Jeli', message.jeli);
        if (message.turn) { delete open['Vous']; delete open['Jeli']; }
        if (message.searching) {
          searching = note('Jeli cherche dans la mémoire : « ' + message.searching + ' »');
          state.textContent = 'Il consulte la mémoire du groupe…';
        }
        if (message.searched && searching) {
          searching.querySelector('span').textContent += ' — trouvé';
          searching = null; state.textContent = 'En ligne — parlez.';
        }
        if (message.state) state.textContent = message.state;
        if (message.end) stop();
      };
      socket.onclose = () => stop();
      const source = context.createMediaStreamSource(stream);
      const node = context.createScriptProcessor(2048, 1, 1);
      node.onaudioprocess = (event) => {
        if (!socket || socket.readyState !== 1) return;
        const input = event.inputBuffer.getChannelData(0);
        const pcm = new Int16Array(input.length);
        for (let i = 0; i < input.length; i++) pcm[i] = Math.max(-1, Math.min(1, input[i])) * 32767;
        socket.send(pcm.buffer);
      };
      source.connect(node); node.connect(context.destination);
      mic = { stream, node, source };
    }

    function stop() {
      live = false;
      document.querySelector('.call').classList.remove('live');
      talk.textContent = 'Appeler';
      state.textContent = 'Appel terminé.';
      if (mic) { mic.stream.getTracks().forEach(t => t.stop()); mic.node.disconnect(); mic.source.disconnect(); mic = null; }
      if (context) { context.close(); context = null; }
      if (socket && socket.readyState === 1) socket.close();
      socket = null;
    }

    talk.onclick = async () => {
      if (live) return stop();
      try { await start(); }
      catch (error) { state.textContent = "Le micro n'a pas pu s'ouvrir : " + error.message; stop(); }
    };
    </script>"""


# --- The call itself ------------------------------------------------------------------------------


@router.websocket("/jeli/call/ws")
async def call_socket(socket: WebSocket) -> None:
    await socket.accept()
    state = socket.app.state
    llm = getattr(state, "llm", None)
    clients = getattr(llm, "_clients", None) or []
    if not clients:
        await socket.send_text(json.dumps({"state": NO_ENGINE, "end": True}))
        await socket.close()
        return
    client: genai.Client = clients[0]
    instructions = await _instructions(state)
    line = Line(socket)
    watchers = [asyncio.create_task(line.listen()), asyncio.create_task(_hang_up_on_silence(line))]
    handle, model, failures = None, CALL_MODELS[0], 0
    try:
        while not line.gone.is_set():
            opened = time.monotonic()
            try:
                async with client.aio.live.connect(
                    model=model, config=_config(instructions, handle)
                ) as session:
                    log.info("A call is running on %s%s", model, " (resumed)" if handle else "")
                    handle = await _talk(socket, session, state, line) or handle
                # A session that lasted is a session that worked. One that died on opening is a
                # failure however politely it closed, and must not be retried in a tight circle.
                failures = 0 if time.monotonic() - opened > SHORTEST_REAL_SESSION else failures + 1
            except WebSocketDisconnect:
                return
            except Exception:
                failures += 1
                log.warning("A call's session on %s did not open (%d in a row)", model, failures, exc_info=True)
                if failures >= GIVE_UP_AFTER:
                    await _say(socket, {"state": NO_ENGINE, "end": True})
                    return
                # Another Live model may take what this one refused: a call is worth all three.
                model = CALL_MODELS[(CALL_MODELS.index(model) + 1) % len(CALL_MODELS)]
                await asyncio.sleep(RECONNECT_PAUSE)
                continue
            if line.gone.is_set():
                break
            if failures >= GIVE_UP_AFTER:
                await _say(socket, {"state": NO_ENGINE, "end": True})
                return
            # The session ended, which Google does every few minutes. That is a reconnection, not
            # the end of the call — even the first time, before any handle has arrived.
            await _say(socket, {"state": RESUMING})
            if failures:
                await asyncio.sleep(RECONNECT_PAUSE)
        await _say(socket, {"state": GONE_QUIET if line.quiet else "Appel terminé.", "end": True})
    finally:
        for watcher in watchers:
            watcher.cancel()
        with contextlib.suppress(Exception):
            await socket.close()


def _config(instructions: str, handle: str | None) -> types.LiveConnectConfig:
    """How every session of a call is opened.

    Two settings keep a call going. Context window compression slides the window instead of ending
    the session when it fills — measured 23 Sep: without it, the call stopped mid-conversation with
    no warning at all. Session resumption gives a handle to reopen with, so a session that does end
    is picked up where it left off rather than started again from nothing.
    """
    return types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        system_instruction=instructions,
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=VOICE))
        ),
        tools=[SEARCH_TOOL],
        input_audio_transcription=types.AudioTranscriptionConfig(),
        output_audio_transcription=types.AudioTranscriptionConfig(),
        context_window_compression=types.ContextWindowCompressionConfig(
            sliding_window=types.SlidingWindow()
        ),
        session_resumption=types.SessionResumptionConfig(handle=handle),
    )


async def _say(socket: WebSocket, note: dict) -> None:
    with contextlib.suppress(Exception):
        await socket.send_text(json.dumps(note))


class Line:
    """The caller's side of the call: their microphone, and whether they are still on it.

    Read once for the whole call and not per session — a reconnection must not cost the socket its
    reader. And the caller leaving must end the call: measured 23 Sep, a closed tab left its Live
    session running to the stopwatch, holding one of the few slots the free tier allows, so the
    *next* person to call found none and was told Jeli could not take the call.
    """

    def __init__(self, socket: WebSocket):
        self.socket = socket
        self.audio: asyncio.Queue = asyncio.Queue(maxsize=200)
        self.gone = asyncio.Event()
        self.quiet = False
        self.spoke_at = time.monotonic()

    async def listen(self) -> None:
        try:
            while True:
                chunk = await self.socket.receive_bytes()
                if self.audio.full():  # the line is ahead of the model: drop the oldest sound
                    with contextlib.suppress(asyncio.QueueEmpty):
                        self.audio.get_nowait()
                await self.audio.put(chunk)
        except Exception:
            log.info("The caller hung up")
        finally:
            self.gone.set()

    def heard(self) -> None:
        self.spoke_at = time.monotonic()


async def _hang_up_on_silence(line: Line) -> None:
    """A forgotten tab holds a microphone open for hours; a conversation does not go quiet for five
    minutes. So a call ends on silence, never on a stopwatch."""
    while not line.gone.is_set():
        quiet = time.monotonic() - line.spoke_at
        if quiet >= QUIET_SECONDS:
            line.quiet = True
            line.gone.set()
            return
        await asyncio.sleep(min(15.0, QUIET_SECONDS - quiet))


async def _talk(socket: WebSocket, session, state, line: Line) -> str | None:
    """One session of a call. Returns the handle to resume with, or None when there is none."""
    resume: dict[str, str | None] = {"handle": None}

    async def send() -> None:
        while True:
            chunk = await line.audio.get()
            await session.send_realtime_input(audio=types.Blob(data=chunk, mime_type="audio/pcm;rate=16000"))

    async def receive() -> None:
        async for message in session.receive():
            update = getattr(message, "session_resumption_update", None)
            if update is not None and getattr(update, "resumable", False) and update.new_handle:
                resume["handle"] = update.new_handle
            if getattr(message, "go_away", None) is not None:
                # Google warns seconds before closing. Reopening now, while the line is still up,
                # is the difference between a pause and a dropped call.
                log.info("The Live API asked to reconnect; reopening before it closes")
                return
            content = getattr(message, "server_content", None)
            if content is not None:
                for part in getattr(getattr(content, "model_turn", None), "parts", None) or []:
                    data = getattr(getattr(part, "inline_data", None), "data", None)
                    if data:
                        await socket.send_bytes(data)
                heard = getattr(getattr(content, "input_transcription", None), "text", "")
                spoken = getattr(getattr(content, "output_transcription", None), "text", "")
                if heard:
                    line.heard()  # somebody is talking: the silence watchdog starts over
                    await _say(socket, {"said": heard})
                if spoken:
                    await _say(socket, {"jeli": spoken})
                if getattr(content, "turn_complete", False):
                    await _say(socket, {"turn": True})  # the sentence is finished: close the line
            calls = getattr(getattr(message, "tool_call", None), "function_calls", None) or []
            for call in calls:
                question = (call.args or {}).get("question", "")
                log.info("A caller asked the memory: %s", question[:120])
                await _say(socket, {"searching": question})
                found = await _search(state, question)
                await session.send_tool_response(
                    function_responses=[
                        types.FunctionResponse(id=call.id, name=call.name, response={"result": found})
                    ]
                )
                await _say(socket, {"searched": True})

    mouth = asyncio.create_task(send())
    ear = asyncio.create_task(receive())
    left = asyncio.create_task(line.gone.wait())  # the caller leaving ends the session with them
    done, pending = await asyncio.wait({mouth, ear, left}, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
    for task in done:
        error = task.exception()
        if error and not isinstance(error, WebSocketDisconnect):
            log.warning("A call session ended on an error", exc_info=error)
    return resume["handle"]
