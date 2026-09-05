#!/bin/bash
set -euo pipefail

echo "KD Job Hunter setup"
echo "==================="

if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3 is required."
  exit 1
fi

echo "Python: $(python3 --version)"

if [ ! -d ".venv" ]; then
  python3 -m venv .venv
fi

# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m playwright install chromium

mkdir -p resumes generated_resumes logs .cache

if [ ! -f profile.yaml ]; then
  cp profile.kd.example.yaml profile.yaml
  echo "Created private profile.yaml from KD template."
fi

echo ""
echo "Setup complete."
echo ""
echo "Still required before first run:"
echo "  1. Put your private master resume at resumes/master_resume.pdf"
echo "  2. Fill your private values in profile.yaml"
echo "  3. export OPENAI_API_KEY='...'"
echo "  4. export KD_JOB_HUNTER_TELEGRAM_BOT_TOKEN='...'"
echo "  5. export KD_JOB_HUNTER_TELEGRAM_CHAT_ID='...'"
echo ""
echo "Then test discovery with:"
echo "  source .venv/bin/activate && python main.py discover"
echo ""
echo "The approval worker is:"
echo "  source .venv/bin/activate && python kd_autopilot.py"
