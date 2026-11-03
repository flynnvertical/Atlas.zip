#!/data/data/com.termux/files/usr/bin/bash
# Instalación en Android con Termux (F-Droid). Costo USD 0; funciona mientras el teléfono esté
# encendido con Termux abierto (usá "termux-wake-lock" para que Android no lo duerma).
set -euo pipefail
pkg update -y
pkg install -y python git libxml2 libxslt rust binutils
cd "$HOME"
[ -d Atlas.zip ] || git clone https://github.com/cargazul33/Atlas.zip.git
cd Atlas.zip
pip install -r requirements.txt
if [ ! -f "$HOME/.agente.env" ]; then
  cat > "$HOME/.agente.env" <<'CFG'
# Completá estos valores (este archivo queda sólo en tu teléfono, permisos 600).
export TELEGRAM_BOT_TOKEN=""
export TELEGRAM_ALLOWED_USERS=""
export GITHUB_MODELS_TOKEN=""      # token fine-grained con permiso "Models: read"
export GROQ_API_KEY=""             # opcional, respaldo
export AGENTE_STATE_KEY=""         # opcional: frase larga para cifrar el estado
export AGENTE_STATE_REPO=""        # opcional: owner/repo para respaldo cifrado
export AGENTE_STATE_GH_TOKEN=""    # opcional: token con Contents read/write en ese repo
export AGENTE_DATA_DIR="$HOME/.agente"
CFG
  chmod 600 "$HOME/.agente.env"
  echo "Editá ~/.agente.env (nano ~/.agente.env) y volvé a ejecutar este script."
  exit 0
fi
source "$HOME/.agente.env"
termux-wake-lock 2>/dev/null || true
python -m agente doctor
exec python -m agente poll
