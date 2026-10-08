# Smart Beehive Monitoring System

Progetto d'esame per "Software Engineering for the Internet of Things"
(corso di laurea in Informatica). Sviluppato in autonomia da uno studente
con background da senior developer web (Laravel/Vue/Filament) ma senza
esperienza pregressa in MQTT, Node-RED, InfluxDB, Grafana.

Requisiti già validati dal professore: monitoraggio a distanza di una o
più arnie (simulate), con loop di controllo reale su riscaldamento/
ventilazione, rilevamento di anomalie, gestione multi-arnia e
forecasting sullo storico. Il testo integrale della bozza inviata ed
approvata è in `.claude/docs/requisiti.md` — **non versionato**
(vive sotto `.claude/`, in `.gitignore`), quindi presente solo sulla
macchina dello sviluppatore, non in un clone fresco del repo. Il
riepilogo sopra e le decisioni architetturali più sotto sono la fonte
di verità per chiunque legga solo questo file.

Scadenza: sviluppo + demo entro fine novembre.

## Come lavoriamo su questo progetto

Lo sviluppatore è in fase di apprendimento delle tecnologie IoT. La
guida deve procedere **uno step alla volta**:

1. Proponi il prossimo step (cosa e perché), prima di scrivere codice.
2. Implementa quello step soltanto.
3. Spiega cosa è stato fatto e le scelte non ovvie.
4. **Fermati** ed aspetta domande o un via libera esplicito prima di
   proseguire allo step successivo. Non generare più step in un colpo
   solo, anche se il prossimo passo sembra ovvio.

Le spiegazioni possono essere sintetiche su tutto ciò che è web
development "classico" (il richiedente lo conosce già), ma devono
essere approfondite e non scontate su MQTT, CoAP, Node-RED, InfluxDB,
Grafana e tecniche di forecasting.

## Materiale di riferimento

Le slide del corso (MQTT/CoAP, Node-RED, InfluxDB/Grafana, IoT
analytics & prediction, IoT platforms, WoT, protocolli di
infrastruttura) sono nella knowledge base del progetto Claude
collegato a questa chat, non in questo repository. Le decisioni
tecniche sotto derivano da lì: in caso di dubbio, quel materiale è il
riferimento — non pattern di altri esami (vedi sotto).

## Decisioni architetturali e perché

Container Docker separati, orchestrati con un unico `docker-compose.yml`:

- **Simulatore arnie** (Python, `paho-mqtt`): un solo container, N arnie
  come task/oggetti indipendenti al suo interno (non un container per
  arnia — vedi "Cosa abbiamo scartato"). Modella una piccola fisica con
  inerzia/feedback (es. temperatura di covata che tende verso un
  asintoto in base allo stato del riscaldamento), altrimenti il control
  loop non avrebbe nulla su cui reagire. Deve pubblicare sugli stessi
  topic che userebbero arnie reali con sensori fisici: è un digital
  twin intercambiabile con l'hardware, non una scorciatoia.
- **Broker MQTT** (Eclipse Mosquitto 2): centro nervoso, pattern
  publish/subscribe. Vedi convenzioni sui topic più sotto.
- **Node-RED**: contiene tre flow logicamente separati nello stesso
  container — ingestion (scrive su InfluxDB), control loop
  (soglie + isteresi per riscaldamento/ventilazione), anomaly detection
  (le tre regole sui requisiti). Flow separati, non microservizi
  separati: per un solo sviluppatore in 9 settimane la granularità di
  processo va tenuta bassa.
- **InfluxDB 2**: serie temporali. Schema concordato: measurement
  `telemetry`, tag `hive_id` e `sensor` (indicizzati, per filtrare),
  field `value` (il valore misurato), timestamp preso dal campo `ts`
  del payload (precisione in secondi).
- **Analytics** (Python, schedulato): legge lo storico da InfluxDB,
  applica tecniche della lezione 10 (media mobile, exponential
  smoothing, regressione lineare) e riscrive previsioni su InfluxDB
  (es. data stimata di esaurimento scorte, momento ottimale di
  smielatura).
- **Grafana**: sola lettura da InfluxDB, dashboard con variabile
  `hive_id`, stato attuatori, alert come annotation.
