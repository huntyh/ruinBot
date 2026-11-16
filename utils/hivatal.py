"""A teljesen komolytalan hivatal tartós, tranzakciós iktatókönyve."""

import json
import random
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


DAILY_LIMIT = 5
BUDAPEST = ZoneInfo("Europe/Budapest")
RNG = random.SystemRandom()

OFFICES = (
    "Indokolatlan Bonyolítások Főosztálya",
    "Majd Hétfőn Osztály",
    "Központi Vibe-ellenőrzési Hatóság",
    "Söralátét-hitelesítési Főigazgatóság",
    "Passzív-agresszív Ügyintézési Központ",
    "Digitális Semmittevési Minisztérium",
    "Általános Tanácstalansági Hivatal",
    "Kifogásügyi és Kávészüneti Bizottság",
)
CLERKS = (
    "dr. Nem Azénasztalom Elemér",
    "Pecsét Elek, helyettes helyettes",
    "Iktató Irén, a laminálás doktora",
    "dr. Majd Visszahívjuk Béla",
    "Űrlap Ottó, megbízott akadály",
    "Kávészünet Katalin, jelenleg távol",
)
EVIDENCE = (
    "egy screenshot, amin pont a lényeg nem látszik",
    "három darab bizalmasan bólogató emoji",
    "egy 2014 óta szerkesztés alatt álló Google-dokumentum",
    "a kérelmező által beküldött „trust me bro” szakvélemény",
    "egy névtelen söralátét terhelő vallomása",
    "a bizottság közös álma egy rosszul szellőző tárgyalóban",
    "egy hangüzenet, amit senki nem volt hajlandó meghallgatni",
    "a vibe-ok hitelesített, de olvashatatlan fénymásolata",
    "egy Excel, amiben minden cella kézzel zöldre van festve",
    "a nyomtató szerint fennálló papírelakadás",
)
REASONS = (
    "A kérelem túl érthető volt, ezért szakértő bevonása vált szükségessé.",
    "A bizottság 4:3 arányban megszavazta, hogy heten vannak.",
    "A benyújtott indoklás megfelel a „végül is” jogelvének.",
    "A Merkúr jelenleg a nyomtatóban van, ami eljárásjogi akadály.",
    "Az ügyintéző elolvasta az első három szót, és kialakult a szakmai álláspontja.",
    "A szerver kollektív mentális állapotát egy ferde pecsét megfelelően tükrözi.",
    "A tényállást összevetettük egy pogácsával. A pogácsa meggyőzőbb volt.",
    "Precedensértékű, hogy ezt valaki egyáltalán megkérdezte.",
)
DECISIONS = {
    "approved": ("ENGEDÉLYEZVE", (
        "Az engedély kizárólag papucsban és indokolatlan önbizalommal érvényes.",
        "A végrehajtás előtt kötelező ünnepélyesen kijelenteni: „ezt a hivatal is megmondta”.",
        "A sikerért a hivatal vállalja az elismerést. Minden más a kérelmező érdeme.",
        "Az engedély átruházhatatlan, de egy jó sztoriban szabad rá hivatkozni.",
    )),
    "denied": ("ELUTASÍTVA", (
        "Helyette javasoljuk a plafon nézését, hatóságilag ajánlott intenzitással.",
        "A kérelmező vigaszdíja egy képzeletbeli langyos Kőbányai.",
        "Az ügyet áthelyeztük a „hát ez van tesó” nyilvántartásba.",
        "A hivatal együttérez, de csak ügyfélfogadási időben: február 30-án.",
    )),
    "incomplete": ("HIÁNYPÓTLÁS", (
        "Csatolj két tanúval hitelesített képernyőképet arról, hogy van kedved élni a lehetőséggel.",
        "Pótold a 420/B jelű nyomtatványt, amely igazolja, hogy nincs szükséged nyomtatványra.",
        "Szükséges egy eredeti, színes fénymásolat a hétfő reggeli lelkesedésedről.",
        "Mellékelj három példányban egy szülői igazolást a belső gyermekedtől.",
    )),
}
APPEAL_REASONS = (
    "Az elsőfokú döntést megfordítottuk, mert a lap másik oldalán több hely volt.",
    "A felülvizsgálat során kiderült, hogy az első ügyintéző egy kabát volt a széken.",
    "A másodfok független: az elsőfokú bizottság bajusszal ült vissza.",
    "A fellebbezés megalapozott: tényleg ezt írtuk. Most inkább mást írunk.",
)


