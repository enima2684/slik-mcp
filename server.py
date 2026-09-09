#!/usr/bin/env python3
"""Serveur MCP Slack, lecture plus brouillons, bibliotheque standard uniquement.

Authentification par jetons de session du client web (xoxc + cookie d), donc
sans installation d'app dans le workspace. La surface d'appel est bornee par
ALLOWED_METHODS et ALLOWED_WRITE_METHODS: la seule ecriture possible est
drafts.create, qui depose un brouillon non envoye dans le client Slack.

Usage:
    server.py            transport stdio MCP (lance par le client)
    server.py --check    verifie les jetons et sort
"""

import json
import os
import re
import sys
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

SLACK_API = "https://slack.com/api/"
TIMEOUT = 30
CACHE_TTL = 6 * 3600
CHANNELS_CACHE = os.path.expanduser(
    os.environ.get("SLACK_MCP_CHANNELS_CACHE", "~/.cache/slack-mcp/channels.json"))

# Le seul garde-fou ecriture: un appel hors de cette liste leve avant le reseau.
ALLOWED_METHODS = frozenset({
    "auth.test",
    "conversations.list",
    "conversations.history",
    "conversations.replies",
    "conversations.info",
    "search.messages",
    "users.info",
    "users.list",
    "chat.getPermalink",
})

# Seule ecriture de tout le serveur. drafts.create depose un brouillon dans le
# client Slack et ne le publie pas: il n'existe aucun chemin d'envoi ici.
ALLOWED_WRITE_METHODS = frozenset({"drafts.create"})

DEFAULT_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


class SlackError(Exception):
    pass


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def load_env():
    path = os.environ.get("SLACK_MCP_ENV_FILE", os.path.expanduser("~/.config/slack-mcp/env"))
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def multipart_body(fields):
    boundary = "----slackmcp" + uuid.uuid4().hex
    parts = []
    for key, value in fields.items():
        parts += ["--" + boundary,
                  'Content-Disposition: form-data; name="%s"' % key,
                  "", str(value)]
    parts += ["--" + boundary + "--", ""]
    return boundary, "\r\n".join(parts).encode("utf-8")


def api(method, params=None, multipart=False):
    if method not in ALLOWED_METHODS and method not in ALLOWED_WRITE_METHODS:
        raise SlackError("methode non autorisee: " + method)
    token = os.environ.get("SLACK_MCP_XOXC_TOKEN", "").strip()
    cookie = os.environ.get("SLACK_MCP_XOXD_TOKEN", "").strip()
    if not token or not cookie:
        raise SlackError("SLACK_MCP_XOXC_TOKEN ou SLACK_MCP_XOXD_TOKEN manquant")

    fields = {k: v for k, v in (params or {}).items() if v not in (None, "")}
    if multipart:
        boundary, body = multipart_body(fields)
        content_type = "multipart/form-data; boundary=" + boundary
    else:
        body = urllib.parse.urlencode(fields).encode("utf-8")
        content_type = "application/x-www-form-urlencoded; charset=utf-8"

    for attempt in range(3):
        req = urllib.request.Request(SLACK_API + method, data=body)
        req.add_header("Authorization", "Bearer " + token)
        # Un jeton xoxc n'est valide qu'accompagne du cookie de session. On
        # decode puis re-encode pour accepter un xoxd colle brut ou deja encode.
        req.add_header("Cookie", "d=" + urllib.parse.quote(urllib.parse.unquote(cookie), safe=""))
        req.add_header("Content-Type", content_type)
        req.add_header("User-Agent", os.environ.get("SLACK_MCP_USER_AGENT") or DEFAULT_UA)
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and attempt < 2:
                time.sleep(max(1, int(exc.headers.get("Retry-After", "2") or 2)))
                continue
            raise SlackError("HTTP %s sur %s" % (exc.code, method))
        except urllib.error.URLError as exc:
            raise SlackError("reseau injoignable: %s" % exc.reason)

        if payload.get("ok"):
            return payload
        err = payload.get("error", "erreur inconnue")
        if err == "ratelimited" and attempt < 2:
            time.sleep(2)
            continue
        if err in ("invalid_auth", "not_authed", "token_revoked"):
            raise SlackError("jetons invalides ou expires (%s): reprendre xoxc et xoxd" % err)
        raise SlackError("%s: %s" % (method, err))
    raise SlackError("%s: abandon apres 3 tentatives" % method)


