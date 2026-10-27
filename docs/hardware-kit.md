# Beacon hardware kit

Everything Beacon can drive. Only the stack light is needed. Add the rest as you like.
The sketches are in `laser_stack_light` (USB, Arduino Uno) and `esp32_stack_light` (Wi-Fi, ESP32).
The installer also copies them to `Arduino\` in the install folder.

> **Safety.** The sensor alerts are an extra warning, not a certified safety system.
> Never leave a running laser unattended. Keep a fire extinguisher near the machine.
> Mains wiring (110/230 V) must be done by a qualified person, using relay or SSR modules rated for the load.

## Parts list

| Part | Qty | Notes | Approx. price |
|---|---|---|---|
| Arduino Uno R3 (or clone) + USB cable | 1 | USB version | $10–25 |
| *or* ESP32 DevKit (WROOM-32) | 1 | Wi-Fi version, 2.4 GHz Wi-Fi only | $6–12 |
| 12/24 V LED stack light (red/green, common +) | 1 | 2- or 3-colour tower | $15–30 |
| Power supply matching the light (12 or 24 V DC) | 1 | | $8–15 |
| ULN2003 / ULN2803 driver board | 1 | Switches the light's colour wires to ground | $2–4 |
| 5 V piezo buzzer | 1 | Passive (needs a tone) or active; set `PASSIVE_BUZZER` | $1 |
| Toggle switch | 1 | Buzzer on/off | $1 |
| Relay board, 1–8 channels (5 V; for ESP32, one that switches from 3.3 V) | 1 | Fans, air assist, exhaust, lights | $5–12 |
| 5 V supply for the relay boards | 1 | Needed for more than 2–3 relays | $5 |
| Momentary push button | 1 | Silences alarms / mute / toggle a relay | $1 |
| Flame sensor module (IR, digital output) or smoke alarm with relay contact | 1 | Fire input | $2–25 |
| TMP36 or LM35 temperature sensor | 1 | LM35 needs 5 V: use TMP36 on the ESP32 | $1–3 |
| Magnetic reed switch (normally open) | 1 | Lid / door, closed when the lid is shut | $2 |
| Air-flow switch (sail switch) or water-flow switch | 1 | Exhaust or chiller flow | $8–20 |
| 10 kΩ resistors | 2 | ESP32 only: pull-ups for GPIO 35 and 39 | – |
| Dupont wires, screw terminals, project box | | | $10 |

## Arduino Uno pins

| Pin | Connect to |
|---|---|
| D10 | Red light driver input (ULN IN1) |
| D9 | Green light driver input (ULN IN2) |
| D8 | Buzzer + (buzzer − to GND) |
| D4 | Buzzer on/off switch → GND |
| D2 D3 D5 D6 D7 D11 D12 D13 A0 | Relay board IN1–IN9 (relay buttons 1–9 in Beacon) |
| A1 | Push button → GND |
| A2 | Fire sensor: flame module DO, or the smoke alarm relay contact → GND. Detection must pull A2 LOW. |
| A3 | Temperature sensor Vout (sensor + to 5 V, − to GND) |
| A4 | Lid reed switch → GND (closed = lid shut) |
| A5 | Flow switch → GND (closed = flow) |
| GND | All grounds: light supply −, driver, relay boards, sensors |
| 5V | Sensors' VCC, and relay boards' VCC for 1–2 relays |

Light wiring: the stack light's common wire goes to + of its power supply.
The red and green return wires go to the ULN2003 outputs OUT1 and OUT2.
The light supply's − goes to the Arduino GND.

## ESP32 pins

| GPIO | Connect to |
|---|---|
| 25 / 26 | Red / green light driver inputs |
| 27 | Buzzer + |
| 14 | Buzzer on/off switch → GND |
| 16 17 18 19 21 22 23 32 33 | Relay inputs 1–9 |
| 4 | Push button → GND |
| 13 | Fire sensor (detection pulls LOW) |
| 34 | TMP36 Vout (powered from 3.3 V) |
| 35 | Lid reed switch → GND, plus a 10 kΩ resistor to 3.3 V |
| 39 | Flow switch → GND, plus a 10 kΩ resistor to 3.3 V |

1. Put your Wi-Fi name and password at the top of the sketch.
2. Upload the sketch to the ESP32.
3. In Beacon, go to Settings › Phone & Wi-Fi › Wi-Fi light.
4. Use the address `beacon-light.local`. If that doesn't work, use the IP address the Serial Monitor shows (115200 baud).

## In Beacon

- **Settings › Alerts & safety:** switch on the sensors you fitted.
  - Choose what the button does.
  - Choose which relay switches off during a fire alarm.
  - The live readings at the bottom let you test each sensor.
- **Settings › Relay buttons:** name the relays and set when they switch on.
  - A relay can follow the job, or run on a schedule.
  - A relay can be a smart plug (Shelly, Tasmota, Kasa, Home Assistant) instead of a wired relay.
- **Help › Wiring:** shows this pinout inside the app.

## Fire alarm behaviour

- The board latches the fire alarm by itself. It sounds the siren and flashes red even if the PC is off or Beacon is closed. The mute switch doesn't stop it.
- To silence it, press the button on A1 or click the red banner in Beacon. A new detection sounds again.
- Once silenced, the alarm clears when the sensor no longer detects anything.
