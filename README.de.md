# Wokearr

[English version](README.md)

Kleine, lokal gehostete Web-UI im Radarr/Sonarr-Look, die Filme und Serien in
eurer Plex-Bibliothek mit einer Ampel-Badge (rot/gelb/grün) versieht, basierend
auf dem Score von [isitwokeornot.com](https://isitwokeornot.com/).

- Fünf Stufen, exakt die offiziellen von isitwokeornot.com: **0–19** nicht
  woke, **20–39** leicht woke, **40–59** woke, **60–79** sehr woke,
  **80–100** extrem woke. Zwei Farbpaletten zur Auswahl (`BADGE_COLOR_SCHEME`)
- Matching läuft über die TMDb-ID, die sowohl Plex als auch isitwokeornot.com
  pro Titel führen
- Bei Serien wird auch jede Staffel gebadged (mit dem Score der Serie, auf dem
  eigenen Staffel-Poster) - nicht nur die Serie selbst, damit beim Reinklicken
  in eine Serie keine unbebadgten Staffel-Kacheln auftauchen. Einzelne
  Episoden-Thumbnails bleiben unangetastet
- Original-Poster bleibt Basis, die Badge wird nur oben drauf gerendert und als
  neues Poster in Plex hochgeladen - Rendern und Hochladen sind zwei getrennte
  Schritte mit je einer eigenen lokalen Datei (`originals/`, `branded/`), so
  lässt sich das gebrannte Ergebnis vor dem Push in Plex ansehen
- **Autopilot:** einmal `AUTO_SYNC_CRON` mit einem Cron-Zeitplan gesetzt,
  läuft alles von selbst - neue Titel bekommen automatisch ihren Score, ihr
  Original-Poster, ihren gerenderten Badge und werden nach Plex hochgeladen,
  entfernte Titel werden aufgeräumt (siehe
  [Autopilot](#autopilot---automatischer-betrieb))

## Screenshot

Poster-Grid mit farbigen Score-Badges, Filterleiste mit den fünf Score-Stufen
und Sortierung (Titel, Score, Veröffentlichung – auf- oder absteigend, merkt
sich der Browser) sowie Buttons für Score-Sync, Plex-Abgleich und Übertragen
der Badges.

## Schnellstart (Docker Compose)

```bash
git clone https://github.com/Jake-double-one/wokearr.git
cd wokearr
cp .env.example .env
# .env mit PLEX_URL / PLEX_TOKEN / LIBRARY_SECTIONS ausfuellen
docker compose up -d
```

Das Compose-File zieht direkt das fertige Image von
`ghcr.io/jake-double-one/wokearr` (siehe [Releases](https://github.com/Jake-double-one/wokearr/releases)) –
kein lokaler Build nötig.

Danach `http://<server-ip>:5005` öffnen.

Ohne gültige `PLEX_URL`/`PLEX_TOKEN` startet die App automatisch im
**Demo-Modus** mit drei Beispieltiteln, damit ihr das UI ohne Risiko
ausprobieren könnt.

## In Portainer als Stack deployen

1. **Stacks -> Add stack**
2. Als Quelle **Web editor** wählen und den Inhalt von `docker-compose.yaml`
   1:1 einfügen – die Datei zieht direkt das fertige Image von
   `ghcr.io/jake-double-one/wokearr`, kein Build nötig.
   (Alternativ **Repository** als Quelle mit Compose-Pfad `docker-compose.yaml`,
   dann baut Portainer stattdessen selbst aus dem Repo-Code – dazu wie im
   Compose-File beschrieben `image:` durch `build:` ersetzen.)
3. Unter **Environment variables** `PLEX_URL`, `PLEX_TOKEN`, `LIBRARY_SECTIONS`
   setzen (die `.env`-Datei wird von Portainer nicht automatisch gelesen).
4. **Deploy the stack**.

Für ein Update auf eine neue [Release](https://github.com/Jake-double-one/wokearr/releases)
reicht in Portainer **Stacks -> wokearr -> Pull and redeploy** (zieht das
`:latest`-Image neu). Wer eine Version fest pinnen will, ändert den Tag in
`image:` z. B. auf `:v0.1.0`.

## Umgebungsvariablen

| Variable           | Pflicht | Standard        | Beschreibung                                      |
|---------------------|---------|-----------------|----------------------------------------------------|
| `PLEX_URL`          | ja*     | –               | z. B. `http://192.168.1.10:32400`                  |
| `PLEX_TOKEN`        | ja*     | –               | [Token finden](https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/) |
| `LIBRARY_SECTIONS`  | nein    | `Filme,Serien`  | Exakte Namen eurer Plex-Bibliotheken, kommagetrennt |
| `LANGUAGE`          | nein    | `en-US`         | Sprache der Oberfläche: `en-US` \| `de-DE` \| `fr-FR` \| `es-ES` \| `it-IT` \| `pl-PL` \| `tr-TR`. `en-US` ist gleichzeitig Fallback für einzelne fehlende Übersetzungen in anderen Sprachen |
| `BADGE_POSITION`    | nein    | `top-right`     | `top-right` \| `top-left` \| `bottom-right` \| `bottom-left` |
| `BADGE_LABEL_STYLE` | nein    | `percent`       | `percent` (`37%`) \| `woke` (`37% woke`) |
| `BADGE_WIDTH_PERCENT` | nein  | `20`            | Breite der Badge relativ zur Posterbreite, in Prozent. Mindestwert fest bei `20` verankert (kleinere Werte werden automatisch angehoben) |
| `BADGE_COLOR_SCHEME` | nein  | `standard`      | Palette für die fünf Stufen: `standard` (wie isitwokeornot.com selbst) \| `modified` (grün/gelb/orange/rot/violett) |
| `RUN_HISTORY_RETENTION` | nein | `4w`         | Aufbewahrung der Protokoll-Einträge: `<Zahl><Einheit>` mit `d`/`w`/`m`, z. B. `3d`, `4w`, `6m`. `0` behält alles |
| `AUTO_SYNC_CRON` | nein | leer (aus) | Cron-Ausdruck (5 Felder) für den kompletten Autopilot-Lauf (Score-Sync, Poster-Cache, aufräumen, automatisch anwenden), ausgewertet in `TZ`. Leer oder `0` deaktiviert. Z. B. `0 * * * *` für stündlich. |
| `TZ` | nein | `Etc/UTC` | Zeitzone für die Log-Zeiten **und** den `AUTO_SYNC_CRON`-Zeitplan, z. B. `Europe/Berlin`. Ohne sie bedeutet `0 7-23 * * *` 7–23 Uhr UTC, nicht eure Ortszeit |
| `CACHE_REBUILD_COOLDOWN_MINUTES` | nein | `5` | Mindestabstand zwischen zwei Sitemap-Abrufen (manuell oder automatisch) |
| `CLEANUP_OLD_POSTERS` | nein | `true` | Nach jedem Übertragen automatisch ältere, selbst hochgeladene Poster-Versionen in Plex löschen (siehe unten) |
| `NOTIFY_URL` | nein | leer (aus) | Ziel für Benachrichtigungen über Autopilot-Läufe: Gotify, ntfy oder ein Webhook (siehe [Benachrichtigungen](#benachrichtigungen)) |
| `NOTIFY_ON` | nein | `error,changes` | Worüber benachrichtigt wird: `error` (eine Stufe ist fehlgeschlagen) und/oder `changes` (neue Titel gebadged, Scores eurer Titel geändert) |
| `LOG_LEVEL` | nein | `INFO` | Ausführlichkeit des Container-Logs: `DEBUG` \| `INFO` \| `WARNING` \| `ERROR`. `DEBUG` ergänzt eine Zeile pro Titel (siehe [Container-Log](#container-log)) |

\* Ohne diese beiden Variablen läuft die App im Demo-Modus.

Änderungen an Umgebungsvariablen werden erst nach einem **Container-Redeploy**
übernommen (Portainer: **Update the stack**, nicht nur die Seite neu laden).
Eine geänderte Badge-Einstellung (`BADGE_COLOR_SCHEME`, `BADGE_POSITION`,
`BADGE_LABEL_STYLE`, `BADGE_WIDTH_PERCENT`) führt beim nächsten Sync
automatisch zum Neurendern und erneuten Hochladen der betroffenen Poster –
die Einstellungen sind fest ins Bild gebrannt, Wokearr merkt sich deshalb,
mit welchen Einstellungen ein Poster gerendert wurde. Der erste Lauf nach so
einer Änderung betrifft dadurch die **komplette** Bibliothek inklusive aller
Staffeln.

### PLEX_URL richtig setzen

Häufigste Fehlerquelle. `PLEX_URL` braucht **Schema + Host + Port**, sonst gibt es
SSL-/Verbindungsfehler im Log (z. B. `TLSV1_UNRECOGNIZED_NAME` oder
`Max retries exceeded`):

- **Schema**: `http://`, nicht `https://` – Plex spricht auf dem lokalen Netz
  standardmäßig unverschlüsseltes HTTP. `https://` ohne eigenes Zertifikat landet
  auf Port 443, wo gar kein passender Server antwortet.
- **Port**: immer `:32400` mit angeben (Plex' Standardport). Ohne Port nimmt
  `https://` automatisch 443, `http://` automatisch 80 – beides falsch.
- **Host**: die lokale IP oder der Hostname eures Plex-Servers, aus Sicht des
  Docker-Hosts/Containers erreichbar (z. B. `192.168.1.10`, nicht `localhost`,
  außer die App läuft im selben Netzwerk-Namespace wie Plex).
- Kein Slash am Ende nötig.

Richtig: `PLEX_URL=http://192.168.1.10:32400`
Falsch: `https://192.168.1.10`, `192.168.1.10:32400` (ohne Schema), `http://192.168.1.10` (ohne Port)

Im Volume `/data` liegen und überstehen Container-Neustarts/-Updates:
`score_cache.json` (Score-Datenbank), `originals/` (unbebadgte Poster),
`branded/` (fertig gerenderte Poster, noch nicht zwingend hochgeladen),
`rendered_state.json`/`pushed_state.json` (merken sich pro Titel, mit
welchem Score und welchen Badge-Einstellungen zuletzt gerendert bzw. zu Plex
hochgeladen wurde), `run_history.json` (das Protokoll im Footer),
`library_index.json` (welche Titel in eurer Plex-Bibliothek stehen – damit
Score-Änderungen nur für eure Titel gemeldet werden),
`url_state.json` (pro Review-URL der letzte Abrufversuch - damit Seiten ohne
verwertbaren Score nicht bei jedem Lauf erneut geholt werden).

### Lauf-Protokoll

Im Footer steht eine gedämpfte Zeile mit dem letzten Lauf und dem, was er
geändert hat (gematchte Titel inkl. Differenz, aktualisierte und geänderte
Scores, gerenderte/übertragene Poster, entfernte Leichen), und bei aktivem
Autopiloten, wann der nächste Lauf ansteht. Ein Klick klappt die letzten
Läufe als kompakte Tabelle auf – standardmäßig zu. Protokolliert wird jeder
Lauf, egal ob vom Autopiloten oder per Button. Wie lange Einträge bleiben,
steuert `RUN_HISTORY_RETENTION`. Daneben steht die laufende Version, bei
`latest`-Images zusätzlich das Build-Datum. Die Version verlinkt auf dieses
Repository – bei einer getaggten Version direkt auf deren Release-Notes.

Ein fehlgeschlagener Lauf nennt Stufe und Ursache im Klartext, z. B.
„Score-Datenbank fehlgeschlagen: isitwokeornot.com antwortet nicht
(Zeitüberschreitung)", und erscheint rot. Zeilen mit mehr Inhalt lassen sich
aufklappen (antippen oder anklicken): welche eurer Titel einen neuen Score
haben (`Barbie: 72 → 81`), welche erstmals ein Badge bekommen haben, und die
technische Fehlermeldung. Alle Details inklusive Traceback stehen im
[Container-Log](#container-log).

Eine Auflistung der Änderungen je Version steht in
[CHANGELOG.md](CHANGELOG.md).

## Autopilot - automatischer Betrieb

`AUTO_SYNC_CRON` auf einen Cron-Zeitplan setzen (z. B. `0 * * * *` für
stündlich, Erstellung z. B. mit [crontab.guru](https://crontab.guru/)) und
Wokearr läuft komplett von selbst, ohne dass ihr die UI anfassen müsst. Jeder
Durchlauf macht der Reihe nach dieselben drei Stufen, die unten auch einzeln
per Button auslösbar sind:

1. **Score-Datenbank aktualisieren** – neue/fehlende Titel bei
   isitwokeornot.com nachziehen (inkrementell).
2. **Jetzt synchronisieren** (Plex-Abgleich) – für jeden Titel eurer
   Plex-Bibliothek mit bekanntem Score: fehlendes Original-Poster von Plex
   holen (`originals/`) und daraus die gebrandete Version rendern
   (`branded/`), falls noch nicht mit dem aktuellen Score geschehen. Räumt
   dabei auch lokale Dateien für Titel auf, die nicht mehr in eurer
   Plex-Bibliothek stehen ("Leichen") - nutzt die ohnehin abgefragte
   Bibliotheksliste, kostet also keinen zusätzlichen Plex-Request. Lädt noch
   nichts zu Plex hoch.
3. **Auf Plex übertragen** – neue Titel oder Titel mit geändertem Score
   bekommen ihr bereits gerendertes `branded/`-Poster zu Plex hochgeladen;
   alte eigene Poster-Versionen werden dabei wie gewohnt aufgeräumt (siehe
   unten). Titel, die schon mit ihrem aktuellen Score hochgeladen sind,
   werden übersprungen - jeder Lauf im Normalbetrieb ist also ein schneller
   No-Op-Check, kein voller Durchlauf durch die Bibliothek.

Der Score-Sync-Schritt teilt sich mit dem manuellen Button unten einen
gemeinsamen Cooldown (`CACHE_REBUILD_COOLDOWN_MINUTES`, Standard 5 Minuten)
seit dem letzten Sitemap-Abruf, damit isitwokeornot.com nicht zu häufig
angefragt wird - ein zu früher Lauf überspringt diese Stufe einfach und macht
mit den restlichen weiter. Ein fehlgeschlagener Sitemap-Abruf (nach einem
zweiten Versuch bei Zeitüberschreitung und Serverfehlern) verbraucht den
Cooldown nicht, da keine Review-Seite angefragt wurde.

Die Stufen scheitern unabhängig voneinander: Ist isitwokeornot.com nicht
erreichbar, macht der Lauf mit den Scores aus der lokalen Datenbank weiter –
neue Plex-Titel bekommen also trotzdem ihr Badge. Nur ein fehlgeschlagener
Plex-Abgleich überspringt das Übertragen, weil es dieselbe Plex-Verbindung
braucht.

Der Zeitplan wird in der Zeitzone des Containers ausgewertet – `TZ` setzen
(z. B. `Europe/Berlin`), sonst läuft er nach UTC.

## Benachrichtigungen

Mit gesetzter `NOTIFY_URL` meldet sich der Autopilot, wenn eine Stufe
fehlschlägt und/oder sich etwas geändert hat (`NOTIFY_ON`): neue Titel mit
Badge, oder isitwokeornot.com hat den Score eines Titels aus eurer Bibliothek
geändert. Ruhige Läufe schicken nichts. Manuelle Läufe benachrichtigen nicht –
da schaut ihr ja ohnehin auf die Oberfläche. Die URL-Formate folgen denen von
[Apprise](https://github.com/caronc/apprise/wiki):

| Dienst | `NOTIFY_URL` |
|---|---|
| Gotify | `gotify://host/APP_TOKEN` (http), `gotifys://host/APP_TOKEN` (https), auch mit Port und Unterpfad: `gotifys://host:8443/gotify/APP_TOKEN` |
| ntfy | `ntfy://TOPIC` (ntfy.sh), `ntfy://host/TOPIC` (http), `ntfys://host/TOPIC` (https), mit Login `ntfys://user:pass@host/TOPIC` oder Token `ntfys://host/TOPIC?token=tk_...` |
| Webhook | jede `http(s)://`-URL – bekommt einen JSON-`POST` mit `title`, `message`, `priority` (`normal`/`high`) |

Fehler kommen mit hoher Priorität (Gotify 8, ntfy 4), Änderungen mit normaler.
Zum Prüfen der Einrichtung eine Testnachricht aus dem Container schicken:

```bash
docker exec wokearr python notify.py
```

## Container-Log

Jede Zeile hat Zeitstempel (in `TZ`), Level und Bereich, z. B.:

```
2026-09-22 19:00:00 INFO    [run] Started: Autopilot
2026-09-22 19:00:02 WARNING [score-sync] Sitemap fetch failed after 2.0s (attempt 1/2): HTTPError: 503 ... - retrying in 15s.
2026-09-22 19:00:17 ERROR   [autopilot] Score database failed: isitwokeornot.com returned HTTP 503
Traceback (most recent call last): ...
2026-09-22 19:00:17 WARNING [autopilot] Continuing with the scores already in the local database.
2026-09-22 19:00:19 WARNING [run] Finished with errors: Autopilot in 19.4s - 27 matched; Score database failed: ...
```

Die Meldungen sind unabhängig von `LANGUAGE` immer englisch – so lassen sie
sich durchsuchen und unverändert in einem Issue teilen. Beim Start protokolliert
Wokearr seine wirksame Konfiguration (Version, Zeitzone, Bibliotheken,
Zeitplan und nächster Lauf, Benachrichtigungsziel) – niemals Tokens oder
Passwörter. `LOG_LEVEL=DEBUG` ergänzt eine Zeile pro Titel (geholt,
gerendert, entfernt). Ansehen mit `docker logs wokearr` oder in Portainer
beim Container unter **Logs**.

## Manuelle Bedienung

Für den Normalbetrieb mit aktivem Autopiloten nicht nötig, aber gedacht für
alle, die den Autopilot bewusst abschalten (`AUTO_SYNC_CRON` leer, Standard)
und jede Stufe selbst antriggern wollen:

- **Score-Datenbank aktualisieren** – nur Stufe 1, inkrementell.
- **Jetzt synchronisieren** – nur Stufe 2 (Plex-Abgleich, Original- und
  gebrandete Poster pflegen, Leichen entfernen). Kein Push zu Plex.
- **Auf Plex übertragen** (Alle-Button oder einzeln pro Titel) – nur Stufe 3,
  erzwungen: lädt das gerenderte Poster unabhängig davon hoch, ob sich der
  Score seit dem letzten Push geändert hat (z. B. praktisch nach einer
  geänderten Badge-Einstellung, um alles neu zu erzwingen). Rendert bei
  Bedarf automatisch nach, falls noch nicht synchronisiert wurde.
- **Kompletter Neuaufbau** – wie "Score-Datenbank aktualisieren", fragt aber
  wirklich alle Titel bei isitwokeornot.com erneut ab (z. B. um
  zwischenzeitlich geänderte Scores nachzuziehen). Dauert entsprechend länger.
- **Alte Poster in Plex löschen** – siehe nächster Abschnitt.

Alle Jobs laufen als Hintergrund-Prozess im Container weiter, auch wenn ihr
die Seite neu ladet, filtert oder den Browser-Tab schließt.

### Plex sammelt alte Poster-Versionen an

Plex behält bei jedem hochgeladenen Poster automatisch die vorherige Version
als "Poster-Historie" (sichtbar in der Poster-Auswahl in Plex) und löscht sie
nie von selbst - das ist normales Plex-Verhalten, nicht auf dieses Tool
beschränkt, füllt den Plattenplatz des Plex-Servers aber mit der Zeit spürbar
(besonders nach mehrfachem Übertragen desselben Titels, z. B. beim Testen).

- **Automatisch:** Mit `CLEANUP_OLD_POSTERS=true` (Standard) räumt die App
  nach jedem Push ("Auf Plex übertragen") alle hochgeladenen Versionen des
  jeweiligen Titels in Plex weg, außer der gerade aktiven - erkannt an Plex'
  eigenem Key-Schema für
  Uploads, nicht am Bildinhalt. Erfasst deshalb auch Uploads von vor diesem
  Feature. TMDb-/Agent-Poster werden nie angerührt (auch technisch nicht
  löschbar über die Plex-API). Faustregel: alles, was mal über Wokearr (oder
  manuell in Plex) hochgeladen wurde und nicht mehr aktiv ist, wird entfernt.
- **Einmalig für die ganze Bibliothek:** Button **"Alte Poster in Plex
  löschen"** geht alle Titel durch und räumt bereits angesammelte alte
  Versionen auf.
- Das betrifft ausschließlich Plex' eigenen Speicher, nicht den `/data`-Docker-
  Volume dieser App.

## Hinweise

- Es gibt kein offizielles API von isitwokeornot.com; die Scores werden aus
  dem strukturierten `schema.org/Review`-Datenblock jeder Titel-Seite
  gelesen. `robots.txt` der Seite sperrt nur `/api/` und `/admin` – normale
  Seitenaufrufe sind erlaubt, trotzdem bitte fair bleiben (Standard-Delay im
  Cache-Skript nicht auf 0 setzen). Normale (nicht-vollständige) Score-Syncs
  fragen dank `<lastmod>` aus der Sitemap nur neue/geänderte Reviews erneut ab
  – auf Wunsch des Betreibers, um wiederholte Läufe schlank zu halten. Eine
  Review-Seite ohne verwertbaren Score (kein Review-Block, keine TMDb-ID, ein
  HTTP-Fehler) wird als solche gemerkt und erst wieder abgefragt, wenn die
  Sitemap eine Änderung meldet – frühestens aber nach einer Woche. Sonst
  würde sie bei jedem Lauf erneut als „neu" gelten. Links
  zu den Review-Seiten in der UI tragen UTM-Parameter (`utm_source=wokearr`),
  damit isitwokeornot.com sehen kann, wie viel Traffic Wokearr ihnen zuführt.
- Dieses Projekt ist ein privates Hobby-Tool ohne Zusammenhang mit
  isitwokeornot.com, Plex Inc. oder TMDb.
- Hochgeladene Poster bleiben in Plex i. d. R. als "ausgewählt" erhalten,
  auch nach einem Metadaten-Refresh. Falls Plex doch das Original
  zurückholt, lässt sich das Poster in der Plex-Web-UI manuell sperren
  (Rechtsklick -> Poster -> Lock).

## Lizenz

MIT, siehe [LICENSE](LICENSE).
