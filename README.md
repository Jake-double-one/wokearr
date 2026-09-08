# Woke Score for Plex

Kleine, lokal gehostete Web-UI im Radarr/Sonarr-Look, die Filme und Serien in
eurer Plex-Bibliothek mit einer Ampel-Badge (rot/gelb/grün) versieht, basierend
auf dem Score von [isitwokeornot.com](https://isitwokeornot.com/).

- **Rot** = hoher Score (Warnung), **Gelb** = mittel, **Grün** = niedrig
- Matching läuft über die TMDb-ID, die sowohl Plex als auch isitwokeornot.com
  pro Titel führen
- Original-Poster bleibt Basis, die Badge wird nur oben drauf gerendert und als
  neues Poster in Plex hochgeladen

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

\* Ohne diese beiden Variablen läuft die App im Demo-Modus.

Der Score-Cache (`score_cache.json`) liegt im Volume `/data` und übersteht
Container-Neustarts/-Updates.

## Bedienung

1. **Cache aktualisieren** – crawlt einmalig die Sitemap von
   isitwokeornot.com (~5.500 Titel) und speichert Score + TMDb-/IMDb-ID lokal.
   Danach reicht ein gelegentliches erneutes Ausführen, um neue Titel
   nachzuziehen.
2. Das Poster-Grid zeigt automatisch nur Titel eurer Plex-Bibliothek, zu denen
   ein Score gefunden wurde.
3. **Anwenden** (einzeln oder "Alle anwenden") lädt das aktuelle Poster,
   brennt die Badge drauf und lädt es zurück nach Plex.

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