class OfficeError(Exception):
    """A kérelmezőnek is megmutatható ügyintézési hiba."""


def make_ruling(previous=None):
    # Másodfokon kötelező mást mondani. Különben minek lenne másodfok?
    decision = RNG.choice([key for key in DECISIONS if key != previous])
    return {
        "decision": decision,
        "office": RNG.choice(OFFICES),
        "clerk": RNG.choice(CLERKS),
        "reason": RNG.choice(APPEAL_REASONS if previous else REASONS),
        "evidence": RNG.sample(EVIDENCE, 2),
        "instruction": RNG.choice(DECISIONS[decision][1]),
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

    @property
    def current(self):
        return self.appeal or self.ruling


class CaseBook:
    def __init__(self, path=Path("usr/hivatal.sqlite3")):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db, db:
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

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def case(row):
        return Case(
            row["id"], row["guild_id"], row["user_id"], row["subject"],
            row["created_at"], json.loads(row["ruling"]),
            json.loads(row["appeal"]) if row["appeal"] else None,
        )

    def create(self, guild_id, user_id, subject, *, now=None):
        subject = " ".join(subject.split())
        if not 3 <= len(subject) <= 300:
            raise OfficeError("A kérelem 3–300 karakter legyen. Az üres papírt már ismerjük.")
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
            cursor = db.execute("""INSERT INTO hivatal_cases
                (guild_id, user_id, subject, created_at, local_day, ruling)
                VALUES (?, ?, ?, ?, ?, ?)""",
                (guild_id, user_id, subject, now.isoformat(), day,
                 json.dumps(make_ruling(), ensure_ascii=False)))
            return self.case(db.execute(
                "SELECT * FROM hivatal_cases WHERE id = ?", (cursor.lastrowid,)
            ).fetchone())

    def get(self, guild_id, case_id):
        with closing(self.connect()) as db:
            row = db.execute("SELECT * FROM hivatal_cases WHERE id = ? AND guild_id = ?",
                             (case_id, guild_id)).fetchone()
            if row is None:
                raise OfficeError("Ilyen iktatószám nincs ezen a szerveren. A darálóban is megnéztük.")
            return self.case(row)

    def recent(self, guild_id, user_id):
        with closing(self.connect()) as db:
            return [self.case(row) for row in db.execute("""SELECT * FROM hivatal_cases
                WHERE guild_id = ? AND user_id = ? ORDER BY id DESC LIMIT 5""",
                (guild_id, user_id))]

    def appeal(self, guild_id, user_id, case_id):
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM hivatal_cases WHERE id = ? AND guild_id = ?",
                             (case_id, guild_id)).fetchone()
            if row is None:
                raise OfficeError("Ilyen iktatószám nincs ezen a szerveren.")
            if row["user_id"] != user_id:
                raise OfficeError("Más papírját ne gyűrögesd. Csak a saját kérelmedet fellebbezheted meg.")
            if row["appeal"]:
                raise OfficeError("Ez már jogerős. Harmadfok nincs, csak harmadik kávé. Nézd meg az /hivatal ugy paranccsal.")
            ruling = make_ruling(json.loads(row["ruling"])["decision"])
            db.execute("UPDATE hivatal_cases SET appeal = ? WHERE id = ?",
                       (json.dumps(ruling, ensure_ascii=False), case_id))
            return self.case(db.execute(
                "SELECT * FROM hivatal_cases WHERE id = ?", (case_id,)
            ).fetchone())