def paginate(method, params, key, max_pages=10):
    out, cursor = [], ""
    for _ in range(max_pages):
        payload = api(method, dict(params, cursor=cursor) if cursor else params)
        out.extend(payload.get(key) or [])
        cursor = (payload.get("response_metadata") or {}).get("next_cursor") or ""
        if not cursor:
            break
    return out


_users = {}
_channels = {"ts": 0.0, "by_id": {}, "by_name": {}}


def user_name(user_id):
    if not user_id:
        return ""
    if user_id not in _users:
        try:
            info = api("users.info", {"user": user_id})["user"]
            _users[user_id] = info.get("real_name") or info.get("name") or user_id
        except SlackError:
            _users[user_id] = user_id
    return _users[user_id]


def load_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def dump_json(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)
    os.replace(tmp, path)


def channel_index(refresh=False):
    if not refresh and _channels["ts"] > time.time() - CACHE_TTL:
        return _channels

    # Reconstruire l'index coute une vingtaine d'appels: sans cache sur disque
    # chaque demarrage du serveur les repaie, ce qui rend une boucle de
    # surveillance inutilisable.
    if not refresh:
        cached = load_json(CHANNELS_CACHE)
        if cached and cached.get("ts", 0) > time.time() - CACHE_TTL:
            _channels.update(cached)
            return _channels

    rows = paginate(
        "conversations.list",
        {"types": "public_channel,private_channel,mpim,im", "limit": 1000, "exclude_archived": "true"},
        "channels",
        max_pages=20,
    )
    _channels["by_id"] = {c["id"]: c for c in rows}
    by_name = {c["name"]: c["id"] for c in rows if c.get("name")}
    # Les DM n'ont pas de nom: on les indexe sous "@" + nom du correspondant,
    # sinon la forme "@personne" annoncee par le schema ne resout jamais.
    for c in rows:
        if c.get("is_im") and c.get("user"):
            by_name["@" + user_name(c["user"])] = c["id"]
    _channels["by_name"] = by_name
    _channels["ts"] = time.time()
    try:
        dump_json(CHANNELS_CACHE, dict(_channels))
    except OSError:
        pass  # un cache non ecrit degrade la latence, pas le resultat
    return _channels


def resolve_channel(ref):
    ref = (ref or "").strip()
    if not ref:
        raise SlackError("channel manquant")
    if re.fullmatch(r"[CDG][A-Z0-9]+", ref):
        return ref
    idx = channel_index()
    name = ref if ref.startswith("@") else ref.lstrip("#")
    if name in idx["by_name"]:
        return idx["by_name"][name]
    idx = channel_index(refresh=True)
    if name in idx["by_name"]:
        return idx["by_name"][name]
    raise SlackError("canal introuvable: " + ref)


# Slack encode les mentions en <@U123> ou <@U123|alias>; sans resolution le
# texte remonte un identifiant que personne ne peut lire.
MENTION_RE = re.compile(r"<@([UW][A-Z0-9]+)(?:\|[^>]*)?>")


def resolve_mentions(text):
    return MENTION_RE.sub(lambda m: "@" + user_name(m.group(1)), text or "")


USER_ID_RE = re.compile(r"[UW][A-Z0-9]+$")


def channel_label(chan):
    """Nom lisible d'un canal renvoye par la recherche.

    En conversation directe, Slack met l'identifiant du correspondant dans le
    champ nom, ce qui remonte un Uxxxx illisible si on le prend tel quel.
    """
    if not isinstance(chan, dict):
        return ""
    name = chan.get("name") or ""
    if chan.get("is_im") or USER_ID_RE.fullmatch(name):
        return "@" + user_name(chan.get("user") or name)
    return name or chan.get("id", "")


def iso(ts):
    try:
        return datetime.fromtimestamp(float(ts)).astimezone().isoformat(timespec="seconds")
    except (TypeError, ValueError):
        return ""


def shape(msg):
    row = {
        "ts": msg.get("ts"),
        "time": iso(msg.get("ts")),
        "user": user_name(msg.get("user") or msg.get("bot_id", "")),
        "text": resolve_mentions(msg.get("text", "")),
    }
    if msg.get("thread_ts") and msg.get("thread_ts") != msg.get("ts"):
        row["thread_ts"] = msg["thread_ts"]
    if msg.get("reply_count"):
        row["reply_count"] = msg["reply_count"]
    if msg.get("permalink"):
        row["permalink"] = msg["permalink"]
    return row


