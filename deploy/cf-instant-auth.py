# Version: v1 · created 2026-11-14 17:15 EST · updated 2026-11-14 17:15 EST
# Changes: v1 v1 initial.
# This file: /home/claude/dev/kanzo/.claude/worktrees/warp-splash/deploy/cf-instant-auth.py
# Session: 1434c01e-24f9-406f-856d-7015b5dd7b69 · vps as claude · cwd /home/claude/dev
# Resume: ssh -t blenko-claude 'cd /home/claude/dev && claude --resume 1434c01e-24f9-406f-856d-7015b5dd7b69'   (from the Mac)
"""Skip the Cloudflare Access login screen for kanzo.blenko.cloud ONLY (Instant Auth).

Sets two fields on the one Access app for kanzo.blenko.cloud: the single allowed login method
(Blenko OIDC, the company sign-in at auth.blenko.cloud) and auto_redirect_to_identity. No
organization setting, login method, policy or other app is read for writing or changed.

    python3 cf-instant-auth.py status     show the two fields and the policies
    python3 cf-instant-auth.py on         company sign-in only, Cloudflare screen skipped
    python3 cf-instant-auth.py off        back to the Cloudflare screen (email PIN or company sign-in)
    python3 cf-instant-auth.py revoke     end every open session of this app only (forces a fresh login)

Run it the way cf-site.py runs: under `bws run` for project dev-vps-runtime, which injects the
Cloudflare credential into this process only. Nothing here prints or stores it.
"""
import os
import sys

sys.path.insert(0, os.path.expanduser("~/.claude/skills/blenko-cloud-publish/scripts"))
import cfapi  # noqa: E402

HOST = "kanzo.blenko.cloud"
# Public id of the login method "Blenko OIDC", the one the blenko.cloud app already pins with Instant Auth.
BLENKO_OIDC = "e5da4164-72da-4f19-b8dd-87d268598544"


def show(app, aid):
    print("app %r: allowed_idps %s auto_redirect %s session %s" % (
        app.get("name"), app.get("allowed_idps"), app.get("auto_redirect_to_identity"), app.get("session_duration")))
    for p in cfapi.list_policies(aid, cfapi.pid(app)):
        print("  policy %r %s: %s" % (p.get("name"), p.get("decision"),
                                     ", ".join(cfapi.describe(i) for i in p.get("include") or [])))


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "status"
    if mode not in ("status", "on", "off", "revoke"):
        sys.exit(__doc__)
    aid = cfapi.zone_account()[1]
    app = cfapi.find_app(aid, HOST)
    if not app:
        sys.exit("no Access app for %s" % HOST)
    base = "/accounts/%s/access/apps/%s" % (aid, cfapi.pid(app))
    if mode == "revoke":
        r = cfapi.api("POST", base + "/revoke_tokens")
        codes = [e.get("code") for e in (r.get("errors") or [])]
        print("revoke sessions of %s: %s" % (HOST, "done" if r.get("success") else "not done, API codes %s" % codes))
        return
    if mode in ("on", "off"):
        body = dict(app)
        body["allowed_idps"] = [BLENKO_OIDC] if mode == "on" else []
        body["auto_redirect_to_identity"] = mode == "on"
        r = cfapi.api("PUT", base, body)
        if not r.get("success"):
            sys.exit("update not applied, API codes %s" % [e.get("code") for e in (r.get("errors") or [])])
        app = cfapi.find_app(aid, HOST)
    show(app, aid)


if __name__ == "__main__":
    main()
