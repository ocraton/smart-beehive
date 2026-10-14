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
  inerzia/feedback, altrimenti il control loop non avrebbe nulla su cui
  reagire. La colonia si termoregola: senza attuatori la covata tende a
  ~34.3 °C finché la temperatura esterna resta nella banda di comfort
  (10–28 °C), e ne esce in proporzione quando fa più freddo o più caldo;
  heater ON porta il target a 35 °C; fan ON abbassa di 12 °C il target che
  la covata avrebbe senza fan, ma mai sotto un pavimento di 33.3 °C (sopra
  le soglie dell'heater: ventilare per l'umidità non deve chiamare il
  riscaldamento). Il fan abbassa il target dell'umidità interna di 13 punti
  (non a un valore fisso). A riposo
  il rumore è tarato per non far mai scattare le soglie del loop: gli
  attuatori intervengono solo se l'ambiente viene perturbato. Peso iniziale
  di ogni arnia = `tare_kg + reserves_kg` dal suo profilo in
  `simulator/profiles.json` (`default` se non elencata). Le
  perturbazioni (scostamento della temperatura esterna, umidità extra,
  attività di volo, evento sciamatura) agiscono sulle CAUSE, mai sui valori
  misurati, e arrivano dal canale `sim/` descritto sotto. Deve pubblicare sugli stessi
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
- **Analytics** (Python, cartella `analytics/`, servizio `analytics`):
  pattern di batch processing delle slide, read da InfluxDB → aggregate →
  logica → write su InfluxDB. Esegue un'analisi completa all'avvio e poi ogni
  `ANALYTICS_INTERVAL_SECONDS` (600 s); `depends_on` influxdb healthy, backfill
  completato, mosquitto. Il peso giornaliero dell'ultimo anno è aggregato dal
  database (Flux `aggregateWindow(every: 1d, fn: mean)`); riserve = peso −
  `tare_kg`. La config per arnia (sezione `stores`) viene letta dai messaggi
  retained di `apiary/+/config`: ci si connette, si raccolgono per 5 s e ci si
  disconnette; al primo run si aspetta (max 60 s) che Node-RED l'abbia
  ripubblicata con `stores`; un'arnia senza `stores` è saltata con warning.
  Tecniche scritte a mano con numpy (`forecasting.py`, niente statsmodels):
  media mobile semplice a 7 giorni, exponential smoothing semplice
  (α = 0,3 sul guadagno giornaliero), regressione lineare ai minimi quadrati
  con R². *Analisi A (esaurimento)*: retta sugli ultimi 30 giorni delle riserve
  lisciate; consumo = −pendenza, giorni = (riserve stimate − soglia critica) /
  consumo; "stabili" se la pendenza è > −0,01 kg/giorno, "ora" se già sotto
  soglia. Se nella finestra c'è un giorno con guadagno > +0,3 kg (nutrizione o
  flusso) la retta parte da lì (+6 giorni di media mobile), se restano almeno
  14 giorni: altrimenti la nutrizione di settembre rende piatta la retta.
  *Analisi B (smielatura)*: la smielatura reale è la serie di giorni
  consecutivi con calo > 0,5 kg di cui uno > 3 kg; il momento ottimale è il
  primo giorno dopo il picco del flusso (guadagno lisciato > +0,15 per ≥ 7
  giorni) in cui il guadagno lisciato resta < +0,05 per 3 giorni, usando solo
  i giorni PRIMA della smielatura reale; miele estraibile = peso − tara −
  `winter_reserves_kg` (min 0). *Backtest*: origini settimanali da novembre
  a inizio febbraio dell'ultimo inverno completo, previsioni a +30/+60/+90
  giorni con baseline naive e retta lineare, solo dati fino all'origine, MAE
  e bias (previsto − reale). Scrive su InfluxDB: `reserves` (tag `hive_id`,
  field `actual`/`smoothed`, mezzanotte UTC, idempotente), `forecast_reserves`
  (tag `hive_id`, `method=linear`, field `reserves_kg`, timestamp futuri; la
  proiezione precedente viene cancellata prima di riscriverla), `forecast`
  (tag `hive_id`, `kind=depletion|harvest`, un punto per run con tutti i campi
  del run) e `backtest` (tag `hive_id`, `method`, `horizon_days`; un punto al
  giorno). Costanti con nome e documentate in `analytics/src/analysis.py`;
  test pytest delle funzioni pure in `analytics/tests/`.