def tool_channels(args):
    idx = channel_index(refresh=bool(args.get("refresh")))
    query = (args.get("query") or "").lstrip("#").lower()
    wanted = set((args.get("types") or "public_channel,private_channel").split(","))
    rows = []
    for chan in idx["by_id"].values():
        kind = ("im" if chan.get("is_im") else "mpim" if chan.get("is_mpim")
                else "private_channel" if chan.get("is_private") else "public_channel")
        if kind not in wanted:
            continue
        name = chan.get("name") or user_name(chan.get("user", ""))
        if query and query not in name.lower():
            continue
        rows.append({"id": chan["id"], "name": name, "type": kind,
                     "members": chan.get("num_members"), "topic": (chan.get("topic") or {}).get("value", "")})
    rows.sort(key=lambda r: -(r.get("members") or 0))
    return rows[: int(args.get("limit") or 100)]


def tool_history(args):
    channel = resolve_channel(args.get("channel"))
    payload = api("conversations.history", {
        "channel": channel,
        "limit": int(args.get("limit") or 50),
        "oldest": args.get("oldest"),
        "latest": args.get("latest"),
        "cursor": args.get("cursor"),
    })
    return {
        "channel": channel,
        "messages": [shape(m) for m in payload.get("messages", [])],
        "next_cursor": (payload.get("response_metadata") or {}).get("next_cursor", ""),
    }


def tool_thread(args):
    channel = resolve_channel(args.get("channel"))
    payload = api("conversations.replies", {
        "channel": channel,
        "ts": args.get("thread_ts"),
        "limit": int(args.get("limit") or 100),
        "cursor": args.get("cursor"),
    })
    return {
        "channel": channel,
        "messages": [shape(m) for m in payload.get("messages", [])],
        "next_cursor": (payload.get("response_metadata") or {}).get("next_cursor", ""),
    }


def tool_search(args):
    payload = api("search.messages", {
        "query": args.get("query"),
        "count": int(args.get("count") or 20),
        "page": int(args.get("page") or 1),
        "sort": args.get("sort") or "timestamp",
    })
    matches = (payload.get("messages") or {}).get("matches") or []
    rows = []
    for m in matches:
        row = shape(m)
        row["channel"] = channel_label(m.get("channel"))
        rows.append(row)
    paging = (payload.get("messages") or {}).get("paging") or {}
    return {"total": (payload.get("messages") or {}).get("total"), "page": paging.get("page"),
            "pages": paging.get("pages"), "matches": rows}


def tool_users(args):
    query = (args.get("query") or "").lower()
    rows = []
    for member in paginate("users.list", {"limit": 200}, "members", max_pages=10):
        if member.get("deleted"):
            continue
        profile = member.get("profile") or {}
        blob = " ".join([member.get("name", ""), member.get("real_name", ""),
                         profile.get("display_name", ""), profile.get("email", "")]).lower()
        if query and query not in blob:
            continue
        rows.append({"id": member["id"], "name": member.get("name"),
                     "real_name": member.get("real_name"), "email": profile.get("email"),
                     "title": profile.get("title")})
        if len(rows) >= int(args.get("limit") or 20):
            break
    return rows


def tool_permalink(args):
    channel = resolve_channel(args.get("channel"))
    return {"permalink": api("chat.getPermalink", {"channel": channel, "message_ts": args.get("ts")}).get("permalink")}


WATCH_STATE = os.path.expanduser(
    os.environ.get("SLACK_MCP_WATCH_STATE", "~/.cache/slack-mcp/watch.json"))


def read_watch_state():
    try:
        with open(WATCH_STATE, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def write_watch_state(state):
    os.makedirs(os.path.dirname(WATCH_STATE), exist_ok=True)
    tmp = WATCH_STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=1)
    os.replace(tmp, WATCH_STATE)


