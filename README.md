# crew-eta

**Selbstgehostetes Live-ETA-Dashboard für Ultra- und Trail-Crews.** Läufer trackt via Garmin LiveTrack — die Crew sieht auf einer Karte die Live-Position und eine laufend neu berechnete Ankunftszeit (ETA) pro Verpflegungspunkt und im Ziel, höhenkorrigiert.

Kein Account bei einem fremden Dienst, keine Cloud, kein Abo: Ein Docker-Container auf **deinem eigenen** Server, du behältst deine Daten.

---

## ⚠️ Bitte zuerst lesen — Nutzungsrahmen

Dieses Projekt liest die Positionsdaten über einen **inoffiziellen, undokumentierten Garmin-LiveTrack-Endpoint**. Das hat Konsequenzen, die du kennen musst, bevor du es einsetzt:

- **Nur für private Nutzung / dein eigenes Crew.** Gedacht ist, dass **du deinen eigenen** LiveTrack-Link trackst und ihn mit **deiner eigenen** Crew teilst. Betreibe daraus **keinen zentralen Dienst**, der fremde Läuferdaten einsammelt.
- **Keine kommerzielle Verwertung.** Den Endpoint für ein bezahltes oder werbefinanziertes Produkt anzuzapfen verstößt gegen Garmins Nutzungsbedingungen (u. a. Weitergabe von End-User-Daten an nicht genehmigte Dritte, Umgehen von Schutzmechanismen). Wer das kommerziell hochzieht, tut das auf eigenes rechtliches Risiko. Der offizielle Weg wäre Garmins **Connect Developer Program** (Enterprise, genehmigungspflichtig) — das liefert allerdings keinen Echtzeit-LiveTrack-Stream.
- **Kann jederzeit brechen.** Garmin kann Format oder Pfade des Endpoints ohne Vorwarnung ändern. Dann funktioniert das Tracking nicht mehr, bis der Endpoint im Code angepasst wird. Teste vor jedem wichtigen Rennen im Simulationsmodus.
- **Datenschutz.** Standortdaten sind besonders sensibel. Wenn du sie mit anderen teilst, hol dir die Einwilligung der betroffenen Person (i. d. R. dir selbst) und betreibe das Tool nicht öffentlich zugänglich mit fremden Daten.
- **Marken.** *Garmin* und *LiveTrack* sind Marken von Garmin Ltd. Dieses Projekt steht in **keiner** Verbindung zu Garmin und wird von Garmin weder unterstützt noch geprüft.

Kurz: privates Community-Tool auf eigenes Risiko — kein Produkt.

---

## Funktionen

- **Mehrere Tracks parallel**, im passwortgeschützten Admin-Bereich angelegt.
- **Crew trägt den Garmin-Link selbst ein**: Ein neu angelegter Track hat noch keinen LiveTrack-Link (den gibt es erst ab Aktivitätsstart). Auf der Übersicht fügt der erste Aufrufende ihn ein — danach läuft die Verfolgung für alle automatisch. Ein Button erlaubt, einen falsch eingegebenen Link zu korrigieren.
- **Höhenkorrigierte ETA** (Grade-Adjusted Pace): Steigung/Gefälle fließen über einen Minetti-artigen Faktor in die Restzeit ein.
- **Adaptiv**: Blend aus rollierendem Pace-Fenster und Gesamt-Rennpace plus leichter Ermüdungsdrift — je näher ein Punkt, desto genauer die ETA.
- **Dunkle Karte** (nachttauglich für lange Rennen), VP-Marker werden nach Durchquerung grün.
- **Simulationsmodus** zum Testen ohne echten Garmin-Link.

## Technik

FastAPI (Python) · Leaflet · Docker. Keine externe Datenbank — Konfiguration und Live-Daten liegen als JSON im gemounteten `data/`-Volume.

## Schnellstart (lokal, mit Simulation)

```bash
git clone https://github.com/BBgoesMAC/crew-eta.git
cd crew-eta
cp .env.example .env
# .env öffnen und ADMIN_PASSWORD auf ein eigenes Passwort setzen!
docker compose up -d --build
```