- **Grafana**: sola lettura da InfluxDB, dashboard con variabile
  `hive_id`, stato attuatori, alert come annotation.
- **Tempo simulato**: un servizio one-shot `backfill` (Python, cartella
  `backfill/`) scrive un anno di storico sintetico direttamente su InfluxDB
  a ogni avvio dello stack, per rendere credibile il forecasting senza
  aspettare mesi reali durante la demo. `simulator` ha `depends_on: backfill`
  con `service_completed_successfully`: parte solo a storico scritto. Ordine:
  influxdb healthy → backfill → simulator. Funzionamento: se c'è una sessione
  live (telemetry più recente di 8 s) lo storico finisce dove quella
  sessione inizia e la sessione non si tocca, altrimenti finisce "adesso";
  poi delete di tutto il measurement `telemetry` precedente (restano
  `actuator` e `alert`) e write a batch di 5000 punti. Risoluzione oraria
  (8760 punti per serie), stesso schema dei dati live, seed fisso
  (`BACKFILL_SEED=42`) e chiavi dei numeri casuali legate alla data
  assoluta, quindi stessa data → stessi valori a ogni esecuzione. Il peso
  è ancorato (traslazione costante) al peso da cui parte il live (primo
  peso della sessione in corso, altrimenti tara + riserve del profilo), così
  storico e live si raccordano senza formule replicate.
  Il generatore è strutturato sulle quattro componenti della lezione 10
  (`trend_component`, `seasonal_component`, `cyclical_component`,
  `irregular_component`). Profili per arnia in `simulator/profiles.json`
  (unica fonte di verità, montato in sola lettura in simulatore e backfill
  via `PROFILES_PATH`): `tare_kg` (arnia vuota + api; una colonia debole ha
  meno api, quindi tara più bassa), `reserves_kg` (riserve ATTUALI, a inizio
  live) e i parametri del backfill. Riserve = peso − tara, serve al
  forecasting; peso iniziale = tara + riserve. I dati live hanno timestamp reali (con fisica accelerata),
  il backfill scrive lo storico nel passato fino ad "adesso".
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

Payload di `config` (JSON, cinque sezioni, tutte obbligatorie):

```json
{
  "heater":       { "on_below": 32.0, "off_above": 33.5 },
  "fan_temp":     { "on_above": 36.0, "off_below": 34.5 },
  "fan_humidity": { "on_above": 75.0, "off_below": 65.0 },
  "stores":       { "tare_kg": 24.0, "critical_reserves_kg": 5.0,
                    "winter_reserves_kg": 15.0 },
  "anomalies": {
    "weight_drop": { "max_drop_kg": 1.5, "window_seconds": 60,
                     "raise_after": 1, "clear_after": 1 },
    "inactivity":  { "max_flights": 8, "min_ext_temp": 15, "max_ext_temp": 30,
                     "max_ext_hum": 80, "raise_after": 24, "clear_after": 3 },
    "humidity":    { "above": 75, "raise_after": 24, "clear_after": 3 }
  }
}
```

Le prime tre sezioni servono al control loop, `anomalies` alle regole di
anomaly detection, `stores` al servizio analytics (Control e Anomalies la
ignorano); ogni consumatore legge solo le proprie. `stores`: `tare_kg` è il
peso dell'arnia vuota + api come lo conosce l'apicoltore, `critical_reserves_kg`
la soglia sotto cui la colonia rischia la fame, `winter_reserves_kg` le scorte
da lasciare all'arnia quando si smiela.

