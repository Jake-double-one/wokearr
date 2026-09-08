# Wokearr

Kleine, lokal gehostete Web-UI im Radarr/Sonarr-Look, die Filme und Serien in
eurer Plex-Bibliothek mit einer Ampel-Badge (rot/gelb/grün) versieht, basierend
auf dem Score von [isitwokeornot.com](https://isitwokeornot.com/).

- **Rot** = hoher Score (Warnung), **Gelb** = mittel, **Grün** = niedrig
- Matching läuft über die TMDb-ID, die sowohl Plex als auch isitwokeornot.com
  pro Titel führen
- Original-Poster bleibt Basis, die Badge wird nur oben drauf gerendert und als
  neues Poster in Plex hochgeladen
- **Autopilot:** einmal `AUTO_SYNC_INTERVAL_MINUTES` gesetzt, läuft alles von
  selbst - neue Titel bekommen automatisch ihren Score, ihr Original-Poster
  und ihren Badge, entfernte Titel werden aufgeräumt (siehe
  [Autopilot](#autopilot---automatischer-betrieb))

## Screenshot

Poster-Grid mit farbigen Score-Badges, Filterleiste (Alle/Rot/Gelb/Grün) und
Buttons zum Aktualisieren des Score-Caches bzw. Anwenden der Badges.

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
reicht in Portainer **Stacks -> woke-score -> Pull and redeploy** (zieht das
`:latest`-Image neu). Wer eine Version fest pinnen will, ändert den Tag in
`image:` z. B. auf `:v0.1.0`.

## Umgebungsvariablen

| Variable           | Pflicht | Standard        | Beschreibung                                      |
|---------------------|---------|-----------------|----------------------------------------------------|
| `PLEX_URL`          | ja*     | –               | z. B. `http://192.168.1.10:32400`                  |
| `PLEX_TOKEN`        | ja*     | –               | [Token finden](https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/) |
| `LIBRARY_SECTIONS`  | nein    | `Filme,Serien`  | Exakte Namen eurer Plex-Bibliotheken, kommagetrennt |
| `BADGE_POSITION`    | nein    | `top-right`     | `top-right` \| `top-left` \| `bottom-right` \| `bottom-left` |
| `BADGE_LABEL_STYLE` | nein    | `percent`       | `percent` (`37%`) \| `woke` (`37% woke`) |
| `AUTO_SYNC_INTERVAL_MINUTES` | nein | `0` (aus) | Intervall in Minuten für den kompletten Autopilot-Lauf (Score-Sync, Poster-Cache, aufräumen, automatisch anwenden). `60` für stündlich. |
| `CACHE_REBUILD_COOLDOWN_MINUTES` | nein | `5` | Mindestabstand zwischen zwei Sitemap-Abrufen (manuell oder automatisch) |
| `CLEANUP_OLD_POSTERS` | nein | `true` | Nach jedem Anwenden automatisch ältere, selbst hochgeladene Poster-Versionen in Plex löschen (siehe unten) |

\* Ohne diese beiden Variablen läuft die App im Demo-Modus.

Änderungen an Umgebungsvariablen werden erst nach einem **Container-Redeploy**
übernommen (Portainer: **Update the stack**, nicht nur die Seite neu laden).
`BADGE_LABEL_STYLE` wirkt sich außerdem nur auf Poster aus, die *ab jetzt* neu
angewendet werden - der Text ist fest ins Bild gebrannt und ändert sich bei
schon vorher angewendeten Postern nicht rückwirkend von selbst.

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

Der Score-Cache (`score_cache.json`), der Original-Poster-Cache (`originals/`)
und der Zustand des Autopiloten (`applied_state.json`) liegen im Volume
`/data` und überstehen Container-Neustarts/-Updates.

## Autopilot - automatischer Betrieb

`AUTO_SYNC_INTERVAL_MINUTES` auf ein Intervall > 0 setzen (z. B. `60` für
stündlich) und Wokearr läuft komplett von selbst, ohne dass ihr die UI
anfassen müsst. Jeder Durchlauf macht der Reihe nach:

1. **Score-Sync** – neue/fehlende Titel bei isitwokeornot.com nachziehen
   (inkrementell, wie gehabt).
2. **Original-Poster nachladen** – für jeden Titel eurer Plex-Bibliothek mit
   bekanntem Score, der noch kein lokal gecachtes Original hat: das saubere
   Original von Plex holen (erkannt über einen unsichtbaren Marker, den jedes
   von Wokearr erzeugte Poster trägt) und lokal speichern (`/data/originals`).
3. **Entfernte Titel aufräumen** – lokale Original-Dateien für Titel löschen,
   die nicht mehr in eurer Plex-Bibliothek stehen. Nutzt die in Schritt 2
   ohnehin abgefragte Bibliotheksliste, kostet also keinen zusätzlichen
   Plex-Request.
4. **Automatisch anwenden** – neue Titel (noch nie gebadgt) oder Titel mit
   geändertem Score bekommen automatisch ihren Badge gebrannt und werden nach
   Plex hochgeladen; alte eigene Poster-Versionen werden dabei wie gewohnt
   aufgeräumt (siehe unten). Titel, die schon mit ihrem aktuellen Score
   gebadgt sind, werden übersprungen - jeder Lauf im Normalbetrieb ist also
   ein schneller No-Op-Check, kein voller Durchlauf durch die Bibliothek.

Button **"Jetzt synchronisieren"** stößt genau diesen Durchlauf sofort manuell
an, ohne auf den nächsten Cron-Tick zu warten - praktisch zum Testen.

Der Score-Sync-Schritt teilt sich mit den manuellen Buttons unten einen
gemeinsamen Cooldown (`CACHE_REBUILD_COOLDOWN_MINUTES`, Standard 5 Minuten)
seit dem letzten Sitemap-Abruf, damit isitwokeornot.com nicht zu häufig
angefragt wird - ein zu früher Lauf überspringt Stufe 1 einfach und macht mit
Stufe 2-4 weiter.

## Manuelle Bedienung

Für den Normalbetrieb mit aktivem Autopiloten nicht nötig, aber nützlich zum
gezielten Eingreifen:

- **Cache aktualisieren** – nur Stufe 1 (Score-Sync), inkrementell.
- **Kompletter Neuaufbau** – fragt wirklich alle Titel bei isitwokeornot.com
  erneut ab (z. B. um zwischenzeitlich geänderte Scores nachzuziehen). Dauert
  entsprechend länger.
- **Anwenden** (einzeln oder "Alle anwenden") – brennt den Badge sofort für
  ausgewählte Titel, unabhängig vom Autopilot-Status.
- **Alte Poster in Plex löschen** – siehe nächster Abschnitt.

Alle Jobs laufen als Hintergrund-Prozess im Container weiter, auch wenn ihr
die Seite neu ladet, filtert oder den Browser-Tab schließt.

### Plex sammelt alte Poster-Versionen an

Plex behält bei jedem hochgeladenen Poster automatisch die vorherige Version
als "Poster-Historie" (sichtbar in der Poster-Auswahl in Plex) und löscht sie
nie von selbst - das ist normales Plex-Verhalten, nicht auf dieses Tool
beschränkt, füllt den Plattenplatz des Plex-Servers aber mit der Zeit spürbar
(besonders nach mehrfachem Anwenden desselben Titels, z. B. beim Testen).

- **Automatisch:** Mit `CLEANUP_OLD_POSTERS=true` (Standard) räumt die App
  nach jedem "Anwenden" alle hochgeladenen Versionen des jeweiligen Titels in
  Plex weg, außer der gerade aktiven - erkannt an Plex' eigenem Key-Schema für
  Uploads, nicht am Bildinhalt. Erfasst deshalb auch Uploads von vor diesem
  Feature. TMDb-/Agent-Poster werden nie angerührt (auch technisch nicht
  löschbar über die Plex-API). Faustregel: alles, was mal über Wokearr (oder
  manuell in Plex) hochgeladen wurde und nicht mehr aktiv ist, wird entfernt.
- **Einmalig für die ganze Bibliothek:** Button **"Alte Poster in Plex
  löschen"** geht alle Titel durch und räumt bereits angesammelte alte
  Versionen auf.
- Das betrifft ausschließlich Plex' eigenen Speicher, nicht den `/data`-Docker-
  Volume dieser App.

## Eigenes Image bauen und veröffentlichen

Der mitgelieferte Workflow `.github/workflows/docker-publish.yml` baut das
Image bei jedem Push auf `main` (und bei Version-Tags `v*`) automatisch und
veröffentlicht es nach `ghcr.io/jake-double-one/wokearr`. Dafür ist keine
zusätzliche Konfiguration nötig, GitHub Actions nutzt den eingebauten
`GITHUB_TOKEN`.

## Hinweise

- Es gibt kein offizielles API von isitwokeornot.com; die Scores werden aus
  dem strukturierten `schema.org/Review`-Datenblock jeder Titel-Seite
  gelesen. `robots.txt` der Seite sperrt nur `/api/` und `/admin` – normale
  Seitenaufrufe sind erlaubt, trotzdem bitte fair bleiben (Standard-Delay im
  Cache-Skript nicht auf 0 setzen).
- Dieses Projekt ist ein privates Hobby-Tool ohne Zusammenhang mit
  isitwokeornot.com, Plex Inc. oder TMDb.
- Hochgeladene Poster bleiben in Plex i. d. R. als "ausgewählt" erhalten,
  auch nach einem Metadaten-Refresh. Falls Plex doch das Original
  zurückholt, lässt sich das Poster in der Plex-Web-UI manuell sperren
  (Rechtsklick -> Poster -> Lock).

## Lizenz

MIT, siehe [LICENSE](LICENSE).
