// Beacon - Arduino Uno R3 stack light + buzzer + relays + safety inputs
// Listens over USB (9600 baud) for the Beacon app's messages:
//   'B' Running        -> RED solid
//   'P' Paused         -> RED + GREEN (both on)
//   'R' Ready          -> GREEN solid
//   'F' Framing        -> GREEN blinking   'f' -> GREEN solid  (blink off in app)
//   'D' Disconnected   -> RED slow blink   'd' -> both off     (blink off in app)
//   'U' Not found      -> both off
//   "K" + 4 hex digits -> extra relays, one bit per relay in RELAY_PINS order
//   'A' / 'a'          -> app alarm on / off (too hot, no exhaust, stuck job...)
//   'C'                -> attention beeps (nobody at the PC while the laser runs)
//   'X'                -> silence the alarm (from the app's banner or Telegram)
// If nothing arrives for 5 s (app closed, PC asleep) the lights and relays
// switch off, so nothing is left running. The fire alarm keeps working.
//
// Every 250 ms the board reports its inputs back to the app:
//   S,<fire>,<temperature raw 0-1023>,<lid open>,<flow>,<button presses>
//
// Buzzer: short beeps only when something happens, never a constant tone.
//   Job finished               -> 3 quick beeps
//   Job paused                 -> 1 beep
//   Laser disconnected mid-job -> 3 long beeps
//   Alarm from the app         -> repeating double beep until silenced
//   FIRE                       -> loud siren + red flashing until silenced
// The on/off switch mutes the everyday beeps. Alarms sound even when it's off.
// Flipping it on gives a tiny chirp so you know it's armed.
//
// Wiring (stack light has one shared + wire, separate red and green returns,
// so the Arduino switches each return to ground):
//   Stack light common wire -> + of the light's power supply (e.g. 12/24 V)
//   Light supply  -        -> Arduino GND (grounds must be joined)
//   Red return wire        -> driver output 1 (switch to GND), input from pin 10
//   Green return wire      -> driver output 2 (switch to GND), input from pin 9
//     Driver: a ULN2003 / ULN2803 board is easiest (IN1/IN2 from the pins,
//     OUT1/OUT2 to the light, GND to GND). Relay boards work too (see OUTPUT_ON).
//   Buzzer  +  -> pin 8, buzzer - -> GND  (5 V piezo buzzer)
//   Switch     -> pin 4 and GND (no resistor needed). Closed = buzzer on.
//   Extra relays -> D2 D3 D5 D6 D7 D11 D12 D13 A0 to the relay boards' IN pins.
//     More than 2-3 relay coils draw too much for the Uno's 5V pin: power the
//     relay boards from their own 5 V supply (JD-VCC) and join its GND to the
//     Arduino GND. D13 blinks at reset, so a relay on it clicks briefly at
//     power-up/upload: put something harmless there.
//
// Safety inputs (switch them on in Beacon > Settings > Alerts & safety):
//   A1  Push button   -> A1 and GND. Silences an alarm; otherwise does what
//                         Settings picks (mute, or toggle a relay).
//   A2  Fire / smoke  -> a flame sensor module's DO pin (VCC 5V, GND), or a
//                         smoke alarm's relay contact between A2 and GND.
//                         Detection must pull A2 LOW (see FIRE_ACTIVE).
//   A3  Temperature   -> LM35 or TMP36: + to 5V, - to GND, middle (Vout) to A3.
//   A4  Lid / door    -> a switch between A4 and GND that is CLOSED when the
//                         lid is shut (a magnetic reed switch is ideal).
//   A5  Flow          -> exhaust air-flow or chiller water-flow switch between
//                         A5 and GND that is CLOSED while there's flow.
// Nothing connected reads as "no fire" and "button not pressed".
//
// Sensor alerts are an extra aid, not a certified safety system. Never leave
// a running laser unattended.
//
// If the light runs on mains (110/230 V AC) do NOT use a transistor: use a
// relay or SSR module per colour, and set OUTPUT_ON below to match it.

// ---- Settings -------------------------------------------------------------
const int RED_PIN    = 10;
const int GREEN_PIN  = 9;
const int BUZZER_PIN = 8;
const int SWITCH_PIN = 4;

const int BUTTON_PIN = A1;
const int FIRE_PIN   = A2;
const int TEMP_PIN   = A3;
const int DOOR_PIN   = A4;
const int FLOW_PIN   = A5;