def tool_watch(args):
    channel = resolve_channel(args.get("channel"))
    state = read_watch_state()
    since = args.get("since") or state.get(channel)

    # Premier appel sans repere: on pose la borne au present et on ne remonte
    # rien, sinon la surveillance rejouerait tout l'historique du canal.
    if not since:
        state[channel] = "%.6f" % time.time()
        write_watch_state(state)
        return {"channel": channel, "new": 0, "messages": [],
                "cursor": state[channel], "note": "repere pose, rien remonte au premier appel"}

    payload = api("conversations.history", {"channel": channel, "oldest": since, "limit": 200})
    # oldest est inclusif chez Slack: sans ce filtre le dernier message vu
    # ressortirait a chaque tour.
    fresh = [m for m in payload.get("messages", []) if m.get("ts") != since]

    if fresh and not args.get("peek"):
        state[channel] = fresh[0]["ts"]
        write_watch_state(state)

    return {
        "channel": channel,
        "new": len(fresh),
        "messages": [shape(m) for m in reversed(fresh)],
        "cursor": state.get(channel),
    }


def tool_draft(args):
    channel = resolve_channel(args.get("channel"))
    text = args.get("text") or ""
    if not text.strip():
        raise SlackError("texte vide")

    destination = {"channel_id": channel}
    if args.get("thread_ts"):
        destination["thread_ts"] = args["thread_ts"]
        destination["broadcast"] = False

    fields = {
        "client_msg_id": str(uuid.uuid4()),
        "blocks": json.dumps([{"type": "rich_text", "elements": [
            {"type": "rich_text_section", "elements": [{"type": "text", "text": text}]}]}]),
        "destinations": json.dumps([destination]),
        "file_ids": "[]",
        "is_from_composer": "false",
    }
    # Endpoint interne, encodage non documente: le client web poste en
    # multipart, d'autres implementations en form-urlencode. On tente le
    # second, moins couteux, et on repasse en multipart si Slack le refuse.
    try:
        payload = api("drafts.create", fields)
    except SlackError as exc:
        message = str(exc).lower()
        # Slack n'accepte qu'un brouillon par conversation, et le refus est mal
        # nomme: sans cette traduction l'agent croit a un probleme d'auth.
        if "attached_draft_exists" in message:
            raise SlackError("un brouillon existe deja dans cette conversation: "
                             "le vider ou l'envoyer dans Slack avant d'en creer un autre")
        if "invalid" not in message:
            raise
        payload = api("drafts.create", fields, multipart=True)

    # La cle portant l'identifiant n'est pas documentee et a change par le
    # passe: on remonte ce que la reponse contient plutot que de la deviner.
    ids = {k: v for k, v in payload.items() if "id" in k.lower() and isinstance(v, (str, int))}
    draft = payload.get("draft")
    if isinstance(draft, dict):
        ids.update({k: v for k, v in draft.items() if "id" in k.lower() and isinstance(v, (str, int))})
    return {
        "ids": ids,
        "channel": channel,
        "note": "Brouillon depose dans Slack (section Brouillons). Non envoye.",
    }


