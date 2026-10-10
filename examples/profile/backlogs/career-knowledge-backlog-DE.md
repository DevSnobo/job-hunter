# Career knowledge backlog — Ada Lovelace

Synthetisches Beispiel im Stil eines echten privaten Backlogs. Alle Firmen,
Daten und Stichpunkte in dieser Datei sind erfunden, damit das öffentliche
Repository keine echten persönlichen Daten enthält. Kopiere `examples/profile/`
nach `profile/` und ersetze alles durch dein eigenes Material.

Die deutsche Datei wird für `--language de` gelesen, die englische für
`--language en`. Siehe [englische Version](./career-knowledge-backlog-EN.md).

## Wie diese Datei gelesen wird

- Eine `###`-Überschrift, die eine Firma und beide Daten eines Lebenslauf-
  Eintrags nennt, passt zu genau diesem Eintrag aus `inputs/current_de.json`.
- In einem passenden Abschnitt bietet jeder `#### N. Titel`-Eintrag seinen
  **Lebenslaufpunkt** als Kandidat an. Die **Ausführliche Version** ist nur
  Hintergrund für dich: sie wird nie angeboten und erreicht nie einen
  Lebenslauf.
- Die Skills der Seitenleiste kommen aus Abschnitt 2. Ohne diesen Abschnitt
  gelten die Werte aus `inputs/current_de.json`.

## 1. Beruflicher Werdegang

### 07.2018 – 02.2022 — Analytical Engines, Berlin

**Werkstudent — Software Developer Backend / Quality Assurance**

Datumszeile im Lebenslauf: `07.2018 - 02.2022 / 06.2017 – 06.2018`.

Vier freigegebene Einträge. Im Lebenslauf die Kurzform verwenden.

#### 1. Testautomatisierung der Web-Konsole

**Ausführliche Version**

Eine Selenium-Suite steuerte die interne Web-Konsole durch ihre wichtigsten
Abläufe und meldete Fehler, bevor das Team mit der Arbeit begann. Die Suite
ersetzte eine manuelle Checkliste, die vor jedem Release einen halben Tag
kostete.

**Lebenslaufpunkt**

- Testautomatisierung einer internen Web-Konsole mit Selenium, nächtlicher Lauf

#### 2. Anbindung eines externen Reporting-Dienstes

**Ausführliche Version**

Ein externer Reporting-Dienst wurde an die Plattform angebunden: Abbildung der
Nutzlast auf das interne Modell, Behandlung von Rate Limits und Wiederholungen.

**Lebenslaufpunkt**

- Anbindung eines externen Reporting-Dienstes an die bestehende API

#### 3. Manuelle Testpläne für Releases

**Ausführliche Version**

Für jedes Release wurde der manuelle Testplan geschrieben und ausgeführt und bei
Änderungen an den Funktionen aktuell gehalten.

**Lebenslaufpunkt**

- Manuelle Release-Testpläne, geschrieben und ausgeführt

#### 4. Umstellung des Builds auf ein neueres JDK

**Ausführliche Version**

Der Build wurde von einem alten JDK auf das nächste LTS-Release umgestellt, die
entstandenen Deprecation-Warnungen wurden behoben.

*(Dieser Eintrag hat absichtlich keine Kurzform. Er bleibt Hintergrund: der
Auswähler sieht ihn nie, denn nur ein markierter **Lebenslaufpunkt** wird zum
Kandidaten.)*

### 03.2022 – 08.2022 — Analytical Engines, Berlin

**Bachelor Thesis — Software Developer Backend**

Ein freigegebener Eintrag.

#### 1. Hexagonale Architektur im Monolithen

**Ausführliche Version**

Bewertet wurde, wie eine hexagonale Architektur innerhalb einer monolithischen
Anwendung entkoppelte Plugins ermöglicht; die Migrationsschritte stehen in der
Arbeit.

**Lebenslaufpunkt**

- Evaluation einer hexagonalen Architektur für entkoppelte Plugins im Monolithen

### 09.2022 – heute — Analytical Engines, Berlin

Dieser Eintrag hat keine `#### N.`-Einträge: die Stichpunkte aus
`inputs/current_de.json` sind die Kandidaten. Eine Datumszeile mit `heute` passt
zu keinem datierten Abschnitt, deshalb bleiben die Werte der Basis unverändert.

## 2. Technische Skills — Seitenleiste des Lebenslaufs

Die Werte unten ersetzen die Skills der Seitenleiste aus der Basisdatei.
`**fett**` markiert den fetten Lauf im Lebenslauf.

### Sprachen & Frameworks

- **Java**, JakartaEE, Guice

### Build & Tools

- **Git, Maven**, IntelliJ, Shell, Linux

### Testing & Qualität

- **JUnit 4/5**, SonarQube, Selenium

### DevOps & Deployment

- **Docker**, Docker Compose, Bitbucket Pipelines

### Methoden

- **Scrum**, Agile, Jira

### Soft Skills

- **Mentoring, analytisches & objektorientiertes Denken**

## 3. Ausbildung und Zertifikate

- Universität Potsdam — Bachelor of Science, Informatik (2017 – 2022)
- Certified Professional for Software Architecture, Foundation Level

## 4. Hinweise

- Jede Aussage muss wahr bleiben. Das Werkzeug prüft Struktur, nicht Wahrheit.
- Fakten, die sich nie ändern dürfen, gehören in `inputs/current_de.json`.