// The level the fire input shows when it detects fire. Most flame sensor
// modules and alarm relay contacts pull LOW. Keep LOW so an unplugged
// sensor (pulled HIGH) never sets off the alarm.
const int FIRE_ACTIVE = LOW;

// HIGH for ULN2003 / MOSFET / transistor / most SSRs.
// LOW for the common blue relay boards that switch on with a LOW input.
// (With mechanical relays, turn "Blink" off in the app to save the relays.)
const int OUTPUT_ON = LOW;

// Extra relays (fan, air assist, ...) switched from the app. Same on-level
// as the stack light relays.
const int RELAY_PINS[] = {2, 3, 5, 6, 7, 11, 12, 13, A0};
const int RELAY_COUNT = sizeof(RELAY_PINS) / sizeof(RELAY_PINS[0]);

// true  = passive buzzer (needs a tone; most small piezo discs)
// false = active buzzer (beeps by itself when powered)
const bool PASSIVE_BUZZER = true;
const unsigned int BUZZ_HZ = 2700;

const unsigned long TIMEOUT_MS = 5000;
const unsigned long REPORT_MS = 250;

// Beep patterns in ms: on, off, on, off ... ending with 0
const unsigned int SND_DONE[]   = {110, 90, 110, 90, 110, 0};
const unsigned int SND_PAUSED[] = {180, 0};
const unsigned int SND_LOST[]   = {400, 200, 400, 200, 400, 0};
const unsigned int SND_CHIRP[]  = {40, 0};
const unsigned int SND_ATTN[]   = {80, 80, 80, 80, 80, 0};
const unsigned int SND_ALARM[]  = {150, 100, 150, 900, 0};   // repeats while on
const unsigned int SND_FIRE[]   = {500, 120, 0};             // repeats while on

// ---- State ----------------------------------------------------------------
char state = 'U';
unsigned long lastMsg = 0;
bool gotAny = false;
bool inJob = false;           // a job was running or paused

unsigned int relayMask = 0;   // bit i = RELAY_PINS[i] on
int hexLeft = 0;              // hex digits still expected after 'K'
unsigned int hexValue = 0;

bool buzzerEnabled = false;

bool appAlarm = false;        // 'A' from the app
bool fireAlarm = false;       // latched until silenced and the sensor is clear
bool silenced = false;        // alarm silenced (button or 'X')
unsigned int presses = 0;
unsigned long lastReport = 0;

const unsigned int* snd = NULL;
bool sndLoud = false;         // alarm sounds ignore the mute switch
byte sndStep = 0;
unsigned long sndAt = 0;

// A debounced input
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

// true when the input settled on a new level
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
  // Alarms take over the buzzer and repeat until silenced
  const unsigned int* alarm = !alarmSounding() ? NULL : (fireAlarm ? SND_FIRE : SND_ALARM);
  if (alarm != NULL && snd != alarm) start(alarm, true);
  if (alarm == NULL && sndLoud) stopSound();
  if (snd == NULL) return;
  if (!sndLoud && !buzzerEnabled) { stopSound(); return; }
  if (now - sndAt < snd[sndStep]) return;
  sndAt = now;
  sndStep++;
  if (snd[sndStep] == 0) {
    if (sndLoud) { sndStep = 0; buzz(true); }   // alarms loop
    else stopSound();
    return;
  }
  buzz(sndStep % 2 == 0);     // even steps are "on"
}

// ---- Inputs ---------------------------------------------------------------
void silence() {
  silenced = true;
  if (fire.stable != FIRE_ACTIVE) fireAlarm = false;   // fire is over: clear it
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
      fireAlarm = true;       // a new detection always sounds, even after silencing
      silenced = false;
    } else if (silenced) {
      fireAlarm = false;
    }
  }
  debounce(door, now, 50);
  debounce(flow, now, 200);
}

void report(unsigned long now) {
  if (now - lastReport < REPORT_MS) return;
  lastReport = now;
  Serial.print("S,");
  Serial.print(fireAlarm ? 1 : 0);
  Serial.print(',');
  Serial.print(analogRead(TEMP_PIN));
  Serial.print(',');
  Serial.print(door.stable == HIGH ? 1 : 0);    // switch open = lid open
  Serial.print(',');
  Serial.print(flow.stable == LOW ? 1 : 0);     // switch closed = flow
  Serial.print(',');
  Serial.println(presses);
}