Nota sulla tara: esiste sia in `simulator/profiles.json` (l'arnia fisica
simulata) sia in `stores.tare_kg` (ciò che l'apicoltore sa della sua arnia).
Con arnie reali esisterebbe solo quest'ultima; i valori devono coincidere
(oggi: hive-01 24 kg, hive-02 22 kg).

Payload di `cmd` e `state`: stringa semplice `ON` / `OFF`.

Payload di `alerts` (JSON, QoS 1, NON retained: sono eventi, uno per
transizione di stato di una regola):

```json
{
  "rule": "weight_drop | inactivity | humidity",
  "state": "raised | resolved",
  "severity": "critical | warning",
  "message": "testo in italiano per l'apicoltore",
  "value": 1.67,
  "ts": "2026-10-12T15:24:36Z",
  "raised_at": "solo nei resolved: ts del raised corrispondente"
}
```

`value` è il valore che ha determinato la transizione (kg di calo, voli,
% di umidità); `ts` è il `ts` della lettura che l'ha causata.

#### Canale del simulatore (non parte del sistema)

```
sim/{hive_id}/perturbation/set     -> console → simulatore, QoS 1, JSON parziale
sim/{hive_id}/perturbation/state   -> simulatore conferma lo stato completo, retained
sim/{hive_id}/event                -> console → simulatore, QoS 1, {"type": "swarm"}
```

Serve solo a pilotare l'ambiente simulato (demo e test): con arnie reali
non esiste, perché meteo e colonia non si comandano. Per questo vive sotto
`sim/` e non sotto `apiary/`: ingestion, control loop e Grafana non lo
sottoscrivono e non devono farlo. Payload di `set`/`state`:
`{"ext_temp_offset": -20..20, "humidity_offset": 0..30, "flight_factor": 0..1}`
(in `set` basta un sottoinsieme dei campi; i valori fuori intervallo vengono
limitati, campi sconosciuti ignorati). Unico consumatore lato Node-RED: la
tab "Scenarios" (console).

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
- **Step 8 — completato**: perturbazioni del simulatore e console web.
  Simulatore: fisica ricalibrata (termoregolazione della colonia, rumore
  ridotto, fan che abbassa l'umidità di 13 punti, peso iniziale per arnia;
  dallo Step 10 viene dai profili in `simulator/profiles.json`),
  perturbazioni per arnia via `sim/{hive_id}/perturbation/set`
  (merge parziale + clamp) con stato retained su `.../perturbation/state`
  pubblicato anche all'avvio, evento `swarm` su `sim/{hive_id}/event` (−2.5 kg
  in 3 tick), dispatch dei messaggi per topic. Node-RED: aggiunto
  `@flowfuse/node-red-dashboard` 1.33.0 (Dashboard 2.0); tab "Scenarios" con
  pagina "Scenari" su http://localhost:1880/dashboard — selettore arnia
  popolato da `apiary/+/status`, tre slider che inviano il solo campo
  modificato al rilascio, cinque preset che inviano l'oggetto completo più
  "Sciamatura", tabella di stato delle arnie online. Gli slider seguono lo
  stato retained dell'arnia e hanno il passthrough disattivato, quindi non
  rimandano mai un set (nessun loop di feedback). Le sottoscrizioni della tab
  usano filtri identici a quelli delle altre tab o disgiunti (stesso motivo
  dello Step 7). Comportamento emerso da tenere presente: con "Umidità
  persistente" il fan resta richiesto, raffredda la covata sotto la soglia
  dell'heater e i due attuatori si alternano (priorità all'heater), mentre
  l'umidità resta sopra 75 %. Anomaly detection e alert NON ancora presenti.
