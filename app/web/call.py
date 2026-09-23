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
import base64
import json
import logging

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
# A call is a conversation, not a broadcast: past this it hangs up, so a forgotten tab cannot hold
# a session open all night.
MAX_CALL_SECONDS = 600
# What the caller hears if nothing can answer.
NO_ENGINE = "Jeli ne peut pas prendre l'appel pour l'instant."

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
    let socket, mic, context, out, playAt = 0, live = false;

    function show(who, text) {
      const row = document.createElement('div');
      row.className = 'row';
      row.innerHTML = '<div class="row-text"><b></b><span></span></div>';
      row.querySelector('b').textContent = who;
      row.querySelector('span').textContent = text;
      said.appendChild(row); said.scrollTop = said.scrollHeight;
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
        const note = JSON.parse(event.data);
        if (note.said) show('Vous', note.said);
        if (note.jeli) show('Jeli', note.jeli);
        if (note.state) state.textContent = note.state;
        if (note.end) stop();
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
    config = types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        system_instruction=await _instructions(state),
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=VOICE))
        ),
        tools=[SEARCH_TOOL],
        input_audio_transcription=types.AudioTranscriptionConfig(),
        output_audio_transcription=types.AudioTranscriptionConfig(),
    )
    for model in CALL_MODELS:
        try:
            async with client.aio.live.connect(model=model, config=config) as session:
                log.info("A call started on %s", model)
                await _talk(socket, session, state)
            return
        except WebSocketDisconnect:
            return
        except Exception:
            log.warning("Live model %s could not take the call", model, exc_info=True)
    await socket.send_text(json.dumps({"state": NO_ENGINE, "end": True}))
    await socket.close()


async def _talk(socket: WebSocket, session, state) -> None:
    """The caller's voice one way, Jeli's the other, until one of them hangs up."""

    async def listen() -> None:
        while True:
            audio = await socket.receive_bytes()
            await session.send_realtime_input(audio=types.Blob(data=audio, mime_type="audio/pcm;rate=16000"))

    async def speak() -> None:
        async for message in session.receive():
            content = getattr(message, "server_content", None)
            if content is not None:
                for part in getattr(getattr(content, "model_turn", None), "parts", None) or []:
                    data = getattr(getattr(part, "inline_data", None), "data", None)
                    if data:
                        await socket.send_bytes(data)
                heard = getattr(getattr(content, "input_transcription", None), "text", "")
                spoken = getattr(getattr(content, "output_transcription", None), "text", "")
                if heard:
                    await socket.send_text(json.dumps({"said": heard}))
                if spoken:
                    await socket.send_text(json.dumps({"jeli": spoken}))
            calls = getattr(getattr(message, "tool_call", None), "function_calls", None) or []
            for call in calls:
                question = (call.args or {}).get("question", "")
                log.info("A caller asked the memory: %s", question[:120])
                await socket.send_text(json.dumps({"state": "Jeli cherche dans la mémoire…"}))
                found = await _search(state, question)
                await session.send_tool_response(
                    function_responses=[
                        types.FunctionResponse(id=call.id, name=call.name, response={"result": found})
                    ]
                )
                await socket.send_text(json.dumps({"state": "En ligne — parlez."}))

    ears = asyncio.create_task(listen())
    mouth = asyncio.create_task(speak())
    done, pending = await asyncio.wait(
        {ears, mouth}, timeout=MAX_CALL_SECONDS, return_when=asyncio.FIRST_COMPLETED
    )
    for task in pending:
        task.cancel()
    for task in done:
        error = task.exception()
        if error and not isinstance(error, WebSocketDisconnect):
            log.warning("A call ended on an error", exc_info=error)
