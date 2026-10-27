// Laser Status - Arduino Uno light (minimal version: two LEDs, nothing else)
// For a quick test on a bare Uno. The full stack light with buzzer, relays and
// safety inputs is laser_stack_light.ino; it understands the same letters.
// Listens for one letter over USB from the Laser Status app:
//   'B' Running        -> RED solid
//   'P' Paused         -> RED + GREEN (both on)
//   'R' Ready          -> GREEN solid
//   'F' Framing        -> GREEN blinking   'f' -> GREEN solid  (blink off in app)
//   'D' Disconnected   -> RED slow blink   'd' -> both off     (blink off in app)
//   'U' Not found      -> both off
// If nothing arrives for 5 s (app closed, PC asleep) both LEDs turn off.
//
// Wiring:
//   Pin 9  -> 220R -> RED LED (long leg)   ; short leg -> GND
//   Pin 10 -> 220R -> GREEN LED (long leg) ; short leg -> GND

const int RED_PIN   = 9;
const int GREEN_PIN = 10;
const unsigned long TIMEOUT_MS = 5000;

char state = 'U';
unsigned long lastMsg = 0;
bool gotAny = false;

void setup() {
  pinMode(RED_PIN, OUTPUT);
  pinMode(GREEN_PIN, OUTPUT);
  Serial.begin(9600);

  // Self-test: red, then green
  digitalWrite(RED_PIN, HIGH);   delay(400);
  digitalWrite(RED_PIN, LOW);
  digitalWrite(GREEN_PIN, HIGH); delay(400);
  digitalWrite(GREEN_PIN, LOW);
}

void loop() {
  // Keep the last valid status letter; anything else (the full app also sends
  // relay and alarm lines) is ignored
  while (Serial.available() > 0) {
    char c = Serial.read();
    if (strchr("BPRFfDdU", c) != NULL && c != '\0') {
      state = c;
      lastMsg = millis();
      gotAny = true;
    }
  }

  // Blink by checking the time rather than delay(), so serial is never missed
  unsigned long now = millis();
  bool timedOut = !gotAny || (now - lastMsg > TIMEOUT_MS);
  bool fast = (now / 300) % 2;   // framing blink
  bool slow = (now / 700) % 2;   // disconnected blink

  bool red = false, green = false;
  if (!timedOut) {
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

  digitalWrite(RED_PIN,   red   ? HIGH : LOW);
  digitalWrite(GREEN_PIN, green ? HIGH : LOW);
}