TOOLS = [
    {
        "name": "slack_channels",
        "description": "Liste ou filtre les canaux du workspace (id, nom, type, nombre de membres).",
        "inputSchema": {"type": "object", "properties": {
            "query": {"type": "string", "description": "Filtre sur le nom, sous-chaine."},
            "types": {"type": "string", "description": "public_channel,private_channel,mpim,im"},
            "limit": {"type": "integer"}, "refresh": {"type": "boolean"}}},
        "handler": tool_channels,
    },
    {
        "name": "slack_history",
        "description": "Messages d'un canal, par id Cxxxx ou par #nom. Pagination par cursor.",
        "inputSchema": {"type": "object", "properties": {
            "channel": {"type": "string"}, "limit": {"type": "integer"},
            "oldest": {"type": "string", "description": "Timestamp Slack de debut."},
            "latest": {"type": "string"}, "cursor": {"type": "string"}},
            "required": ["channel"]},
        "handler": tool_history,
    },
    {
        "name": "slack_thread",
        "description": "Fil complet a partir du ts du message parent.",
        "inputSchema": {"type": "object", "properties": {
            "channel": {"type": "string"}, "thread_ts": {"type": "string"},
            "limit": {"type": "integer"}, "cursor": {"type": "string"}},
            "required": ["channel", "thread_ts"]},
        "handler": tool_thread,
    },
    {
        "name": "slack_search",
        "description": "Recherche de messages. Accepte la syntaxe Slack (in:#canal, from:@user, after:2026-01-01).",
        "inputSchema": {"type": "object", "properties": {
            "query": {"type": "string"}, "count": {"type": "integer"},
            "page": {"type": "integer"}, "sort": {"type": "string"}},
            "required": ["query"]},
        "handler": tool_search,
    },
    {
        "name": "slack_users",
        "description": "Cherche un membre par nom, identifiant ou email.",
        "inputSchema": {"type": "object", "properties": {
            "query": {"type": "string"}, "limit": {"type": "integer"}}},
        "handler": tool_users,
    },
    {
        "name": "slack_permalink",
        "description": "Lien permanent d'un message, pour le citer ailleurs.",
        "inputSchema": {"type": "object", "properties": {
            "channel": {"type": "string"}, "ts": {"type": "string"}},
            "required": ["channel", "ts"]},
        "handler": tool_permalink,
    },
    {
        "name": "slack_draft",
        "description": ("Depose un brouillon non envoye dans le client Slack, visible dans la section "
                        "Brouillons. N'envoie jamais: la relecture et l'envoi restent manuels. Le texte "
                        "est insere tel quel, sans interpretation du markdown Slack."),
        "inputSchema": {"type": "object", "properties": {
            "channel": {"type": "string", "description": "Id Cxxxx, #canal ou @personne."},
            "text": {"type": "string", "description": "Corps du message, sauts de ligne conserves."},
            "thread_ts": {"type": "string", "description": "Pour brouillonner une reponse dans un fil."}},
            "required": ["channel", "text"]},
        "handler": tool_draft,
    },
    {
        "name": "slack_watch",
        "description": ("Messages arrives dans un canal depuis le dernier appel, et avance le repere. "
                        "Le premier appel pose le repere et ne remonte rien. A appeler en boucle par "
                        "un pilote: le serveur ne signale rien de lui-meme."),
        "inputSchema": {"type": "object", "properties": {
            "channel": {"type": "string", "description": "Id Cxxxx, #canal ou @personne."},
            "since": {"type": "string", "description": "Forcer le point de depart, timestamp Slack."},
            "peek": {"type": "boolean", "description": "Lire sans avancer le repere."}},
            "required": ["channel"]},
        "handler": tool_watch,
    },
]

HANDLERS = {t["name"]: t.pop("handler") for t in TOOLS}
KNOWN_PROTOCOLS = {"2024-11-05", "2025-03-26", "2025-06-18"}


def respond(msg_id, result=None, error=None):
    out = {"jsonrpc": "2.0", "id": msg_id}
    if error is not None:
        out["error"] = error
    else:
        out["result"] = result
    sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def handle(msg):
    method = msg.get("method")
    msg_id = msg.get("id")
    if msg_id is None:
        return  # notification: rien a renvoyer

    if method == "initialize":
        asked = (msg.get("params") or {}).get("protocolVersion")
        respond(msg_id, {
            "protocolVersion": asked if asked in KNOWN_PROTOCOLS else "2025-06-18",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "slack-local", "version": "1.0.0"},
        })
    elif method == "ping":
        respond(msg_id, {})
    elif method == "tools/list":
        respond(msg_id, {"tools": TOOLS})
    elif method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name")
        if name not in HANDLERS:
            respond(msg_id, error={"code": -32602, "message": "outil inconnu: %s" % name})
            return
        try:
            result = HANDLERS[name](params.get("arguments") or {})
            payload = json.dumps(result, ensure_ascii=False, indent=1)
            respond(msg_id, {"content": [{"type": "text", "text": payload}], "isError": False})
        except SlackError as exc:
            respond(msg_id, {"content": [{"type": "text", "text": str(exc)}], "isError": True})
        except Exception as exc:  # remonte l'erreur a l'agent plutot que tuer le serveur
            respond(msg_id, {"content": [{"type": "text", "text": "%s: %s" % (type(exc).__name__, exc)}],
                             "isError": True})
    else:
        respond(msg_id, error={"code": -32601, "message": "methode non supportee: %s" % method})


def main():
    load_env()
    if "--check" in sys.argv:
        try:
            who = api("auth.test")
            log("ok: %s sur %s (%s)" % (who.get("user"), who.get("team"), who.get("url")))
            return 0
        except SlackError as exc:
            log("echec: %s" % exc)
            return 1
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        handle(msg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
