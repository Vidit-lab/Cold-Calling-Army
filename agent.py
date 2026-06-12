import asyncio
import json
import logging
import os
import ssl
import time
import certifi
from typing import Optional

from dotenv import load_dotenv

# Patch SSL before any network import
_orig_ssl = ssl.create_default_context
def _certifi_ssl(purpose=ssl.Purpose.SERVER_AUTH, **kwargs):
    if not kwargs.get("cafile") and not kwargs.get("capath") and not kwargs.get("cadata"):
        kwargs["cafile"] = certifi.where()
    return _orig_ssl(purpose, **kwargs)
ssl.create_default_context = _certifi_ssl

from livekit import agents, api, rtc
from livekit.agents import Agent, AgentSession, RoomInputOptions
try:
    from livekit.agents import RoomOptions as _RoomOptions
    _HAS_ROOM_OPTIONS = True
except ImportError:
    _HAS_ROOM_OPTIONS = False
from livekit.plugins import noise_cancellation, silero

from db import init_db, log_error, get_enabled_tools, log_worker_boot

# Bump this whenever you deploy so the Logs tab shows which build is actually live.
WORKER_VERSION = "booking-fix-v5-egress-status"
from prompts import build_prompt
from tools import AppointmentTools

load_dotenv(".env")
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("outbound-agent")

SIP_DOMAIN = os.getenv("VOBIZ_SIP_DOMAIN", "")


async def _log(level: str, msg: str, detail: str = "") -> None:
    if level == "info":      logger.info(msg)
    elif level == "warning": logger.warning(msg)
    else:                    logger.error(msg)
    try:
        await log_error("agent", msg, detail, level)
    except Exception:
        pass


# Never let DB-stored settings override the credentials we use to REACH the DB.
# Otherwise a stale value saved via the dashboard silently redirects the worker
# to a different Supabase, and nothing it writes shows up where the UI reads.
_PROTECTED_BOOTSTRAP_KEYS = {"SUPABASE_URL", "SUPABASE_SERVICE_KEY"}


def load_db_settings_to_env() -> None:
    """Load Supabase settings table into os.environ before worker starts."""
    url = os.getenv("SUPABASE_URL", "")
    key = os.getenv("SUPABASE_SERVICE_KEY", "")
    if not url or not key:
        logger.error("SUPABASE_URL / SUPABASE_SERVICE_KEY missing in worker env — "
                     "call_logs and appointments will NOT be saved.")
        return
    logger.info("Worker Supabase target: %s", url)
    try:
        from supabase import create_client
        client = create_client(url, key)
        result = client.table("settings").select("key, value").execute()
        for row in (result.data or []):
            k = row.get("key")
            if k in _PROTECTED_BOOTSTRAP_KEYS:
                continue  # keep the .env value that actually got us here
            if row.get("value"):
                os.environ[k] = row["value"]
        logger.info("✅ Worker loaded %d settings rows from Supabase", len(result.data or []))
    except Exception as exc:
        logger.warning("Could not load settings from Supabase: %s", exc)


# ── Import Google plugin paths ───────────────────────────────────────────────
_google_realtime = None
_google_beta_realtime = None
_google_llm = None
_google_tts = None

try:
    from livekit.plugins import google as _gp
    try:
        _google_realtime = _gp.realtime.RealtimeModel
        logger.info("Loaded google.realtime.RealtimeModel (stable path)")
    except AttributeError:
        pass
    try:
        _google_beta_realtime = _gp.beta.realtime.RealtimeModel
        logger.info("Loaded google.beta.realtime.RealtimeModel (beta path)")
    except AttributeError:
        pass
    try:
        _google_llm = _gp.LLM
        _google_tts = _gp.TTS
    except AttributeError:
        pass
except ImportError:
    logger.warning("livekit-plugins-google not installed")

_deepgram_stt = None
try:
    from livekit.plugins import deepgram as _dg
    _deepgram_stt = _dg.STT
except ImportError:
    pass


# ── Session factory ──────────────────────────────────────────────────────────