Dann `http://127.0.0.1:8010/tracking/admin` öffnen, mit dem Passwort anmelden, einen Track anlegen und **Simulation** ankreuzen — nach 2–3 Minuten laufen die ersten ETAs unter `http://127.0.0.1:8010/tracking/`.

> Der Container lauscht nur auf `127.0.0.1`. Für den Zugriff von außen einen Reverse Proxy (z. B. Nginx) davorsetzen — siehe unten.

## Die drei Seiten

| Pfad | Zugriff | Zweck |
|---|---|---|
| `/tracking/` | offen | Übersicht aller Tracks; Link-Eingabe für Tracks ohne Garmin-Link |
| `/tracking/t/<id>` | offen | Dashboard eines Tracks: Karte + ETA pro VP |
| `/tracking/admin` | Passwort | Tracks anlegen / bearbeiten / löschen |

Der Admin-Bereich ist per HTTP-Basic-Auth geschützt (Passwort = `ADMIN_PASSWORD`, Benutzername beliebig). Das Eintragen des Garmin-Links auf der Übersicht ist bewusst offen, damit die Crew das ohne Zugangsdaten erledigen kann.

## Konfiguration (Umgebungsvariablen)

| Variable | Default | Bedeutung |
|---|---|---|
| `ADMIN_PASSWORD` | `changeme-in-env` | Passwort für den Admin-Bereich — **unbedingt ändern** |
| `POLL_INTERVAL` | `60` | Sekunden zwischen LiveTrack-Abfragen |
| `ROLLING_WINDOW_MIN` | `30` | Fenster für die aktuelle Pace |
| `DRIFT_PER_HOUR` | `0.03` | Ermüdungszuschlag auf die Restzeit pro Stunde |

## VP-Marker eingeben

Im Admin-Bereich eine Zeile pro Verpflegungspunkt, Format `Name, km`:

```
VP1-Polop, 12
VP2-Benimantell, 33.3
VP3-Confrides, 58.5
```

Namen ohne Leerzeichen (Bindestrich statt Leerzeichen), da am ersten Leerzeichen/Komma getrennt wird. Das Ziel wird automatisch ans Streckenende gehängt.

## Betrieb hinter Nginx

```nginx
location /tracking/ {
    proxy_pass http://127.0.0.1:8010;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-Proto $scheme;
    client_max_body_size 20m;   # GPX-Upload
}
```

Nginx nimmt den längsten passenden Präfix, `/tracking/` koexistiert also mit einer bestehenden Seite unter `/`.

## Wie die ETA rechnet

1. GPX → höhenkorrigierte Äquivalenzdistanz (Minetti-artiger Gradientenfaktor).
2. Live-Position wird monoton auf die Strecke projiziert (robust gegen Kreuzungen und GPS-Ausreißer).
3. Pace = 75 % rollierendes 30-min-Fenster + 25 % Gesamtpace → nahe VPs folgen der aktuellen Form, ferne bleiben konservativ.
4. Ermüdungsdrift (+3 %/h) auf die Restzeit.

Pausen am VP kippen die ETA nicht: das Fenster glättet, bei Stillstand fällt die Rechnung auf die Gesamtpace zurück. **Geplante lange VP-Pausen muss die Crew gedanklich draufrechnen.**

## Wenn der Endpoint bricht

LiveTrack starten, im Browser die Netzwerk-Requests der Share-Seite ansehen, die aktuelle Trackpoints-URL herausziehen und in `app/main.py` (`fetch_trackpoints`) anpassen. Pull Requests willkommen.

## Mitmachen

Issues und PRs gern gesehen — besonders für Endpoint-Anpassungen, ETA-Kalibrierung und Übersetzungen. Bitte den Nutzungsrahmen oben respektieren.

## Lizenz

[MIT](LICENSE) — mach damit was du willst, ohne Gewähr. Der Nutzungsrahmen oben ist ein dringender Hinweis, keine zusätzliche Lizenzbedingung.
