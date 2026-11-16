"""A teljesen komolytalan hivatal tartós, tranzakciós iktatókönyve."""

import json
import random
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from utils.hivatal_content import (
    APPEAL_REASONS, CONCLUSIONS, DECISIONS, EVIDENCE, FIRST_NAMES, OFFICES, REASONS, TITLES,
)

DAILY_LIMIT = 5
BUDAPEST = ZoneInfo("Europe/Budapest")
RNG = random.SystemRandom()
HISTORY_WINDOW = 20
MENTION = re.compile(r"<@!?([0-9]{1,19})>")

class OfficeError(Exception):
    """A kérelmezőnek is megmutatható ügyintézési hiba."""


@lru_cache(maxsize=1)
def adjectives():
    path = Path(__file__).resolve().parents[1] / "res" / "adjective.json"
    with path.open(encoding="utf-8") as file:
        words = json.load(file)
    return tuple(dict.fromkeys(word for group in words.values() for word in group
                               if isinstance(word, str) and word.strip() and len(word) <= 50))


def mentioned_users(subject):
    # Role- és everyone-mention nem ad jogot, a régi <@!id> alak viszont igen.
    return tuple(dict.fromkeys(int(match) for match in MENTION.findall(subject)
                               if 0 < int(match) <= 2**63 - 1))


def fresh_choice(options, used):
    available = [option for option in options if option not in used]
    return RNG.choice(available or options)


def make_ruling(previous=None, *, recent=()):
    # Másodfokon kötelező mást mondani. Különben minek lenne másodfok?
    decision = RNG.choice([key for key in DECISIONS if key != previous])
    reasons = [ruling["reason"] for ruling in recent]
    observations = APPEAL_REASONS if previous else REASONS
    observation = fresh_choice(observations, {part for part in observations
                                             if any(part in reason for reason in reasons)})
    conclusion = fresh_choice(CONCLUSIONS, {part for part in CONCLUSIONS
                                           if any(part in reason for reason in reasons)})
    # Külön tartjuk az alapnevet, így az új melléknév nem rejti el az ismétlést.
    office = fresh_choice(OFFICES, {ruling.get("office_base", ruling["office"]) for ruling in recent})
    first_name = fresh_choice(FIRST_NAMES, {ruling.get("first_name") for ruling in recent})
    title = fresh_choice(TITLES, {ruling.get("clerk_title") for ruling in recent})
    adjective = fresh_choice(adjectives(), {ruling.get("clerk_adjective") for ruling in recent})
    used_evidence = {item for ruling in recent for item in ruling["evidence"]}
    evidence_one = fresh_choice(EVIDENCE, used_evidence)
    evidence_two = fresh_choice([item for item in EVIDENCE if item != evidence_one], used_evidence)
    return {
        "decision": decision,
        "office": f"{RNG.choice(adjectives()).capitalize()} {office}",
        "office_base": office,
        "clerk": f"dr. {adjective.capitalize()} {first_name}, {title}",
        "first_name": first_name,
        "clerk_title": title,
        "clerk_adjective": adjective,
        "reason": f"{observation} {conclusion}",
        "evidence": [evidence_one, evidence_two],
        "instruction": fresh_choice(DECISIONS[decision][1], {ruling["instruction"] for ruling in recent}),
    }


@dataclass(frozen=True)
class Case:
    id: int
    guild_id: int
    user_id: int
    subject: str
    created_at: str
    ruling: dict
    appeal: dict | None
    target_ids: tuple[int, ...] = ()

    @property
    def current(self):
        return self.appeal or self.ruling