- **Tempo simulato**: uno script di backfill scrive mesi di storico
  sintetico (con stagionalità) direttamente su InfluxDB, per rendere
  credibile il forecasting senza aspettare mesi reali durante la demo.
  I dati live hanno timestamp reali (con fisica accelerata), mentre il
  backfill scriverà lo storico nel passato fino ad "adesso".
- **Soglie/configurazione**: file JSON per arnia, ripubblicato da
  Node-RED come messaggio MQTT *retained* su `apiary/{hive_id}/config`.
  Niente container dedicato per ora (vedi "Cosa abbiamo scartato").

### Convenzioni sui topic MQTT

```
apiary/{hive_id}/sensors/{brood_temp|humidity|weight|flights|ext_temp|ext_hum}
apiary/{hive_id}/actuators/{heater|fan}/cmd      -> Node-RED → arnia, QoS 1
apiary/{hive_id}/actuators/{heater|fan}/state    -> arnia conferma, retained
apiary/{hive_id}/status                          -> online/offline, LWT + retained
apiary/{hive_id}/alerts                          -> anomalie rilevate
apiary/{hive_id}/config                          -> soglie, retained
```

Payload di `config` (JSON, tre sezioni, tutte obbligatorie):

```json
{
  "heater":       { "on_below": 32.0, "off_above": 33.5 },
  "fan_temp":     { "on_above": 36.0, "off_below": 34.5 },
  "fan_humidity": { "on_above": 75.0, "off_below": 65.0 }
}
```

Payload di `cmd` e `state`: stringa semplice `ON` / `OFF`.

QoS: telemetria a 0 (fire-and-forget, va bene perdere una lettura ogni
tanto), comandi agli attuatori a 1 (almeno una consegna; il comando è
idempotente quindi i duplicati non sono un problema). I `cmd` sono QoS 1
ma NON retained: un comando vecchio verrebbe riconsegnato all'arnia alla
riconnessione, sovrascrivendo decisioni più recenti. Ogni arnia
registra un Last Will `offline` (retained) e pubblica `online`
(retained) alla connessione.

## Cosa abbiamo scartato, e perché

- **CoAP**: il corso lo tratta ma la scelta è ridurre la complessità
  restando solo su MQTT. Possibile estensione finale se c'è tempo.
- **Un container Docker per arnia**: il multi-arnia è già garantito a
  livello di topic MQTT (wildcard `apiary/+/sensors/#`); un container
  per arnia aggiungerebbe complessità di orchestrazione (service
  discovery, ciclo di vita dei container) che i requisiti non chiedono.
- **Pattern MAPE-K / microservizi Monitor-Analyzer-Planner-Executor**:
  usato in un *altro* esame (Software Engineering for Autonomous
  Systems, progetto smart-olive-grove) che ha requisiti diversi. Questo
  progetto NON deve adottarne la terminologia né la decomposizione in
  container: deve restare aderente agli argomenti trattati nelle slide
  del corso SE4IoT elencate sopra.
- **Config service HTTP dedicato**: idea presa in prestito (in forma
  di principio, non di implementazione) dal progetto sopra citato. Per
  ora basta file JSON + retained message. Da promuovere a servizio
  HTTP/Filament solo se serve modificare le soglie a runtime da UI, e
  solo come estra finale dopo il core.

## Stato di avanzamento

- **Step 1 — completato**: broker Mosquitto via `docker-compose.yml`
  (`mosquitto/config/mosquitto.conf`, `allow_anonymous true`,
  persistenza su named volume). Verificati a mano con
  `mosquitto_pub`/`mosquitto_sub`: wildcard `+`/`#`, retained message,
  Last Will and Testament, comportamento del QoS.
- **Step 2 — completato**: simulatore arnie in Python containerizzato.
  Ogni arnia ha connessione MQTT propria con client_id = hive_id,
  pubblica telemetria JSON sui topic `apiary/{hive_id}/sensors/*` (QoS 0),
  riceve comandi ON/OFF per heater/fan su `apiary/{hive_id}/actuators/*/cmd`
  (QoS 1) e conferma stato su `apiary/{hive_id}/actuators/*/state` (retained).
  Fisica simulata: ciclo esterno 24h sinusoidale con rumore, inerzia termica
  su brood_temp verso target dipendente da attuatori, umidità con logica simile,
  weight con ciclo diurno/notturno, flights legati a condizioni meteo.
  La fisica gira a tempo accelerato (15 min simulati per tick), mentre i
  timestamp `ts` nei payload sono l'ora reale UTC. Loop sincrono senza
  threading, una connessione per arnia per corretto online/offline per singola
  unità.
