// Beacon - Wi-Fi stack light for an ESP32 (any ESP32 DevKit / WROOM board)
// The same light, buzzer, relays and safety inputs as the USB Arduino
// version, but the PC talks to it over Wi-Fi, so it can sit anywhere in the
// house. In Beacon: Settings > Phone & Wi-Fi > Wi-Fi light, address
// beacon-light.local (or the IP address printed in the Serial Monitor).
//
// Messages arrive as UDP packets on port 4210 (sent every half second):
//   'B' 'P' 'R' 'F' 'f' 'D' 'd' 'U'  status, exactly as the USB sketch
//   "K" + 4 hex digits               extra relays, bit i = RELAY_PINS[i]
//   'A' / 'a'                        app alarm on / off
//   'C'                              attention beeps
//   'X'                              silence the alarm
// Each packet is answered with the inputs:
//   S,<fire>,<temperature raw 0-1023 as on a 5 V Uno>,<lid open>,<flow>,<presses>
// If no packet arrives for 5 s, lights and relays switch off. The fire alarm
// keeps working on its own.
//
// Setup:
//   1. Arduino IDE > Boards Manager: install "esp32" by Espressif.
//   2. Put your Wi-Fi name and password below (2.4 GHz networks only).
//   3. Tools > Board > ESP32 Dev Module, pick the port, Upload.
//   4. Open the Serial Monitor at 115200 baud to see the light's IP address.
//
// Wiring (ESP32 pins are 3.3 V: use a ULN2003 board, logic-level MOSFETs or
// relay/SSR modules that switch reliably from a 3.3 V input):
//   GPIO 25  red light driver       GPIO 26  green light driver
//   GPIO 27  buzzer +               GPIO 14  buzzer on/off switch to GND
//   GPIO 16 17 18 19 21 22 23 32 33  extra relays (9)
//   GPIO 4   push button to GND
//   GPIO 13  fire / smoke sensor (detection pulls it LOW)
//   GPIO 34  temperature: TMP36 (+ to 3.3V, - to GND, Vout to 34). An LM35
//            needs 4 V or more: power it from 5V (VIN) - its output stays low.
//   GPIO 35  lid switch to GND, closed when the lid is shut
//   GPIO 39  flow switch to GND, closed while there's flow
//            35 and 39 have no internal pull-up: add a 10 k resistor from
//            each to 3.3V.
//   Power the ESP32 from USB or 5 V on VIN, and join all grounds.
//
// Sensor alerts are an extra aid, not a certified safety system. Never leave
// a running laser unattended.

#include <WiFi.h>
#include <WiFiUdp.h>
#include <ESPmDNS.h>

// ---- Settings -------------------------------------------------------------
const char* WIFI_NAME     = "YOUR-WIFI-NAME";
const char* WIFI_PASSWORD = "YOUR-WIFI-PASSWORD";
const char* HOST_NAME     = "beacon-light";      // -> beacon-light.local
const uint16_t UDP_PORT   = 4210;

const int RED_PIN    = 25;
const int GREEN_PIN  = 26;
const int BUZZER_PIN = 27;
const int SWITCH_PIN = 14;

const int BUTTON_PIN = 4;
const int FIRE_PIN   = 13;
const int TEMP_PIN   = 34;
const int DOOR_PIN   = 35;
const int FLOW_PIN   = 39;

const int FIRE_ACTIVE = LOW;    // keep LOW: an unplugged sensor never alarms

// HIGH for ULN2003 / MOSFET / most SSRs; LOW for relay boards that switch
// on with a LOW input.
const int OUTPUT_ON = HIGH;

const int RELAY_PINS[] = {16, 17, 18, 19, 21, 22, 23, 32, 33};
const int RELAY_COUNT = sizeof(RELAY_PINS) / sizeof(RELAY_PINS[0]);

const bool PASSIVE_BUZZER = true;
const unsigned int BUZZ_HZ = 2700;

const unsigned long TIMEOUT_MS = 5000;

// Beep patterns in ms: on, off, on, off ... ending with 0 (as in the Uno
// sketch). The last two repeat while their alarm is on.
const unsigned int SND_DONE[]   = {110, 90, 110, 90, 110, 0};
const unsigned int SND_PAUSED[] = {180, 0};
const unsigned int SND_LOST[]   = {400, 200, 400, 200, 400, 0};
const unsigned int SND_CHIRP[]  = {40, 0};
const unsigned int SND_ATTN[]   = {80, 80, 80, 80, 80, 0};
const unsigned int SND_ALARM[]  = {150, 100, 150, 900, 0};
const unsigned int SND_FIRE[]   = {500, 120, 0};

// ---- State ----------------------------------------------------------------
// The status, relay and alarm state below works exactly as in the Uno sketch
// (laser_stack_light.ino), which explains each part in more detail.
WiFiUDP udp;

char state = 'U';
unsigned long lastMsg = 0;
bool gotAny = false;
bool inJob = false;