def _build_session(system_prompt: str) -> AgentSession:
    """
    Build AgentSession with Gemini Live or pipeline fallback.

    CRITICAL SILENCE-PREVENTION CONFIG — all 3 required:
    1. SessionResumptionConfig(transparent=True) → auto-reconnects after timeout
    2. ContextWindowCompressionConfig → sliding window prevents token limit freeze
    3. RealtimeInputConfig(END_SENSITIVITY_LOW) → less aggressive VAD, 2s silence threshold

    ⚠️ EndSensitivity MUST use full string form: END_SENSITIVITY_LOW (not .LOW — AttributeError!)
    """
    gemini_model = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-live-preview")
    gemini_voice = os.getenv("GEMINI_TTS_VOICE", "Aoede")
    use_realtime = os.getenv("USE_GEMINI_REALTIME", "true").lower() != "false"

    RealtimeClass = _google_realtime or (_google_beta_realtime if use_realtime else None)

    if use_realtime and RealtimeClass is not None:
        logger.info("SESSION MODE: Gemini Live realtime (%s, voice=%s)", gemini_model, gemini_voice)
        try:
            from google.genai import types as _gt
            _realtime_input_cfg = _gt.RealtimeInputConfig(
                automatic_activity_detection=_gt.AutomaticActivityDetection(
                    end_of_speech_sensitivity=_gt.EndSensitivity.END_SENSITIVITY_LOW,
                    silence_duration_ms=2000,
                    prefix_padding_ms=200,
                ),
            )
            _session_resumption_cfg = _gt.SessionResumptionConfig(transparent=True)
            _ctx_compression_cfg = _gt.ContextWindowCompressionConfig(
                trigger_tokens=25600,
                sliding_window=_gt.SlidingWindow(target_tokens=12800),
            )
            logger.info("Silence-prevention config applied (VAD LOW, transparent resumption, context compression)")
        except Exception as _cfg_err:
            logger.warning("Could not build silence-prevention config: %s", _cfg_err)
            _realtime_input_cfg = None
            _session_resumption_cfg = None
            _ctx_compression_cfg = None

        realtime_kwargs: dict = dict(model=gemini_model, voice=gemini_voice, instructions=system_prompt)
        if _realtime_input_cfg is not None:
            realtime_kwargs["realtime_input_config"]      = _realtime_input_cfg
            realtime_kwargs["session_resumption"]         = _session_resumption_cfg
            realtime_kwargs["context_window_compression"] = _ctx_compression_cfg

        # NOTE: tools are registered on the Agent (OutboundAssistant), NOT here.
        # AgentSession has no `tools` param in livekit-agents 1.x — passing it
        # silently drops the tools and the model can never book/log anything.
        return AgentSession(llm=RealtimeClass(**realtime_kwargs))

    if _google_llm is None:
        raise RuntimeError("No Google AI backend. Run: pip install 'livekit-plugins-google>=1.0'")

    logger.info("SESSION MODE: pipeline (Deepgram STT + Gemini LLM + Google TTS)")
    stt = _deepgram_stt(model="nova-3", language="multi") if _deepgram_stt else None
    tts = _google_tts() if _google_tts else None
    return AgentSession(stt=stt, llm=_google_llm(model="gemini-2.0-flash"), tts=tts, vad=silero.VAD.load())


class OutboundAssistant(Agent):
    def __init__(self, instructions: str, tools: Optional[list] = None) -> None:
        # Tools MUST be attached to the Agent so the LLM can actually call them
        # (book_appointment, end_call, lookup_contact, …). Without this the model
        # talks normally but never invokes a single tool.
        super().__init__(instructions=instructions, tools=tools or [])


