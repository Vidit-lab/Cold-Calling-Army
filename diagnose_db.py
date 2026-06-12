"""
Supabase write/read diagnostic for Cold-Calling-Army.

Run inside the SAME environment the agent runs in:
    python diagnose_db.py

It tells you EXACTLY why nothing is landing in Supabase:
  • which URL / key are actually in use (and whether the key is service_role or anon)
  • whether each table exists and is readable
  • whether a real INSERT → SELECT → DELETE round-trips on call_logs & appointments
Every failure prints the precise exception instead of swallowing it.
"""
import base64
import json
import os
import ssl
import uuid
from datetime import datetime

import certifi
from dotenv import load_dotenv

# Match the agent/server SSL patch so we test under identical conditions.
_orig_ssl = ssl.create_default_context
def _certifi_ssl(purpose=ssl.Purpose.SERVER_AUTH, **kwargs):
    if not kwargs.get("cafile") and not kwargs.get("capath") and not kwargs.get("cadata"):
        kwargs["cafile"] = certifi.where()
    return _orig_ssl(purpose, **kwargs)
ssl.create_default_context = _certifi_ssl

load_dotenv(".env", override=True)

URL = os.getenv("SUPABASE_URL", "")
KEY = os.getenv("SUPABASE_SERVICE_KEY", "")

GREEN, RED, YEL, RST = "\033[92m", "\033[91m", "\033[93m", "\033[0m"
def ok(m):   print(f"{GREEN}✅ {m}{RST}")
def bad(m):  print(f"{RED}❌ {m}{RST}")
def warn(m): print(f"{YEL}⚠️  {m}{RST}")