// ---- Status changes -------------------------------------------------------
void setState(char next) {
  if (next == state) return;
  bool stopped = (next == 'R' || next == 'F' || next == 'f');
  bool lost    = (next == 'D' || next == 'd');

  if (inJob && stopped)                  play(SND_DONE);
  else if (inJob && lost)                play(SND_LOST);
  else if (state == 'B' && next == 'P')  play(SND_PAUSED);

  // 'U' (LightBurn minimised / not visible) keeps the job going
  if (next == 'B' || next == 'P') inJob = true;
  else if (stopped || lost)       inJob = false;
  state = next;
}

// ---- Main -----------------------------------------------------------------
int hexDigit(char c) {
  if (c >= '0' && c <= '9') return c - '0';
  if (c >= 'A' && c <= 'F') return c - 'A' + 10;
  if (c >= 'a' && c <= 'f') return c - 'a' + 10;
  return -1;
}

void setLight(int pin, bool on) {
  digitalWrite(pin, on ? OUTPUT_ON : !OUTPUT_ON);
}

void setup() {
  // Set the "off" level before enabling outputs so relays don't click at boot
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
  pinMode(DOOR_PIN, INPUT_PULLUP);
  pinMode(FLOW_PIN, INPUT_PULLUP);
  Serial.begin(9600);

  delay(5);
  Input* all[] = {&sw, &button, &fire, &door, &flow};
  for (Input* in : all) in->reading = in->stable = digitalRead(in->pin);
  buzzerEnabled = (sw.stable == LOW);
  fireAlarm = (fire.stable == FIRE_ACTIVE);

  // Self-test: red, then green, then a chirp if the buzzer is on
  setLight(RED_PIN, true);    delay(400);
  setLight(RED_PIN, false);
  setLight(GREEN_PIN, true);  delay(400);
  setLight(GREEN_PIN, false);
  play(SND_CHIRP);
}

void loop() {
  unsigned long now = millis();

  while (Serial.available() > 0) {
    char c = Serial.read();
    if (hexLeft > 0) {          // inside a "K" relay message
      int v = hexDigit(c);
      if (v < 0) { hexLeft = 0; continue; }   // garbled: drop it
      hexValue = (hexValue << 4) | v;
      if (--hexLeft == 0) relayMask = hexValue;
      continue;
    }
    switch (c) {
      case 'K': hexLeft = 4; hexValue = 0;  continue;
      case 'A':
        if (!appAlarm) silenced = false;      // a new alarm sounds again
        appAlarm = true;
        continue;
      case 'a': appAlarm = false;           continue;
      case 'C': play(SND_ATTN);             continue;
      case 'X': silence();                  continue;
    }
    if (c != '\0' && strchr("BPRFfDdU", c) != NULL) {
      if (gotAny) setState(c); else state = c;   // no beep for the first message
      lastMsg = now;
      gotAny = true;
    }
  }

  bool timedOut = !gotAny || (now - lastMsg > TIMEOUT_MS);
  if (timedOut && gotAny) {   // app went away: forget the job, start fresh
    gotAny = false;
    inJob = false;
    state = 'U';
    relayMask = 0;
    appAlarm = false;
  }
  if (!fireAlarm && !appAlarm) silenced = false;

  updateInputs(now);
  updateSound(now);
  report(now);

  bool fast = (now / 300) % 2;   // framing blink
  bool slow = (now / 700) % 2;   // disconnected blink

  bool red = false, green = false;
  if (fireAlarm) {
    red = (now / 150) % 2;       // fire: fast red flash, even with the PC off
  } else if (!timedOut) {
    switch (state) {
      case 'B': red = true;                break;
      case 'P': red = true; green = true;  break;
      case 'R': green = true;              break;
      case 'F': green = fast;              break;
      case 'f': green = true;              break;
      case 'D': red = slow;                break;
      default:                             break;   // 'd', 'U' -> off
    }
  }

  setLight(RED_PIN, red);
  setLight(GREEN_PIN, green);
  for (int i = 0; i < RELAY_COUNT; i++)
    setLight(RELAY_PINS[i], !timedOut && ((relayMask >> i) & 1));
}
