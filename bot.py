"""Recipe Log — Telegram bot. Handlers, jobs, entrypoint.

Run: uv run bot.py   (reads .env next to this file)
"""

from __future__ import annotations

import asyncio
import html
import logging
import os
import re
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent
load_dotenv(HERE / ".env")

from telegram import (  # noqa: E402
    BotCommand,
    InlineKeyboardButton as Btn,
    InlineKeyboardMarkup as Kb,
    LinkPreviewOptions,
    Message,
    MessageEntity,
    Update,
)
from telegram.constants import ParseMode  # noqa: E402
from telegram.error import BadRequest  # noqa: E402
from telegram.ext import (  # noqa: E402
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)

import db  # noqa: E402
import export  # noqa: E402
import ingest  # noqa: E402

log = logging.getLogger("recipebot")

ALLOWED_USER_ID = int(os.environ.get("ALLOWED_USER_ID") or 0)
TZ = ZoneInfo(os.environ.get("LOCAL_TZ") or "UTC")
NUDGE_TIME = dtime(int(os.environ.get("NUDGE_HOUR") or 20), int(os.environ.get("NUDGE_MINUTE") or 30),
                   tzinfo=TZ)
DATA_DIR = db.data_dir()

EMOJI = {"good": "👍", "ok": "👌", "bad": "👎"}
VERDICT_BY_EMOJI = {v: k for k, v in EMOJI.items()}
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)
HTML = ParseMode.HTML
UNDO_WINDOW = timedelta(minutes=10)

HELP = """<b>Recipe Log</b>

<b>Save</b>
• Share or paste a link → saved, with title/ingredients when the site allows
• Send a photo (caption = title)

<b>Log a cook</b>
• Tap 👍 👌 👎 on a recipe card
• Or reply to a card with 👍 / 👌 / 👎
• Reply to a card with any other text → added to its notes
• /cooked — pick from recent recipes · /cooked <i>words</i> — search first

<b>Find</b>
• Just type words → search (with an option to save them as a recipe)
• /search <i>words</i> · /recent · /untried · /what (what should I cook?)

<b>Other</b>
/stats · /undo (last save or cook, within 10 min) · /export (Markdown file)"""

COMMANDS = [
    ("cooked", "Log something you cooked"),
    ("what", "Suggest what to cook"),
    ("search", "Search recipes"),
    ("recent", "Recently saved"),
    ("untried", "Never cooked yet"),
    ("stats", "Totals"),
    ("undo", "Undo the last save or cook"),
    ("export", "Download everything as Markdown"),
    ("help", "How to use"),
]


# --------------------------------------------------------------------------- time & formatting

def local_now() -> datetime:
    return datetime.now(TZ)


def today() -> str:
    return local_now().strftime("%Y-%m-%d")


def fmt_day(d: date) -> str:
    s = f"{d.day} {d:%b}"
    return s if d.year == local_now().year else f"{s} {d.year}"


def fmt_local(s: str) -> str:
    return fmt_day(date.fromisoformat(s))


def fmt_utc(ts: str) -> str:
    return fmt_day(db.parse_utc(ts).astimezone(TZ).date())


def esc(s) -> str:
    return html.escape(str(s or ""), quote=True)