- **Step 3 — completato**: infrastruttura InfluxDB 2.7 + Node-RED 5 in
  `docker-compose.yml`, senza flow né dati. Credenziali in `.env`
  (template `.env.example`), InfluxDB inizializzato in setup mode con
  org/bucket `beehive`, retention infinita e healthcheck `influx ping`.
  Node-RED buildato da `nodered/` con `node-red-contrib-influxdb`,
  `/data` in bind mount su `nodered/data` (flows.json versionato,
  credenziali dei flow in `.gitignore`), avviato solo a InfluxDB healthy.
- **Step 4 — completato**: flow di ingestion in Node-RED (tab "Ingestion" in
  `nodered/data/flows.json`): `mqtt in` su `apiary/+/sensors/#` → function che
  costruisce il punto (`telemetry`, tag `hive_id`/`sensor`, field `value`,
  timestamp da `ts`) → `influxdb batch` (config node v2.0, org/bucket
  `beehive`). Token solo in `flows_cred.json` (non versionato). Verificato con
  query Flux: 12 serie (2 arnie x 6 sensori), nessun punto nel 1970.
- **Step 5 — completato**: container Grafana OSS 13.0.2 in `docker-compose.yml`
  (porta 3000, volume `grafana_data`, avviato a InfluxDB healthy). Data source
  InfluxDB provisioned come codice in `grafana/provisioning/datasources/` con
  uid fisso `influxdb-beehive`, linguaggio Flux, token passato solo via
  variabile d'ambiente (mai in file versionati). Nessuna dashboard ancora.
  Verificati health della data source e una query Flux su `brood_temp`.
- **Step 6 — completato**: dashboard Grafana "Apiary" (uid `apiary`)
  provisioned come codice in `grafana/provisioning/dashboards/` (provider
  `dashboards.yaml` + `apiary.json`, cartella "Smart Beehive", non
  modificabile da UI: si modifica il JSON e Grafana lo ricarica entro 30 s).
  Variabile `hive_id` (multi-value + All) popolata da `schema.tagValues`;
  pannelli time series per i 6 sensori con `aggregateWindow(every:
  v.windowPeriod)` e filtro `${hive_id:json}`, più 4 stat con l'ultimo valore
  per arnia, senza soglie colorate (arriveranno dal config del control loop).
  Impostata come home dashboard, con accesso anonimo `Viewer` pensato solo per
  la demo locale. Stato attuatori e alert NON ancora presenti in dashboard
  perché non ancora scritti in InfluxDB.
- **Step 7 — completato**: control loop heater/fan. Soglie in
  `nodered/data/config/thresholds.json` (versionato): `defaults` + override
  per arnia sotto `hives`, merge per sezione campo per campo. Tab "Config":
  a ogni `online` su `apiary/+/status` rilegge il file, valida e pubblica la
  config retained (quindi anche al riavvio di Node-RED, perché lo status è
  retained). Tab "Control": tre bisogni a isteresi con memoria propria
  (`heaterNeed`, `fanTempNeed`, `fanHumNeed`) in flow context, inizializzati
  dallo stato confermato dopo un riavvio; heater ha priorità sul fan; comando
  emesso solo se lo stato desiderato differisce da quello confermato su
  `.../state`, e ripetuto a ogni lettura finché l'arnia non conferma (nessun
  timer). Il simulatore pubblica lo stato iniziale OFF (retained) di entrambi
  gli attuatori alla connessione. Stati confermati scritti in InfluxDB:
  measurement `actuator`, tag `hive_id` e `actuator`, field `state` (1.0/0.0),
  timestamp di scrittura. Dashboard: row "Attuatori" con state-timeline (la
  query riporta a inizio intervallo l'ultimo stato precedente). Nota MQTT: la
  tab Control si sottoscrive a `apiary/+/sensors/#` (stesso filtro di
  Ingestion) e filtra nella function, perché due filtri sovrapposti ma diversi
  sulla stessa connessione MQTT 3.1.1 fanno consegnare a Mosquitto ogni
  messaggio due volte. Console scenari, anomaly detection e alert NON ancora
  presenti.
- **Step successivo**: da concordare con lo sviluppatore — non
  procedere senza il suo via libera esplicito.
