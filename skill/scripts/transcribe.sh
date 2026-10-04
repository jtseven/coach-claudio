#!/usr/bin/env bash
# Transcribe a Telegram voice message to text, locally.
#
# Telegram delivers voice notes as Opus in an Ogg container (.oga), which is
# neither readable as text nor accepted by whisper.cpp. This converts to the
# 16 kHz mono WAV whisper wants, runs it on this machine, and prints the
# transcript to stdout. Nothing leaves the machine.
#
#   transcribe.sh /path/to/voice.oga [language]
#
# Language defaults to auto-detection, which copes with an athlete switching
# languages mid-thread. Pass a code ("en", "de", ...) when a short clip gets
# detected wrong. Setup: docs/deployment.md "Voice messages".

set -euo pipefail

# shellcheck source=lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

WHISPER_DIR="${WHISPER_DIR:-$COACH_HOME/vendor/whisper.cpp}"
WHISPER_BIN="${WHISPER_BIN:-$WHISPER_DIR/build/bin/whisper-cli}"
WHISPER_MODEL="${WHISPER_MODEL:-$WHISPER_DIR/models/ggml-small.bin}"

if [ $# -lt 1 ]; then
    echo "usage: transcribe.sh <audio-file> [language]" >&2
    exit 64
fi

AUDIO="$1"
LANG_CODE="${2:-auto}"

for required in "$AUDIO" "$WHISPER_BIN" "$WHISPER_MODEL"; do
    if [ ! -e "$required" ]; then
        echo "transcribe.sh: missing $required" >&2
        exit 66
    fi
done

TMP_DIR="$(mktemp -d)"
WAV="$TMP_DIR/voice.wav"
trap 'rm -rf "$TMP_DIR"' EXIT

# 16 kHz mono PCM is the only format whisper.cpp reads.
if ! ffmpeg -nostdin -loglevel error -y -i "$AUDIO" \
        -ar 16000 -ac 1 -c:a pcm_s16le "$WAV" 2>&1; then
    echo "transcribe.sh: ffmpeg could not decode $AUDIO" >&2
    exit 65
fi

THREADS="$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 4)"

"$WHISPER_BIN" \
    --model "$WHISPER_MODEL" \
    --file "$WAV" \
    --language "$LANG_CODE" \
    --threads "$THREADS" \
    --no-timestamps \
    --no-prints \
    2>/dev/null \
    | sed 's/^[[:space:]]*//; s/[[:space:]]*$//' \
    | grep -v '^$'