- **Step 9 — completato**: anomaly detection. Tab "Anomalies" in Node-RED:
  tre regole per arnia valutate a ogni lettura — `weight_drop` (massimo
  dei pesi degli ultimi `window_seconds` secondi − peso corrente, critical;
  la finestra è nel tempo, misurata sul `ts` del payload, non in letture:
  con le letture, dopo un riavvio del simulatore o un cambio di profilo la
  finestra salvata su file conteneva pesi di una sessione precedente e un
  restart più leggero di `max_drop_kg` faceva scattare un falso calo di peso), `inactivity`
  (voli sotto soglia con meteo favorevole, critical), `humidity` (umidità
  interna sopra soglia, warning). Debounce nel tempo: `raised` dopo
  `raise_after` letture anomale consecutive, `resolved` dopo `clear_after`
  normali consecutive, quindi un solo messaggio per transizione (equivalente
  temporale dell'isteresi del control loop). `inactivity` con meteo
  sfavorevole non è valutabile: contatori congelati, un alert attivo non si
  risolve di notte. Soglie nella sezione `anomalies` di `thresholds.json`
  (quarta sezione del merge, validata nella tab Config). Stato delle regole
  nel context store `file` (`contextStorage` in `settings.js`: `default` in
  memoria + `file` localfilesystem, cartella `nodered/data/context/` in
  `.gitignore`), così sopravvive a un riavvio di Node-RED. Alert su
  `apiary/{hive_id}/alerts` e in InfluxDB: measurement `alert`, tag
  `hive_id`/`rule`/`severity`, field `state`, `message`, `value` e, solo nei
  resolved, `raised_at` (serve a Grafana per disegnare l'episodio).
  Grafana: due annotation a regione (critiche rosse, avvisi arancioni) da
  raised a resolved o fino ad "adesso" se ancora attive, e row "Anomalie" con
  la tabella degli eventi. Console: colonna "Anomalie attive". Simulatore:
  il fan non porta più la covata a 30 °C fissi ma la raffredda di 12 °C con
  pavimento a 33.3 °C, quindi con "Umidità persistente" il fan resta acceso
  e l'heater non interviene. Backfill e analytics/forecasting NON ancora
  presenti.
- **Step 10 — completato**: backfill di un anno di storico. Servizio
  one-shot `backfill` in `docker-compose.yml` (`restart: "no"`, dopo
  influxdb healthy; `simulator` aspetta `service_completed_successfully`);
  `HIVE_IDS` definito una sola volta con un'ancora YAML (`x-hive-ids`).
  Fine dello storico = inizio della sessione live in corso (telemetry più
  recente di 8 s, la sessione non si tocca) oppure "adesso"; delete di tutto
  `telemetry` precedente e write orario (8760 punti per serie, 2 arnie x 6
  sensori), peso ancorato al primo peso live o a tara + riserve del profilo
  (±0.1 kg). Modello per componenti della lezione 10, meteo da catena di
  Markov (giorni buoni/brutti), calendario apistico (raccolto maggio-luglio,
  smielatura a metà luglio, nutrizione a metà settembre, consumo da ottobre).
  Profili condivisi in `simulator/profiles.json` (montato in sola lettura
  in simulatore e backfill, `PROFILES_PATH`), con `tare_kg` e `reserves_kg`
  (peso iniziale = tara + riserve): hive-01 forte (tara 24.0, riserve 19.5 →
  43.5 kg, smielatura 12 kg), hive-02 debole (tara 22.0, riserve 8.0 →
  30.0 kg, smielatura 6 kg; meno api, quindi tara più bassa). Grafana: row "Storico"
  con peso, temperatura esterna (media, min e max giornalieri) e voli
  (media x 24, indipendente dalla risoluzione) sugli ultimi 12 mesi, con
  relative time override. Verificati conteggi, continuità del peso (buco
  < 1 h), stagionalità, determinismo, `up -d` a stack avviato e che control
  loop e anomalie funzionano ancora. Analytics/forecasting NON ancora
  presenti.
- **Step 11 — completato**: servizio `analytics` (Python, numpy, tecniche
  della lezione 10 a mano) e dashboard Grafana "Apiary - Previsioni" (uid
  `apiary-forecast`, intervallo di default da now-120d a now+180d, link
  reciproci con Apiary, che resta la home). Config: nuova sezione `stores` in
  `thresholds.json` (default 24/5/15 kg, override hive-02 tara 22 kg), validata
  nella tab Config e ignorata da Control e Anomalies. Il servizio gira all'avvio
  e ogni 10 minuti: esaurimento delle scorte (retta sugli ultimi 30 giorni, con
  salto degli eventi di nutrizione), momento ottimale di smielatura sulla
  stagione passata e backtest sull'inverno passato; scrive `reserves`,
  `forecast_reserves`, `forecast` e `backtest` (vedi decisione "Analytics").
  Risultati al 14/10/2026: hive-02 a 8,0 kg di riserve, consumo 0,039
  kg/giorno, soglia critica fra 77 giorni (30/12/2026); hive-01 a 19,4 kg,
  consumo 0,030 kg/giorno, soglia fra 483 giorni (il consumo attuale è basso e
  il metodo lo mantiene costante). Il backtest conferma che la retta batte la
  baseline e che è ottimista (bias positivo), sempre di più a fine inverno.
  Dashboard: riserve reali, lisciate, proiezione tratteggiata e soglia critica;
  stat per arnia; tabella smielatura con annotation sul peso; tabella backtest.
- **Step successivo**: da concordare con lo sviluppatore — non
  procedere senza il suo via libera esplicito.

## Possibili estensioni

Da valutare solo dopo il core, nell'ordine che conviene:

- **Pulsante "Rigenera storico / Reset demo"** nella console: servizio
  `backfill` sempre attivo che ascolta `sim/backfill/run` (canale `sim/`,
  non `apiary/`) e rigenera lo storico. NON tramite docker socket montato
  nella console: sarebbe un accesso root all'host per un pulsante.
- **Configuratore soglie Filament**: UI per modificare le soglie a runtime
  (oggi file JSON + messaggio MQTT retained), promuovendo il config service
  HTTP scartato sopra.
- **CoAP**: estensione finale se c'è tempo (vedi "Cosa abbiamo scartato").