def jwt_role(token: str) -> str:
    """Decode the role claim from a Supabase JWT (no verification — just inspection)."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)  # pad base64
        data = json.loads(base64.urlsafe_b64decode(payload))
        return data.get("role", "?")
    except Exception as exc:
        return f"<could not decode: {exc}>"


def main() -> None:
    print("\n" + "=" * 60)
    print(" SUPABASE DIAGNOSTIC")
    print("=" * 60)

    # ── 1. Config ────────────────────────────────────────────────
    if not URL:
        bad("SUPABASE_URL is EMPTY — agent cannot connect. Set it in .env."); return
    if not KEY:
        bad("SUPABASE_SERVICE_KEY is EMPTY — agent cannot connect. Set it in .env."); return
    # ── 0. Is the Supabase library even installed in THIS interpreter? ──
    try:
        import supabase as _sb
        ok(f"supabase package installed (v{getattr(_sb, '__version__', '?')})")
    except ModuleNotFoundError:
        bad("supabase package is NOT installed in this Python environment.")
        warn("Every DB write in the worker silently fails with ModuleNotFoundError.")
        warn("Fix: python -m pip install -r requirements.txt   (in the worker's environment)")
        warn(f"This interpreter: {os.sys.executable}")
        return

    print(f"\nURL : {URL}")
    role = jwt_role(KEY)
    print(f"KEY : ...{KEY[-6:]}  (JWT role = {role})")
    if role == "service_role":
        ok("Key is the service_role key — RLS will be bypassed. Good.")
    elif role == "anon":
        bad("Key is the ANON key, NOT service_role! If RLS is ON, every write is silently dropped.")
        warn("Fix: Supabase → Project Settings → API → copy the 'service_role' secret into SUPABASE_SERVICE_KEY.")
    else:
        warn(f"Could not confirm key role ({role}). Make sure it's the service_role secret.")

    # ── 2. Connect ───────────────────────────────────────────────
    try:
        from supabase import create_client
        db = create_client(URL, KEY)
        ok("Created Supabase client.")
    except Exception as exc:
        bad(f"create_client FAILED: {exc}"); return

    # ── 3. Read every table ──────────────────────────────────────
    tables = ["settings", "call_logs", "appointments", "error_logs",
              "campaigns", "contact_memory", "agent_profiles"]
    print("\n--- TABLE READ TEST ---")
    missing = []
    for t in tables:
        try:
            r = db.table(t).select("*").limit(1).execute()
            ok(f"{t:<16} readable (rows sampled: {len(r.data or [])})")
        except Exception as exc:
            bad(f"{t:<16} READ FAILED: {exc}")
            missing.append(t)
    if missing:
        warn(f"Tables failing reads: {missing}")
        warn("Most likely the schema was never run. Run supabase_schema.sql in Supabase → SQL Editor.")

    # ── 4. Real write round-trip on the two tables that matter ───
    print("\n--- WRITE ROUND-TRIP TEST (insert → read → delete) ---")
    _write_test(db, "call_logs", {
        "id": str(uuid.uuid4()), "phone_number": "+10000000000",
        "lead_name": "DIAGNOSTIC", "outcome": "diagnostic", "reason": "self-test",
        "duration_seconds": 0, "timestamp": datetime.now().isoformat(),
    })
    _write_test(db, "appointments", {
        "id": str(uuid.uuid4()), "name": "DIAGNOSTIC", "phone": "+10000000000",
        "date": "2099-01-01", "time": "00:00", "service": "self-test",
        "status": "booked", "created_at": datetime.now().isoformat(),
    })

    # ── 5. ASYNC client test — this is the path the REAL app uses (_adb) ──
    # init_db() and the test above use the SYNC client. Every actual read/write
    # in the running app goes through the ASYNC client. If sync works but async
    # fails, that alone explains empty logs + no call_logs while init_db says OK.
    print("\n--- ASYNC CLIENT TEST (the path the live app actually uses) ---")
    import asyncio
    asyncio.run(_async_test())

    print("\n" + "=" * 60)
    print(" If SYNC writes ✅ but ASYNC ❌  → the async client path is the bug")
    print("   (db.py _adb()). That's why logs + call_logs are empty.")
    print(" If both ✅  → DB is fine; the worker isn't calling tools (redeploy).")
    print(" If both ❌  → the printed error above is the exact cause.")
    print("=" * 60 + "\n")


async def _async_test() -> None:
    """Replicate db.py's _adb() exactly, then do an async insert→read→delete."""
    # Try the same import the app uses, then the stable public factory.
    client = None
    try:
        from supabase._async.client import create_client as acreate
        client = await acreate(URL, KEY)
        ok("async client via supabase._async.client.create_client (what db.py uses)")
    except Exception as exc:
        bad(f"supabase._async.client.create_client FAILED → {exc}")
        try:
            from supabase import acreate_client
            client = await acreate_client(URL, KEY)
            warn("Fell back to public acreate_client() — db.py should switch to this.")
        except Exception as exc2:
            bad(f"public acreate_client also FAILED → {exc2}")
            return
    rid = str(uuid.uuid4())
    try:
        await client.table("call_logs").insert({
            "id": rid, "phone_number": "+10000000000", "lead_name": "DIAGNOSTIC-ASYNC",
            "outcome": "diagnostic", "reason": "async-self-test",
            "duration_seconds": 0, "timestamp": datetime.now().isoformat(),
        }).execute()
        back = await client.table("call_logs").select("id").eq("id", rid).execute()
        if back.data:
            ok("ASYNC call_logs insert + read-back SUCCEEDED — the live app's path works.")
        else:
            bad("ASYNC insert returned no error but row NOT visible.")
        await client.table("call_logs").delete().eq("id", rid).execute()
    except Exception as exc:
        import traceback
        bad(f"ASYNC call_logs write FAILED → {exc}")
        print(traceback.format_exc())


def _write_test(db, table: str, row: dict) -> None:
    rid = row["id"]
    try:
        db.table(table).insert(row).execute()
    except Exception as exc:
        bad(f"{table}: INSERT FAILED → {exc}")
        return
    try:
        back = db.table(table).select("id").eq("id", rid).execute()
        if back.data:
            ok(f"{table}: insert + read-back SUCCEEDED — writes work here.")
        else:
            bad(f"{table}: insert returned no error but row is NOT visible → RLS is silently dropping writes (wrong key).")
    except Exception as exc:
        bad(f"{table}: read-back FAILED → {exc}")
    finally:
        try:
            db.table(table).delete().eq("id", rid).execute()
        except Exception:
            pass


if __name__ == "__main__":
    main()
