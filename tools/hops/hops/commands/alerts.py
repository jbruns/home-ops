"""hops alerts: firing alerts and active silences in Alertmanager."""
import json

from hops import alertmanager, output

HELP = "firing alerts (including silenced and inhibited ones) and active silences"


def register(subparsers) -> None:
    p = subparsers.add_parser("alerts", help=HELP, description=HELP + ". Read-only.")
    output.add_json_flag(p)
    p.set_defaults(func=run)


def matcher(m: dict) -> str:
    op = ("=" if m.get("isEqual", True) else "!") + ("~" if m.get("isRegex") else "")
    op = "!=" if op == "!" else op
    return f"{m['name']}{op}{json.dumps(m['value'])}"


def run(args, ctx) -> int:
    def query(*q):
        return json.loads(alertmanager.amtool(ctx.runner, ctx.declared, *q, "-o", "json").stdout or "[]")

    alerts = query("alert", "query", "--active", "--silenced", "--inhibited")
    silences = query("silence", "query")
    data = {
        "alerts": [{
            "labels": a.get("labels") or {},
            "annotations": a.get("annotations") or {},
            "state": (a.get("status") or {}).get("state"),
            "starts_at": a.get("startsAt"),
            "silenced_by": (a.get("status") or {}).get("silencedBy") or [],
            "inhibited_by": (a.get("status") or {}).get("inhibitedBy") or [],
        } for a in alerts],
        "silences": [{
            "id": s.get("id"),
            "state": (s.get("status") or {}).get("state"),
            "matchers": [matcher(m) for m in s.get("matchers") or []],
            "starts_at": s.get("startsAt"),
            "ends_at": s.get("endsAt"),
            "created_by": s.get("createdBy"),
            "comment": s.get("comment"),
        } for s in silences],
    }
    if args.json:
        output.print_json(data, ctx.stdout)
        return 0
    ctx.stdout.write("Alerts:\n")
    output.table(["ALERT", "TIER", "STACK", "STATE", "SINCE", "SUMMARY"], [
        [a["labels"].get("alertname"), a["labels"].get("tier"), a["labels"].get("stack"), a["state"],
         a["starts_at"], a["annotations"].get("summary")] for a in data["alerts"]], ctx.stdout)
    ctx.stdout.write("\nSilences:\n")
    output.table(["ID", "ENDS", "BY", "MATCHERS", "COMMENT"], [
        [s["id"], s["ends_at"], s["created_by"], " ".join(s["matchers"]), s["comment"]]
        for s in data["silences"]], ctx.stdout)
    return 0
