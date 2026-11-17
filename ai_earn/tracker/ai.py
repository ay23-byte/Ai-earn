"""AI assistant handlers (34-41) — extracted."""
from .helpers import _read, _confirm_yes, _key


def handle_list_tools(ai_assistant, token, stdin, out):
    try:
        tools = ai_assistant.registry.list()
        for tl in tools:
            out.write(f"{tl['name']} | {tl['scope']} | {tl['description'][:80]}\n")
        if not tools:
            out.write("No tools registered.\n")
    except Exception as e:
        out.write(str(e) + "\n")


def handle_request_approval(ai_assistant, token, stdin, out):
    tname = _read(stdin, out, "Tool name (consequential only, e.g. publish_service): ").strip()
    args_raw = _read(stdin, out, "Args JSON (e.g. {\"service_id\": \"...\"}): ").strip()
    import json as _json
    try:
        args = _json.loads(args_raw) if args_raw else {}
        apid = ai_assistant.queue.request(token, tname, args, operation_key=_key())
        out.write(f"Approval requested: {apid} (pending, expires in 15m)\n")
        out.write("Use 37 to approve/reject; 41 to execute after approval.\n")
    except Exception as e:
        out.write(str(e) + "\n")


def handle_list_approvals(ai_assistant, token, stdin, out):
    filt = _read(stdin, out, "Filter status [pending/approved/rejected/expired/revoked or empty]: ").strip()
    if filt == "":
        filt = None
    cursor = None
    try:
        while True:
            entries = ai_assistant.queue.list(token, after_id=cursor, limit=20, status=filt)
            for item in entries:
                out.write(f"{item['id']} | {item['tool_name']} | {item['status']}->{item['effective_status']} | expires:{item['expires_at']} | {item['args_json'][:60]}\n")
            if len(entries) < 20:
                break
            if not _confirm_yes(stdin, out, "More? Type YES: "):
                break
            cursor = entries[-1]["id"]
        if cursor is None and not entries:
            out.write("No approvals.\n")
    except Exception as e:
        out.write(str(e) + "\n")


def handle_decide_approval(ai_assistant, token, stdin, out):
    apid = _read(stdin, out, "Approval ID: ").strip()
    dec = _read(stdin, out, "Decision [approved/rejected]: ").strip()
    try:
        out.write(f"Confirm {dec} for {apid}? This is explicit approval.\n")
        if not _confirm_yes(stdin, out, "Type YES to confirm: "):
            out.write("No change.\n")
        else:
            ai_assistant.queue.decide(token, apid, dec, confirmed=True, operation_key=_key())
            out.write(f"Approval {dec}.\n")
    except Exception as e:
        out.write(str(e) + "\n")


def handle_revoke_approval(ai_assistant, token, stdin, out):
    apid = _read(stdin, out, "Approval ID to revoke: ").strip()
    try:
        out.write(f"Revoke {apid}? This prevents future execution.\n")
        if not _confirm_yes(stdin, out, "Type YES to confirm: "):
            out.write("No change.\n")
        else:
            ai_assistant.queue.revoke(token, apid, operation_key=_key())
            out.write("Approval revoked.\n")
    except Exception as e:
        out.write(str(e) + "\n")


def handle_ai_chat(ai_assistant, token, stdin, out):
    prompt = _read(stdin, out, "Prompt (1-4096): ")
    ext = _read(stdin, out, "External content (untrusted, empty for none): ")
    if ext == "":
        ext = None
    try:
        result = ai_assistant.chat(token, prompt, external_content=ext)
        out.write(f"AI draft (unverified, {result['model']}): {result['output'][:500]}\n")
        out.write(f"Policy: {result['policy']}\n")
        out.write("Note: external content treated as data, never as instruction; no consequential tool auto-executed.\n")
    except Exception as e:
        out.write(str(e) + "\n")


def handle_emergency(ai_assistant, token, stdin, out):
    out.write("Emergency: 1 stop  2 clear  (requires explicit YES)\n")
    sub = _read(stdin, out, "Choice (1-2): ").strip()
    try:
        if sub == "1":
            reason = _read(stdin, out, "Reason (0-500, empty for none): ")
            if not _confirm_yes(stdin, out, "Type YES to confirm EMERGENCY STOP: "):
                out.write("No change.\n")
            else:
                ai_assistant.queue.emergency_stop(token, confirmed=True, reason=reason, operation_key=_key())
                out.write("Emergency stop ACTIVE; consequential actions blocked.\n")
        elif sub == "2":
            if not _confirm_yes(stdin, out, "Type YES to confirm CLEAR emergency: "):
                out.write("No change.\n")
            else:
                ai_assistant.queue.emergency_clear(token, confirmed=True, operation_key=_key())
                out.write("Emergency cleared.\n")
        else:
            out.write("Invalid emergency choice.\n")
    except Exception as e:
        out.write(str(e) + "\n")


def handle_execute_tool(ai_assistant, token, stdin, out):
    tname = _read(stdin, out, "Tool name: ").strip()
    args_raw = _read(stdin, out, "Args JSON: ").strip()
    apid = _read(stdin, out, "Approval ID (required for consequential): ").strip()
    if apid == "":
        apid = None
    import json as _json2
    try:
        args = _json2.loads(args_raw) if args_raw else {}
        result = ai_assistant.registry.execute(token, tname, args, ai_assistant._auth, ai_assistant._state, approval_id=apid)
        out.write(f"Executed {tname} -> {result.get('result','ok')} (verification:{result.get('verification')})\n")
        if 'note' in result:
            out.write(result['note'] + "\n")
    except Exception as e:
        out.write(str(e) + "\n")
