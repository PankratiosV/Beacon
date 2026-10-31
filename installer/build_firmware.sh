#!/bin/bash
# Compiles the Arduino Uno sketch (laser_stack_light) into the ready-made
# firmware files that Beacon's "Install on Arduino" button uploads, so users
# need no Arduino IDE. Run it after changing the sketch, then commit firmware/.
#
# One file per hardware choice the sketch has (see OUTPUT_ON and
# PASSIVE_BUZZER in it):
#   firmware/laser_stack_light-<low|high>-<passive|active>.hex
#     low / high: outputs switch on with LOW (blue relay boards) or HIGH
#                 (ULN2003, MOSFET, SSR)
#     passive / active: the buzzer type
#
# Uses Arduino's official command-line tool, downloaded into build/arduino
# with its compilers (nothing is installed on this computer). macOS or Linux.
set -euo pipefail
cd "$(dirname "$0")/.."

CLI_VERSION=1.1.1
AVR_CORE=arduino:avr@1.8.6
DIR="$PWD/build/arduino"
CLI="$DIR/arduino-cli"
OUT=firmware

if [ ! -x "$CLI" ]; then
    case "$(uname -s)-$(uname -m)" in
        Darwin-*)       PLAT=macOS_64bit ;;
        Linux-x86_64)   PLAT=Linux_64bit ;;
        Linux-aarch64)  PLAT=Linux_ARM64 ;;
        *) echo "Unsupported system: $(uname -s) $(uname -m)"; exit 1 ;;
    esac
    echo "Downloading arduino-cli $CLI_VERSION - first time only..."
    mkdir -p "$DIR"
    curl -fsSL "https://downloads.arduino.cc/arduino-cli/arduino-cli_${CLI_VERSION}_${PLAT}.tar.gz" |
        tar -xz -C "$DIR" arduino-cli
fi

# Everything arduino-cli downloads (the AVR compiler, the Uno core) stays in
# build/arduino instead of the usual Arduino folders in your home folder.
cat > "$DIR/arduino-cli.yaml" <<EOF
directories:
  data: $DIR/data
  downloads: $DIR/staging
  user: $DIR/user
EOF
ARD=("$CLI" --config-file "$DIR/arduino-cli.yaml")
"${ARD[@]}" core update-index >/dev/null
"${ARD[@]}" core install "$AVR_CORE" >/dev/null

mkdir -p "$OUT"
for out in low high; do
    for buzz in passive active; do
        level=$([ $out = low ] && echo LOW || echo HIGH)
        passive=$([ $buzz = passive ] && echo true || echo false)
        tmp="$DIR/out-$out-$buzz"
        rm -rf "$tmp"
        "${ARD[@]}" compile --fqbn arduino:avr:uno --output-dir "$tmp" \
            --build-property "build.extra_flags=-DBEACON_OUTPUT_ON=$level -DBEACON_PASSIVE_BUZZER=$passive" \
            laser_stack_light | grep -E "Sketch uses|Global variables" | sed "s/^/  $out-$buzz: /"
        cp "$tmp/laser_stack_light.ino.hex" "$OUT/laser_stack_light-$out-$buzz.hex"
    done
done
echo "Done: $OUT/"
ls -l "$OUT"