def short(s: str, n: int = 40) -> str:
    s = re.sub(r"\s+", " ", s or "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def normalize_emoji(text: str) -> str:
    # drop variation selectors and skin-tone modifiers so 👍🏽 counts as 👍
    return re.sub("[️\U0001F3FB-\U0001F3FF]", "", text.strip())


def remove_files(paths) -> None:
    for p in paths or ():
        try:
            (DATA_DIR / p).unlink(missing_ok=True)
        except OSError:
            log.warning("could not remove %s", p)


# --------------------------------------------------------------------------- the card

def card_keyboard(r) -> Kb:
    rid = r["id"]
    rows = [
        [Btn("👍", callback_data=f"c:{rid}:good"), Btn("👌", callback_data=f"c:{rid}:ok"),
         Btn("👎", callback_data=f"c:{rid}:bad")],
        [Btn("🏷 tag", callback_data=f"t:{rid}"), Btn("🗑", callback_data=f"x:{rid}")],
    ]
    if r["fetch_error"]:
        rows[1].insert(0, Btn("✏️ title", callback_data=f"title:{rid}"))
    return Kb(rows)


def card_text(conn, r, *, prefix: str | None = None, history: bool = False) -> str:
    title = esc(r["title"])
    head = f'🍲 <b><a href="{esc(r["url"])}">{title}</a></b>' if r["url"] else f"🍲 <b>{title}</b>"
    lines = [prefix, head] if prefix else [head]
    lines.append(f"{esc(r['source'] or 'manual')} · saved {fmt_utc(r['added_at'])}")
    n, verdict, last = db.cook_summary(conn, r["id"])
    if n:
        lines.append(f"Cooked {n}× · last: {EMOJI[verdict]} {fmt_local(last)}")
    tags = db.tags_for(conn, r["id"])
    if tags:
        lines.append("🏷 " + ", ".join(esc(t) for t in tags))
    if r["fetch_error"] and "no title found" in r["fetch_error"]:
        lines.append("<i>Couldn't read this page — tap ✏️ to name it.</i>")
    if history and n:
        lines.append("")
        for c in db.cooks_for(conn, r["id"])[:10]:
            note = f" — {esc(short(c['notes'], 80))}" if c["notes"] else ""
            lines.append(f"• {fmt_local(c['cooked_at'])} {EMOJI[c['verdict']]}{note}")
    return "\n".join(lines)


async def send_card(message: Message, conn, recipe_id: int, **kw) -> Message | None:
    r = db.get_recipe(conn, recipe_id)
    if not r:
        await message.reply_text("That recipe no longer exists.")
        return None
    sent = await message.reply_text(card_text(conn, r, **kw), parse_mode=HTML,
                                    reply_markup=card_keyboard(r), link_preview_options=NO_PREVIEW)
    db.map_message(conn, sent.chat_id, sent.message_id, recipe_id)
    return sent


async def edit_into_card(message: Message, conn, recipe_id: int, **kw) -> None:
    r = db.get_recipe(conn, recipe_id)
    if not r:
        await safe_edit(message, "That recipe no longer exists.")
        return
    await safe_edit(message, card_text(conn, r, **kw), reply_markup=card_keyboard(r))
    db.map_message(conn, message.chat_id, message.message_id, recipe_id)


async def safe_edit(message: Message, text: str, reply_markup=None) -> None:
    try:
        await message.edit_text(text, parse_mode=HTML, reply_markup=reply_markup,
                                link_preview_options=NO_PREVIEW)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            raise


def list_keyboard(rows, prefix: str) -> list[list[Btn]]:
    return [[Btn(f"{i}. {short(r['title'])}", callback_data=f"{prefix}:{r['id']}")]
            for i, r in enumerate(rows, 1)]


def list_text(conn, header: str, rows) -> str:
    lines = [header]
    for i, r in enumerate(rows, 1):
        n, verdict, _ = db.cook_summary(conn, r["id"])
        cooked = f" · {EMOJI[verdict]}×{n}" if n else ""
        lines.append(f"{i}. {esc(short(r['title'], 60))} <i>({esc(r['source'] or 'manual')}{cooked})</i>")
    return "\n".join(lines)


# --------------------------------------------------------------------------- auth gate

async def gate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or user.id != ALLOWED_USER_ID:
        if user:
            log.info("ignored update from user %s", user.id)
        raise ApplicationHandlerStop
    msg = update.message
    if msg and msg.text and msg.text.startswith("/"):
        context.user_data.pop("awaiting", None)  # a command abandons any pending prompt


# --------------------------------------------------------------------------- commands

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    with db.session() as conn:
        db.set_config(conn, "chat_id", update.effective_chat.id)
    await update.message.reply_text(HELP, parse_mode=HTML)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP, parse_mode=HTML)


