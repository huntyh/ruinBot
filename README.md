ez csak cirkadián adjusztment nem igazi bruszt tesomsz.

setup:
Python 3.12-t használj, a Docker image is ezen fut.\
Csinalsz magadnak discord botot meg tokent hozza https://discord.com/developers/applications  
A Developer Portalon a Bot alatt bekapcsolod mindharom Privileged Gateway Intentet (Presence, Server Members, Message Content), a kod mindet keri.\
Meghivod sajat szerverre `bot` es `applications.commands` scope-pal.\
Legyen joga latni a csatornat, olvasni az elozo uzeneteket, irni, fajlt kuldeni es embedet berakni; linkjavitasnal az eredeti embed elrejtesehez Manage Messages, rolepickerhez Manage Roles es a kiosztando role-ok fole rakott bot role, a `/release`-hez neked es a botnak is Manage Events kell.\
A projekt mappajabol dolgozol, csinalsz egy `usr` mappat, ha meg nincs (`mkdir usr`); az adatbazist meg a tobbi allapotot a bot rakja bele.\
Az `.env-example`-t lemasolod `.env` neven, beirod a `DISCORD_TOKEN`-t meg a `CHANNEL` csatorna ID-t, ahova irja h na re.\
A `MORNING_HOUR` / `MORNING_MINUTE` is legyen kitoltve, kulonben a reggeli cog nem tolt be. UTC-ben ertendo: a pelda szerinti 7:00 itthon telen 8, nyaron 9.\
Imgflip fiokadat csak az Imgflip-es memekhez kell, a tobbi opcionalis cucc maradhat uresen.\
A sajat szervered ID-jara atirod a `232227916036046849`-et a `main.py` fajlban, az indulaskori slash sync ezt hasznalja.\
Requirementset felrakod: `python -m pip install -r requirements.txt` (erdemes virtualenvben).\
Elinditod ugyanebbol a mappabol: `python main.py`. Prefix: `r!`, a slash parancsokat indulaskor szinkronizalja.\
Ha valami nem tolt be, nezd a konzolt meg a `ruinbot.log`-ot. A `usr` mappat tartsd meg ujrainditas/frissites kozott, ott laknak az adatok.\

bot-baratok (opcionalis):  
.env-be BOT_FRIENDS=<bot user id, vesszovel tobbet> — az itt felsorolt botok taggelhetik/replyzhetik a ruinbotot es valaszt kapnak (pinggel), minden mas bot tovabbra is ignoralva. A bot-barat szovegebol NEM tanul a markov.
linkjavitas ki/be (opcionalis):  
`r!linkfixer ki` — az egesz szerveren kikapcsolja (Manage Server jog kell), `r!linkfixer ki csatorna` — csak abban a csatornaban (Manage Channels jog kell), `r!linkfixer be` visszakapcsolja, `r!linkfixer allapot` megmutatja. A beallitas a usr/linkfixer.json-ben marad meg.
A csatornaszintu allapot a szerver es thread eseten a szulocsatorna tiltasat is jelzi; a `be csatorna` ezeket nem irja felul.
A Facebook-linkeket (facebook.com, www/m/mbasic/web valtozatok) facebed.com linkekre javitja, az eredeti utvonallal es query parameterekkel.

emoji import:
`/yoink emoji:<emoji>` vagy `r!yoink <emoji>` — masik szerver egyeni emojijat hozzaadja az eredeti nevevel (az animaciot is megtartja). Neked es a botnak is Create Expressions vagy Manage Expressions jog kell.
Mas uzenetebol is megy: valaszolj ra `r!yoink`-kal (a botnak Read Message History jog kell), vagy az uzenet menujeben Apps → Yoink emoji. Az uzenet pontosan egy egyeni emojit tartalmazzon, mas szoveg nelkul. Mindig annak a szervernek az emojijaihoz adja hozza, ahol hasznalod; az Apps valaszat csak te latod.
Ha mar van ilyen nevu emoji, megmutatja a kettot, es csak az eredeti kero erositheti meg a hozzaadast az „Igen, masik nevvel” gombbal; ekkor egy uj, meg szabad nevet kell megadnia. A „Megse” gomb megszakitja, a kerdes 3 perc utan lejar.

ruin ügyfélszolgálat:
`/hivatal kerveny targy:engedélyt kérek még egy sörre` — a hivatal iktatja a kérelmet, és nyilvános határozatot hoz: engedély, elutasítás vagy teljesen értelmetlen hiánypótlás. Saját ügyintéző, indoklás, bizonyítékok, krumplis pecsét. A kérelem 3–300 karakter lehet.
Az „Ezt megfellebbezem” gombbal vagy `/hivatal fellebbezes iktatoszam:42` paranccsal a kérelmező vagy bármelyik megjelölt érintett kérhet másodfokot. Ügyenként összesen egy fellebbezés jár, nem fejenként. A másodfok garantáltan más döntést hoz, de nem feltétlenül jobbat, és végleges. A jogerős határozat új, nyilvános üzenetként jelenik meg abban a csatornában, ahol fellebbezel; az eredeti iratot nem szerkeszti át. Ez a privát ügyiratmásolat gombjáról indított fellebbezésre is igaz. A hibákat csak a kezdeményező látja; az eredeti gomb újbóli megnyomásakor jelzi, ha az ügy már jogerős.
Másról is adhatsz be kérelmet: `/hivatal kerveny targy:@Cserna-nak egy ban` — a Discord valódi felhasználói megjelölését használd, ne csak begépeld a nevet. A tárgy összes megjelölt felhasználója érintett lesz. Az opcionális `erintett` mezőben a Discord tagválasztójával is megadhatsz valakit, például `/hivatal kerveny targy:egy ban erintett:@Cserna`. A határozat külön felsorolja az érintetteket; a tagek megjelennek, értesítési ping nincs. Szerepkör és @everyone nem ad fellebbezési jogot. A régi ügyek tárgyában megőrzött felhasználói tageket az első frissített induláskor átvesszük.
`/hivatal ugy iktatoszam:42` — az adott szerver ügyiratának aktuális másolata, csak neked. `/hivatal sajat` — az öt legutóbbi ügyed, beleértve azokat is, amelyekben megjelölt érintett vagy. Régebbi iratok számmal továbbra is lekérhetők.
Fejenként és szerverenként napi 5 új ügy, a limit budapesti éjfélkor újraindul; a fellebbezés nem fogyaszt sorszámot. Az ügyek, limitek és fellebbezések a `usr/hivatal.sqlite3` fájlban maradnak meg. A gombok újraindítás után is működnek. Nincs új függőség vagy konfiguráció; a cog automatikusan betöltődik, a slash parancsok a szokásos induláskori sync után jelennek meg.
Ez végig paródia: nincs valódi joghatás, díj, szerepkör- vagy jogosultságváltozás, és senkit nem pingel.
Az ügyintéző- és hivatalnevek a közös `res/adjective.json` több ezer melléknevét használják. Az indoklások két részből állnak; külön bővítettük a bizonyítékokat, utasításokat és másodfokú indokokat is. Szerverenként az utolsó 20 határozat szövegelemeit kerüli a generátor, ezt az előzményt is megőrzi újraindításkor. Az eredeti ügyiratok szövege nem változik meg a frissítéstől.