class CaseBook:
    def __init__(self, path=Path("usr/hivatal.sqlite3")):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""CREATE TABLE IF NOT EXISTS hivatal_cases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                subject TEXT NOT NULL,
                created_at TEXT NOT NULL,
                local_day TEXT NOT NULL,
                ruling TEXT NOT NULL,
                appeal TEXT
            )""")
            db.execute("""CREATE INDEX IF NOT EXISTS hivatal_applicant
                ON hivatal_cases(guild_id, user_id, local_day)""")
            has_targets = db.execute("""SELECT 1 FROM sqlite_master
                WHERE type = 'table' AND name = 'hivatal_targets'""").fetchone()
            db.execute("""CREATE TABLE IF NOT EXISTS hivatal_targets (
                case_id INTEGER NOT NULL REFERENCES hivatal_cases(id),
                user_id INTEGER NOT NULL,
                PRIMARY KEY (case_id, user_id)
            )""")
            db.execute("CREATE INDEX IF NOT EXISTS hivatal_target_user ON hivatal_targets(user_id, case_id)")
            if not has_targets:
                # A korábbi ügyekben tárolt valódi tagek is kapjanak jogorvoslatot.
                for row in db.execute("SELECT id, subject FROM hivatal_cases"):
                    db.executemany("INSERT INTO hivatal_targets (case_id, user_id) VALUES (?, ?)",
                                   ((row["id"], user) for user in mentioned_users(row["subject"])))
            has_history = db.execute("""SELECT 1 FROM sqlite_master
                WHERE type = 'table' AND name = 'hivatal_history'""").fetchone()
            db.execute("""CREATE TABLE IF NOT EXISTS hivatal_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                payload TEXT NOT NULL
            )""")
            db.execute("CREATE INDEX IF NOT EXISTS hivatal_history_guild ON hivatal_history(guild_id, id)")
            if not has_history:
                for guild in db.execute("SELECT DISTINCT guild_id FROM hivatal_cases"):
                    rows = db.execute("""SELECT ruling, appeal FROM hivatal_cases
                        WHERE guild_id = ? ORDER BY id DESC LIMIT ?""", (guild[0], HISTORY_WINDOW))
                    payloads = [payload for row in rows for payload in (row["appeal"], row["ruling"])
                                if payload][:HISTORY_WINDOW]
                    for payload in reversed(payloads):
                        self.remember(db, guild[0], json.loads(payload))

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def case(db, row):
        return Case(
            row["id"], row["guild_id"], row["user_id"], row["subject"],
            row["created_at"], json.loads(row["ruling"]),
            json.loads(row["appeal"]) if row["appeal"] else None,
            tuple(target[0] for target in db.execute(
                "SELECT user_id FROM hivatal_targets WHERE case_id = ? ORDER BY user_id", (row["id"],))),
        )

    @staticmethod
    def history(db, guild_id):
        # A gördülő előzmény szerverenként, újraindítás után is működik.
        return [json.loads(row[0]) for row in db.execute("""SELECT payload FROM hivatal_history
            WHERE guild_id = ? ORDER BY id DESC LIMIT ?""", (guild_id, HISTORY_WINDOW))]

    @staticmethod
    def remember(db, guild_id, ruling):
        db.execute("INSERT INTO hivatal_history (guild_id, payload) VALUES (?, ?)",
                   (guild_id, json.dumps(ruling, ensure_ascii=False)))
        db.execute("""DELETE FROM hivatal_history WHERE guild_id = ? AND id NOT IN (
            SELECT id FROM hivatal_history WHERE guild_id = ? ORDER BY id DESC LIMIT ?
        )""", (guild_id, guild_id, HISTORY_WINDOW))

    def create(self, guild_id, user_id, subject, target_id=None, *, now=None):
        subject = " ".join(subject.split())
        if not 3 <= len(subject) <= 300:
            raise OfficeError("A kérelem 3–300 karakter legyen. Az üres papírt már ismerjük.")
        target_ids = set(mentioned_users(subject))
        if target_id is not None:
            target_ids.add(target_id)
        now = now or datetime.now(timezone.utc)
        day = now.astimezone(BUDAPEST).date().isoformat()
        with closing(self.connect()) as db, db:
            # A limit és a sorszám több egyidejű beadványnál is helyes marad.
            db.execute("BEGIN IMMEDIATE")
            count = db.execute("""SELECT COUNT(*) FROM hivatal_cases
                WHERE guild_id = ? AND user_id = ? AND local_day = ?""",
                (guild_id, user_id, day)).fetchone()[0]
            if count >= DAILY_LIMIT:
                raise OfficeError(
                    f"Mára elfogyott az {DAILY_LIMIT} sorszámod ezen a szerveren. "
                    "Budapesti idő szerint éjfélkor újranyitunk. A kávégép is elfáradt."
                )
            ruling = make_ruling(recent=self.history(db, guild_id))
            cursor = db.execute("""INSERT INTO hivatal_cases
                (guild_id, user_id, subject, created_at, local_day, ruling)
                VALUES (?, ?, ?, ?, ?, ?)""",
                (guild_id, user_id, subject, now.isoformat(), day,
                 json.dumps(ruling, ensure_ascii=False)))
            db.executemany("INSERT INTO hivatal_targets (case_id, user_id) VALUES (?, ?)",
                           ((cursor.lastrowid, target) for target in target_ids))
            self.remember(db, guild_id, ruling)
            return self.case(db, db.execute(
                "SELECT * FROM hivatal_cases WHERE id = ?", (cursor.lastrowid,)
            ).fetchone())

    def get(self, guild_id, case_id):
        with closing(self.connect()) as db:
            row = db.execute("SELECT * FROM hivatal_cases WHERE id = ? AND guild_id = ?",
                             (case_id, guild_id)).fetchone()
            if row is None:
                raise OfficeError("Ilyen iktatószám nincs ezen a szerveren. A darálóban is megnéztük.")
            return self.case(db, row)

    def recent(self, guild_id, user_id):
        with closing(self.connect()) as db:
            return [self.case(db, row) for row in db.execute("""SELECT * FROM hivatal_cases AS cases
                WHERE guild_id = ? AND (user_id = ? OR EXISTS (
                    SELECT 1 FROM hivatal_targets WHERE case_id = cases.id AND user_id = ?
                )) ORDER BY id DESC LIMIT 5""", (guild_id, user_id, user_id))]

    def appeal(self, guild_id, user_id, case_id):
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM hivatal_cases WHERE id = ? AND guild_id = ?",
                             (case_id, guild_id)).fetchone()
            if row is None:
                raise OfficeError("Ilyen iktatószám nincs ezen a szerveren.")
            target = db.execute("SELECT 1 FROM hivatal_targets WHERE case_id = ? AND user_id = ?",
                                (case_id, user_id)).fetchone()
            if row["user_id"] != user_id and not target:
                raise OfficeError("Más papírját ne gyűrögesd. Csak a kérelmező és a megjelölt érintettek fellebbezhetnek.")
            if row["appeal"]:
                raise OfficeError("Ez már jogerős. Harmadfok nincs, csak harmadik kávé. Nézd meg az /hivatal ugy paranccsal.")
            history = self.history(db, guild_id)
            original = json.loads(row["ruling"])
            ruling = make_ruling(original["decision"], recent=[original, *history])
            ruling["appealed_by"] = user_id
            db.execute("UPDATE hivatal_cases SET appeal = ? WHERE id = ?",
                       (json.dumps(ruling, ensure_ascii=False), case_id))
            self.remember(db, guild_id, ruling)
            return self.case(db, db.execute(
                "SELECT * FROM hivatal_cases WHERE id = ?", (case_id,)
            ).fetchone())