def picker(conn, query: str | None):
    rows = db.search(conn, query, 10) if query else db.list_recent(conn, 10)
    if not rows:
        return ("Nothing matches that." if query else "No recipes yet — share a link first."), None
    return "What did you cook?", Kb(list_keyboard(rows, "pick"))


async def cmd_cooked(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    with db.session() as conn:
        text, kb = picker(conn, " ".join(context.args).strip() or None)
    await update.message.reply_text(text, reply_markup=kb)


async def cmd_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = " ".join(context.args).strip()
    if not q:
        await update.message.reply_text("Usage: /search <words>  (or just type the words)")
        return
    await do_search(update.message, context, q, offer_save=False)


async def do_search(message: Message, context, q: str, offer_save: bool) -> None:
    with db.session() as conn:
        rows = db.search(conn, q, 8)
        text = list_text(conn, f"🔎 <b>{esc(short(q, 60))}</b>", rows) if rows else \
            f"No matches for “{esc(short(q, 60))}”."
    kb = list_keyboard(rows, "v")
    if offer_save:
        kb.append([Btn(f"➕ save “{short(q, 28)}” as a new recipe", callback_data="new")])
    sent = await message.reply_text(text, parse_mode=HTML, reply_markup=Kb(kb) if kb else None)
    if offer_save:
        pending = context.user_data.setdefault("pending_new", {})
        pending[sent.message_id] = q
        for old in sorted(pending)[:-20]:
            pending.pop(old, None)


async def cmd_recent(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    with db.session() as conn:
        rows = db.list_recent(conn, 10)
        text = list_text(conn, "🕘 <b>Recently saved</b>", rows) if rows else "No recipes yet."
    await update.message.reply_text(text, parse_mode=HTML,
                                    reply_markup=Kb(list_keyboard(rows, "v")) if rows else None)


async def cmd_untried(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    with db.session() as conn:
        rows = db.list_untried(conn, 20)
        total = db.count_untried(conn)
        header = f"🆕 <b>Never cooked</b> ({total}, oldest first)"
        text = list_text(conn, header, rows) if rows else "Everything has been cooked at least once 🎉"
    await update.message.reply_text(text, parse_mode=HTML,
                                    reply_markup=Kb(list_keyboard(rows, "v")) if rows else None)


async def cmd_what(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    with db.session() as conn:
        r, why = db.pick_what(conn, today())
        if not r:
            await update.message.reply_text("Nothing to suggest yet — save some recipes first.")
            return
        prefix = ("⭐ A favourite you haven't made in a while:" if why == "favourite"
                  else "🆕 Never cooked yet — how about:")
        await send_card(update.message, conn, r["id"], prefix=prefix)


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    with db.session() as conn:
        s = db.stats(conn, local_now().strftime("%Y-%m"))
    text = (f"📊 <b>{s['recipes']}</b> recipes · <b>{s['cooks']}</b> cooks ({s['good']} 👍)\n"
            f"This month: <b>{s['cooks_month']}</b> cooks\n"
            f"Never cooked: <b>{s['never_cooked']}</b>")
    if s["fetch_errors"]:
        text += f"\nSaved with fetch problems: {s['fetch_errors']}"
    await update.message.reply_text(text, parse_mode=HTML)


async def cmd_undo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    with db.session() as conn:
        last = db.latest_created(conn)
        if not last:
            await update.message.reply_text("Nothing to undo.")
            return
        age = datetime.now(timezone.utc) - db.parse_utc(last["ts"])
        if age > UNDO_WINDOW:
            await update.message.reply_text(
                f"Nothing to undo — the last change was {int(age.total_seconds() // 60)} min ago "
                f"(undo only works within {int(UNDO_WINDOW.total_seconds() // 60)} min).")
            return
        if last["kind"] == "recipe":
            info = db.delete_recipe(conn, last["id"])
            remove_files(info["files"])
            text = f"↩️ Deleted recipe “{esc(info['title'])}”."
        else:
            cook = db.get_cook(conn, last["id"])
            r = db.get_recipe(conn, cook["recipe_id"])
            info = db.delete_cook(conn, last["id"])
            remove_files(info["files"])
            text = (f"↩️ Removed cook {EMOJI[cook['verdict']]} {fmt_local(cook['cooked_at'])} "
                    f"of “{esc(r['title'])}”.")
    await update.message.reply_text(text, parse_mode=HTML)


async def cmd_export(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    out = DATA_DIR / "export" / "recipes.md"
    with db.session() as conn:
        path, count = export.export_markdown(conn, out)
    with open(path, "rb") as f:
        await update.message.reply_document(f, filename=f"recipes-{today()}.md",
                                            caption=f"{count} recipes")


async def cmd_unknown(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Unknown command. /help")


# --------------------------------------------------------------------------- messages

def first_url(message: Message) -> str | None:
    ents = message.parse_entities([MessageEntity.URL, MessageEntity.TEXT_LINK])
    for ent in sorted(ents, key=lambda e: e.offset):
        url = ent.url if ent.type == MessageEntity.TEXT_LINK else ents[ent]
        if url:
            return url if re.match(r"(?i)https?://", url) else "https://" + url
    return ingest.find_url(message.text)


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    text = msg.text or ""
    url = first_url(msg)

    awaiting = context.user_data.pop("awaiting", None)
    bare_link = url and text.strip() in (url, url.removeprefix("https://"))
    if awaiting and not bare_link:
        await handle_awaiting(msg, awaiting, text)
        return

    if url:
        await capture_url(msg, url)
        return

    if msg.reply_to_message:
        with db.session() as conn:
            rid = db.recipe_for_message(conn, msg.chat_id, msg.reply_to_message.message_id)
            if rid and db.get_recipe(conn, rid):
                verdict = VERDICT_BY_EMOJI.get(normalize_emoji(text))
                if verdict:
                    await log_cook(msg, conn, rid, verdict, card=msg.reply_to_message)
                else:
                    db.append_recipe_note(conn, rid, text, today())
                    r = db.get_recipe(conn, rid)
                    await msg.reply_text(f"📝 Note added to “{esc(r['title'])}”.", parse_mode=HTML)
                return

    await do_search(msg, context, text.strip(), offer_save=True)


async def handle_awaiting(msg: Message, awaiting: tuple, text: str) -> None:
    kind, ident = awaiting
    text = text.strip()
    with db.session() as conn:
        if kind == "notes":
            cook = db.get_cook(conn, ident)
            if not cook:
                await msg.reply_text("That cook no longer exists.")
                return
            db.append_cook_note(conn, ident, text)
            r = db.get_recipe(conn, cook["recipe_id"])
            await msg.reply_text(f"📝 Notes saved for {EMOJI[cook['verdict']]} “{esc(r['title'])}”.",
                                 parse_mode=HTML)
        elif kind == "tags":
            if not db.get_recipe(conn, ident):
                await msg.reply_text("That recipe no longer exists.")
                return
            parts = re.split(r"[,\n#]" if re.search(r"[,\n#]", text) else r"\s+", text)
            added = db.add_tags(conn, ident, parts)
            if not added:
                await msg.reply_text("No tags given.")
                return
            await send_card(msg, conn, ident, prefix="🏷 Tagged.")
        elif kind == "title":
            if not db.get_recipe(conn, ident):
                await msg.reply_text("That recipe no longer exists.")
                return
            first, _, rest = text.partition("\n")
            db.update_recipe(conn, ident, title=first.strip()[:300] or "Untitled", fetch_error=None)
            if rest.strip():
                db.append_recipe_note(conn, ident, rest, today())
            await send_card(msg, conn, ident, prefix="✏️ Renamed.")


async def capture_url(msg: Message, url: str) -> None:
    status = await msg.reply_text("⏳ Saving…")
    try:
        res = await asyncio.to_thread(ingest.save_url, url, today())
    except Exception as e:
        log.exception("save_url failed for %s", url)
        # last line of defence for "a capture is never lost"
        try:
            with open(DATA_DIR / "failed_captures.txt", "a", encoding="utf-8") as f:
                f.write(f"{db.utc_now()}\t{url}\t{type(e).__name__}: {e}\n")
        except OSError:
            pass
        await safe_edit(status, f"❌ Couldn't save this link ({esc(type(e).__name__)}). "
                                f"It was written to failed_captures.txt — send it again later.")
        return
    with db.session() as conn:
        if res["already_exists"]:
            await edit_into_card(status, conn, res["recipe_id"], prefix="↪️ Already saved", history=True)
        else:
            await edit_into_card(status, conn, res["recipe_id"])
        db.map_message(conn, msg.chat_id, msg.message_id, res["recipe_id"])


async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    tg_file = msg.photo[-1] if msg.photo else msg.document
    caption = (msg.caption or "").strip()

    awaiting = context.user_data.get("awaiting")
    if awaiting and awaiting[0] == "notes":  # photo of the dish for a cook just logged
        context.user_data.pop("awaiting", None)
        cook_id = awaiting[1]
        with db.session() as conn:
            if not db.get_cook(conn, cook_id):
                await msg.reply_text("That cook no longer exists.")
                return
            rel = f"cook_photos/{cook_id}.jpg"
            try:
                (DATA_DIR / "cook_photos").mkdir(parents=True, exist_ok=True)
                f = await tg_file.get_file()
                await f.download_to_drive(DATA_DIR / rel)
                db.set_cook_photo(conn, cook_id, rel)
            except Exception:
                log.exception("cook photo download failed")
                await msg.reply_text("⚠️ Couldn't download that photo.")
            if caption:
                db.append_cook_note(conn, cook_id, caption)
        await msg.reply_text("📸 Added to that cook.")
        return

    # capture: insert first, then fetch the file, so the row exists whatever happens
    first, _, rest = caption.partition("\n")
    title = first.strip()[:300] or f"Recipe {today()}"
    with db.session() as conn:
        rid = db.insert_recipe(conn, title=title, source="photo",
                               notes=f"[{today()}] {rest.strip()}" if rest.strip() else None)
        try:
            rel = f"images/{rid}.jpg"
            (DATA_DIR / "images").mkdir(parents=True, exist_ok=True)
            f = await tg_file.get_file()
            await f.download_to_drive(DATA_DIR / rel)
            db.update_recipe(conn, rid, image_path=rel)
        except Exception as e:
            log.exception("photo download failed")
            db.update_recipe(conn, rid, fetch_error=f"photo download: {type(e).__name__}: {e}"[:500])
        await send_card(msg, conn, rid)
        db.map_message(conn, msg.chat_id, msg.message_id, rid)
    if not caption:
        await msg.reply_text(
            "What's this recipe called?",
            reply_markup=Kb([[Btn("✏️ Add title", callback_data=f"title:{rid}"),
                              Btn(f"Skip (“{title}”)", callback_data=f"ts:{rid}")]]))


async def log_cook(msg: Message, conn, recipe_id: int, verdict: str, card: Message | None = None) -> None:
    cook_id = db.insert_cook(conn, recipe_id, verdict, today())
    r = db.get_recipe(conn, recipe_id)
    if card is not None and card.from_user and card.from_user.is_bot:
        try:
            await edit_into_card(card, conn, recipe_id)  # refresh the history line
        except Exception:
            log.debug("could not refresh card", exc_info=True)
    sent = await msg.reply_text(
        f"{EMOJI[verdict]} Logged “{esc(r['title'])}” for {fmt_local(today())}.",
        parse_mode=HTML,
        reply_markup=Kb([[Btn("📝 Add notes / photo", callback_data=f"cn:{cook_id}")]]))
    db.map_message(conn, sent.chat_id, sent.message_id, recipe_id)


# --------------------------------------------------------------------------- callbacks

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    parts = (q.data or "").split(":")
    kind, arg = parts[0], (int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None)
    msg = q.message

    with db.session() as conn:
        if kind == "c" and arg and len(parts) == 3 and parts[2] in EMOJI:
            if not db.get_recipe(conn, arg):
                await q.answer("That recipe no longer exists.")
                return
            await q.answer(f"Logged {EMOJI[parts[2]]}")
            await log_cook(msg, conn, arg, parts[2], card=msg)

        elif kind == "cn" and arg:
            cook = db.get_cook(conn, arg)
            if not cook:
                await q.answer("That cook no longer exists.")
                return
            await q.answer()
            context.user_data["awaiting"] = ("notes", arg)
            r = db.get_recipe(conn, cook["recipe_id"])
            await msg.edit_reply_markup(None)
            await msg.reply_text(f"Send notes for “{esc(r['title'])}” — text, or a photo of the dish.",
                                 parse_mode=HTML)

        elif kind == "pick" and arg:
            await q.answer()
            await edit_into_card(msg, conn, arg, prefix="How was it?")

        elif kind == "v" and arg:
            await q.answer()
            await send_card(msg, conn, arg)

        elif kind == "t" and arg:
            r = db.get_recipe(conn, arg)
            if not r:
                await q.answer("That recipe no longer exists.")
                return
            await q.answer()
            context.user_data["awaiting"] = ("tags", arg)
            current = db.tags_for(conn, arg)
            have = f" (has: {esc(', '.join(current))})" if current else ""
            await msg.reply_text(f"Tags for “{esc(r['title'])}”{have}? Comma-separated.",
                                 parse_mode=HTML)

        elif kind == "title" and arg:
            r = db.get_recipe(conn, arg)
            if not r:
                await q.answer("That recipe no longer exists.")
                return
            await q.answer()
            context.user_data["awaiting"] = ("title", arg)
            if msg.text and msg.text.startswith("What's this recipe called"):
                await msg.edit_reply_markup(None)
            await msg.reply_text(f"Send a title for “{esc(r['title'])}”.", parse_mode=HTML)

        elif kind == "ts" and arg:
            await q.answer()
            r = db.get_recipe(conn, arg)
            await safe_edit(msg, f"OK, keeping “{esc(r['title'])}”." if r else "OK.")

        elif kind == "x" and arg:
            await q.answer()
            await msg.edit_reply_markup(Kb([[Btn("🗑 Yes, delete", callback_data=f"xy:{arg}"),
                                              Btn("Cancel", callback_data=f"xc:{arg}")]]))

        elif kind == "xy" and arg:
            info = db.delete_recipe(conn, arg)
            if info:
                remove_files(info["files"])
                await q.answer("Deleted")
                await safe_edit(msg, f"🗑 Deleted “{esc(info['title'])}”.")
            else:
                await q.answer("Already gone")
                await safe_edit(msg, "🗑 Deleted.")

        elif kind == "xc" and arg:
            await q.answer()
            r = db.get_recipe(conn, arg)
            if r:
                await msg.edit_reply_markup(card_keyboard(r))

        elif kind == "more":
            await q.answer()
            text, kb = picker(conn, None)
            await safe_edit(msg, text, reply_markup=kb)

        elif kind == "no":
            await q.answer("OK")
            try:
                await msg.delete()
            except BadRequest:
                await safe_edit(msg, "OK — nothing logged.")

        elif kind == "new":
            text = context.user_data.get("pending_new", {}).pop(msg.message_id, None)
            if not text:
                await q.answer("Expired — send the text again.", show_alert=True)
                return
            first, _, rest = text.partition("\n")
            rid = db.insert_recipe(conn, title=first.strip()[:300], source="manual",
                                   notes=f"[{today()}] {rest.strip()}" if rest.strip() else None)
            await q.answer("Saved")
            await edit_into_card(msg, conn, rid, prefix="➕ Saved")

        else:
            await q.answer()


# --------------------------------------------------------------------------- nudge

async def maybe_nudge(bot, *, catch_up: bool) -> bool:
    """Send today's nudge if all conditions hold. Idempotent via the nudges table."""
    now = local_now()
    day = now.strftime("%Y-%m-%d")
    if catch_up and now.timetz().replace(tzinfo=None) < NUDGE_TIME.replace(tzinfo=None):
        return False
    with db.session() as conn:
        chat_id = db.get_config(conn, "chat_id")
        if not chat_id:
            return False
        if db.nudge_sent(conn, day) or db.has_cook_on(conn, day):
            return False
        since = (datetime.now(timezone.utc) - timedelta(days=14)).strftime("%Y-%m-%dT%H:%M:%SZ")
        if not (db.count_untried(conn) > 0 or db.recent_save_since(conn, since)):
            return False
        rows = db.recent_uncooked(conn, 3) or db.list_recent(conn, 3)
        if not db.claim_nudge(conn, day):  # recorded at send time, before any reply
            return False
    kb = [[Btn(short(r["title"], 24), callback_data=f"pick:{r['id']}")] for r in rows]
    kb.append([Btn("something else", callback_data="more"), Btn("nope", callback_data="no")])
    await bot.send_message(int(chat_id), "Cooked anything today?", reply_markup=Kb(kb))
    log.info("nudge sent for %s", day)
    return True


async def nudge_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    await maybe_nudge(context.bot, catch_up=False)


async def nudge_catch_up_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    # covers a restart, or a laptop that was asleep at NUDGE_TIME and woke later
    await maybe_nudge(context.bot, catch_up=True)


# --------------------------------------------------------------------------- wiring

async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("handler error", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(f"⚠️ Something went wrong: {context.error}")
        except Exception:
            pass


async def post_init(app: Application) -> None:
    await app.bot.set_my_commands([BotCommand(c, d) for c, d in COMMANDS])


def build_app(token: str) -> Application:
    app = Application.builder().token(token).post_init(post_init).build()
    app.add_handler(TypeHandler(Update, gate), group=-1)

    for name, fn in [("start", cmd_start), ("help", cmd_help), ("cooked", cmd_cooked),
                     ("search", cmd_search), ("recent", cmd_recent), ("what", cmd_what),
                     ("untried", cmd_untried), ("stats", cmd_stats), ("undo", cmd_undo),
                     ("export", cmd_export)]:
        app.add_handler(CommandHandler(name, fn, filters=filters.UpdateType.MESSAGE))
    new_msg = filters.UpdateType.MESSAGE
    app.add_handler(MessageHandler(new_msg & filters.COMMAND, cmd_unknown))
    app.add_handler(MessageHandler(new_msg & (filters.PHOTO | filters.Document.IMAGE), on_photo))
    app.add_handler(MessageHandler(new_msg & filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_error_handler(on_error)

    jq = app.job_queue
    jq.run_daily(nudge_job, time=NUDGE_TIME, name="nudge")
    jq.run_once(nudge_catch_up_job, when=15, name="nudge-startup")
    jq.run_repeating(nudge_catch_up_job, interval=600, first=600, name="nudge-catch-up")
    return app


def main() -> None:
    logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    token = os.environ.get("TELEGRAM_TOKEN")
    if not token or not ALLOWED_USER_ID:
        raise SystemExit("TELEGRAM_TOKEN and ALLOWED_USER_ID must be set (see .env.example)")
    db.connect().close()  # create/upgrade the schema up front
    build_app(token).run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
