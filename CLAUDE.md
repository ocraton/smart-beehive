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
- **InfluxDB 2**: serie temporali. `hive_id` come *tag* (indicizzato,
  per filtrare), le misure come *field*.
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

QoS: telemetria a 0 (fire-and-forget, va bene perdere una lettura ogni
tanto), comandi agli attuatori a 1 (almeno una consegna; il comando è
idempotente quindi i duplicati non sono un problema). Ogni arnia
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
- **Step successivo**: da concordare con lo sviluppatore — non
  procedere senza il suo via libera esplicito.