unsigned int relayMask = 0;
int hexLeft = 0;
unsigned int hexValue = 0;

bool buzzerEnabled = false;
bool appAlarm = false;
bool fireAlarm = false;
bool silenced = false;
unsigned int presses = 0;

const unsigned int* snd = NULL;
bool sndLoud = false;
byte sndStep = 0;
unsigned long sndAt = 0;

// A debounced input: a new level counts once it has been steady for a while.
struct Input {
  int pin;
  int reading, stable;
  unsigned long changed;
};
Input sw     = {SWITCH_PIN, HIGH, HIGH, 0};
Input button = {BUTTON_PIN, HIGH, HIGH, 0};
Input fire   = {FIRE_PIN,   HIGH, HIGH, 0};
Input door   = {DOOR_PIN,   HIGH, HIGH, 0};
Input flow   = {FLOW_PIN,   HIGH, HIGH, 0};

bool debounce(Input& in, unsigned long now, unsigned long ms) {
  int r = digitalRead(in.pin);
  if (r != in.reading) {
    in.reading = r;
    in.changed = now;
  }
  if (r != in.stable && now - in.changed > ms) {
    in.stable = r;
    return true;
  }
  return false;
}

// ---- Buzzer ---------------------------------------------------------------
void buzz(bool on) {
  if (PASSIVE_BUZZER) {
    if (on) tone(BUZZER_PIN, BUZZ_HZ); else noTone(BUZZER_PIN);
  } else {
    digitalWrite(BUZZER_PIN, on ? HIGH : LOW);
  }
}

void stopSound() {
  snd = NULL;
  buzz(false);
}

void start(const unsigned int* pattern, bool loud) {
  snd = pattern;
  sndLoud = loud;
  sndStep = 0;
  sndAt = millis();
  buzz(true);
}

bool alarmSounding() {
  return (fireAlarm || appAlarm) && !silenced;
}

void play(const unsigned int* pattern) {
  if (!buzzerEnabled || alarmSounding()) return;
  start(pattern, false);
}

void updateSound(unsigned long now) {
  const unsigned int* alarm = !alarmSounding() ? NULL : (fireAlarm ? SND_FIRE : SND_ALARM);
  if (alarm != NULL && snd != alarm) start(alarm, true);
  if (alarm == NULL && sndLoud) stopSound();
  if (snd == NULL) return;
  if (!sndLoud && !buzzerEnabled) { stopSound(); return; }
  if (now - sndAt < snd[sndStep]) return;
  sndAt = now;
  sndStep++;
  if (snd[sndStep] == 0) {
    if (sndLoud) { sndStep = 0; buzz(true); }
    else stopSound();
    return;
  }
  buzz(sndStep % 2 == 0);
}

// ---- Inputs ---------------------------------------------------------------
void silence() {
  silenced = true;
  if (fire.stable != FIRE_ACTIVE) fireAlarm = false;
}

void updateInputs(unsigned long now) {
  if (debounce(sw, now, 30)) {
    buzzerEnabled = (sw.stable == LOW);
    if (buzzerEnabled) play(SND_CHIRP); else if (!sndLoud) stopSound();
  }
  if (debounce(button, now, 30) && button.stable == LOW) {
    presses++;
    if (alarmSounding()) silence();
  }
  if (debounce(fire, now, 200)) {
    if (fire.stable == FIRE_ACTIVE) {
      fireAlarm = true;
      silenced = false;
    } else if (silenced) {
      fireAlarm = false;
    }
  }
  debounce(door, now, 50);
  debounce(flow, now, 200);
}

int temperatureRaw() {
  // Report what a 5 V Uno would read, so the app's conversion stays the same
  uint32_t mv = analogReadMilliVolts(TEMP_PIN);
  return (int)min<uint32_t>(1023, mv * 1023UL / 5000UL);
}

void reply() {
  udp.beginPacket(udp.remoteIP(), udp.remotePort());
  udp.printf("S,%d,%d,%d,%d,%u\n", fireAlarm ? 1 : 0, temperatureRaw(),
             door.stable == HIGH ? 1 : 0, flow.stable == LOW ? 1 : 0, presses);
  udp.endPacket();
}

// ---- Status changes -------------------------------------------------------
// A new status letter: beep on a finished job, a lost connection or a pause.
void setState(char next) {
  if (next == state) return;
  bool stopped = (next == 'R' || next == 'F' || next == 'f');
  bool lost    = (next == 'D' || next == 'd');

  if (inJob && stopped)                  play(SND_DONE);
  else if (inJob && lost)                play(SND_LOST);
  else if (state == 'B' && next == 'P')  play(SND_PAUSED);

  if (next == 'B' || next == 'P') inJob = true;
  else if (stopped || lost)       inJob = false;
  state = next;
}

int hexDigit(char c) {
  if (c >= '0' && c <= '9') return c - '0';
  if (c >= 'A' && c <= 'F') return c - 'A' + 10;
  if (c >= 'a' && c <= 'f') return c - 'a' + 10;
  return -1;
}