async def entrypoint(ctx: agents.JobContext) -> None:
    """
    Main entrypoint. Called per job. Reads metadata JSON from ctx.job.metadata.

    DIAL-FIRST PATTERN — CRITICAL:
    Start Gemini Live ONLY after create_sip_participant(wait_until_answered=True) completes.
    If you start the session during ring time (~20-30s), the Gemini idle timeout fires
    and the session dies silently before the call is even answered.

    NO close_on_disconnect — SIP legs have brief audio dropouts that look like disconnects.
    Instead, watch participant_disconnected event for the specific SIP identity.
    """
    await _log("info", f"Job started — room: {ctx.room.name}")

    phone_number: Optional[str] = None
    lead_name = "there"
    business_name = "our company"
    service_type = "our service"
    custom_prompt: Optional[str] = None
    voice_override: Optional[str] = None
    model_override: Optional[str] = None
    tools_override: Optional[str] = None

    if ctx.job.metadata:
        try:
            data = json.loads(ctx.job.metadata)
            phone_number   = data.get("phone_number")
            lead_name      = data.get("lead_name", lead_name)
            business_name  = data.get("business_name", business_name)
            service_type   = data.get("service_type", service_type)
            custom_prompt  = data.get("system_prompt")
            voice_override = data.get("voice_override")
            model_override = data.get("model_override")
            tools_override = data.get("tools_override")
        except (json.JSONDecodeError, AttributeError):
            await _log("warning", "Invalid JSON in job metadata")

    await _log("info", f"Call job received — phone={phone_number} lead={lead_name} biz={business_name}")

    system_prompt = build_prompt(lead_name=lead_name, business_name=business_name,
                                  service_type=service_type, custom_prompt=custom_prompt)
    tool_ctx = AppointmentTools(ctx, phone_number, lead_name)

    if voice_override:
        os.environ["GEMINI_TTS_VOICE"] = voice_override
    if model_override:
        os.environ["GEMINI_MODEL"] = model_override

    if tools_override:
        try:
            enabled_tools = json.loads(tools_override)
        except Exception:
            enabled_tools = await get_enabled_tools()
    else:
        enabled_tools = await get_enabled_tools()

    # ── Connect ──────────────────────────────────────────────────────────────
    await ctx.connect()
    await _log("info", f"Connected to LiveKit room: {ctx.room.name}")

    # ── Dial — MUST come before session.start() ──────────────────────────────
    if phone_number:
        trunk_id = os.getenv("OUTBOUND_TRUNK_ID")
        if not trunk_id:
            await _log("error", "OUTBOUND_TRUNK_ID not set — cannot place outbound call")
            ctx.shutdown()
            return
        await _log("info", f"Dialing {phone_number} via SIP trunk {trunk_id}")
        try:
            await ctx.api.sip.create_sip_participant(
                api.CreateSIPParticipantRequest(
                    room_name=ctx.room.name,
                    sip_trunk_id=trunk_id,
                    sip_call_to=phone_number,
                    participant_identity=f"sip_{phone_number}",
                    wait_until_answered=True,
                )
            )
        except Exception as exc:
            await _log("error", f"SIP dial FAILED for {phone_number}: {exc}")
            ctx.shutdown()
            return
        await _log("info", f"Call ANSWERED — {phone_number} picked up, starting AI session now")

    # ── Build and start Gemini Live ──────────────────────────────────────────
    gemini_model = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-live-preview")
    await _log("info", f"Building AI session — model={gemini_model}")
    active_tools = tool_ctx.build_tool_list(enabled_tools)
    await _log("info", f"Tools loaded: {[t.__name__ for t in active_tools]}")
    session = _build_session(system_prompt=system_prompt)
    assistant = OutboundAssistant(instructions=system_prompt, tools=active_tools)

    # Use RoomOptions if available (non-deprecated), else fall back
    # NEVER use close_on_disconnect=True with SIP — drops on any audio blip
    if _HAS_ROOM_OPTIONS:
        from livekit.agents import RoomOptions as _RO
        _session_kwargs = dict(
            room=ctx.room,
            agent=assistant,
            room_options=_RO(input_options=RoomInputOptions(noise_cancellation=noise_cancellation.BVCTelephony())),
        )
    else:
        _session_kwargs = dict(
            room=ctx.room,
            agent=assistant,
            room_input_options=RoomInputOptions(noise_cancellation=noise_cancellation.BVCTelephony()),
        )

    # ── Per-call cost tracking ───────────────────────────────────────────────
    # Gemini reports token usage (split by modality) in its own usageMetadata;
    # LiveKit surfaces it via metrics_collected. We accumulate it on tool_ctx so
    # both end_call and the disconnect fallback can price the call. This reads
    # Gemini's numbers only — it is NOT LiveKit-cost tracking.
    @session.on("metrics_collected")
    def _on_metrics(ev) -> None:
        try:
            tool_ctx.usage.add(getattr(ev, "metrics", ev))
        except Exception as _mx:
            logger.warning("usage metrics fold failed: %s", _mx)

    await session.start(**_session_kwargs)
    await _log("info", "Agent session started — AI ready, generating greeting")

    # ── Optional S3 recording ────────────────────────────────────────────────
    _egress_id: Optional[str] = None
    if phone_number:
        _aws_key    = os.getenv("S3_ACCESS_KEY_ID") or os.getenv("AWS_ACCESS_KEY_ID", "")
        _aws_secret = os.getenv("S3_SECRET_ACCESS_KEY") or os.getenv("AWS_SECRET_ACCESS_KEY", "")
        _aws_bucket = os.getenv("S3_BUCKET") or os.getenv("AWS_BUCKET_NAME", "")
        _s3_endpoint = os.getenv("S3_ENDPOINT_URL") or os.getenv("S3_ENDPOINT", "")
        _s3_region  = os.getenv("S3_REGION") or os.getenv("AWS_REGION", "ap-northeast-1")
        if _aws_key and _aws_secret and _aws_bucket:
            try:
                _recording_path = f"recordings/{ctx.room.name}.ogg"
                # Supabase's S3-compatible endpoint ONLY accepts path-style
                # addressing (bucket in the PATH, not the hostname). Without
                # force_path_style the egress uses virtual-hosted style, Supabase
                # rejects it, and the upload fails AFTER the call — silently, so
                # the recordings/ folder is never created. This flag is required.
                _egress_req = api.RoomCompositeEgressRequest(
                    room_name=ctx.room.name, audio_only=True,
                    file_outputs=[api.EncodedFileOutput(
                        file_type=api.EncodedFileType.OGG, filepath=_recording_path,
                        s3=api.S3Upload(access_key=_aws_key, secret=_aws_secret,
                                        bucket=_aws_bucket, region=_s3_region, endpoint=_s3_endpoint,
                                        force_path_style=True),
                    )],
                )
                _egress = await ctx.api.egress.start_room_composite_egress(_egress_req)
                _egress_id = _egress.egress_id
                # Build a BROWSER-PLAYABLE URL, not the S3-protocol path.
                # The egress UPLOADS via /storage/v1/s3 (signed), but that path
                # can never be opened unsigned → "Missing signature / AccessDenied".
                # For a PUBLIC bucket the playable URL is /storage/v1/object/public/.
                _sb_base = os.getenv("SUPABASE_URL", "").rstrip("/")
                if not _sb_base and _s3_endpoint:
                    _sb_base = _s3_endpoint.split("/storage/v1/s3")[0].rstrip("/")
                if _sb_base:
                    tool_ctx.recording_url = f"{_sb_base}/storage/v1/object/public/{_aws_bucket}/{_recording_path}"
                else:
                    tool_ctx.recording_url = f"s3://{_aws_bucket}/{_recording_path}"
                await _log("info", f"Recording started: egress={_egress.egress_id} url={tool_ctx.recording_url}")
            except Exception as _exc:
                await _log("warning", f"Recording start failed (non-fatal): {_exc}")

    # ── Greeting ─────────────────────────────────────────────────────────────
    # gemini-3.1 and gemini-2.5 native-audio speak autonomously from system prompt.
    # generate_reply() is blocked by the plugin for these models — skip it entirely.
    _active_model = os.getenv("GEMINI_MODEL", "")
    if "3.1" in _active_model or "2.5" in _active_model:
        await _log("info", "Gemini native-audio: model will greet autonomously from system prompt")
    else:
        greeting = (
            f"The call just connected. Greet the lead and ask if you're speaking with {lead_name}."
            if phone_number else "Greet the caller warmly."
        )
        try:
            await session.generate_reply(instructions=greeting)
        except Exception as _gr_exc:
            await _log("warning", f"generate_reply failed: {_gr_exc}")

    # ── Keep session alive until SIP participant actually leaves ─────────────
    # Without this block, the entrypoint returns and the process spins down.
    # We watch participant_disconnected for the specific SIP identity.
    if phone_number:
        _sip_identity = f"sip_{phone_number}"
        _disconnect_event = asyncio.Event()

        def _on_participant_disconnected(participant: rtc.RemoteParticipant):
            if participant.identity == _sip_identity:
                _disconnect_event.set()
        def _on_disconnected():
            _disconnect_event.set()

        ctx.room.on("participant_disconnected", _on_participant_disconnected)
        ctx.room.on("disconnected", _on_disconnected)

        # Hard cap on call length. Even if the model never calls end_call AND the
        # carrier never sends a clean hangup, force-terminate at MAX_CALL_SECONDS
        # so we never sit on a 1-hour open line recording dead air and burning
        # Gemini + Vobiz + LiveKit minutes. Default 300s (5 min); tune via env.
        _max_call_seconds = int(os.getenv("MAX_CALL_SECONDS", "300"))
        _hit_cap = False
        try:
            await asyncio.wait_for(_disconnect_event.wait(), timeout=_max_call_seconds)
        except asyncio.TimeoutError:
            _hit_cap = True
            await _log("warning", f"Call hit {_max_call_seconds}s hard cap — force-ending the call")
            # Stopping our wait is NOT enough — the SIP leg would linger until the
            # room's empty_timeout. Delete the room to drop the carrier leg (and
            # stop egress) immediately, so the phone actually hangs up now.
            try:
                await ctx.api.room.delete_room(api.DeleteRoomRequest(room=ctx.room.name))
                await _log("info", "Room deleted on hard cap — carrier leg dropped")
            except Exception as _cap_exc:
                await _log("error", f"Failed to delete room on hard cap: {_cap_exc}")

        if _hit_cap:
            await _log("info", f"Ending session for {phone_number} (hard cap reached)")
        else:
            await _log("info", f"SIP participant disconnected — ending session for {phone_number}")

        # SAFETY NET: guarantee every call lands in Supabase even if the model
        # never invoked end_call. Without this, a model that skips the tool means
        # the call never appears in Stats/CRM/Calls.
        if not tool_ctx.call_logged:
            try:
                from db import log_call
                _dur = int(time.time() - tool_ctx._call_start_time)
                _cost = tool_ctx.usage.cost(os.getenv("GEMINI_MODEL", "gemini-3.1-flash-live-preview"))
                await log_call(
                    phone_number=phone_number, lead_name=lead_name,
                    outcome="completed",
                    reason=(f"force-ended at {_max_call_seconds}s hard cap (model did not call end_call)"
                            if _hit_cap else "auto-logged on disconnect (model did not call end_call)"),
                    duration_seconds=_dur, recording_url=tool_ctx.recording_url,
                    cost_usd=_cost,
                )
                tool_ctx.call_logged = True
                await _log("info", f"Fallback call_log written for {phone_number} (cost=${_cost:.4f})")
            except Exception as _exc:
                await _log("error", f"Fallback call_log FAILED for {phone_number}: {_exc}")

        # Surface the egress UPLOAD result into the dashboard Logs tab so the
        # actual S3 error (SignatureDoesNotMatch / AccessDenied / NoSuchBucket…)
        # is visible without opening the LiveKit Cloud UI.
        if _egress_id:
            try:
                for _ in range(4):  # poll up to ~8s — must fit inside LiveKit's
                                    # ~15s entrypoint-shutdown grace or it gets cancelled
                    _res = await ctx.api.egress.list_egress(api.ListEgressRequest(egress_id=_egress_id))
                    if _res.items:
                        _e = _res.items[0]
                        _terminal = {api.EgressStatus.EGRESS_COMPLETE, api.EgressStatus.EGRESS_FAILED,
                                     api.EgressStatus.EGRESS_ABORTED, api.EgressStatus.EGRESS_LIMIT_REACHED}
                        if _e.status in _terminal:
                            _sname = api.EgressStatus.Name(_e.status)
                            if _e.status == api.EgressStatus.EGRESS_COMPLETE:
                                await _log("info", f"✅ Recording uploaded: egress={_egress_id} status={_sname}")
                            else:
                                await _log("error", f"❌ Recording upload FAILED: egress={_egress_id} "
                                                    f"status={_sname} error={_e.error or '(none)'}")
                            break
                    await asyncio.sleep(2)
            except Exception as _exc:
                await _log("warning", f"Could not fetch egress status for {_egress_id}: {_exc}")

        await session.aclose()
    else:
        _done = asyncio.Event()
        ctx.room.on("disconnected", lambda: _done.set())
        try:
            await asyncio.wait_for(_done.wait(), timeout=3600)
        except asyncio.TimeoutError:
            pass


if __name__ == "__main__":
    init_db()
    load_db_settings_to_env()
    log_worker_boot(WORKER_VERSION)  # visible in dashboard Logs tab → proves new code is live
    agents.cli.run_app(
        agents.WorkerOptions(entrypoint_fnc=entrypoint, agent_name="outbound-caller")
    )