// One byte of a message from the app. After 'K' the next four bytes are hex
// digits of the relay mask; a non-hex byte there drops the message.
void handle(char c, unsigned long now) {
  if (hexLeft > 0) {
    int v = hexDigit(c);
    if (v < 0) { hexLeft = 0; return; }
    hexValue = (hexValue << 4) | v;
    if (--hexLeft == 0) relayMask = hexValue;
    return;
  }
  switch (c) {
    case 'K': hexLeft = 4; hexValue = 0;  return;
    case 'A':
      if (!appAlarm) silenced = false;
      appAlarm = true;
      return;
    case 'a': appAlarm = false;           return;
    case 'C': play(SND_ATTN);             return;
    case 'X': silence();                  return;
  }
  if (c != '\0' && strchr("BPRFfDdU", c) != NULL) {
    if (gotAny) setState(c); else state = c;
    lastMsg = now;
    gotAny = true;
  }
}

void setLight(int pin, bool on) {
  digitalWrite(pin, on ? OUTPUT_ON : !OUTPUT_ON);
}

// ---- Main -----------------------------------------------------------------
// Lights and relays off before the pins become outputs (no click at power-up),
// inputs with pull-ups, a red-green self-test, then join the Wi-Fi network and
// listen for UDP packets.
void setup() {
  setLight(RED_PIN, false);
  setLight(GREEN_PIN, false);
  pinMode(RED_PIN, OUTPUT);
  pinMode(GREEN_PIN, OUTPUT);
  for (int i = 0; i < RELAY_COUNT; i++) {
    setLight(RELAY_PINS[i], false);
    pinMode(RELAY_PINS[i], OUTPUT);
  }
  pinMode(BUZZER_PIN, OUTPUT);
  pinMode(SWITCH_PIN, INPUT_PULLUP);
  pinMode(BUTTON_PIN, INPUT_PULLUP);
  pinMode(FIRE_PIN, INPUT_PULLUP);
  pinMode(DOOR_PIN, INPUT);          // external 10 k pull-up
  pinMode(FLOW_PIN, INPUT);          // external 10 k pull-up
  analogReadResolution(12);
  Serial.begin(115200);

  delay(5);
  Input* all[] = {&sw, &button, &fire, &door, &flow};
  for (Input* in : all) in->reading = in->stable = digitalRead(in->pin);
  buzzerEnabled = (sw.stable == LOW);
  fireAlarm = (fire.stable == FIRE_ACTIVE);

  setLight(RED_PIN, true);    delay(400);
  setLight(RED_PIN, false);
  setLight(GREEN_PIN, true);  delay(400);
  setLight(GREEN_PIN, false);
  play(SND_CHIRP);

  WiFi.mode(WIFI_STA);
  WiFi.setHostname(HOST_NAME);
  WiFi.setAutoReconnect(true);
  WiFi.begin(WIFI_NAME, WIFI_PASSWORD);
  udp.begin(UDP_PORT);
}

void loop() {
  unsigned long now = millis();

  // Wi-Fi comes and goes; announce the name each time it (re)connects
  static bool online = false;
  if (WiFi.status() == WL_CONNECTED && !online) {
    online = true;
    MDNS.end();
    MDNS.begin(HOST_NAME);
    Serial.printf("Beacon light online: %s.local  IP %s  port %u\n", HOST_NAME,
                  WiFi.localIP().toString().c_str(), UDP_PORT);
  } else if (WiFi.status() != WL_CONNECTED && online) {
    online = false;
    Serial.println("Wi-Fi lost, reconnecting...");
  }

  // One packet from the app may hold several commands; handle each byte, then
  // answer with the input report so the app knows the light is there.
  int size = udp.parsePacket();
  if (size > 0) {
    char buf[128];
    int n = udp.read(buf, sizeof(buf));
    for (int i = 0; i < n; i++) handle(buf[i], now);
    reply();
  }

  bool timedOut = !gotAny || (now - lastMsg > TIMEOUT_MS);
  if (timedOut && gotAny) {
    gotAny = false;
    inJob = false;
    state = 'U';
    relayMask = 0;
    appAlarm = false;
  }
  if (!fireAlarm && !appAlarm) silenced = false;

  updateInputs(now);
  updateSound(now);

  bool fast = (now / 300) % 2;
  bool slow = (now / 700) % 2;

  bool red = false, green = false;
  if (fireAlarm) {
    red = (now / 150) % 2;
  } else if (!timedOut) {
    switch (state) {
      case 'B': red = true;                break;
      case 'P': red = true; green = true;  break;
      case 'R': green = true;              break;
      case 'F': green = fast;              break;
      case 'f': green = true;              break;
      case 'D': red = slow;                break;
      default:                             break;
    }
  }

  setLight(RED_PIN, red);
  setLight(GREEN_PIN, green);
  for (int i = 0; i < RELAY_COUNT; i++)
    setLight(RELAY_PINS[i], !timedOut && ((relayMask >> i) & 1));
  delay(1);
}
